"""Real PostgreSQL and ASGI requests, in a disposable database; no live model calls."""
import datetime as dt
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import make_url

from service import accounts
from service.db import Base, Identity, LocalCredential, LoginSession, Space, Usage, User, ModelProvider, ModelPolicy, ModelPreference, factory
from service.security import challenge, digest, hasher, password_ok, runtime_token, token
from service.settings import config, database_url


class Contracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = 'hc_test_' + uuid.uuid4().hex
        cls.admin_engine = create_engine(database_url(), isolation_level='AUTOCOMMIT')
        with cls.admin_engine.connect() as db:
            db.execute(text('CREATE DATABASE ' + cls.name))
        cls.url_patch = patch('service.db.database_url', return_value=make_url(database_url()).set(database=cls.name).render_as_string(hide_password=False))
        cls.url_patch.start()
        factory.cache_clear()
        Base.metadata.create_all(factory().kw['bind'])
        from service.app import app
        cls.app = app

    @classmethod
    def tearDownClass(cls):
        factory().kw['bind'].dispose()
        factory.cache_clear()
        cls.url_patch.stop()
        with cls.admin_engine.connect() as db:
            db.execute(text('DROP DATABASE ' + cls.name + ' WITH (FORCE)'))
        cls.admin_engine.dispose()

    def setUp(self):
        self.client = TestClient(self.app)
        self.client.headers['Origin'] = config()['public_url']

    def tearDown(self):
        self.client.close()
        with factory()() as db:
            for table in (ModelPreference, ModelPolicy, ModelProvider):
                db.execute(delete(table))
            db.commit()

    def user(self, status='active', verified=True):
        name = 'u' + uuid.uuid4().hex[:14]
        password = token()
        with factory()() as db:
            user = User(username=name, email=name + '@test.invalid', status=status, email_verified=verified)
            db.add(user)
            db.flush()
            db.add(LocalCredential(user_id=user.id, password_hash=hasher.hash(password)))
            db.add(Identity(id='local:' + name, user_id=user.id, subject=name))
            db.add(Space(user_id=user.id))
            db.commit()
            result = accounts.issue(db, user, 'test')
        return user, password, accounts.payload(result)

    def test_wrong_password_and_unknown_user_rejected(self):
        u, _, _ = self.user()
        for name in (u.username, 'unknown'):
            result = self.client.post('/auth/password-login', json={'username': name, 'password': 'wrong'})
            self.assertEqual(result.status_code, 401)
        self.assertFalse(password_ok('wrong'))

    def test_admin_otp_policy(self):
        import pyotp
        u, password, _ = self.user()
        secret = pyotp.random_base32()
        with factory()() as db:
            admin = db.get(User, u.id)
            admin.admin, admin.totp_secret = True, secret
            db.commit()
        policy = dict(config())
        for required in (False, True):
            policy['admin_otp_required'] = required
            with patch('service.accounts.config', return_value=policy):
                response = self.client.post('/auth/password-login', json={'username': u.username, 'password': password})
                self.assertEqual(response.status_code, 401 if required else 200)
        with patch('service.accounts.config', return_value=policy):
            response = self.client.post('/auth/password-login', json={'username': u.username, 'password': password, 'otp': pyotp.TOTP(secret).now()})
            self.assertEqual(response.status_code, 200)
        del policy['admin_otp_required']
        with patch('service.accounts.config', return_value=policy):
            response = self.client.post('/auth/password-login', json={'username': u.username, 'password': password})
            self.assertEqual(response.status_code, 401)

    def test_admin_can_approve_before_email_verification(self):
        admin, _, credentials = self.user()
        target, _, target_credentials = self.user(status='pending', verified=False)
        with factory()() as db:
            db.get(User, admin.id).admin = True
            db.commit()
        headers = {'Authorization': 'Bearer ' + credentials['access_token']}
        for action in ('approve', 'approve', 'reactivate'):
            response = self.client.post('/platform/api/admin/users/' + target.id,
                                        json={'action': action}, headers=headers)
            self.assertEqual(response.status_code, 200)
        with factory()() as db:
            stored = db.get(User, target.id)
            self.assertEqual(stored.status, 'active')
            self.assertFalse(stored.email_verified)
        response = self.client.post('/platform/api/runtime/start',
                                    headers={'Authorization': 'Bearer ' + target_credentials['access_token']})
        self.assertEqual(response.status_code, 403)

    def test_pending_can_view_status_but_cannot_start(self):
        _, _, p = self.user(status='pending')
        headers = {'Authorization': 'Bearer ' + p['access_token']}
        self.assertEqual(self.client.get('/platform/api/me', headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/platform/api/runtime/start', headers=headers).status_code, 403)

    def test_email_unverified_cannot_start(self):
        _, _, p = self.user(verified=False)
        self.assertEqual(self.client.post('/platform/api/runtime/start', headers={'Authorization': 'Bearer ' + p['access_token']}).status_code, 403)

    def test_user_cannot_administer_accounts(self):
        _, _, p = self.user()
        self.assertEqual(self.client.get('/platform/api/admin/users', headers={'Authorization': 'Bearer ' + p['access_token']}).status_code, 403)

    def test_logout_revokes_bearer(self):
        _, _, p = self.user()
        headers = {'Authorization': 'Bearer ' + p['access_token']}
        self.assertEqual(self.client.post('/auth/logout', headers=headers).status_code, 200)
        self.assertEqual(self.client.get('/platform/api/me', headers=headers).status_code, 401)

    def test_refresh_rotates_both_tokens(self):
        _, _, p = self.user()
        r = self.client.post('/auth/native/refresh', json={'refresh_token': p['refresh_token']})
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r.json()['access_token'], p['access_token'])
        self.assertEqual(self.client.post('/auth/native/refresh', json={'refresh_token': p['refresh_token']}).status_code, 401)
        self.assertEqual(self.client.get('/platform/api/me', headers={'Authorization': 'Bearer ' + p['access_token']}).status_code, 401)

    def test_cookie_mutations_need_csrf(self):
        _, _, p = self.user()
        self.client.cookies.set(accounts.AT_COOKIE, p['access_token'])
        self.assertEqual(self.client.post('/auth/logout').status_code, 403)
        self.assertEqual(self.client.post('/auth/logout', headers={'X-CSRF-Token': p['csrf_token']}).status_code, 200)

    def test_foreign_origin_denied(self):
        self.assertEqual(self.client.post('/auth/password-login', json={'username': 'x', 'password': 'x'},
                                         headers={'Origin': 'https://evil.invalid'}).status_code, 403)

    def test_desktop_json_cookie_transport_can_mint_ticket_without_origin(self):
        _, _, p = self.user()
        self.client.cookies.set(accounts.AT_COOKIE, p['access_token'])
        del self.client.headers['Origin']
        r = self.client.post('/api/auth/ws-ticket', json={})
        self.assertEqual(r.status_code, 200)
        self.assertIn('ticket', r.json())
        r = self.client.post('/auth/logout', json={})
        self.assertEqual(r.status_code, 200)

    def test_runtime_credential_cannot_validate_another_user(self):
        a, _, pa = self.user()
        b, _, pb = self.user()
        headers = {'Authorization': 'Bearer ' + runtime_token(a.id)}
        r = self.client.post('/internal/runtime-auth', headers=headers, json={'owner_id': a.id, 'access_token': pb['access_token']})
        self.assertEqual(r.status_code, 403)
        r = self.client.post('/internal/runtime-auth', headers=headers, json={'owner_id': a.id, 'access_token': pa['access_token']})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['user_id'], a.id)

    def test_suspended_account_loses_existing_session(self):
        u, _, p = self.user()
        with factory()() as db:
            db.get(User, u.id).status = 'suspended'
            db.commit()
        self.assertEqual(self.client.get('/platform/api/me', headers={'Authorization': 'Bearer ' + p['access_token']}).status_code, 403)
        self.assertEqual(self.client.post('/auth/native/refresh', json={'refresh_token': p['refresh_token']}).status_code, 403)

    def test_action_tokens_are_single_use(self):
        from service.db import ActionToken, now
        u, _, _ = self.user(status='pending', verified=False)
        raw = token()
        with factory()() as db:
            db.add(ActionToken(digest=digest(raw), user_id=u.id, kind='verify', expires_at=now() + dt.timedelta(minutes=5)))
            db.commit()
        self.assertEqual(self.client.post('/platform/api/email/verify', json={'token': raw}).status_code, 200)
        self.assertEqual(self.client.post('/platform/api/email/verify', json={'token': raw}).status_code, 400)

    def test_native_pkce_single_use_and_wrong_verifier(self):
        u, password, _ = self.user()
        verifier = token()
        redirect = 'http://127.0.0.1:43119/callback'
        r = self.client.get('/auth/native/authorize', params={'code_challenge': challenge(verifier),
            'code_challenge_method': 'S256', 'redirect_uri': redirect, 'state': token()}, follow_redirects=False)
        self.assertEqual(r.status_code, 307)
        broker = r.headers['location'].split('broker_state=')[1]
        r = self.client.post('/auth/password-login', json={'username': u.username, 'password': password, 'broker_state': broker})
        self.assertEqual(r.status_code, 200)
        from urllib.parse import parse_qs, urlsplit
        code = parse_qs(urlsplit(r.json()['redirect_url']).query)['code'][0]
        self.assertEqual(self.client.post('/auth/native/token', json={'code': code, 'code_verifier': token()}).status_code, 400)
        self.assertEqual(self.client.post('/auth/native/token', json={'code': code, 'code_verifier': verifier}).status_code, 200)
        self.assertEqual(self.client.post('/auth/native/token', json={'code': code, 'code_verifier': verifier}).status_code, 400)

    def test_native_redirect_rejects_non_loopback(self):
        r = self.client.get('/auth/native/authorize', params={'code_challenge': challenge(token()),
             'code_challenge_method': 'S256', 'redirect_uri': 'http://evil.invalid/callback', 'state': token()})
        self.assertEqual(r.status_code, 400)

    def test_budget_and_parallel_limit_are_transactional(self):
        from service.models import reserve, settle
        from fastapi import HTTPException
        u, _, _ = self.user()
        ids = []
        try:
            for _ in range(config()['max_model_parallel']):
                with factory()() as db:
                    ids.append(reserve(db, u.id, 100))
            with factory()() as db:
                with self.assertRaises(HTTPException):
                    reserve(db, u.id, 100)
        finally:
            for call in ids:
                settle(call)
        with factory()() as db:
            usage = db.scalar(select(Usage).where(Usage.user_id == u.id))
            self.assertEqual(usage.inflight, 0)
            self.assertEqual(usage.tokens, len(ids) * 100)

    def test_anonymous_websocket_denied(self):
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect('/api/ws?ticket=bad'):
                pass

    def test_parallel_reservations_cannot_bypass_limit(self):
        import concurrent.futures
        import threading
        from fastapi import HTTPException
        from service.models import reserve, settle
        user, _, _ = self.user()
        barrier = threading.Barrier(6)

        def attempt(_):
            barrier.wait(timeout=10)
            with factory()() as db:
                try:
                    return reserve(db, user.id, 100)
                except HTTPException as exc:
                    self.assertEqual(exc.status_code, 429)
                    return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            calls = [x for x in pool.map(attempt, range(6)) if x]
        self.assertEqual(len(calls), config()['max_model_parallel'])
        for call in calls:
            settle(call)

    def provider_admin(self):
        user, _, credentials = self.user()
        with factory()() as db:
            db.get(User, user.id).admin = True
            db.commit()
        return {'Authorization': 'Bearer ' + credentials['access_token']}

    def test_provider_admin_permissions_and_encrypted_key(self):
        _, _, credentials = self.user()
        denied = self.client.get('/platform/api/admin/model-providers', headers={'Authorization': 'Bearer ' + credentials['access_token']})
        self.assertEqual(denied.status_code, 403)
        headers = self.provider_admin()
        body = {'enabled': True, 'models': ['managed-gpt'], 'default_model': 'managed-gpt', 'api_key': 'sk-test-secret'}
        self.assertEqual(self.client.put('/platform/api/admin/model-providers/openai', json=body,
                                        headers={'Authorization': 'Bearer ' + credentials['access_token']}).status_code, 403)
        response = self.client.put('/platform/api/admin/model-providers/openai', json=body, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('sk-test-secret', response.text)
        listing = self.client.get('/platform/api/admin/model-providers', headers=headers)
        self.assertNotIn('sk-test-secret', listing.text)
        import httpx
        from service import providers
        real_client = httpx.AsyncClient
        def transport(request):
            self.assertEqual(str(request.url), 'https://api.openai.com/v1/models')
            self.assertEqual(request.headers['Authorization'], 'Bearer sk-test-secret')
            return httpx.Response(200, json={'data': [{'id': 'managed-gpt'}]})
        with patch.object(providers.httpx, 'AsyncClient', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(transport), **kw)):
            tested = self.client.post('/platform/api/admin/model-providers/openai/test', json={}, headers=headers)
        self.assertEqual(tested.status_code, 200)
        self.assertEqual(tested.json()['models'], ['managed-gpt'])
        from service.providers import vault
        with factory()() as db:
            row = db.get(ModelProvider, 'openai')
            self.assertNotIn('sk-test-secret', row.key_cipher)
            self.assertEqual(vault().decrypt(row.key_cipher.encode()).decode(), 'sk-test-secret')
        body['api_key'] = None
        self.assertEqual(self.client.put('/platform/api/admin/model-providers/openai', json=body, headers=headers).status_code, 200)
        with factory()() as db:
            self.assertTrue(db.get(ModelProvider, 'openai').key_cipher)

    def test_model_allowlist_selection_and_disable(self):
        headers = self.provider_admin()
        with patch('service.providers.secret', return_value=''):
            self.assertEqual(self.client.get('/platform/api/models', headers=headers).json()['models'], [])
        body = {'enabled': True, 'models': ['managed-gpt'], 'default_model': 'managed-gpt', 'api_key': 'sk-test'}
        self.assertEqual(self.client.put('/platform/api/admin/model-providers/openai', json=body, headers=headers).status_code, 200)
        user, _, credentials = self.user()
        user_headers = {'Authorization': 'Bearer ' + credentials['access_token']}
        runtime_headers = {'Authorization': 'Bearer ' + runtime_token(user.id)}
        listing = self.client.get('/v1/models', headers=runtime_headers)
        self.assertEqual([m['id'] for m in listing.json()['data']], ['managed-gpt'])
        self.assertEqual(self.client.put('/platform/api/models/selection', json={'model': 'forbidden'}, headers=user_headers).status_code, 403)
        self.assertEqual(self.client.put('/platform/api/models/selection', json={'model': 'managed-gpt'}, headers=user_headers).status_code, 200)
        self.assertEqual(self.client.post('/v1/chat/completions', json={'model': 'forbidden'}, headers=runtime_headers).status_code, 403)
        body['enabled'] = False
        self.assertEqual(self.client.put('/platform/api/admin/model-providers/openai', json=body, headers=headers).status_code, 200)
        self.assertEqual(self.client.get('/v1/models', headers=runtime_headers).json()['data'], [])
        self.assertEqual(self.client.post('/v1/chat/completions', json={'model': 'managed-gpt'}, headers=runtime_headers).status_code, 403)
        self.assertEqual(self.client.post('/v1/chat/completions', json={'model': 'central-default'}, headers=runtime_headers).status_code, 403)

    def test_anthropic_routing_alias_tools_and_usage(self):
        import json
        import httpx
        from service import models
        headers = self.provider_admin()
        body = {'enabled': True, 'models': ['claude-test'], 'default_model': 'claude-test', 'api_key': 'sk-ant-test', 'workspace': 'workspace_test'}
        self.assertEqual(self.client.put('/platform/api/admin/model-providers/anthropic', json=body, headers=headers).status_code, 200)
        user, _, credentials = self.user()
        self.assertEqual(self.client.put('/platform/api/models/selection', json={'model': 'claude-test'}, headers={'Authorization': 'Bearer ' + credentials['access_token']}).status_code, 200)
        real_client = httpx.AsyncClient
        tool = {'type': 'function', 'function': {'name': 'echo', 'parameters': {'type': 'object'}}}
        def transport(request):
            self.assertEqual(str(request.url), 'https://api.anthropic.com/v1/chat/completions')
            self.assertEqual(request.headers['Authorization'], 'Bearer sk-ant-test')
            self.assertEqual(request.headers['anthropic-workspace-id'], 'workspace_test')
            payload = json.loads(request.content)
            self.assertEqual(payload['model'], 'claude-test')
            self.assertEqual(payload['tools'], [tool])
            return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'tool_calls': [{'id': 'call1', 'type': 'function', 'function': {'name': 'echo', 'arguments': '{}'}}]}}], 'usage': {'total_tokens': 9}})
        with patch.object(models.httpx, 'AsyncClient', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(transport), **kw)):
            response = self.client.post('/v1/chat/completions', headers={'Authorization': 'Bearer ' + runtime_token(user.id)},
                                        json={'model': 'central-default', 'messages': [{'role': 'user', 'content': 'hi'}], 'tools': [tool]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['choices'][0]['message']['tool_calls'][0]['function']['name'], 'echo')
        with factory()() as db:
            usage = db.scalar(select(Usage).where(Usage.user_id == user.id))
            self.assertEqual((usage.tokens, usage.inflight), (9, 0))

    def test_gui_models_use_only_configured_admin_allowlist(self):
        from service.db import ModelProvider
        from service.providers import vault
        user, _, credentials = self.user()
        headers = {'Authorization': 'Bearer ' + credentials['access_token']}
        with factory()() as db:
            db.add_all([ModelProvider(id='openai', enabled=True, models=['allowed-gpt'], workspace='', key_cipher=vault().encrypt(b'sk-test').decode()),
                        ModelProvider(id='anthropic', enabled=False, models=['hidden-claude'], workspace='', key_cipher=vault().encrypt(b'sk-ant-test').decode())])
            db.commit()
        for suffix in ('', '?include_unconfigured=true&refresh=true&profile=foreign'):
            response = self.client.get('/api/model/options'+suffix, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['providers'][0]['models'], ['allowed-gpt'])
            self.assertNotIn('hidden-claude', response.text)
        chosen = self.client.post('/api/model/set', headers=headers, json={'scope':'main','provider':'custom','model':'allowed-gpt'})
        self.assertEqual(chosen.status_code, 200)
        self.assertEqual(self.client.get('/api/model/auxiliary', headers=headers).json()['main']['model'], 'allowed-gpt')
        for model in ('hidden-claude', 'invented-model'):
            response = self.client.post('/api/model/set', headers=headers, json={'scope':'main','provider':'custom','model':model})
            self.assertEqual(response.status_code, 403)
        with factory()() as db:
            row = db.get(ModelProvider, 'openai')
            row.enabled = False
            db.commit()
        self.assertEqual(self.client.get('/api/model/options', headers=headers).json()['providers'], [])

    def test_invalid_provider_settings_do_not_change_policy(self):
        headers = self.provider_admin()
        body = {'enabled': True, 'models': ['bad/model'], 'default_model': 'bad/model', 'api_key': 'sk-test'}
        self.assertEqual(self.client.put('/platform/api/admin/model-providers/openai', json=body, headers=headers).status_code, 422)
        body.update(models=['valid'], default_model='not-allowed')
        self.assertEqual(self.client.put('/platform/api/admin/model-providers/openai', json=body, headers=headers).status_code, 422)
        body.update(default_model='valid', api_key='sk-ant-oat-subscription-token')
        self.assertEqual(self.client.put('/platform/api/admin/model-providers/anthropic', json=body, headers=headers).status_code, 422)
        with factory()() as db:
            self.assertIsNone(db.get(ModelProvider, 'anthropic'))

    def test_provider_stream_usage_settles_reservation(self):
        import httpx
        from service import models
        user, _, _ = self.user()
        real_client = httpx.AsyncClient
        payload = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: {"usage":{"total_tokens":12},"choices":[]}\n\ndata: [DONE]\n\n'
        def transport(request):
            self.assertEqual(request.headers['Authorization'], 'Bearer test-provider-key')
            return httpx.Response(200, text=payload, headers={'content-type': 'text/event-stream'})
        def provider_client(**kwargs):
            return real_client(transport=httpx.MockTransport(transport), **kwargs)
        with patch.object(models, 'secret', return_value='test-provider-key'), patch('service.providers.secret', return_value='test-provider-key'), patch.object(models.httpx, 'AsyncClient', side_effect=provider_client):
            result = self.client.post('/v1/chat/completions', headers={'Authorization': 'Bearer ' + runtime_token(user.id)},
                                      json={'model': config()['default_model'], 'messages': [{'role': 'user', 'content': 'hi'}], 'stream': True})
        self.assertEqual(result.status_code, 200)
        self.assertIn('[DONE]', result.text)
        with factory()() as db:
            usage = db.scalar(select(Usage).where(Usage.user_id == user.id))
            self.assertEqual((usage.tokens, usage.inflight), (12, 0))


if __name__ == '__main__':
    unittest.main()
