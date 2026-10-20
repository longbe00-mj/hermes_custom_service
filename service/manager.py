"""Trusted Docker boundary. Never exposed to the browser or a tenant network."""
import hmac
import threading
import uuid

import docker
from docker.errors import DockerException, NotFound
from fastapi import Depends, FastAPI, HTTPException, Request

from service.security import runtime_token
from service.settings import config, secret

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_lock = threading.RLock()
LABEL = 'hermes.custom.owner'
DEPLOY = 'hermes.custom.deployment'
DEPLOYMENT = 'hermes-custom'


def authorized(request: Request):
    if not hmac.compare_digest(request.headers.get('x-manager-key', ''), secret('manager_key')):
        raise HTTPException(401)


def identifier(raw):
    try:
        return str(uuid.UUID(raw))
    except ValueError as exc:
        raise HTTPException(422, 'Invalid owner ID') from exc


def client():
    return docker.from_env(timeout=15)


def runtime_name(owner):
    return 'hc-user-' + identifier(owner)


def owned(container, owner):
    if container.labels.get(LABEL) != owner or container.labels.get(DEPLOY) != DEPLOYMENT:
        raise HTTPException(409, 'Foreign container collision')


def report(container, owner):
    owned(container, owner)
    container.reload()
    return {'status': container.status, 'hostname': container.name, 'owner_id': owner}


@app.get('/healthz')
def health():
    client().ping()
    return {'ok': True}


@app.get('/runtimes/{owner}', dependencies=[Depends(authorized)])
def status(owner: str):
    owner = identifier(owner)
    try:
        cli = client()
        container = cli.containers.get(runtime_name(owner))
        owned(container, owner)
        if container.status == 'running':
            connect_services(cli, cli.networks.get(runtime_name(owner) + '-net'))
        return report(container, owner)
    except NotFound:
        return {'status': 'stopped', 'owner_id': owner}


def ensure_network(cli, name, labels):
    try:
        net = cli.networks.get(name)
        if net.attrs.get('Labels', {}).get(LABEL) != labels[LABEL]:
            raise HTTPException(409, 'Foreign network collision')
        return net
    except NotFound:
        return cli.networks.create(name, internal=True, labels=labels)


def connect_services(cli, network):
    for service, alias in (('central', 'control'), ('egress', 'egress')):
        peers = cli.containers.list(filters={'label': [f'hermes.platform={service}',
                                                       'com.docker.compose.project=hermes-custom']})
        if len(peers) != 1:
            raise HTTPException(503, f'Expected one running {service} service')
        network.reload()
        if peers[0].id not in network.attrs.get('Containers', {}):
            network.connect(peers[0], aliases=[alias])


@app.post('/runtimes/{owner}/start', dependencies=[Depends(authorized)])
def start(owner: str):
    owner = identifier(owner)
    name, cfg = runtime_name(owner), config()
    cli = client()
    labels = {LABEL: owner, DEPLOY: DEPLOYMENT}
    with _lock:
        try:
            container = cli.containers.get(name)
        except NotFound:
            container = None
        if container:
            owned(container, owner)
            connect_services(cli, cli.networks.get(name + '-net'))
            if container.status == 'running':
                return report(container, owner)
        running = cli.containers.list(filters={'label': [f'{DEPLOY}={DEPLOYMENT}', LABEL]})
        if len(running) >= cfg['max_active_runtimes']:
            raise HTTPException(429, '활성 환경 한도입니다. 다른 작업이 끝난 뒤 다시 시도하세요.')
        if container:
            container.start()
            return report(container, owner)
        volume_name = name + '-data'
        try:
            volume = cli.volumes.get(volume_name)
            if volume.attrs.get('Labels', {}).get(LABEL) != owner:
                raise HTTPException(409, 'Foreign volume collision')
        except NotFound:
            volume = cli.volumes.create(volume_name, labels=labels)
        # Only the root of this service-owned volume is initialized; no host paths accepted.
        cli.containers.run(cfg['runtime_image'], ['10000:10000', '/opt/data'], entrypoint='chown', user='0:0',
                           volumes={volume.name: {'bind': '/opt/data', 'mode': 'rw'}}, remove=True,
                           network_disabled=True, cap_drop=['ALL'], cap_add=['CHOWN'], labels=labels)
        network = ensure_network(cli, name + '-net', labels)
        connect_services(cli, network)
        container = cli.containers.run(
            cfg['runtime_image'], name=name, detach=True, init=True, user='10000:10000',
            network=network.name, labels=labels, read_only=True, cap_drop=['ALL'],
            security_opt=['no-new-privileges:true'], pids_limit=256, mem_limit=cfg['runtime_memory'],
            nano_cpus=int(cfg['runtime_cpus'] * 1_000_000_000),
            volumes={volume.name: {'bind': '/opt/data', 'mode': 'rw'}},
            tmpfs={'/tmp': 'rw,nosuid,nodev,size=1g'}, shm_size='256m',
            environment={'HERMES_HOME': '/opt/data', 'HOME': '/opt/data', 'OWNER_ID': owner,
                         'RUNTIME_TOKEN': runtime_token(owner), 'DEFAULT_MODEL': cfg['default_model'],
                         'HTTP_PROXY': 'http://egress:3128', 'HTTPS_PROXY': 'http://egress:3128',
                         'http_proxy': 'http://egress:3128', 'https_proxy': 'http://egress:3128',
                         'NO_PROXY': 'localhost,127.0.0.1,control', 'no_proxy': 'localhost,127.0.0.1,control'},
            restart_policy={'Name': 'unless-stopped'},
            log_config=docker.types.LogConfig(type='json-file', config={'max-size': '10m', 'max-file': '3'}))
        return report(container, owner)


@app.post('/runtimes/{owner}/stop', dependencies=[Depends(authorized)])
def stop(owner: str):
    owner = identifier(owner)
    with _lock:
        try:
            container = client().containers.get(runtime_name(owner))
            owned(container, owner)
            container.stop(timeout=5)
        except NotFound:
            pass
    return {'status': 'stopped', 'owner_id': owner}


@app.post('/drain', dependencies=[Depends(authorized)])
def drain():
    with _lock:
        containers = client().containers.list(filters={'label': f'{DEPLOY}={DEPLOYMENT}'})
        for container in containers:
            if LABEL in container.labels:
                container.stop(timeout=5)
    return {'stopped': len(containers)}


@app.post('/runtimes/{owner}/purge', dependencies=[Depends(authorized)])
def purge(owner: str):
    """Operator-only deletion; no public platform endpoint exposes this operation."""
    owner = identifier(owner)
    name, cli = runtime_name(owner), client()
    with _lock:
        try:
            container = cli.containers.get(name)
            owned(container, owner)
            if container.status == 'running':
                raise HTTPException(409, 'Stop runtime before explicit purge')
            container.remove()
        except NotFound:
            pass
        try:
            network = cli.networks.get(name + '-net')
            if network.attrs.get('Labels', {}).get(LABEL) != owner:
                raise HTTPException(409)
            for member in network.containers:
                network.disconnect(member)
            network.remove()
        except NotFound:
            pass
        try:
            volume = cli.volumes.get(name + '-data')
            if volume.attrs.get('Labels', {}).get(LABEL) != owner:
                raise HTTPException(409)
            volume.remove()
        except NotFound:
            pass
    return {'purged': owner}


@app.exception_handler(DockerException)
async def docker_error(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({'detail': 'Docker runtime operation failed; inspect manager logs.'}, status_code=503)
