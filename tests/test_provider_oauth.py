import asyncio
import datetime as dt
import json
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
from sqlalchemy import delete, select

import test_contracts as base
from service import provider_oauth as oauth
from service.db import ModelProvider, ModelCall, ProviderCall, ProviderOAuth, ProviderOAuthFlow, User, Usage, factory, now
from service.models import reserve, settle
from service.security import runtime_token


class OAuthContracts(unittest.TestCase):
    setUpClass = classmethod(base.Contracts.setUpClass.__func__)
    tearDownClass = classmethod(base.Contracts.tearDownClass.__func__)
    setUp = base.Contracts.setUp
    user = base.Contracts.user
    provider_admin = base.Contracts.provider_admin

    def tearDown(self):
        with factory()() as db:
            for table in (ProviderCall, ProviderOAuthFlow, ProviderOAuth):
                db.execute(delete(table))
            db.commit()
        base.Contracts.tearDown(self)

    def grant(self, provider='openai', expired=False):
        model = 'oauth-gpt' if provider == 'openai' else 'oauth-claude'
        with factory()() as db:
            db.add(ProviderOAuth(provider=provider, selected=True, status='connected', client_id='client-test', generation=1,
                                 available_models=[model], expires_at=now()+dt.timedelta(seconds=-5 if expired else 3600),
                                 token_cipher=oauth.encrypt({'access_token':'oauth-secret','refresh_token':'refresh-original','scope':'chatgpt.tokens.use.direct user:inference'})))
            db.add(ModelProvider(id=provider, enabled=True, models=[model, 'not-granted'], workspace=''))
            db.commit()
        return model

    def test_start_callback_bound_to_admin_session_and_one_use(self):
        admin = self.provider_admin()
        _, _, user_session = self.user()
        ordinary = {'Authorization':'Bearer '+user_session['access_token']}
        self.assertEqual(self.client.post('/platform/api/admin/model-providers/openai/oauth/start',headers=ordinary,json={}).status_code,403)
        result = self.client.post('/platform/api/admin/model-providers/openai/oauth/start',headers=admin,json={})
        self.assertEqual(result.status_code,200)
        query=parse_qs(urlsplit(result.json()['authorization_url']).query)
        self.assertEqual(query['code_challenge_method'],['S256'])
        self.assertEqual(query['client_id'],['dynamic_agent_client'])
        state=query['state'][0]
        self.assertEqual(self.client.get('/auth/callback?state=wrong&code=fake',follow_redirects=False).status_code,400)
        callback=self.client.get('/auth/callback',params={'state':state,'code':'test-code','client_id':'oaiapp_test'},follow_redirects=False)
        self.assertEqual(callback.status_code,303)
        self.assertNotIn('test-code',callback.headers['location'])
        self.assertEqual(self.client.get('/auth/callback',params={'state':state,'code':'test-code'},follow_redirects=False).status_code,400)
        second=self.provider_admin()
        path='/platform/api/admin/model-providers/openai/oauth/finish'
        self.assertEqual(self.client.post(path,json={'flow_id':result.json()['flow_id']},headers=second).status_code,409)
        payload={'access_token':'new-oauth-secret','refresh_token':'refresh-new','expires_in':3600,'scope':'chatgpt.tokens.use.direct','id_token':'test-id'}
        with patch.object(oauth,'exchange',new=AsyncMock(return_value=payload)), patch.object(oauth,'identity',new=AsyncMock(return_value='subject-test')), patch.object(oauth,'catalog',new=AsyncMock(return_value=['oauth-gpt'])):
            finished=self.client.post(path,json={'flow_id':result.json()['flow_id']},headers=admin)
        self.assertEqual(finished.status_code,200)
        self.assertNotIn('new-oauth-secret',finished.text)
        self.assertEqual(self.client.post(path,json={'flow_id':result.json()['flow_id']},headers=admin).status_code,409)
        with factory()() as db:
            grant=db.get(ProviderOAuth,'openai')
            self.assertNotIn('new-oauth-secret',grant.token_cipher)
            self.assertEqual(oauth.decrypt(grant.token_cipher)['access_token'],'new-oauth-secret')

    def test_oidc_signature_nonce_audience_validation(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk['kid']='test-key'
        claims={'iss':'https://auth.openai.com','aud':'oaiapp_test','sub':'subject-test','nonce':'nonce-test',
                'iat':int(now().timestamp()),'exp':int(now().timestamp())+600}
        signed=jwt.encode(claims,key,algorithm='RS256',headers={'kid':'test-key'})
        real=httpx.AsyncClient
        def transport(request):
            return httpx.Response(200,json={'keys':[jwk]})
        with patch.object(oauth.httpx,'AsyncClient',side_effect=lambda **kw:real(transport=httpx.MockTransport(transport),**kw)):
            self.assertEqual(asyncio.run(oauth.identity({'id_token':signed},'oaiapp_test','nonce-test')),'subject-test')
            with self.assertRaises(Exception):
                asyncio.run(oauth.identity({'id_token':signed},'oaiapp_test','wrong-nonce'))
            with self.assertRaises(Exception):
                asyncio.run(oauth.identity({'id_token':signed},'other-client','nonce-test'))

    def test_shared_connection_limit_and_disconnect_hide_models(self):
        model=self.grant()
        first,_,_=self.user()
        second,_,_=self.user()
        with factory()() as db:
            call=reserve(db,first.id,100,'openai')
        with factory()() as db:
            with self.assertRaises(Exception):
                reserve(db,second.id,100,'openai')
        settle(call,9)
        headers=self.provider_admin()
        catalog=self.client.get('/platform/api/models',headers=headers).json()
        self.assertEqual(catalog['models'],[model])
        self.assertEqual(self.client.post('/platform/api/admin/model-providers/openai/oauth/disconnect',headers=headers,json={}).status_code,200)
        self.assertEqual(self.client.get('/platform/api/models',headers=headers).json()['models'],[])

    def test_refresh_is_rotated_once_across_concurrent_workers(self):
        import concurrent.futures
        self.grant(expired=True)
        payload={'access_token':'refreshed-secret','refresh_token':'refresh-rotated','expires_in':3600}
        with patch.object(oauth,'_exchange_sync',return_value=payload) as exchange:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                values=list(pool.map(lambda _:oauth._access_token('openai'),range(2)))
        self.assertEqual(values,['refreshed-secret','refreshed-secret'])
        self.assertEqual(exchange.call_count,1)
        with factory()() as db:
            self.assertEqual(oauth.decrypt(db.get(ProviderOAuth,'openai').token_cipher)['refresh_token'],'refresh-rotated')

    def test_refresh_failure_requires_reauth_and_no_api_key_fallback(self):
        from fastapi import HTTPException
        self.grant(expired=True)
        with factory()() as db:
            row=db.get(ModelProvider,'openai')
            row.key_cipher=oauth.encrypt('fallback-must-not-be-used')
            db.commit()
        with patch.object(oauth,'_exchange_sync',side_effect=HTTPException(502,'rejected')):
            with self.assertRaises(HTTPException):
                oauth._access_token('openai')
        with factory()() as db:
            self.assertEqual(db.get(ProviderOAuth,'openai').status,'reauth_required')
            from service.providers import allowed
            self.assertEqual(allowed(db),{})

    def test_changed_generation_and_unregistered_client_are_rejected(self):
        from fastapi import HTTPException
        self.grant()
        with self.assertRaises(HTTPException):
            oauth._access_token('openai',0)
        headers=self.provider_admin()
        self.assertEqual(self.client.post('/platform/api/admin/model-providers/anthropic/oauth/start',headers=headers,json={}).status_code,409)
        _,_,credentials=self.user()
        self.assertEqual(self.client.post('/platform/api/admin/model-providers/anthropic/oauth/start',headers={'Authorization':'Bearer '+credentials['access_token']},json={}).status_code,403)

    def test_responses_sdk_tool_stream_and_usage(self):
        from service import oauth_inference as inference
        model=self.grant()
        user,_,_=self.user()
        frames=[
            {'type':'response.output_item.added','output_index':0,'item':{'type':'function_call','id':'item1','call_id':'call1','name':'echo','arguments':''}},
            {'type':'response.function_call_arguments.delta','item_id':'item1','output_index':0,'delta':'{}'},
            {'type':'response.completed','response':{'id':'resp1','status':'completed','output':[],'usage':{'input_tokens':3,'output_tokens':4,'total_tokens':7}}},
        ]
        def transport(request):
            self.assertEqual(str(request.url),'https://api.openai.com/v1/responses')
            self.assertEqual(request.headers['authorization'],'Bearer oauth-secret')
            body=json.loads(request.content)
            self.assertFalse(body['store'])
            self.assertTrue(body['stream'])
            return httpx2.Response(200,text=''.join('data: '+json.dumps(f)+'\n\n' for f in frames),headers={'content-type':'text/event-stream'})
        with patch.object(inference.openai,'DefaultAsyncHttpxClient',side_effect=lambda **kw:httpx2.AsyncClient(transport=httpx2.MockTransport(transport),**kw)):
            response=self.client.post('/v1/chat/completions',headers={'Authorization':'Bearer '+runtime_token(user.id)},json={'model':model,'messages':[{'role':'user','content':'hi'}],'stream':True})
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('call1',response.text)
        self.assertIn('[DONE]',response.text)
        with factory()() as db:
            usage=db.scalar(select(Usage).where(Usage.user_id==user.id))
            self.assertEqual((usage.tokens,usage.inflight),(7,0))

    def test_anthropic_sdk_text_response_and_no_api_key_fallback(self):
        from service import oauth_inference as inference
        model=self.grant('anthropic')
        user,_,_=self.user()
        cfg=dict(oauth.config())
        cfg['provider_oauth']={**cfg['provider_oauth'],'anthropic':{**cfg['provider_oauth']['anthropic'],'client_id':'approved-client'}}
        frames=[
            {'type':'message_start','message':{'id':'msg1','type':'message','role':'assistant','content':[],'model':model,'usage':{'input_tokens':3,'output_tokens':0}}},
            {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
            {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'hello'}},
            {'type':'content_block_stop','index':0},
            {'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':4}},
            {'type':'message_stop'},
        ]
        def transport(request):
            self.assertEqual(request.headers['authorization'],'Bearer oauth-secret')
            self.assertNotIn('x-api-key',request.headers)
            return httpx2.Response(200,text=''.join('event: '+f['type']+'\ndata: '+json.dumps(f)+'\n\n' for f in frames),headers={'content-type':'text/event-stream'})
        with patch.object(oauth,'config',return_value=cfg),patch.object(inference.anthropic,'DefaultAsyncHttpxClient',side_effect=lambda **kw:httpx2.AsyncClient(transport=httpx2.MockTransport(transport),**kw)):
            response=self.client.post('/v1/chat/completions',headers={'Authorization':'Bearer '+runtime_token(user.id)},json={'model':model,'messages':[{'role':'user','content':'hi'}]})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['choices'][0]['message']['content'],'hello')
        self.assertEqual(response.json()['usage']['total_tokens'],7)
