import datetime as dt
import re
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from service import accounts
from service.db import Identity, LocalCredential, NativeFlow, Space, User, audit, factory, now
from service.security import challenge, csrf_for, digest, hasher, token, valid_loopback, validate_password, validate_username

router = APIRouter()


class Register(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=12, max_length=128)
    email: str = Field(max_length=254)


class PasswordLogin(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=128)
    otp: str = Field(default='', max_length=8)
    provider: str = 'local'
    broker_state: str = ''
    next: str = ''


class Email(BaseModel):
    email: str = Field(max_length=254)


class Action(BaseModel):
    token: str = Field(max_length=128)
    password: str = Field(default='', max_length=128)


@router.post('/platform/api/register', status_code=201)
def register(body: Register, request: Request):
    try:
        username = validate_username(body.username)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    email = body.email.strip().lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise HTTPException(422, '이메일 형식을 확인하세요.')
    with factory()() as db:
        accounts.throttle(db, 'signup:' + request.client.host, maximum=10)
        user = User(username=username, email=email)
        db.add(user)
        try:
            db.flush()
            db.add(LocalCredential(user_id=user.id, password_hash=hasher.hash(body.password)))
            db.add(Identity(id='local:' + username, user_id=user.id, provider='local', subject=username))
            db.add(Space(user_id=user.id))
            audit(db, user.id, 'signup', user.id)
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, '등록할 수 없는 ID 또는 이메일입니다.') from exc
        accounts.send_action(db, user, 'verify')
    return {'message': '확인 메일을 전송했습니다. 이메일 확인 후 관리자 승인을 기다리세요.'}


@router.post('/auth/password-login')
def password_login(body: PasswordLogin, request: Request):
    if body.provider != 'local':
        raise HTTPException(400, '활성화되지 않은 인증 공급자입니다.')
    with factory()() as db:
        accounts.throttle(db, 'login-ip:' + request.client.host, maximum=30)
        accounts.throttle(db, 'login-user:' + body.username.lower())
        result = accounts.login(db, body.username, body.password, body.otp, request.headers.get('user-agent', ''))
        response = JSONResponse(accounts.payload(result))
        if body.broker_state:
            if request.cookies.get('native_flow') != body.broker_state:
                raise HTTPException(400, 'Native flow cookie mismatch')
            response = JSONResponse({**accounts.payload(result), 'redirect_url': finish_native(db, body.broker_state, result[0])})
        return accounts.cookies(response, result)


@router.get('/api/auth/providers')
def providers():
    return {'providers': [{'name': 'local', 'display_name': 'ID / Password', 'supports_password': True,
                           'supports_session': True}]}


@router.get('/api/auth/me')
@router.get('/platform/api/me')
def me(request: Request):
    with factory()() as db:
        user, session, _ = accounts.require(request, db)
        space = db.scalar(select(Space).where(Space.user_id == user.id))
        return {'user_id': user.id, 'username': user.username, 'email': user.email, 'display_name': user.username,
                'org_id': '', 'provider': 'local', 'status': user.status, 'email_verified': user.email_verified,
                'admin': user.admin, 'space_id': space.id, 'runtime_status': space.runtime_status,
                'expires_at': int(session.access_until.timestamp()), 'csrf_token': csrf_for(session.id)}


@router.post('/auth/native/refresh')
def refresh(body: dict):
    with factory()() as db:
        return accounts.payload(accounts.rotate(db, str(body.get('refresh_token', ''))))


@router.post('/auth/refresh')
def refresh_browser(request: Request):
    with factory()() as db:
        result = accounts.rotate(db, request.cookies.get(accounts.RT_COOKIE, ''))
        return accounts.cookies(JSONResponse(accounts.payload(result)), result)


@router.post('/auth/logout')
def logout(request: Request):
    with factory()() as db:
        user, row, _ = accounts.require(request, db)
        row.revoked = True
        audit(db, user.id, 'logout', row.id)
        db.commit()
    response = JSONResponse({'ok': True})
    for name in (accounts.AT_COOKIE, accounts.RT_COOKIE):
        response.delete_cookie(name, path='/')
    return response


@router.post('/platform/api/email/resend')
def resend(body: Email, request: Request):
    with factory()() as db:
        accounts.throttle(db, 'email:' + request.client.host, maximum=5)
        user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
        if user and not user.email_verified:
            accounts.send_action(db, user, 'verify')
    return {'message': '등록된 확인 대기 계정이면 메일을 전송했습니다.'}


@router.post('/platform/api/email/verify')
def verify(body: Action):
    with factory()() as db:
        user = accounts.consume_action(db, body.token, 'verify')
        user.email_verified = True
        audit(db, user.id, 'email_verified', user.id)
        db.commit()
    return {'message': '이메일 확인 완료. 관리자 승인을 기다리세요.'}


@router.post('/platform/api/password/forgot')
def forgot(body: Email, request: Request):
    with factory()() as db:
        accounts.throttle(db, 'reset:' + request.client.host, maximum=5)
        user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
        if user and not user.admin:
            accounts.send_action(db, user, 'reset')
    return {'message': '등록된 일반 계정이면 재설정 메일을 전송했습니다. 관리자는 운영 CLI를 사용하세요.'}


@router.post('/platform/api/password/reset')
def reset(body: Action):
    try:
        validate_password(body.password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    with factory()() as db:
        user = accounts.consume_action(db, body.token, 'reset')
        db.get(LocalCredential, user.id).password_hash = hasher.hash(body.password)
        accounts.revoke_all(db, user.id)
        audit(db, user.id, 'password_reset', user.id)
        db.commit()
    return {'message': '비밀번호가 변경되었습니다. 다시 로그인하세요.'}


@router.get('/auth/native/authorize')
def authorize(request: Request, code_challenge: str, code_challenge_method: str, redirect_uri: str, state: str):
    if code_challenge_method != 'S256' or not re.fullmatch(r'[A-Za-z0-9_-]{43}', code_challenge):
        raise HTTPException(400, 'PKCE S256 challenge required')
    if not valid_loopback(redirect_uri) or not 16 <= len(state) <= 256:
        raise HTTPException(400, 'Invalid loopback redirect or state')
    raw = token()
    with factory()() as db:
        accounts.throttle(db, 'native:' + request.client.host, maximum=20)
        db.add(NativeFlow(id=digest(raw), challenge=code_challenge, redirect_uri=redirect_uri, state=state,
                          expires_at=now() + dt.timedelta(minutes=5)))
        db.commit()
    response = RedirectResponse('/login?broker_state=' + raw)
    response.set_cookie('native_flow', raw, httponly=True, samesite='lax', max_age=300)
    return response


def finish_native(db, raw, user):
    row = db.scalar(select(NativeFlow).where(NativeFlow.id == digest(raw)).with_for_update())
    if not row or row.consumed or row.code_hash or row.expires_at <= now():
        raise HTTPException(400, 'Expired native login')
    if user.status != 'active' or not user.email_verified:
        raise HTTPException(403, '승인된 계정만 Desktop에 연결할 수 있습니다.')
    code = token()
    row.code_hash, row.user_id = digest(code), user.id
    row.expires_at = now() + dt.timedelta(seconds=120)
    db.commit()
    return row.redirect_uri + '?' + urlencode({'code': code, 'state': row.state})


@router.post('/auth/native/token')
def native_token(body: dict):
    with factory()() as db:
        row = db.scalar(select(NativeFlow).where(NativeFlow.code_hash == digest(str(body.get('code', '')))).with_for_update())
        verifier = str(body.get('code_verifier', ''))
        if (not row or row.consumed or row.expires_at <= now() or not 43 <= len(verifier) <= 128
                or challenge(verifier) != row.challenge
                or ('redirect_uri' in body and str(body['redirect_uri']) != row.redirect_uri)):
            raise HTTPException(400, 'Invalid or consumed native code')
        user = db.get(User, row.user_id)
        if user.status != 'active':
            raise HTTPException(403, 'Account not active')
        row.consumed = True
        return accounts.payload(accounts.issue(db, user, 'Desktop PKCE'))
