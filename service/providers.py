"""Central provider credentials and model policy; no provider token reaches a tenant."""
import base64
import hashlib
import re

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select, text

from service import accounts
from service.db import ModelPolicy, ModelPreference, ModelProvider, audit, factory
from service.settings import config, secret

router = APIRouter()
ENDPOINTS = {'openai': 'https://api.openai.com/v1', 'anthropic': 'https://api.anthropic.com/v1'}
OAUTH = {
    'openai': {'supported': False, 'reason': 'ChatGPT OAuth는 사용자·워크스페이스 단위입니다. 중앙 다중 사용자 공유는 공급자 승인과 별도 통합이 필요합니다.',
               'documentation': 'https://developers.openai.com/siwc/token-sharing-open-source'},
    'anthropic': {'supported': False, 'reason': '다른 사용자에게 제공하는 서비스는 Anthropic API 키 또는 지원 클라우드 연결을 사용해야 합니다.',
                  'documentation': 'https://support.claude.com/en/articles/13189465-log-in-to-your-claude-account'},
}


def vault():
    key = hashlib.sha256(('model-provider-v1:' + secret('app_key')).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def entries(db):
    rows = list(db.scalars(select(ModelProvider).order_by(ModelProvider.id)))
    if not rows:
        # Preserve operator config on upgrade until the first explicit admin save.
        return [ModelProvider(id='openai', enabled=bool(secret('provider_api_key')), models=config()['allowed_models'], workspace='')]
    return rows


def allowed(db):
    return {model: row for row in entries(db) if row.enabled for model in row.models}


def default_model(db):
    models = allowed(db)
    policy = db.get(ModelPolicy, 'global')
    chosen = policy.default_model if policy else config()['default_model']
    return chosen if chosen in models else next(iter(models), '')


def selected_model(db, owner):
    models = allowed(db)
    preference = db.get(ModelPreference, owner)
    return preference.model if preference and preference.model in models else default_model(db)


def metadata(row):
    configured = bool(row.key_cipher) or (row.id == 'openai' and bool(secret('provider_api_key')))
    return {'id': row.id, 'enabled': row.enabled, 'models': row.models, 'configured': configured,
            'workspace': row.workspace, 'auth_method': 'api_key', 'oauth': OAUTH[row.id]}


@router.post('/platform/api/admin/model-providers/{provider}/test')
async def test_provider(provider: str, request: Request):
    if provider not in ENDPOINTS:
        raise HTTPException(404)
    with factory()() as db:
        admin, _, _ = accounts.require(request, db, active=True, admin=True)
        accounts.throttle(db, 'provider-test:' + admin.id, maximum=10)
        row = next((p for p in entries(db) if p.id == provider), None)
        if not row or not metadata(row)['configured']:
            raise HTTPException(409, 'API 키를 먼저 저장하세요.')
        key = vault().decrypt(row.key_cipher.encode()).decode() if row.key_cipher else secret('provider_api_key')
        headers = {'Authorization': 'Bearer ' + key}
        if provider == 'anthropic':
            headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
        if row.workspace:
            headers['anthropic-workspace-id'] = row.workspace
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                response = await client.get(ENDPOINTS[provider] + '/models', headers=headers)
            if response.status_code != 200:
                raise HTTPException(502, '공급자 연결 확인 실패 (HTTP ' + str(response.status_code) + ')')
            models = [m['id'] for m in response.json().get('data', []) if isinstance(m.get('id'), str)]
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise HTTPException(502, '공급자 모델 목록을 조회할 수 없습니다.') from exc
        audit(db, admin.id, 'model_provider_test', provider)
        db.commit()
        return {'ok': True, 'models': models, 'note': '모델 목록 조회 성공. 실제 생성·도구 사용 가능 여부는 별도 검증이 필요합니다.'}


class ProviderInput(BaseModel):
    enabled: bool
    models: list[str] = Field(max_length=100)
    api_key: SecretStr | None = None
    workspace: str = Field(default='', max_length=128, pattern=r'^[a-zA-Z0-9_-]*$')
    default_model: str = Field(max_length=128, pattern=r'^[a-zA-Z0-9._:-]*$')


@router.get('/platform/api/admin/model-providers')
def providers(request: Request):
    with factory()() as db:
        accounts.require(request, db, active=True, admin=True)
        present = {row.id: row for row in entries(db)}
        return {'providers': [metadata(present.get(name) or ModelProvider(id=name, enabled=False, models=[], workspace=''))
                              for name in ENDPOINTS], 'default_model': default_model(db)}


@router.put('/platform/api/admin/model-providers/{provider}')
def save_provider(provider: str, body: ProviderInput, request: Request):
    if provider not in ENDPOINTS:
        raise HTTPException(404)
    models = list(dict.fromkeys(m.strip() for m in body.models))
    if any(not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}', m) or m == 'central-default' for m in models):
        raise HTTPException(422, '모델 ID 형식을 확인하세요.')
    supplied = body.api_key.get_secret_value().strip() if body.api_key else ''
    if supplied and (len(supplied) > 4096 or any(c.isspace() for c in supplied)):
        raise HTTPException(422, 'API 키 형식을 확인하세요.')
    if supplied.startswith('sk-ant-oat'):
        raise HTTPException(422, '구독 OAuth 토큰 대신 Anthropic API 키를 입력하세요.')
    with factory()() as db:
        admin, _, _ = accounts.require(request, db, active=True, admin=True)
        db.execute(text('SELECT pg_advisory_xact_lock(58231002)'))
        if not list(db.scalars(select(ModelProvider))):
            db.add(ModelProvider(id='openai', enabled=bool(secret('provider_api_key')), models=config()['allowed_models'], workspace=''))
            db.flush()
        row = db.get(ModelProvider, provider)
        if not row:
            row = ModelProvider(id=provider, enabled=False, models=[], workspace='')
            db.add(row)
        if supplied:
            row.key_cipher = vault().encrypt(supplied.encode()).decode()
        row.enabled, row.models, row.workspace = body.enabled, models, body.workspace
        db.flush()
        if row.enabled and (not models or not metadata(row)['configured']):
            raise HTTPException(422, '활성화하려면 API 키와 허용 모델을 설정하세요.')
        all_models = [m for p in entries(db) if p.enabled for m in p.models]
        if len(set(all_models)) != len(all_models):
            raise HTTPException(422, '모델 ID는 공급자 간 중복될 수 없습니다.')
        if all_models and body.default_model not in all_models:
            raise HTTPException(422, '기본 모델은 활성 공급자의 허용 모델이어야 합니다.')
        policy = db.get(ModelPolicy, 'global')
        if policy:
            policy.default_model = body.default_model if all_models else ''
        else:
            db.add(ModelPolicy(id='global', default_model=body.default_model if all_models else ''))
        audit(db, admin.id, 'model_provider_update', provider, enabled=body.enabled, models=models,
              default_model=body.default_model, credential_changed=bool(supplied))
        db.commit()
        return metadata(row)


@router.get('/platform/api/models')
def user_models(request: Request):
    with factory()() as db:
        user, _, _ = accounts.require(request, db)
        return {'models': list(allowed(db)), 'default_model': default_model(db), 'selected_model': selected_model(db, user.id)}


class Selection(BaseModel):
    model: str = Field(max_length=128)


@router.put('/platform/api/models/selection')
def select_model(body: Selection, request: Request):
    with factory()() as db:
        user, _, _ = accounts.require(request, db, active=True)
        if body.model not in allowed(db):
            raise HTTPException(403, '관리자가 허용하지 않은 모델입니다.')
        row = db.get(ModelPreference, user.id)
        if row:
            row.model = body.model
        else:
            db.add(ModelPreference(user_id=user.id, model=body.model))
        audit(db, user.id, 'model_selection', body.model)
        db.commit()
        return {'model': body.model}
