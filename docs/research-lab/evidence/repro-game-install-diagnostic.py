"""Check the actual G14 build assertion without containers, DB or a browser.

The API response is synthetic; this verifies failure observability, not the
cause of the deployment's exit 127 or the complete Game Studio journey.
"""
import ast
import asyncio
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[3] / 'tests/test_e2e_game_studio.py'


async def check():
    tree = ast.parse(SOURCE.read_text())
    journey = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == 'test_game_studio_flow_with_recorded_worker_output')
    build = next(n for n in journey.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'build')
    module = ast.Module(body=[build], type_ignores=[])
    code = compile(ast.fix_missing_locations(module), str(SOURCE), 'exec')

    class Response:
        status_code = 202
        text = ''

        def json(self):
            return {'id': 'synthetic-build'}

    class Client:
        async def post(self, *args, **kwargs):
            return Response()

    for status, locked, log in [('failed', False, 'synthetic-install-stderr-exit-127'),
                                ('succeeded', True, ''), ('succeeded', False, '')]:
        done = {'status': status, 'error': 'synthetic failure' if status == 'failed' else None,
                'log_tail': log, 'lock_verified': locked}

        async def wait_build(*args, response=done, **kwargs):
            return response

        namespace = {'client': Client(), 'auth': {}, 'gid': 'synthetic-game', 'wait_build': wait_build}
        exec(code, namespace)  # noqa: S102 - execute the repository's exact assertion
        try:
            result = await namespace['build']()
        except AssertionError as exc:
            assert status != 'succeeded' or not locked
            if status == 'failed':
                assert log in str(exc), f'G14 hides install stderr: {exc}'
                print('failed build: error and original log are visible')
            else:
                print('unverified lock: rejected')
        else:
            assert status == 'succeeded' and locked and result is done
            print('successful locked build: accepted')

    smoke_path = SOURCE.with_name('test_game_browser_smoke.py')
    smoke_tree = ast.parse(smoke_path.read_text())
    smoke = next(n for n in smoke_tree.body if isinstance(n, ast.AsyncFunctionDef)
                 and n.name == 'build_smoke_game')
    status_assertion = next(n for n in smoke.body if isinstance(n, ast.Assert)
                            and 'succeeded' in ast.unparse(n.test))
    smoke_code = compile(ast.fix_missing_locations(ast.Module(
        body=[status_assertion], type_ignores=[])), str(smoke_path), 'exec')
    for status in ('failed', 'succeeded'):
        done = {'status': status, 'error': 'synthetic install failure',
                'log_tail': 'synthetic G13 install stderr'}
        try:
            exec(smoke_code, {'done': done})  # noqa: S102 - repository assertion
        except AssertionError as exc:
            assert status == 'failed' and done['log_tail'] in str(exc)
            print('G13 failed build: original log is visible')
        else:
            assert status == 'succeeded'
            print('G13 successful build: accepted')


if __name__ == '__main__':
    asyncio.run(check())
