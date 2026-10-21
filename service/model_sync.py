"""Persist central selection into Hermes defaults without mutating live conversation context."""
import httpx
from fastapi import HTTPException
from sqlalchemy import select

from service.db import Space, factory


async def apply(owner, session_id, model):
    with factory()() as db:
        space = db.scalar(select(Space).where(Space.user_id == owner))
        running = space and space.runtime_status == 'running'
    if not running:
        return {'runtime_applied': False, 'requires_new_session': True, 'apply_on_start': True}
    from service.proxy import bridge, runtime_base
    try:
        base = await runtime_base(owner)
        async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
            response = await client.post(base + '/api/model/set', headers={'Authorization': 'Bearer ' + bridge(session_id)},
                                         json={'scope': 'main', 'provider': 'custom', 'model': model,
                                               'base_url': 'http://control:8000/v1', 'confirm_expensive_model': True})
        if response.status_code != 200 or not response.json().get('ok'):
            raise HTTPException(503, '모델 선택은 저장했지만 실행 환경 설정에 적용하지 못했습니다. 환경을 중지·시작하고 새 채팅을 여세요.')
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, '모델 선택은 저장했지만 실행 환경에 연결하지 못했습니다. 환경을 중지·시작하고 새 채팅을 여세요.') from exc
    return {'runtime_applied': True, 'requires_new_session': True, 'apply_on_start': False}
