import pytest

from ttakkari.runs import profiles


@pytest.mark.parametrize("text, expected", [
    ("Selected model is at capacity. Please try a different model.", True),
    ("SELECTED MODEL IS AT CAPACITY. Please try a different model.", True),
    ("permission denied: Selected model is at capacity", False),
    ("capacity planning document", False),
    ("You've hit your usage limit.", False),
])
def test_model_capacity_is_separate_from_quota(text, expected):
    assert profiles.is_model_capacity(text) is expected
    if expected:
        assert not profiles.is_usage_limit(text)
