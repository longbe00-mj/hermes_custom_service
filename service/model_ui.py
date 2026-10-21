"""Hermes GUI picker contract backed exclusively by central model policy."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from service import accounts
from service.db import factory
from service.providers import allowed, save_selection, selected_model

router = APIRouter()


def options(db, owner):
    models = list(allowed(db))
    return {'provider': 'custom', 'model': selected_model(db, owner),
            'providers': [{'slug': 'custom', 'name': 'Central · 관리자 허용 모델',
                           'models': models, 'total_models': len(models), 'is_current': True,
                           'is_user_defined': True, 'authenticated': True,
                           'source': 'admin', 'auth_type': 'central'}] if models else []}


@router.get('/api/model/options')
def gui_options(request: Request):
    with factory()() as db:
        user, _, _ = accounts.require(request, db, active=True)
        return options(db, user.id)


@router.get('/api/model/auxiliary')
def gui_assignments(request: Request):
    with factory()() as db:
        user, _, _ = accounts.require(request, db, active=True)
        return {'main': {'provider': 'custom', 'model': selected_model(db, user.id)}, 'tasks': []}


class Assignment(BaseModel):
    scope: str = Field(max_length=32)
    provider: str = Field(max_length=64)
    model: str = Field(max_length=128)
    base_url: str = Field(default='', max_length=512)
    api_key: str = Field(default='', max_length=4096, exclude=True)


@router.post('/api/model/set')
async def gui_assignment(body: Assignment, request: Request):
    if body.scope != 'main' or body.provider != 'custom' or body.base_url or body.api_key:
        raise HTTPException(403, '중앙 허용 모델만 선택할 수 있습니다. 공급자·보조 모델 설정은 관리자가 관리합니다.')
    with factory()() as db:
        user, session, _ = accounts.require(request, db, active=True, gui=True)
        result = save_selection(db, user, body.model)
    from service import model_sync
    applied = await model_sync.apply(user.id, session.id, body.model)
    return {'ok': True, 'scope': 'main', 'provider': 'custom', 'model': result['model'], **applied}
