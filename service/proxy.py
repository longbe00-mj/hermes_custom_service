"""Authenticated data plane. The destination is derived from the principal, never a client URL."""
import asyncio
import datetime as dt
import hmac
import json
import logging

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from service import accounts
from service.db import ActionToken, LoginSession, User, factory, now
from service.operations import manager
from service.security import digest, runtime_token, token
from service.settings import config

router = APIRouter()
_HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
        'transfer-encoding', 'upgrade', 'content-length', 'set-cookie'}
# Central policy stays outside the tenant-controlled config. Reading provider keys is never exposed.
_DENIED = ('/api/env', '/api/config/env', '/api/auth/', '/api/mcp/oauth', '/api/oauth', '/api/providers', '/api/settings/providers')
_MANAGED = ('/api/config', '/api/gateway', '/api/plugins', '/api/update', '/api/profiles', '/api/model')


def live_session(db, sid):
    row = db.get(LoginSession, sid)
    user = db.get(User, row.user_id) if row else None
    return bool(row and not row.revoked and row.access_until > now() and user
                and user.status == 'active' and user.email_verified)


def bridge(sid):
    from service.settings import secret
    import hashlib
    expiry = str(int(now().timestamp()) + 60)
    value = sid + ':' + expiry
    signature = hmac.new(secret('app_key').encode(), value.encode(), hashlib.sha256).hexdigest()
    return 'bridge:' + value + ':' + signature


def resolve_bridge(db, raw):
    from service.settings import secret
    import hashlib
    try:
        prefix, sid, expiry, signature = raw.split(':')
        expected = hmac.new(secret('app_key').encode(), (sid + ':' + expiry).encode(), hashlib.sha256).hexdigest()
        if prefix != 'bridge' or not hmac.compare_digest(expected, signature) or int(expiry) <= int(now().timestamp()):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise HTTPException(401) from exc
    if not live_session(db, sid):
        raise HTTPException(401)
    row = db.get(LoginSession, sid)
    return db.get(User, row.user_id), row


@router.post('/api/auth/ws-ticket')
def ws_ticket(request: Request):
    with factory()() as db:
        # Existing Hermes UI sends same-origin POSTs without our portal's CSRF header.
        access = request.headers.get('authorization', '').removeprefix('Bearer ') or getattr(request.state, 'access', request.cookies.get(accounts.AT_COOKIE, ''))
        user, session = accounts.resolve(db, access, active=True)
        if (not request.headers.get('authorization') and request.headers.get('origin') != config()['public_url']
                and not accounts.native_json(request)):
            raise HTTPException(403, 'Origin required')
        raw = token()
        db.add(ActionToken(digest=digest(raw), user_id=user.id, kind='ws', session_id=session.id,
                           expires_at=now() + dt.timedelta(seconds=30)))
        db.commit()
        return {'ticket': raw, 'ttl_seconds': 30}


async def runtime_base(owner):
    result = await manager(owner)
    if result['status'] != 'running':
        raise HTTPException(409, '개인 환경을 먼저 시작하세요.')
    return f'http://hc-user-{owner}:9119'


def permitted(path, method):
    if path.startswith(_DENIED):
        raise HTTPException(403, '중앙 관리 기능입니다. 포털을 사용하세요.')
    if path.startswith(_MANAGED) and method not in ('GET', 'HEAD'):
        raise HTTPException(403, '환경 정책은 중앙에서 관리합니다.')


@router.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'])
async def http_proxy(path: str, request: Request):
    path = '/' + path
    permitted(path, request.method)
    with factory()() as db:
        access = request.headers.get('authorization', '').removeprefix('Bearer ') or getattr(request.state, 'access', request.cookies.get(accounts.AT_COOKIE, ''))
        user, session = accounts.resolve(db, access, active=True)
        owner, sid = user.id, session.id
    if request.method not in ('GET', 'HEAD') and not request.headers.get('authorization'):
        if request.headers.get('origin') != config()['public_url'] and not accounts.native_json(request):
            raise HTTPException(403, 'Origin required')
    base = await runtime_base(owner)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 25 * 1024 * 1024:
            raise HTTPException(413)
    headers = {k: v for k, v in request.headers.items() if k.lower() in ('content-type', 'accept', 'range', 'if-none-match')}
    headers.update({'authorization': 'Bearer ' + bridge(sid), 'host': f'hc-user-{owner}:9119'})
    client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5), trust_env=False)
    try:
        upstream = await client.send(client.build_request(request.method, base + path,
                                    params=list(request.query_params.multi_items()), headers=headers, content=bytes(body)), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(503, '환경 준비 중입니다. 잠시 후 새로고침하세요.') from exc

    async def chunks():
        try:
            async for data in upstream.aiter_raw():
                with factory()() as db:
                    if not live_session(db, sid):
                        break
                yield data
        finally:
            await upstream.aclose()
            await client.aclose()
    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP}
    return StreamingResponse(chunks(), status_code=upstream.status_code, headers=response_headers)


@router.websocket('/{path:path}')
async def websocket_proxy(socket: WebSocket, path: str):
    if path not in ('api/ws', 'api/pty', 'api/pub', 'api/events'):
        await socket.close(code=1008)
        return
    origin = socket.headers.get('origin')
    # Packaged Electron renders from file://. It still needs the same short-lived,
    # one-use ticket, issued only to an authenticated session, as browser clients.
    if origin and origin not in (config()['public_url'], 'file://'):
        logging.getLogger(__name__).warning('Rejected WebSocket origin: %r', origin)
        await socket.close(code=1008)
        return
    try:
        with factory()() as db:
            raw = socket.query_params.get('ticket', '')
            ticket = db.scalar(select(ActionToken).where(ActionToken.digest == digest(raw)).with_for_update())
            if not ticket or ticket.kind != 'ws' or ticket.consumed or ticket.expires_at <= now() or not live_session(db, ticket.session_id):
                raise HTTPException(401)
            ticket.consumed = True
            owner, sid = ticket.user_id, ticket.session_id
            db.commit()
        base = await runtime_base(owner)
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            result = await client.post(base + '/api/auth/ws-ticket', headers={'Authorization': 'Bearer ' + bridge(sid)})
            if result.status_code != 200:
                raise HTTPException(401)
            backend_ticket = result.json()['ticket']
        from urllib.parse import urlencode
        query = [(k, v) for k, v in socket.query_params.multi_items() if k not in ('ticket', 'token')]
        query.append(('ticket', backend_ticket))
        url = base.replace('http:', 'ws:', 1) + '/' + path + '?' + urlencode(query)
        async with websockets.connect(url, origin=base, max_size=16 * 1024 * 1024, proxy=None) as upstream:
            await socket.accept()

            async def incoming():
                while True:
                    frame = await socket.receive()
                    if frame['type'] == 'websocket.disconnect':
                        return
                    with factory()() as db:
                        if not live_session(db, sid):
                            return
                    if path == 'api/ws' and frame.get('text'):
                        try:
                            rpc = json.loads(frame['text'])
                        except ValueError:
                            rpc = None
                        if isinstance(rpc, dict) and rpc.get('method') == 'model.options':
                            from service.model_ui import options
                            with factory()() as db:
                                payload = options(db, owner)
                            if 'id' in rpc:
                                await socket.send_json({'jsonrpc': '2.0', 'id': rpc['id'], 'result': payload})
                            continue
                    await upstream.send(frame.get('bytes') if frame.get('bytes') is not None else frame['text'])

            async def outgoing():
                async for frame in upstream:
                    if isinstance(frame, bytes):
                        await socket.send_bytes(frame)
                    else:
                        await socket.send_text(frame)

            async def watch():
                while True:
                    await asyncio.sleep(2)
                    with factory()() as db:
                        if not live_session(db, sid):
                            return
            tasks = [asyncio.create_task(fn()) for fn in (incoming, outgoing, watch)]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (HTTPException, httpx.HTTPError, OSError, websockets.WebSocketException, WebSocketDisconnect):
        pass
    finally:
        try:
            await socket.close(code=1008)
        except RuntimeError:
            pass
