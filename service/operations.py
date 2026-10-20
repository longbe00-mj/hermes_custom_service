import hmac

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from service import accounts
from service.db import Audit, LoginSession, Space, Usage, User, audit, factory, now
from service.security import runtime_token
from service.settings import config, secret

router = APIRouter()


async def manager(owner, action='', method='GET'):
    suffix = '/' + action if action else ''
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        try:
            response = await client.request(method, f"{config()['manager_url']}/runtimes/{owner}{suffix}",
                                            headers={'x-manager-key': secret('manager_key')})
        except httpx.HTTPError as exc:
            raise HTTPException(503, 'Runtime Manager에 연결할 수 없습니다.') from exc
        if response.status_code >= 400:
            raise HTTPException(response.status_code, response.json().get('detail', 'Runtime operation failed'))
        return response.json()


@router.get('/platform/api/runtime')
async def runtime_status(request: Request):
    with factory()() as db:
        user, _, _ = accounts.require(request, db, active=True)
        result = await manager(user.id)
        space = db.scalar(select(Space).where(Space.user_id == user.id))
        space.runtime_status = result['status']
        db.commit()
        return result


@router.post('/platform/api/runtime/{action}')
async def runtime_action(action: str, request: Request):
    if action not in ('start', 'stop'):
        raise HTTPException(404)
    with factory()() as db:
        user, _, _ = accounts.require(request, db, active=True)
        result = await manager(user.id, action, 'POST')
        db.scalar(select(Space).where(Space.user_id == user.id)).runtime_status = result['status']
        audit(db, user.id, 'runtime_' + action, user.id)
        db.commit()
    return result


@router.get('/platform/api/admin/users')
def users(request: Request):
    with factory()() as db:
        accounts.require(request, db, active=True, admin=True)
        return [{'id': u.id, 'username': u.username, 'email': u.email, 'status': u.status,
                 'email_verified': u.email_verified, 'admin': u.admin} for u in db.scalars(select(User).order_by(User.created_at))]


class Decision(BaseModel):
    action: str
    reason: str = ''


@router.post('/platform/api/admin/users/{user_id}')
async def decide(user_id: str, body: Decision, request: Request):
    with factory()() as db:
        admin, _, _ = accounts.require(request, db, active=True, admin=True)
        target = db.scalar(select(User).where(User.id == user_id).with_for_update())
        if not target or target.admin:
            raise HTTPException(400, '일반 사용자 계정만 관리할 수 있습니다.')
        states = {'approve': 'active', 'reject': 'rejected', 'suspend': 'suspended', 'reactivate': 'active'}
        if body.action not in states or len(body.reason) > 500:
            raise HTTPException(422)
        if body.action in ('reject', 'suspend') and not body.reason.strip():
            raise HTTPException(422, '사유를 입력하세요.')
        next_state = states[body.action]
        if target.status != next_state:
            old = target.status
            target.status = next_state
            if next_state != 'active':
                accounts.revoke_all(db, target.id)
            audit(db, admin.id, body.action, target.id, previous=old, reason=body.reason)
            db.commit()
        # Retry stop even on idempotent suspension retries; the DB denial persists if Docker is down.
        if next_state != 'active':
            await manager(target.id, 'stop', 'POST')
    return {'status': next_state}


@router.get('/platform/api/sessions')
def sessions(request: Request):
    with factory()() as db:
        user, current, _ = accounts.require(request, db)
        return [{'id': s.id, 'device': s.device, 'current': s.id == current.id, 'revoked': s.revoked,
                 'created_at': s.created_at.isoformat()} for s in db.scalars(select(LoginSession).where(LoginSession.user_id == user.id))]


@router.delete('/platform/api/sessions/{sid}')
def revoke(sid: str, request: Request):
    with factory()() as db:
        user, _, _ = accounts.require(request, db)
        rows = list(db.scalars(select(LoginSession).where(LoginSession.user_id == user.id)))
        for row in rows:
            if sid == 'all' or row.id == sid:
                row.revoked = True
        audit(db, user.id, 'session_revoke', sid)
        db.commit()
    return {'ok': True}


@router.get('/platform/api/usage')
def usage(request: Request):
    with factory()() as db:
        user, _, _ = accounts.require(request, db)
        rows = db.scalars(select(Usage).where(Usage.user_id == user.id).order_by(Usage.day.desc()).limit(31))
        return {'daily_token_limit': config()['daily_token_limit'],
                'days': [{'day': r.day, 'charged_tokens': r.tokens, 'inflight': r.inflight} for r in rows]}


@router.get('/platform/api/admin/audit')
def audit_events(request: Request):
    with factory()() as db:
        accounts.require(request, db, active=True, admin=True)
        return [{'actor': e.actor, 'event': e.event, 'target': e.target, 'detail': e.detail,
                 'at': e.created_at.isoformat()} for e in db.scalars(select(Audit).order_by(Audit.created_at.desc()).limit(200))]


@router.post('/internal/runtime-auth')
def runtime_auth(body: dict, request: Request):
    owner = str(body.get('owner_id', ''))
    auth = request.headers.get('authorization', '')
    if not hmac.compare_digest(auth, 'Bearer ' + runtime_token(owner)):
        raise HTTPException(401)
    with factory()() as db:
        access = str(body.get('access_token', ''))
        if access.startswith('bridge:'):
            from service.proxy import resolve_bridge
            user, session = resolve_bridge(db, access)
        else:
            user, session = accounts.resolve(db, access, active=True)
        if user.id != owner:
            raise HTTPException(403)
        return {'user_id': user.id, 'email': user.email, 'display_name': user.username, 'org_id': '',
                'provider': 'central', 'expires_at': int(session.access_until.timestamp())}
