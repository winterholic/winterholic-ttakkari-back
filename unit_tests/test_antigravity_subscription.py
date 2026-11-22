
import pytest

from ttakkari.config import get_settings
from ttakkari.runs.usage import Usage, Window


@pytest.mark.asyncio
async def test_activation_uses_one_isolated_cli_request_with_credits_off(tmp_path, monkeypatch):
    from ttakkari.runs.antigravity_subscription import activate_subscription

    fake = tmp_path / "agy"
    fake.write_text("""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
settings = json.loads(args[args.index('--settings') + 1])
assert settings['useG1Credits'] is False
assert 'command(*)' in settings['permissions']['deny']
assert 'mcp(*)' in settings['permissions']['deny']
assert '--sandbox' in args and '--disable-slash-commands' in args
assert '--conversation' not in args and '--dangerously-skip-permissions' not in args
assert args[args.index('--model') + 1] == 'gemini-3.8-flash-low'
assert 'GEMINI_API_KEY' not in os.environ
assert os.listdir() == []
print(json.dumps({'type': 'result', 'result': {'status': 'SUCCESS', 'response': 'OK',
                 'usage': {'input_tokens': 10, 'output_tokens': 1}}}))
""")
    fake.chmod(0o700)
    monkeypatch.setenv("GEMINI_API_KEY", "fixture")
    monkeypatch.setattr("ttakkari.runs.antigravity_subscription.get_settings",
                        lambda: get_settings().model_copy(update={"antigravity_bin": str(fake)}))
    observed = Usage(source="antigravity_cli", updated_at=1000, windows=[
        Window("agy_gemini_models_weekly", "Gemini weekly", 0, 605800, 100),
        Window("agy_gemini_models_5h", "Gemini 5h", 0, 19000, 100),
    ])
    assert await activate_subscription(None, preflight=observed) == {
        "status": "succeeded", "tokens": {"input_tokens": 10, "output_tokens": 1},
    }
