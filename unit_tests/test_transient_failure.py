from ttakkari.runs import profiles


def test_codex_model_capacity_has_its_own_path():
    # "is at capacity" 는 MODEL_CAPACITY_RE(모델별 쿨다운)가 맡고 계정 전체 일시 장애로 세지 않는다.
    text = "Selected model is at capacity. Please try a different model."
    assert profiles.is_model_capacity(text) and profiles.transient_cooldown(text) is None


def test_stream_disconnect_and_5xx_are_transient():
    assert profiles.transient_cooldown("stream disconnected before completion") == profiles.TRANSIENT_COOLDOWN_SECONDS
    assert profiles.transient_cooldown("unexpected status 503 Service Unavailable") == profiles.TRANSIENT_COOLDOWN_SECONDS


def test_muse_fingerprint_rests_longer():
    assert profiles.transient_cooldown("Muse MSP schema fingerprint differs from the pinned SDK") == profiles.ENGINE_BROKEN_COOLDOWN_SECONDS


def test_permission_error_and_plain_failure_are_not_transient():
    assert profiles.transient_cooldown("permission denied: /etc/503.conf") is None
    assert profiles.transient_cooldown("SyntaxError: bad input", None) is None


def test_cli_rejecting_our_configuration_rests_the_account_and_falls_back():
    for text in ('Error: Model "auto" does not support reasoning effort configuration (requested: "high").',
                 "error: unexpected argument '-p' found",
                 "error: unrecognized option '--reasoning-effort'"):
        assert profiles.transient_cooldown(text) == profiles.ENGINE_BROKEN_COOLDOWN_SECONDS, text


def test_permission_text_with_option_words_is_not_treated_as_config_rejection():
    assert profiles.transient_cooldown("permission denied: unknown option file /etc/x") is None
