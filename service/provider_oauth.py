"""Administrator-bound OAuth grants; central tokens never enter a user runtime."""
import datetime as dt
import json
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from service import accounts
from service.db import ProviderOAuth, ProviderOAuthFlow, ModelProvider, audit, factory, now
from service.providers import ENDPOINTS, vault
from service.security import challenge, digest, token
from service.settings import config

router = APIRouter()


def settings(provider):
    if provider not in ENDPOINTS:
        raise HTTPException(404)
    cfg = config().get('provider_oauth', {}).get(provider, {})
    if not cfg.get('client_id'):
        raise HTTPException(409, '이 서비스용 OAuth client_id를 config.yaml에 등록해야 합니다.')
    redirect = urlsplit(cfg.get('redirect_uri', ''))
    if (redirect.path != '/auth/callback' or redirect.query or redirect.fragment or redirect.username
            or redirect.scheme not in ('http', 'https')
            or (redirect.scheme == 'http' and redirect.hostname not in ('localhost', '127.0.0.1'))):
        raise HTTPException(503, 'OAuth callback 등록 주소를 확인하세요.')
    if provider == 'openai':
        if cfg['client_id'] == 'dynamic_agent_client' and (redirect.scheme != 'http' or redirect.hostname != '127.0.0.1'):
            raise HTTPException(503, '동적 ChatGPT 등록은 127.0.0.1 callback이 필요합니다. 원격 서비스는 승인된 client_id와 callback을 등록하세요.')
        return {**cfg, 'authorization_url': 'https://auth.openai.com/api/accounts/authorize',
                'token_url': 'https://auth.openai.com/api/accounts/oauth/token'}
    # Operator-owned registration metadata, constrained to official TLS endpoints.
    for name in ('authorization_url', 'token_url'):
        url = urlsplit(cfg.get(name, ''))
        if url.scheme != 'https' or url.hostname not in ('claude.ai', 'platform.claude.com', 'console.anthropic.com') or url.username:
            raise HTTPException(503, 'OAuth 공급자 endpoint 설정을 확인하세요.')
    return cfg


def encrypt(value):
    return vault().encrypt(json.dumps(value).encode()).decode()


def decrypt(value):
    return json.loads(vault().decrypt(value.encode()))


def usable(row):
    return bool(row and row.selected and row.status == 'connected' and row.token_cipher
                and row.expires_at)


def status(db, provider):
    row = db.get(ProviderOAuth, provider)
    cfg = config().get('provider_oauth', {}).get(provider, {})
    return {'configured_client': bool(cfg.get('client_id')), 'selected': bool(row and row.selected),
            'status': row.status if row else 'disconnected', 'connected': usable(row),
            'available_models': row.available_models if usable(row) else [],
            'expires_at': row.expires_at.isoformat() if row and row.expires_at else None}


@router.post('/platform/api/admin/model-providers/{provider}/oauth/start')
def start(provider: str, request: Request):
    with factory()() as db:
        admin, session, _ = accounts.require(request, db, active=True, admin=True)
        cfg = settings(provider)
        accounts.throttle(db, 'provider-oauth:' + admin.id, maximum=10)
        row = db.scalar(select(ProviderOAuth).where(ProviderOAuth.provider == provider).with_for_update())
        if not row:
            row = ProviderOAuth(provider=provider)
            db.add(row)
            db.flush()
        raw, verifier, nonce = token(), token(), token()
        client_id = row.client_id or cfg['client_id']
        flow = ProviderOAuthFlow(id=digest(raw), provider=provider, session_id=session.id,
                                 verifier_cipher=encrypt(verifier), nonce=nonce, redirect_uri=cfg['redirect_uri'],
                                 client_id=client_id, generation=row.generation, expires_at=now() + dt.timedelta(minutes=10))
        db.add(flow)
        params = {'client_id': client_id, 'redirect_uri': flow.redirect_uri, 'response_type': 'code',
                  'scope': cfg['scopes'], 'state': raw, 'nonce': nonce,
                  'code_challenge': challenge(verifier), 'code_challenge_method': 'S256'}
        if provider == 'openai':
            params.update(resource='https://api.openai.com/v1', ext_agent_host_id='urn:uuid:' + row.host_id)
            if client_id == 'dynamic_agent_client':
                params['agent_name_hint'] = 'Hermes Custom Service'
        audit(db, admin.id, 'provider_oauth_start', provider)
        db.commit()
        return {'flow_id': flow.id, 'authorization_url': cfg['authorization_url'] + '?' + urlencode(params), 'expires_in': 600}


@router.get('/auth/callback')
def callback(state: str = '', code: str = '', client_id: str = '', error: str = ''):
    if len(state) > 256 or len(code) > 4096 or len(client_id) > 256:
        raise HTTPException(400, 'Invalid callback')
    with factory()() as db:
        flow = db.scalar(select(ProviderOAuthFlow).where(ProviderOAuthFlow.id == digest(state)).with_for_update())
        if not flow or flow.status != 'pending' or flow.expires_at <= now():
            raise HTTPException(400, '만료되었거나 사용된 OAuth 연결입니다.')
        if error or not code or (flow.client_id == 'dynamic_agent_client' and (not client_id or client_id == 'dynamic_agent_client')):
            flow.status = 'failed'
        else:
            if flow.client_id == 'dynamic_agent_client':
                flow.client_id = client_id
            flow.code_cipher = encrypt(code)
            flow.status = 'callback_received'
        db.commit()
    # The callback may be a different loopback host. Exchange requires the initiating admin session.
    return RedirectResponse(config()['public_url'].rstrip('/') + '/?oauth_flow=' + flow.id, status_code=303)


class Finish(BaseModel):
    flow_id: str = Field(min_length=64, max_length=64)


@router.get('/platform/api/admin/oauth-flows/{flow_id}')
def flow_status(flow_id: str, request: Request):
    with factory()() as db:
        _, session, _ = accounts.require(request, db, active=True, admin=True)
        flow = db.get(ProviderOAuthFlow, flow_id)
        if not flow or flow.session_id != session.id or flow.expires_at <= now():
            raise HTTPException(404, 'OAuth 연결 요청을 찾을 수 없습니다.')
        return {'provider': flow.provider, 'status': flow.status}


def _exchange_sync(cfg, provider, data):
    try:
        with httpx.Client(timeout=20, trust_env=False) as client:
            result = client.post(cfg['token_url'], **({'json': data} if provider == 'anthropic' else {'data': data}))
        if result.status_code != 200:
            raise HTTPException(502, 'OAuth 인증 교환 실패. 관리자 재연결이 필요합니다.')
        payload = result.json()
        if not payload.get('access_token') or not 60 <= int(payload.get('expires_in', 0)) <= 86400 * 30:
            raise ValueError('invalid token response')
        return payload
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(502, 'OAuth 인증 응답을 확인할 수 없습니다.') from exc


async def exchange(cfg, provider, data):
    return await run_in_threadpool(_exchange_sync, cfg, provider, data)


async def identity(payload, client_id, nonce):
    try:
        raw = payload['id_token']
        header = jwt.get_unverified_header(raw)
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get('https://auth.openai.com/.well-known/jwks.json')
            response.raise_for_status()
        key = next(k for k in response.json()['keys'] if k.get('kid') == header.get('kid'))
        claims = jwt.decode(raw, jwt.PyJWK.from_dict(key).key, algorithms=['RS256'],
                            issuer='https://auth.openai.com', audience=client_id,
                            options={'require': ['exp', 'iat', 'sub', 'iss', 'aud', 'nonce']})
        if claims['nonce'] != nonce:
            raise ValueError('nonce mismatch')
        return claims['sub']
    except (KeyError, ValueError, TypeError, StopIteration, jwt.PyJWTError, httpx.HTTPError) as exc:
        raise HTTPException(502, 'OAuth 계정 신원을 검증할 수 없습니다.') from exc


async def catalog(provider, access, workspace=''):
    headers = {'Authorization': 'Bearer ' + access}
    if provider == 'anthropic':
        headers['anthropic-version'] = '2023-06-01'
    if workspace:
        headers['anthropic-workspace-id'] = workspace
    try:
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            result = await client.get(ENDPOINTS[provider] + '/models', headers=headers)
            result.raise_for_status()
        data = result.json()
        if provider == 'openai':
            return [m['slug'] for m in data['models'] if m.get('visibility') == 'list' and isinstance(m.get('slug'), str)]
        return [m['id'] for m in data['data'] if isinstance(m.get('id'), str)]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(502, 'OAuth 계정의 모델 권한을 확인할 수 없습니다.') from exc


@router.post('/platform/api/admin/model-providers/{provider}/oauth/finish')
async def finish(provider: str, body: Finish, request: Request):
    with factory()() as db:
        admin, session, _ = accounts.require(request, db, active=True, admin=True)
        cfg = settings(provider)
        flow = db.scalar(select(ProviderOAuthFlow).where(ProviderOAuthFlow.id == body.flow_id).with_for_update())
        row = db.scalar(select(ProviderOAuth).where(ProviderOAuth.provider == provider).with_for_update())
        if (not flow or flow.provider != provider or flow.session_id != session.id or not row
                or flow.generation != row.generation or flow.expires_at <= now() or flow.status != 'callback_received'):
            raise HTTPException(409, 'OAuth 완료 상태와 시작한 관리자 기기를 확인하세요.')
        code, verifier = decrypt(flow.code_cipher), decrypt(flow.verifier_cipher)
        flow.status, flow.code_cipher = 'consumed', None
        db.commit()
        data = {'grant_type': 'authorization_code', 'client_id': flow.client_id, 'code': code,
                'redirect_uri': flow.redirect_uri, 'code_verifier': verifier}
        if provider == 'openai':
            data['resource'] = 'https://api.openai.com/v1'
        payload = await exchange(cfg, provider, data)
        required = 'chatgpt.tokens.use.direct' if provider == 'openai' else 'user:inference'
        if required not in payload.get('scope', '').split():
            raise HTTPException(403, 'OAuth 추론 권한이 부여되지 않았습니다.')
        subject = await identity(payload, flow.client_id, flow.nonce) if provider == 'openai' else ''
        models = await catalog(provider, payload['access_token'])
        # Reacquire the generation lock after network I/O; disconnect/reconnect cannot be resurrected.
        db.expire_all()
        row = db.scalar(select(ProviderOAuth).where(ProviderOAuth.provider == provider).with_for_update())
        if row.generation != flow.generation:
            raise HTTPException(409, '연결 상태가 변경되었습니다. 다시 연결하세요.')
        if row.subject and subject and row.subject != subject:
            raise HTTPException(409, '다른 계정으로 변경하려면 기존 연결을 먼저 해제하세요.')
        row.token_cipher, row.client_id, row.subject = encrypt(payload), flow.client_id, subject
        row.status, row.selected, row.available_models = 'connected', True, models
        row.expires_at = now() + dt.timedelta(seconds=int(payload['expires_in']))
        row.generation += 1
        audit(db, admin.id, 'provider_oauth_connected', provider)
        db.commit()
        return status(db, provider)


@router.post('/platform/api/admin/model-providers/{provider}/oauth/disconnect')
def disconnect(provider: str, request: Request):
    if provider not in ENDPOINTS:
        raise HTTPException(404)
    with factory()() as db:
        admin, _, _ = accounts.require(request, db, active=True, admin=True)
        row = db.scalar(select(ProviderOAuth).where(ProviderOAuth.provider == provider).with_for_update())
        if row:
            row.token_cipher, row.available_models, row.subject, row.client_id = None, [], '', ''
            row.status, row.selected = 'disconnected', True
            row.generation += 1
            audit(db, admin.id, 'provider_oauth_disconnected', provider)
            db.commit()
        return {'status': 'disconnected', 'note': '중앙 연결을 해제했습니다. 공급자 설정의 앱 권한 철회는 별도입니다.'}


def _access_token(provider, generation=None):
    """A DB row lock serializes rotating refresh tokens across processes."""
    cfg = settings(provider)
    with factory()() as db:
        row = db.scalar(select(ProviderOAuth).where(ProviderOAuth.provider == provider).with_for_update())
        if not row or not row.selected or row.status != 'connected' or not row.token_cipher:
            raise HTTPException(503, '관리자 OAuth 재연결이 필요합니다.')
        if generation is not None and row.generation != generation:
            raise HTTPException(409, 'OAuth 연결이 변경되었습니다. 요청을 다시 실행하세요.')
        payload = decrypt(row.token_cipher)
        if row.expires_at <= now() + dt.timedelta(seconds=60):
            if not payload.get('refresh_token'):
                row.status = 'reauth_required'
                db.commit()
                raise HTTPException(503, 'OAuth 인증이 만료되었습니다. 관리자 재연결이 필요합니다.')
            try:
                fresh = _exchange_sync(cfg, provider, {'grant_type': 'refresh_token', 'client_id': row.client_id,
                                                      'refresh_token': payload['refresh_token'],
                                                      **({'resource': 'https://api.openai.com/v1'} if provider == 'openai' else {})})
            except HTTPException:
                row.status = 'reauth_required'
                db.commit()
                raise
            payload.update(fresh)
            required = 'chatgpt.tokens.use.direct' if provider == 'openai' else 'user:inference'
            if required not in payload.get('scope', '').split():
                row.status = 'reauth_required'
                db.commit()
                raise HTTPException(503, 'OAuth 추론 권한이 철회되었습니다. 관리자 재연결이 필요합니다.')
            row.token_cipher = encrypt(payload)
            row.expires_at = now() + dt.timedelta(seconds=int(fresh['expires_in']))
            db.commit()
        return payload['access_token']


async def access_token(provider, generation=None):
    return await run_in_threadpool(_access_token, provider, generation)
