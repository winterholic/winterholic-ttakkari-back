
from ttakkari.creative.catalog import (
    FORMATS,
    get_catalog,
    validate_project_spec,
)
from ttakkari.creative.service import ProjectInput, Studio


def test_catalog_structure_and_completeness():
    cat = get_catalog()
    assert "formats" in cat and len(cat["formats"]) >= 4
    assert "templates" in cat and len(cat["templates"]) >= 6
    assert "tools" in cat and len(cat["tools"]) >= 8

    # Check key formats exist
    format_ids = {f["id"] for f in cat["formats"]}
    assert {"shorts_9_16", "tech_explainer", "music_video", "narration_story"}.issubset(format_ids)

    # Check templates
    template_ids = {t["id"] for t in cat["templates"]}
    assert {"intro_hook", "title_card", "code_diagram", "ken_burns", "highlight_text", "outro"}.issubset(template_ids)


def test_validation_rejects_wrong_aspect():
    bad = {
        "content_type": "shorts_9_16",
        "aspect": "16:9",  # shorts requires 9:16
        "scenes": [
            {"template_id": "intro_hook", "duration": 3.0, "animation": "zoom_in", "transition": "cut", "caption_style": "center_highlight", "caption": "훅"},
            {"template_id": "highlight_text", "duration": 3.0, "animation": "pulse", "transition": "cut", "caption_style": "center_highlight", "caption": "강조"},
            {"template_id": "outro", "duration": 4.0, "animation": "zoom_out", "transition": "fade", "caption_style": "title_card", "caption": "끝"},
        ]
    }
    errors = validate_project_spec(bad)
    assert any("화면비" in e for e in errors)


def test_validation_rejects_missing_hook_for_shorts():
    bad = {
        "content_type": "shorts_9_16",
        "aspect": "9:16",
        "scenes": [
            {"template_id": "ken_burns", "duration": 5.0, "animation": "zoom_in", "transition": "fade", "caption_style": "bottom_card", "caption": "훅 없음"},
            {"template_id": "highlight_text", "duration": 3.0, "animation": "pulse", "transition": "cut", "caption_style": "center_highlight", "caption": "강조"},
            {"template_id": "outro", "duration": 4.0, "animation": "zoom_out", "transition": "fade", "caption_style": "title_card", "caption": "끝"},
        ]
    }
    errors = validate_project_spec(bad)
    assert any("첫 장면에 반드시 훅" in e for e in errors)


def test_validation_rejects_excessive_caption_lines():
    bad = {
        "content_type": "shorts_9_16",
        "aspect": "9:16",
        "scenes": [
            {"template_id": "intro_hook", "duration": 3.0, "animation": "zoom_in", "transition": "cut", "caption_style": "center_highlight", "caption": "1줄\n2줄\n3줄 초과"},
            {"template_id": "highlight_text", "duration": 3.0, "animation": "pulse", "transition": "cut", "caption_style": "center_highlight", "caption": "강조"},
            {"template_id": "outro", "duration": 4.0, "animation": "zoom_out", "transition": "fade", "caption_style": "title_card", "caption": "끝"},
        ]
    }
    errors = validate_project_spec(bad)
    assert any("자막 줄 수" in e for e in errors)


def test_validation_rejects_disallowed_animation():
    bad = {
        "content_type": "tech_explainer",
        "aspect": "16:9",
        "scenes": [
            {"template_id": "title_card", "duration": 4.0, "animation": "pulse", "transition": "fade", "caption_style": "title_card", "caption": "제목"},  # pulse not allowed for title_card
            {"template_id": "code_diagram", "duration": 8.0, "animation": "still", "transition": "cut", "caption_style": "code_overlay", "caption": "코드"},
            {"template_id": "outro", "duration": 4.0, "animation": "zoom_out", "transition": "fade", "caption_style": "title_card", "caption": "끝"},
        ]
    }
    errors = validate_project_spec(bad)
    assert any("애니메이션은 허용되지 않습니다" in e for e in errors)


def test_validation_passes_valid_spec():
    good = {
        "content_type": "tech_explainer",
        "aspect": "16:9",
        "scenes": [
            {"template_id": "title_card", "duration": 4.0, "animation": "zoom_in", "transition": "fade", "caption_style": "title_card", "caption": "시스템 아키텍처"},
            {"template_id": "code_diagram", "duration": 8.0, "animation": "still", "transition": "cut", "caption_style": "code_overlay", "caption": "import os\nprint('hello')"},
            {"template_id": "outro", "duration": 4.0, "animation": "zoom_out", "transition": "fade", "caption_style": "title_card", "caption": "정리 완료"},
        ]
    }
    errors = validate_project_spec(good)
    assert errors == []


def test_studio_create_with_format_initializes_templates(tmp_path):
    studio = Studio(tmp_path)
    proj = studio.create(ProjectInput(name="쇼츠 프로젝트", format_id="shorts_9_16", aspect="9:16", duration=30))
    assert proj["content_type"] == "shorts_9_16"
    assert proj["aspect"] == "9:16"
    assert len(proj["scenes"]) == len(FORMATS["shorts_9_16"].default_scene_templates)
    assert proj["scenes"][0]["template_id"] == "intro_hook"
    assert proj["scenes"][0]["caption_style"] == "center_highlight"


def test_validation_rejects_freeform_scene_exceeding_format_limits():
    # 비훅 장면 template_id=None + 6줄 자막 -> 포맷 자막 줄 수 제한으로 차단
    bad = {
        "content_type": "shorts_9_16",
        "aspect": "9:16",
        "scenes": [
            {
                "template_id": "intro_hook",
                "duration": 3.0,
                "animation": "zoom_in",
                "transition": "cut",
                "caption_style": "center_highlight",
                "caption": "훅 장면",
            },
            {
                "template_id": None,
                "duration": 4.0,
                "animation": "zoom_in",
                "transition": "cut",
                "caption_style": "bottom_card",
                "caption": "1줄\n2줄\n3줄\n4줄\n5줄\n6줄",
            },
            {
                "template_id": "outro",
                "duration": 4.0,
                "animation": "zoom_out",
                "transition": "fade",
                "caption_style": "title_card",
                "caption": "끝",
            },
        ],
    }
    errors = validate_project_spec(bad)
    assert len(errors) > 0
    assert any("자막 줄 수" in e for e in errors)


def test_validation_rejects_unregistered_template_and_disallowed_tools():
    # 'nonexistent' 템플릿 + 허용 밖 pulse + 150자 자막 -> 차단
    bad = {
        "content_type": "tech_explainer",
        "aspect": "16:9",
        "scenes": [
            {
                "template_id": "title_card",
                "duration": 4.0,
                "animation": "zoom_in",
                "transition": "fade",
                "caption_style": "title_card",
                "caption": "소개",
            },
            {
                "template_id": "nonexistent",
                "duration": 8.0,
                "animation": "pulse",
                "transition": "cut",
                "caption_style": "code_overlay",
                "caption": "이것은 기술 설명 영상의 비등록 템플릿 테스트 자막으로 포맷 수준의 글자 수 제한을 초과하도록 작성된 매우 긴 자막 텍스트입니다. " * 3,
            },
            {
                "template_id": "outro",
                "duration": 4.0,
                "animation": "zoom_out",
                "transition": "fade",
                "caption_style": "title_card",
                "caption": "끝",
            },
        ],
    }
    errors = validate_project_spec(bad)
    assert len(errors) > 0
    assert any("등록되지 않은 템플릿" in e for e in errors)
    assert any("애니메이션" in e for e in errors)
    assert any("자막 길이" in e for e in errors)


def test_default_creations_for_all_six_formats_are_valid(tmp_path):
    # 기존 6개 포맷 기본 생성물은 계속 errors == []
    studio = Studio(tmp_path)
    for fmt_id in FORMATS:
        proj = studio.create(
            ProjectInput(
                name=f"기본 프로젝트 {fmt_id}",
                format_id=fmt_id,
                duration=FORMATS[fmt_id].default_duration,
            )
        )
        errors = validate_project_spec(proj)
        assert errors == [], f"포맷 {fmt_id} 기본 생성물 검증 실패: {errors}"

