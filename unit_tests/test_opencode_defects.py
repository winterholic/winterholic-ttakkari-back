import json
from pathlib import Path

from ttakkari.agents.base import LaunchSpec, ParseState
from ttakkari.agents.opencode import OpencodeAdapter
from ttakkari.runs.profiles import USAGE_LIMIT_RE, is_auth_error, is_usage_limit
from ttakkari.security import seatbelt


def test_defect1_seatbelt_wrappable_engines():
    """결함 1: opencode와 copilot이 seatbelt WRAPPABLE_ENGINES에 포함되어야 함."""
    assert "opencode" in seatbelt.WRAPPABLE_ENGINES
    assert "copilot" in seatbelt.WRAPPABLE_ENGINES


def test_defect1_opencode_command_does_not_use_dangerous_auto():
    """결함 1: 어댑터는 증명 전까지 dangerous --auto 플래그를 쓰지 않아야 함."""
    adapter = OpencodeAdapter()
    spec = LaunchSpec(
        cwd=Path("/tmp/workspace"),
        prompt="hello",
        engine_session_id=None,
        model=None,
        effort=None,
        outbox=Path("/tmp/workspace/out"),
        write_roots=[Path("/tmp/workspace")],
        system_note="system notes",
        session_id="00000000-0000-0000-0000-000000000001",
    )
    cmd = adapter.build_command(spec, {})
    assert "--auto" not in cmd


def test_defect2_mcp_config_via_opencode_config_env():
    """결함 2: MCP 설정이 올바른 opencode 스키마로 준비되고 OPENCODE_CONFIG 환경변수가 지정되어야 함."""
    adapter = OpencodeAdapter()
    session_id = "00000000-0000-0000-0000-000000000002"
    mcp_servers = {
        "ttakkari": {
            "command": "/bin/echo",
            "args": ["hello"],
            "env": {"FOO": "bar"}
        }
    }
    spec = LaunchSpec(
        cwd=Path("/tmp/workspace"),
        prompt="hello",
        engine_session_id=None,
        model=None,
        effort=None,
        outbox=Path("/tmp/workspace/out"),
        write_roots=[Path("/tmp/workspace")],
        system_note="system notes",
        session_id=session_id,
        mcp_servers=mcp_servers,
    )
    env: dict[str, str] = {}
    adapter.build_command(spec, env)

    assert "OPENCODE_CONFIG" in env
    cfg_path = Path(env["OPENCODE_CONFIG"])
    assert cfg_path.exists()
    content = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert content["mcp"]["ttakkari"]["type"] == "local"
    assert content["mcp"]["ttakkari"]["command"] == ["/bin/echo", "hello"]
    assert content["mcp"]["ttakkari"]["enabled"] is True


def test_defect3_auth_failure_detection():
    """결함 3: 인증 만료/실패 문자열이 is_auth_error로 감지되어야 함."""
    auth_err1 = "OpenCode error: unauthorized. Token expired or not authenticated"
    auth_err2 = "401 Unauthorized: please run 'opencode auth login' first"
    auth_err3 = "Error: Bad credentials. Please re-authenticate."

    assert is_auth_error(auth_err1) is True
    assert is_auth_error(auth_err2) is True
    assert is_auth_error(auth_err3) is True


def test_defect4_nested_error_parser():
    """결함 4: error 이벤트의 중첩된 data.message를 놓치지 않고 추출해야 함."""
    adapter = OpencodeAdapter()
    state = ParseState()

    nested_event = {
        "type": "error",
        "error": {
            "name": "APIError",
            "data": {
                "message": "Nested error: Copilot premium request allowance exceeded"
            }
        }
    }
    adapter.parse_line(nested_event, state, Path("/tmp"))
    assert state.is_error is True
    assert "Nested error: Copilot premium request allowance exceeded" in state.error
    assert state.limit_hit is True


def test_defect5_copilot_premium_request_limits():
    """결함 5: Copilot premium requests 한도 문구가 USAGE_LIMIT_RE에 감지되어야 함."""
    sample1 = "You have reached your monthly limit of premium requests"
    sample2 = "Error: Copilot premium request allowance exceeded. Please try again later."
    sample3 = "resource has been exhausted: quota limit reached"

    assert USAGE_LIMIT_RE.search(sample1) is not None
    assert is_usage_limit(sample1) is True

    assert USAGE_LIMIT_RE.search(sample2) is not None
    assert is_usage_limit(sample2) is True

    assert USAGE_LIMIT_RE.search(sample3) is not None
    assert is_usage_limit(sample3) is True
