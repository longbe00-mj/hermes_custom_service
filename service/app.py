from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from service import accounts
from service.db import factory
from service.settings import config

app = FastAPI(title='Hermes Central', docs_url=None, redoc_url=None, openapi_url=None)
from urllib.parse import urlsplit
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(config()['public_url']).hostname, 'localhost', '127.0.0.1', 'control', 'platform', 'testserver'])


from fastapi import HTTPException


@app.exception_handler(HTTPException)
async def api_error(request, error):
    body = {'detail': error.detail}
    if error.status_code == 401:
        body.update(error='unauthenticated', reason='invalid_or_expired_session', login_url='/login')
    return JSONResponse(body, status_code=error.status_code, headers=error.headers)


@app.middleware('http')
async def boundary(request: Request, call_next):
    origin = request.headers.get('origin')
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin and origin != config()['public_url']:
        return JSONResponse({'detail': 'Origin not allowed'}, status_code=403)
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.url.path.startswith(('/auth/', '/platform/api/')):
        if not origin and request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': 'Cross-site request denied'}, status_code=403)
    refreshed = None
    if (not request.headers.get('authorization') and not request.url.path.startswith(('/auth/', '/internal/', '/v1/'))
            and request.cookies.get(accounts.RT_COOKIE)):
        from fastapi import HTTPException
        with factory()() as db:
            try:
                accounts.resolve(db, request.cookies.get(accounts.AT_COOKIE, ''))
            except HTTPException as exc:
                if exc.status_code == 401:
                    try:
                        refreshed = accounts.rotate(db, request.cookies[accounts.RT_COOKIE])
                        request.state.access = refreshed[2]
                    except HTTPException:
                        pass
    response = await call_next(request)
    if refreshed:
        accounts.cookies(response, refreshed)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/healthz')
def health():
    with factory()() as db:
        db.execute(text('select 1'))
    return {'ok': True}


@app.get('/api/status')
def status():
    return {'status': 'ok', 'auth_required': True, 'auth_flows': ['native_pkce'], 'platform': 'hermes-custom', 'version': '0.1.0'}


@app.get('/')
@app.get('/login')
@app.get('/platform')
def portal():
    return HTMLResponse(Path('/app/service/portal.html').read_text())


from service.auth_routes import router as auth_router
from service.operations import router as operations_router
from service.models import router as models_router
from service.proxy import router as proxy_router

app.include_router(auth_router)
app.include_router(operations_router)
app.include_router(models_router)
app.include_router(proxy_router)
