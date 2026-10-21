"""Native SDK inference adapters. Hermes retains the agent and tool execution loop."""
import json
import time
import uuid

import anthropic
import httpx
import openai
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy import select

from service.db import ProviderOAuth, User, factory
from service.provider_oauth import access_token


def text_content(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(p, dict) and p.get('type') == 'text' for p in value):
        return '\n'.join(p.get('text', '') for p in value)
    raise HTTPException(422, 'OAuth SDK 경로는 텍스트와 함수 도구 호출을 지원합니다. 이미지·오디오는 지원하지 않습니다.')


def responses_input(body):
    items = []
    for message in body.get('messages', []):
        role = message.get('role')
        if role == 'tool':
            items.append({'type': 'function_call_output', 'call_id': message['tool_call_id'], 'output': text_content(message.get('content'))})
            continue
        content = text_content(message.get('content'))
        if content:
            items.append({'role': role, 'content': content})
        for call in message.get('tool_calls', []):
            items.append({'type': 'function_call', 'call_id': call['id'], 'name': call['function']['name'], 'arguments': call['function']['arguments']})
    tools = []
    for tool in body.get('tools', []):
        if tool.get('type') != 'function':
            raise HTTPException(422, 'Only function tools are supported')
        tools.append({'type': 'function', **tool['function']})
    return items, tools


def messages_input(body):
    system, messages, tools = [], [], []
    for message in body.get('messages', []):
        role = message.get('role')
        content = text_content(message.get('content'))
        if role in ('system', 'developer'):
            system.append(content)
            continue
        blocks = []
        if role == 'tool':
            role = 'user'
            blocks.append({'type': 'tool_result', 'tool_use_id': message['tool_call_id'], 'content': content})
        else:
            if content:
                blocks.append({'type': 'text', 'text': content})
            for call in message.get('tool_calls', []):
                blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': json.loads(call['function']['arguments'])})
        if not blocks:
            continue
        if messages and messages[-1]['role'] == role:
            messages[-1]['content'].extend(blocks)
        else:
            messages.append({'role': role, 'content': blocks})
    for tool in body.get('tools', []):
        if tool.get('type') != 'function':
            raise HTTPException(422, 'Only function tools are supported')
        fn = tool['function']
        tools.append({'name': fn['name'], 'description': fn.get('description', ''), 'input_schema': fn.get('parameters', {'type': 'object'})})
    return '\n'.join(system), messages, tools


def validate(body, provider):
    try:
        if not isinstance(body.get('messages'), list) or not body['messages']:
            raise ValueError('messages required')
        if body.get('stop') or (provider == 'anthropic' and body.get('reasoning_effort')):
            raise HTTPException(422, '이 OAuth SDK 경로는 stop 또는 해당 reasoning 설정을 지원하지 않습니다.')
        (responses_input if provider == 'openai' else messages_input)(body)
        if body.get('response_format', {}).get('type', 'text') != 'text':
            raise HTTPException(422, 'OAuth SDK 경로의 구조화 출력은 지원하지 않습니다.')
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise HTTPException(422, '메시지·도구 호출 형식을 확인하세요.') from exc


async def events(provider, body, access, workspace):
    if provider == 'openai':
        items, tools = responses_input(body)
        transport = openai.DefaultAsyncHttpxClient(timeout=300, trust_env=False)
        client = openai.AsyncOpenAI(api_key=access, base_url='https://api.openai.com/v1', http_client=transport, max_retries=0)
        try:
            options = {'model': body['model'], 'input': items, 'store': False, 'stream': True,
                       'max_output_tokens': body['max_completion_tokens']}
            if 'temperature' in body:
                options['temperature'] = body['temperature']
            if 'top_p' in body:
                options['top_p'] = body['top_p']
            if tools:
                options['tools'] = tools
            if 'tool_choice' in body:
                choice = body['tool_choice']
                options['tool_choice'] = {'type': 'function', 'name': choice['function']['name']} if isinstance(choice, dict) else choice
            if 'parallel_tool_calls' in body:
                options['parallel_tool_calls'] = body['parallel_tool_calls']
            if body.get('reasoning_effort'):
                options['reasoning'] = {'effort': body['reasoning_effort']}
            stream = await client.responses.create(**options)
            indices = {}
            async with stream:
                async for event in stream:
                    data = event.model_dump()
                    kind = data['type']
                    if kind == 'response.output_text.delta':
                        yield {'delta': {'content': data['delta']}}
                    elif kind == 'response.output_item.added' and data['item'].get('type') == 'function_call':
                        item = data['item']
                        index = len(indices)
                        indices[item['id']] = index
                        yield {'delta': {'tool_calls': [{'index': index, 'id': item['call_id'], 'type': 'function',
                                                        'function': {'name': item['name'], 'arguments': item.get('arguments', '')}}]}}
                    elif kind == 'response.function_call_arguments.delta':
                        yield {'delta': {'tool_calls': [{'index': indices[data['item_id']], 'function': {'arguments': data['delta']}}]}}
                    elif kind in ('response.completed', 'response.incomplete'):
                        response = data['response']
                        usage = response.get('usage') or {}
                        yield {'finish': 'length' if kind == 'response.incomplete' else ('tool_calls' if indices else 'stop'),
                               'usage': {'prompt_tokens': usage.get('input_tokens', 0), 'completion_tokens': usage.get('output_tokens', 0),
                                         'total_tokens': usage.get('total_tokens')} if usage else None}
                    elif kind in ('response.failed', 'error'):
                        raise RuntimeError('Provider inference failed')
        finally:
            await client.close()
    else:
        system, messages, tools = messages_input(body)
        headers = {'anthropic-workspace-id': workspace} if workspace else {}
        transport = anthropic.DefaultAsyncHttpxClient(timeout=300, trust_env=False)
        client = anthropic.AsyncAnthropic(api_key=None, auth_token=access, http_client=transport, max_retries=0, default_headers=headers)
        try:
            options = {'model': body['model'], 'max_tokens': body['max_completion_tokens'], 'messages': messages}
            if system:
                options['system'] = system
            if 'temperature' in body:
                options['temperature'] = body['temperature']
            if 'top_p' in body:
                options['top_p'] = body['top_p']
            if tools:
                options['tools'] = tools
            if 'tool_choice' in body:
                choice = body['tool_choice']
                options['tool_choice'] = {'type': 'tool', 'name': choice['function']['name']} if isinstance(choice, dict) else {'type': {'required': 'any'}.get(choice, choice)}
            input_tokens, output_tokens, tool_indices = 0, 0, {}
            async with client.messages.stream(**options) as stream:
                async for event in stream:
                    data = event.model_dump()
                    if data['type'] == 'message_start':
                        usage = data['message']['usage']
                        input_tokens = sum(usage.get(k, 0) or 0 for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
                    elif data['type'] == 'content_block_start' and data['content_block']['type'] == 'tool_use':
                        block = data['content_block']
                        index = len(tool_indices)
                        tool_indices[data['index']] = index
                        yield {'delta': {'tool_calls': [{'index': index, 'id': block['id'], 'type': 'function', 'function': {'name': block['name'], 'arguments': ''}}]}}
                    elif data['type'] == 'content_block_delta':
                        delta = data['delta']
                        if delta['type'] == 'text_delta':
                            yield {'delta': {'content': delta['text']}}
                        elif delta['type'] == 'input_json_delta':
                            yield {'delta': {'tool_calls': [{'index': tool_indices[data['index']], 'function': {'arguments': delta['partial_json']}}]}}
                    elif data['type'] == 'message_delta':
                        output_tokens = data['usage']['output_tokens']
                        yield {'finish': {'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(data['delta'].get('stop_reason'), 'stop'),
                               'usage': {'prompt_tokens': input_tokens, 'completion_tokens': output_tokens, 'total_tokens': input_tokens + output_tokens}}
        finally:
            await client.close()


async def completion(provider, body, owner, call_id, generation, workspace):
    from service.models import settle
    source = None
    try:
        access = await access_token(provider, generation)
        source = events(provider, body, access, workspace)
        first = await anext(source)
    except Exception as exc:
        if source:
            await source.aclose()
        settle(call_id, failed=True)
        if getattr(exc, 'status_code', 0) in (401, 403):
            with factory()() as db:
                grant = db.scalar(select(ProviderOAuth).where(ProviderOAuth.provider == provider).with_for_update())
                if grant and grant.generation == generation:
                    grant.status = 'reauth_required'
                    db.commit()
        raise HTTPException(502, 'OAuth 모델 연결에 실패했습니다. 관리자 연결 상태를 확인하세요.') from exc
    response_id, created = 'chatcmpl-' + uuid.uuid4().hex, int(time.time())

    async def chunks():
        actual, success = None, False
        try:
            event = first
            while True:
                with factory()() as db:
                    user, connection = db.get(User, owner), db.get(ProviderOAuth, provider)
                    if (not user or user.status != 'active' or not user.email_verified or not connection
                            or not connection.selected or connection.status != 'connected' or connection.generation != generation):
                        raise RuntimeError('Access revoked')
                usage = event.get('usage')
                if usage and usage.get('total_tokens') is not None:
                    actual = int(usage['total_tokens'])
                data = {'id': response_id, 'object': 'chat.completion.chunk', 'created': created, 'model': body['model'],
                        'choices': [{'index': 0, 'delta': event.get('delta', {}), 'finish_reason': event.get('finish')}], 'usage': usage}
                yield data
                if 'finish' in event:
                    success = True
                try:
                    event = await anext(source)
                except StopAsyncIteration:
                    break
            if not success:
                raise RuntimeError('Provider response was interrupted')
        finally:
            await source.aclose()
            settle(call_id, actual, not success)

    if body.get('stream'):
        async def wire():
            try:
                async for chunk in chunks():
                    yield ('data: ' + json.dumps(chunk) + '\n\n').encode()
                yield b'data: [DONE]\n\n'
            except Exception:
                yield b'data: {"error":{"message":"OAuth model stream interrupted"}}\n\n'
        async def cleanup():
            await source.aclose()
            settle(call_id, failed=True)
        return StreamingResponse(wire(), media_type='text/event-stream', background=BackgroundTask(cleanup))
    content, tools, usage, finish = '', {}, None, 'stop'
    async for chunk in chunks():
        delta = chunk['choices'][0]['delta']
        content += delta.get('content', '')
        for call in delta.get('tool_calls', []):
            index = call['index']
            if index not in tools:
                tools[index] = {'id': call.get('id'), 'type': 'function', 'function': {'name': call.get('function', {}).get('name', ''), 'arguments': ''}}
            tools[index]['function']['arguments'] += call.get('function', {}).get('arguments', '')
        if chunk.get('usage'):
            usage = chunk['usage']
        finish = chunk['choices'][0].get('finish_reason') or finish
    message = {'role': 'assistant', 'content': content or None}
    if tools:
        message['tool_calls'] = list(tools.values())
    return JSONResponse({'id': response_id, 'object': 'chat.completion', 'model': body['model'], 'created': created,
                         'choices': [{'index': 0, 'message': message, 'finish_reason': finish}], 'usage': usage})
