"""Tenant-scoped model gateway with durable reservation and streaming accounting."""
import hmac
import json

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from service.db import ModelCall, Usage, User, factory, now
from service.security import runtime_token
from service.settings import config, secret
from service.providers import ENDPOINTS, allowed, selected_model, vault

router = APIRouter()


def principal(db, request):
    raw = request.headers.get('authorization', '').removeprefix('Bearer ')
    # The small initial deployment has at most dozens of users; never accept a client-supplied owner.
    for user in db.scalars(select(User).where(User.status == 'active', User.email_verified.is_(True))):
        if hmac.compare_digest(raw, runtime_token(user.id)):
            return user
    raise HTTPException(401)


@router.get('/v1/models')
def catalog(request: Request):
    with factory()() as db:
        principal(db, request)
        models = list(allowed(db))
    return {'object': 'list', 'data': [{'id': m, 'object': 'model'} for m in models]}


def reserve(db, owner, amount):
    user = db.scalar(select(User).where(User.id == owner).with_for_update())
    if not user or user.status != 'active':
        raise HTTPException(403, 'Account is not active')
    concurrent = db.scalar(select(func.count()).select_from(ModelCall).where(ModelCall.user_id == owner, ModelCall.state == 'running'))
    if concurrent >= config()['max_model_parallel']:
        raise HTTPException(429, 'Concurrent model-call limit reached')
    day = now().date().isoformat()
    key = owner + ':' + day
    db.execute(insert(Usage).values(id=key, user_id=owner, day=day, tokens=0, inflight=0).on_conflict_do_nothing())
    row = db.scalar(select(Usage).where(Usage.id == key).with_for_update())
    if row.tokens + amount > config()['daily_token_limit'] or row.inflight >= config()['max_model_parallel']:
        raise HTTPException(429, 'Daily budget or concurrent model-call limit reached')
    row.tokens += amount
    row.inflight += 1
    call = ModelCall(user_id=owner, usage_id=key, reserved=amount)
    db.add(call)
    db.commit()
    return call.id


def settle(call_id, actual=None, failed=False):
    with factory()() as db:
        call = db.scalar(select(ModelCall).where(ModelCall.id == call_id).with_for_update())
        if call.state != 'running':
            return
        row = db.scalar(select(Usage).where(Usage.id == call.usage_id).with_for_update())
        row.inflight = max(0, row.inflight - 1)
        call.actual = actual
        # Missing usage and interrupted streams retain the reservation, never silently free spend.
        if actual is not None:
            row.tokens += actual - call.reserved
        call.state = 'failed' if failed else ('measured' if actual is not None else 'reserved')
        db.commit()


@router.post('/v1/chat/completions')
async def completion(request: Request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 2 * 1024 * 1024:
            raise HTTPException(413)
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, 'Invalid JSON') from exc
    if not isinstance(body, dict):
        raise HTTPException(400, 'Expected JSON object')
    with factory()() as db:
        owner = principal(db, request).id
        model = selected_model(db, owner) if body.get('model') == 'central-default' else body.get('model')
        if not isinstance(model, str) or model not in allowed(db):
            raise HTTPException(403, 'Model is not allowed')
        provider = allowed(db)[model]
        key = vault().decrypt(provider.key_cipher.encode()).decode() if provider.key_cipher else (secret('provider_api_key') if provider.id == 'openai' else '')
        endpoint = ENDPOINTS[provider.id]
        workspace = provider.workspace
    if not key:
        raise HTTPException(503, '관리자가 모델 API 키를 설정해야 합니다.')
    if body.get('n', 1) != 1:
        raise HTTPException(422, 'Only one completion per request is supported')
    body['model'] = model
    cap = body.get('max_completion_tokens', body.get('max_tokens', 4096))
    if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= 16384:
        raise HTTPException(422, 'Output token cap must be 1..16384')
    body['max_completion_tokens'] = cap
    body.pop('max_tokens', None)
    if body.get('stream'):
        body['stream_options'] = {'include_usage': True}
    # Byte-based reservation is conservative for text, not a provider billing quote.
    amount = len(raw) + cap + 1024
    with factory()() as db:
        principal(db, request)
        call_id = reserve(db, owner, amount)
    client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10), trust_env=False)
    try:
        upstream = await client.send(client.build_request('POST', endpoint + '/chat/completions',
                                    json=body, headers={'Authorization': 'Bearer ' + key, **({'anthropic-workspace-id': workspace} if workspace else {})}), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        settle(call_id, failed=True)
        raise HTTPException(502, 'Model provider connection failed') from exc
    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        settle(call_id, failed=True)
        raise HTTPException(upstream.status_code, 'Model provider rejected the request')

    async def stream():
        actual, failed = None, True
        try:
            if body.get('stream'):
                async for line in upstream.aiter_lines():
                    with factory()() as db:
                        user = db.get(User, owner)
                        if user.status != 'active':
                            return
                    if line.startswith('data: ') and line != 'data: [DONE]':
                        try:
                            usage = json.loads(line[6:]).get('usage') or {}
                            if 'total_tokens' in usage:
                                actual = max(0, int(usage['total_tokens']))
                        except (ValueError, TypeError):
                            pass
                    yield (line + '\n').encode()
                failed = False
            else:
                data = await upstream.aread()
                usage = json.loads(data).get('usage') or {}
                actual = int(usage['total_tokens']) if 'total_tokens' in usage else None
                failed = False
                yield data
        finally:
            await upstream.aclose()
            await client.aclose()
            settle(call_id, actual, failed)
    return StreamingResponse(stream(), media_type='text/event-stream' if body.get('stream') else 'application/json')
