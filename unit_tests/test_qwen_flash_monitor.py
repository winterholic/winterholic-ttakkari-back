from __future__ import annotations

import httpx
import pytest

from ttakkari.runs import qwen_flash_monitor as monitor


@pytest.mark.asyncio
async def test_reads_only_matching_flash_model_and_metrics():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == '/props':
            return httpx.Response(200, json={'model_alias': 'qwen-flash-next-q2k'})
        return httpx.Response(200, text='''# counters are current server lifetime
llamacpp:prompt_tokens_total 6732
llamacpp:prompt_tokens_cached_total 26609
llamacpp:tokens_predicted_total 1804
llamacpp:requests_processing 0
llamacpp:requests_deferred 1
''')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url='http://127.0.0.1:18987') as client:
        result = await monitor.read(client, expected_model='qwen-flash-next-q2k')
    assert result['server']['status'] == 'running'
    assert result['scope'] == 'server_lifetime'
    assert result['input_tokens'] == 6732
    assert result['cached_input_tokens'] == 26609
    assert result['output_tokens'] == 1804
    assert result['requests_processing'] == 0
    assert result['requests_deferred'] == 1
    assert result['metrics_status'] == 'available'
    assert [r.url.path for r in requests] == ['/props', '/metrics']
    assert all(r.method == 'GET' for r in requests)


@pytest.mark.asyncio
async def test_intake_model_is_never_reported_as_flash_usage():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={'model_alias': 'qwen3.5-4b-q4_k_m'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url='http://127.0.0.1:18987') as client:
        result = await monitor.read(client, expected_model='qwen-flash-next-q2k')
    assert result['server']['status'] == 'model_mismatch'
    assert result['input_tokens'] is None
    assert paths == ['/props']


@pytest.mark.asyncio
@pytest.mark.parametrize('metric', ['NaN', 'Inf', '-1', '1.5', 'unreadable'])
async def test_invalid_metrics_are_missing_instead_of_idle_zero(metric):
    def handle(request):
        if request.url.path == '/props':
            return httpx.Response(200, json={'model_alias': 'qwen-flash-next-q2k'})
        return httpx.Response(200, text=f'llamacpp:prompt_tokens_total {metric}\nllamacpp:tokens_predicted_total 0\n')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url='http://127.0.0.1:18987') as client:
        result = await monitor.read(client, expected_model='qwen-flash-next-q2k')
    assert result['server']['status'] == 'running'
    assert result['input_tokens'] is None
    assert result['output_tokens'] == 0
    assert result['cached_input_tokens'] is None
    assert result['metrics_status'] == 'partial'


@pytest.mark.asyncio
async def test_stopped_server_does_not_turn_into_zero_usage():
    def handle(request):
        raise httpx.ConnectError('not listening', request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url='http://127.0.0.1:18987') as client:
        result = await monitor.read(client, expected_model='qwen-flash-next-q2k')
    assert result['server']['status'] == 'unavailable'
    assert result['input_tokens'] is None
    assert result['metrics_status'] == 'unavailable'


@pytest.mark.asyncio
async def test_loading_and_metrics_disabled_are_distinct():
    for props_status, expected in [(503, 'starting'), (401, 'unavailable'), (200, 'running')]:
        def handle(request, status=props_status):
            if request.url.path == '/props':
                return httpx.Response(status, json={'model_alias': 'qwen-flash-next-q2k'})
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url='http://127.0.0.1:18987') as client:
            result = await monitor.read(client, expected_model='qwen-flash-next-q2k')
        assert result['server']['status'] == expected
        assert result['output_tokens'] is None
        assert result['metrics_status'] == 'unavailable'
