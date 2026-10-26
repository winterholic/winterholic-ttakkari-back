"""Exercise pytest's real failure rendering; no containers, DB or browser."""
import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("filename,function_name", [
    ("test_e2e_game_studio.py", "build"),
    ("test_game_browser_smoke.py", "build_smoke_game"),
])
def test_failed_build_log_retains_middle_mount_path_in_pytest_output(tmp_path, filename, function_name):
    tree = ast.parse((ROOT / 'tests' / filename).read_text(encoding='utf-8'))
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == function_name)
    assertion = next(n for n in function.body if isinstance(n, ast.Assert) and 'succeeded' in ast.unparse(n.test))
    marker = '/synthetic/actual-mount-path/must-remain-visible/build-id'
    done = {'status': 'failed', 'error': 'install exit 127',
            'log_tail': 'prefix-' + 'x' * 600 + marker + 'y' * 600 + '-suffix'}
    builds = ast.parse((ROOT / 'src/ttakkari/games/builds.py').read_text(encoding='utf-8'))
    branch = next(n for n in ast.walk(builds) if isinstance(n, ast.If) and 'not res.success' in ast.unparse(n.test))
    failure_body = ast.unparse(ast.Module(body=branch.body, type_ignores=[]))
    body = ('done = ' + repr(done) + '\n'
            'build = SimpleNamespace(log_tail=done["log_tail"])\n'
            'res = SimpleNamespace(error=done["error"])\n'
            'plan = SimpleNamespace(build_id="synthetic")\n'
            'log = logging.getLogger("ttakkari.games.builds")\n' + failure_body + '\n' + ast.unparse(assertion))
    target = tmp_path / 'test_render.py'
    target.write_text('import logging\nfrom types import SimpleNamespace\ndef test_render():\n' +
                      textwrap.indent(body, '    ') + '\n', encoding='utf-8')
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '--tb=short', '--color=no', '-p', 'no:cacheprovider', '-c', '/dev/null', str(target)],
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert marker in result.stdout, result.stdout
    assert 'prefix-' in result.stdout and '-suffix' in result.stdout
