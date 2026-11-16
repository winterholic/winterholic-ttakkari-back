"""Gateway-only deployment credentials never change the protected app policy."""
import json

import pytest

from ttakkari.config import Settings
from ttakkari.context_sharing.gateway import (
    GatewayNotConfigured,
    build_control,
    gateway_status,
)

TOKEN = "synthetic-control-" + "x" * 48


def configured_file(tmp_path, *, mode=0o600, extra=None):
    s = Settings(_env_file=None, home=tmp_path / ".ttakkari", env="prod",
                 jwt_secret="synthetic-" * 5, cors_origins=["https://ttakkari.winterholic.net"])
    path = tmp_path / "private-resources/service-data/context-gateway/secrets/publisher.json"
    path.parent.mkdir(parents=True)
    payload = {"control_url": "https://share-control.example.test", "control_token": TOKEN,
               "public_url": "https://share.example.test"}
    payload.update(extra or {})
    path.write_text(json.dumps(payload))
    path.chmod(mode)
    return s, path


def test_owner_only_gateway_credentials_supply_only_connection_settings(tmp_path):
    s, _ = configured_file(tmp_path)
    assert gateway_status(s) == {"configured": True, "missing": [], "problems": [],
                                "control_host": "share-control.example.test", "public_url": "https://share.example.test"}
    control = build_control(s)
    control.require()
    assert TOKEN not in repr(control) + repr(gateway_status(s))
    assert s.context_gateway_control_token == ""


@pytest.mark.parametrize("extra,mode", [
    ({"jwt_secret": "not-allowed"}, 0o600),
    ({"control_url": "http://remote.example.test"}, 0o600),
    ({"public_url": "https://user:password@share.example.test"}, 0o600),
    ({"control_token": "short"}, 0o600),
    ({}, 0o644),
])
def test_invalid_credentials_fail_closed_without_echoing_values(tmp_path, extra, mode):
    s, _ = configured_file(tmp_path, extra=extra, mode=mode)
    result = gateway_status(s)
    assert result["configured"] is False and result["problems"]
    assert TOKEN not in json.dumps(result)
    with pytest.raises(GatewayNotConfigured):
        build_control(s).require()


def test_credentials_symlink_is_rejected(tmp_path):
    s, path = configured_file(tmp_path)
    real = path.with_suffix(".real")
    path.rename(real)
    path.symlink_to(real)
    assert gateway_status(s)["configured"] is False


def test_explicit_environment_configuration_takes_precedence_without_fallback(tmp_path):
    s, path = configured_file(tmp_path)
    path.write_text("malformed")
    explicit = s.model_copy(update={"context_gateway_control_url": "https://explicit.example.test",
                                    "context_gateway_control_token": TOKEN})
    assert gateway_status(explicit)["control_host"] == "explicit.example.test"
    partial = s.model_copy(update={"context_gateway_control_url": "https://explicit.example.test"})
    assert gateway_status(partial)["configured"] is False


def test_development_settings_do_not_load_production_credentials(tmp_path):
    s, _ = configured_file(tmp_path)
    assert gateway_status(s.model_copy(update={"env": "dev"}))["configured"] is False
