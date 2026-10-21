import datetime as dt
import smtplib
from email.message import EmailMessage

import pyotp
from fastapi import HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from service.db import ActionToken, Attempt, LocalCredential, LoginSession, User, audit, now
from service.security import csrf_for, digest, password_ok, token
from service.settings import config

AT_COOKIE = 'hermes_session_at'
RT_COOKIE = 'hermes_session_rt'


def throttle(db, key, maximum=8, seconds=300):
    key = digest(key)
    db.execute(insert(Attempt).values(key=key, count=0, until=now() + dt.timedelta(seconds=seconds)).on_conflict_do_nothing())
    row = db.scalar(select(Attempt).where(Attempt.key == key).with_for_update())
    if row.until <= now():
        row.count, row.until = 0, now() + dt.timedelta(seconds=seconds)
    row.count += 1
    blocked = row.count > maximum
    db.commit()
    if blocked:
        raise HTTPException(429, '요청이 너무 많습니다. 잠시 후 다시 시도하세요.')


def login(db, username, password, otp, device):
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    credential = db.get(LocalCredential, user.id) if user else None
    ok = password_ok(password, credential.password_hash if credential else None)
    if not user or not ok:
        audit(db, 'anonymous', 'login_failed', digest(username.strip().lower()))
        db.commit()
        raise HTTPException(401, 'ID 또는 비밀번호가 올바르지 않습니다.')
    if user.admin and config().get('admin_otp_required', True) and (not user.totp_secret or not pyotp.TOTP(user.totp_secret).verify(otp, valid_window=1)):
        raise HTTPException(401, '관리자 OTP를 확인하세요.')
    if user.status not in ('pending', 'active'):
        raise HTTPException(403, '사용이 제한된 계정입니다.')
    return issue(db, user, device)


def issue(db, user, device=''):
    at, rt, csrf = token(), token(), token()
    row = LoginSession(user_id=user.id, access_hash=digest(at), refresh_hash=digest(rt), csrf_hash=digest(csrf),
                       access_until=now() + dt.timedelta(seconds=config()['access_seconds']),
                       refresh_until=now() + dt.timedelta(seconds=config()['refresh_seconds']), device=device[:256])
    db.add(row)
    db.flush()
    csrf = csrf_for(row.id)
    row.csrf_hash = digest(csrf)
    audit(db, user.id, 'login', user.id)
    db.commit()
    return user, row, at, rt, csrf


def resolve(db, access, active=False):
    row = db.scalar(select(LoginSession).where(LoginSession.access_hash == digest(access), LoginSession.revoked.is_(False)))
    if not row or row.access_until <= now():
        raise HTTPException(401, '로그인이 필요합니다.')
    user = db.get(User, row.user_id)
    if user.status not in ('pending', 'active') or (active and (user.status != 'active' or not user.email_verified)):
        raise HTTPException(403, '관리자 승인과 이메일 확인이 필요합니다.')
    return user, row


def rotate(db, refresh):
    row = db.scalar(select(LoginSession).where(LoginSession.refresh_hash == digest(refresh)).with_for_update())
    if not row or row.revoked or row.refresh_until <= now():
        raise HTTPException(401, '세션이 만료되었습니다.')
    user = db.get(User, row.user_id)
    if user.status not in ('pending', 'active'):
        raise HTTPException(403, '계정이 정지되었습니다.')
    at, rt = token(), token()
    row.access_hash, row.refresh_hash = digest(at), digest(rt)
    row.access_until = now() + dt.timedelta(seconds=config()['access_seconds'])
    row.refresh_until = now() + dt.timedelta(seconds=config()['refresh_seconds'])
    audit(db, user.id, 'refresh', row.id)
    db.commit()
    return user, row, at, rt, csrf_for(row.id)


def require(request: Request, db, active=False, admin=False, gui=False):
    authorization = request.headers.get('authorization', '')
    bearer = authorization.startswith('Bearer ')
    access = authorization[7:] if bearer else getattr(request.state, 'access', request.cookies.get(AT_COOKIE, ''))
    user, session = resolve(db, access, active=active)
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and not bearer:
        csrf = request.headers.get('x-csrf-token', '')
        import hmac
        gui_origin = (gui and request.headers.get('origin') == config()['public_url']
                      and request.headers.get('sec-fetch-site') != 'cross-site'
                      and request.headers.get('content-type', '').split(';')[0].lower() == 'application/json')
        if (not csrf or not hmac.compare_digest(digest(csrf), session.csrf_hash)) and not native_json(request) and not gui_origin:
            raise HTTPException(403, 'CSRF 검증 실패. 다시 로그인하세요.')
    if admin and not user.admin:
        raise HTTPException(403, '관리자 권한이 필요합니다.')
    return user, session, access


def native_json(request):
    """Electron main's cookie transport has no Origin; cross-origin browser JSON requires CORS."""
    return (not request.headers.get('origin') and not request.headers.get('sec-fetch-site')
            and request.headers.get('content-type', '').split(';')[0].lower() == 'application/json')


def payload(result):
    user, session, at, rt, csrf = result
    return {'access_token': at, 'refresh_token': rt, 'token_type': 'Bearer',
            'expires_at': int(session.access_until.timestamp()), 'provider': 'local',
            'user_id': user.id, 'csrf_token': csrf}


def cookies(response, result):
    _, _, at, rt, _ = result
    for name, value, ttl in ((AT_COOKIE, at, config()['access_seconds']), (RT_COOKIE, rt, config()['refresh_seconds'])):
        response.set_cookie(name, value, max_age=ttl, httponly=True, secure=config()['secure_cookies'], samesite='lax', path='/')
    return response


def revoke_all(db, user_id):
    db.execute(update(LoginSession).where(LoginSession.user_id == user_id).values(revoked=True))


def send_action(db, user, kind):
    raw = token()
    row = ActionToken(digest=digest(raw), user_id=user.id, kind=kind, expires_at=now() + dt.timedelta(minutes=30))
    db.add(row)
    db.commit()
    message = EmailMessage()
    message['From'], message['To'] = config()['smtp_from'], user.email
    message['Subject'] = 'Hermes 이메일 확인' if kind == 'verify' else 'Hermes 비밀번호 재설정'
    message.set_content(f"{config()['public_url']}/?action={kind}#token={raw}\n\n이 링크는 30분간 한 번만 사용할 수 있습니다.")
    try:
        with smtplib.SMTP(config()['smtp_host'], config()['smtp_port'], timeout=10) as smtp:
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        row.consumed = True
        db.commit()
        raise HTTPException(503, '메일 전송 실패. 다시 요청하세요.') from exc


def consume_action(db, raw, kind):
    row = db.scalar(select(ActionToken).where(ActionToken.digest == digest(raw)).with_for_update())
    if not row or row.kind != kind or row.consumed or row.expires_at <= now():
        raise HTTPException(400, '만료되었거나 사용된 링크입니다.')
    row.consumed = True
    return db.get(User, row.user_id)
