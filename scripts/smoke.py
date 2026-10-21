"""Live Compose test. Creates only prefixed test users; removes their resources on exit."""
import asyncio
import json
import re
import time
import uuid

import docker
import httpx
import pyotp
import websockets
from sqlalchemy import delete, select

from service.db import ActionToken, Attempt, Audit, Identity, LocalCredential, LoginSession, ModelCall, ModelPreference, NativeFlow, Space, Usage, User, factory
from service.security import hasher, token
from service.settings import config

created = []
password = token()
prefix = 'smoke' + uuid.uuid4().hex[:8]
client = httpx.Client(base_url='http://platform:8000', timeout=90, trust_env=False,
                      headers={'Host': 'localhost:8088', 'Origin': config()['public_url']})


def expect(response, code=200):
    if response.status_code != code:
        raise AssertionError(f'{response.request.method} {response.request.url.path}: {response.status_code} {response.text[:400]}')
    return response.json()


def login(username, otp=''):
    return expect(client.post('/auth/password-login', json={'username': username, 'password': password, 'otp': otp}))


def headers(p):
    return {'Authorization': 'Bearer ' + p['access_token']}


def signup(index):
    name = prefix + str(index)
    expect(client.post('/platform/api/register', json={'username': name, 'email': name + '@test.invalid', 'password': password}), 201)
    with factory()() as db:
        user = db.scalar(select(User).where(User.username == name))
        created.append(user.id)
    mail = httpx.Client(base_url='http://mail:8025', trust_env=False)
    messages = mail.get('/api/v1/messages').json()['messages']
    message = next(m for m in messages if any(t['Address'] == name + '@test.invalid' for t in m['To']))
    text = mail.get('/api/v1/message/' + message['ID']).json()['Text']
    action = re.search(r'#token=([A-Za-z0-9_-]+)', text).group(1)
    expect(client.post('/platform/api/email/verify', json={'token': action}))
    mail.close()
    return user


async def check_ws(p):
    ticket = expect(client.post('/api/auth/ws-ticket', headers=headers(p)))['ticket']
    url = 'ws://platform:8000/api/ws?ticket=' + ticket
    async with websockets.connect(url, origin=config()['public_url'], proxy=None) as socket:
        ready = json.loads(await asyncio.wait_for(socket.recv(), 20))
        assert ready, 'missing gateway greeting'
        await socket.send(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'session.list', 'params': {}}))
        for _ in range(20):
            message = json.loads(await asyncio.wait_for(socket.recv(), 20))
            if message.get('id') == 1:
                assert 'result' in message, message
                break
        else:
            raise AssertionError('session.list response missing')
        await socket.send(json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'model.options', 'params': {'include_unconfigured': True}}))
        for _ in range(20):
            message = json.loads(await asyncio.wait_for(socket.recv(), 20))
            if message.get('id') == 2:
                central = expect(client.get('/platform/api/models', headers=headers(p)))['models']
                shown = [m for provider in message['result']['providers'] for m in provider['models']]
                assert shown == central, 'RPC picker bypassed central policy'
                break
        else:
            raise AssertionError('model.options response missing')
        pty_ticket = expect(client.post('/api/auth/ws-ticket', headers=headers(p)))['ticket']
        async with websockets.connect('ws://platform:8000/api/pty?ticket=' + pty_ticket,
                                      origin=config()['public_url'], proxy=None) as pty:
            output = bytearray()
            deadline = asyncio.get_running_loop().time() + 30
            while asyncio.get_running_loop().time() < deadline:
                frame = await asyncio.wait_for(pty.recv(), 30)
                output.extend(frame if isinstance(frame, bytes) else frame.encode())
                if b'hermes' in output.lower():
                    break
            assert b'hermes' in output.lower(), 'PTY did not render Hermes'
            assert b'Traceback' not in output, 'PTY startup traceback'
        expect(client.post('/auth/logout', headers=headers(p)))
        try:
            while True:
                await asyncio.wait_for(socket.recv(), 8)
        except websockets.ConnectionClosed:
            pass


def cleanup():
    # Cleanup is restricted to UUIDs created by this invocation, with Docker ownership labels checked.
    with factory()() as db:
        for owner in created:
            user = db.get(User, owner)
            if not user or not user.username.startswith(prefix):
                raise RuntimeError('Refusing cleanup outside smoke fixture')
            user.status = 'suspended'
        db.commit()
        for model in (ModelPreference, NativeFlow, ActionToken, LoginSession, Identity, LocalCredential, Space, ModelCall, Usage):
            db.execute(delete(model).where(model.user_id.in_(created)))
        db.execute(delete(User).where(User.id.in_(created)))
        db.commit()


def main():
    otp = pyotp.random_base32()
    with factory()() as db:
        admin = User(username=prefix + 'admin', email=prefix + 'admin@test.invalid', status='active',
                     email_verified=True, admin=True, totp_secret=otp)
        db.add(admin)
        db.flush()
        created.append(admin.id)
        db.add(LocalCredential(user_id=admin.id, password_hash=hasher.hash(password)))
        db.add(Space(user_id=admin.id))
        db.commit()
    try:
        a, b = signup(1), signup(2)
        pa = login(a.username)
        expect(client.post('/platform/api/runtime/start', headers=headers(pa)), 403)
        admin_session = login(admin.username, pyotp.TOTP(otp).now())
        for user in (a, b):
            expect(client.post('/platform/api/admin/users/' + user.id, headers=headers(admin_session), json={'action': 'approve'}))
        expect(client.post('/platform/api/admin/users/' + a.id, headers=headers(admin_session), json={'action': 'approve'}))
        pa, pb = login(a.username), login(b.username)
        result = expect(client.post('/platform/api/runtime/start', headers=headers(pa)))
        assert result['owner_id'] == a.id
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            response = client.get('/chat', headers=headers(pa))
            if response.status_code == 200 and '<html' in response.text.lower():
                break
            time.sleep(2)
        else:
            raise AssertionError('Hermes dashboard did not become ready: ' + response.text[:200])
        runtime = docker.from_env().containers.get('hc-user-' + a.id)
        runtime.reload()
        assert runtime.attrs['HostConfig']['ReadonlyRootfs'] is True
        assert runtime.attrs['HostConfig']['Privileged'] is False
        assert runtime.attrs['Config']['User'] == '10000:10000'
        assert not any(m['Destination'] == '/var/run/docker.sock' for m in runtime.attrs['Mounts'])
        probe = runtime.exec_run(['/opt/hermes/.venv/bin/python', '-c',
            "import sys,os,pathlib; sys.path.insert(0,'/opt/hermes'); import hermes_bootstrap; from hermes_cli.runtime_provider import resolve_runtime_provider; r=resolve_runtime_provider(); assert r['base_url']=='http://control:8000/v1','wrong model endpoint'; assert r['api_key']==os.environ['RUNTIME_TOKEN'],'wrong runtime credential'; pathlib.Path('/opt/data/workspace/smoke-marker').write_text('durable')"])
        assert probe.exit_code == 0, probe.output.decode(errors='replace')[-500:]
        choices = expect(client.get('/platform/api/models', headers=headers(pa)))['models']
        if choices:
            chosen = choices[-1]
            selection = expect(client.post('/api/model/set', headers=headers(pa), json={'scope':'main','provider':'custom','model':chosen}))
            assert selection['runtime_applied'], selection
            probe = runtime.exec_run(['/opt/hermes/.venv/bin/python','-c',
                "from pathlib import Path; from ruamel.yaml import YAML; m=YAML(typ='safe').load(Path('/opt/data/config.yaml').read_text())['model']; assert m['default']==" + repr(chosen) + "; assert m['key_env']=='RUNTIME_TOKEN'"])
            assert probe.exit_code == 0, probe.output.decode(errors='replace')[-400:]
        expect(client.get('/api/profiles', headers=headers(pa)))
        expect(client.post('/api/profiles', headers=headers(pa), json={}), 403)
        probe = runtime.exec_run(['/opt/hermes/.venv/bin/python', '-c',
            "import sys,json; sys.path.insert(0,'/opt/hermes'); import hermes_bootstrap; from tools.browser_tool import browser_navigate; r=json.loads(browser_navigate('https://example.com',task_id='smoke-browser')); assert r.get('success'),str(r)[:400]"])
        assert probe.exit_code == 0, probe.output.decode(errors='replace')[-600:]
        expect(client.get('/chat', headers=headers(pb)), 409)
        # A different user's access token is rejected by A's runtime provider.
        from service.security import runtime_token
        wrong = client.post('/internal/runtime-auth', headers={'Authorization': 'Bearer ' + runtime_token(a.id)},
                            json={'owner_id': a.id, 'access_token': pb['access_token']})
        expect(wrong, 403)
        asyncio.run(check_ws(pa))
        pa = login(a.username)
        expect(client.post('/platform/api/runtime/stop', headers=headers(pa)))
        expect(client.post('/platform/api/runtime/start', headers=headers(pa)))
        runtime.reload()
        probe = runtime.exec_run(['/opt/hermes/.venv/bin/python', '-c',
                                  "from pathlib import Path; assert Path('/opt/data/workspace/smoke-marker').read_text()=='durable'"])
        assert probe.exit_code == 0, 'Workspace did not survive restart'
        expect(client.post('/platform/api/admin/users/' + a.id, headers=headers(admin_session),
                           json={'action': 'suspend', 'reason': 'Smoke test revocation'}))
        expect(client.get('/platform/api/me', headers=headers(pa)), 401)
        print('PASS: SMTP verification, approval, real Hermes GUI, runtime model resolution/scoped key, sandbox attributes, tenant denial, JSON-RPC WS/PTY, logout closes WS, persistent restart, suspension.')
    finally:
        from service.operations import manager
        for owner in created:
            asyncio.run(manager(owner, 'stop', 'POST'))
            asyncio.run(manager(owner, 'purge', 'POST'))
        cleanup()
        print('Only this invocation\'s fixture users, runtimes and volumes removed.')


if __name__ == '__main__':
    main()
