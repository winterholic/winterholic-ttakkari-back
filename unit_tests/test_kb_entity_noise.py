import pytest

from ttakkari.kb_pipeline.entity_filter import is_concept_heading


@pytest.mark.parametrize("heading", [
    "Bookmark", "Current browse context", "References & Citations", "Key Features", "On this page", "Patch Changes",
    "3.0.0", "Works with 26 AI coding agents", "Hire the Top 3% of Freelance PHP Developers", "この章で学ぶこと",
])
def test_web_page_template_headings_are_not_concepts(heading):
    assert not is_concept_heading(heading)


@pytest.mark.parametrize("heading", ["Attention Mechanism", "Gestalt Principles", "Retrieval-Augmented Generation", "Color Contrast"])
def test_real_concepts_survive(heading):
    assert is_concept_heading(heading)
