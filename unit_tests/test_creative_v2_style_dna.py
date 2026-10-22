import json

import pytest
from pydantic import ValidationError

from ttakkari.creative.v2.style_dna import (
    MERGE_FIELDS,
    REQUIRED_FIELDS,
    Facet,
    Override,
    ReferenceAsset,
    Resolution,
    StyleDNA,
    StyleDNADoc,
    StyleError,
    StyleRef,
    StyleStore,
    UnresolvedStyleConflict,
    seed_docs,
)

CUTE = StyleRef(style_id="cute_3d", version="1.0.0")
HORROR = StyleRef(style_id="horror_lighting", version="1.0.0")
SF = StyleRef(style_id="sf_world", version="1.0.0")


@pytest.fixture
def store(tmp_path):
    return StyleStore(tmp_path, seeds=seed_docs())


def test_sixteen_required_fields_include_identity_and_version():
    assert len(REQUIRED_FIELDS) == 16
    assert set(StyleDNA.model_fields) == set(REQUIRED_FIELDS)
    assert set(MERGE_FIELDS) | {"style_id", "version"} == set(REQUIRED_FIELDS)


def test_all_seed_documents_validate_and_resolve(store):
    for doc in seed_docs():
        assert store.validate_doc(doc) == [], doc.style_id
        ref = StyleRef(style_id=doc.style_id, version=doc.version)
        assert isinstance(store.effective(ref), StyleDNA)
    assert (
        store.effective(CUTE).maturity == "draft"
    )  # 참조·예시가 없으면 draft로 표시한다


def test_root_style_must_fill_all_fields(store):
    base = seed_docs()[1].model_dump()
    base.update(style_id="broken_one", version="1.0.0", lighting=None)
    with pytest.raises(StyleError, match="lighting"):
        store.put(StyleDNADoc.model_validate(base))


def test_child_overriding_parent_field_requires_declared_reason(store):
    doc = StyleDNADoc(
        style_id="sneaky",
        version="1.0.0",
        parent=StyleRef(style_id="neutral_base", version="1.0.0"),
        lighting=Facet(text="몰래 바꾼 조명"),
    )
    with pytest.raises(StyleError, match="묵시적 덮어쓰기"):
        store.put(doc)
    ok = doc.model_copy(
        update={
            "overrides": [
                Override(field="lighting", reason="장면 요구로 조명을 바꾼다")
            ]
        }
    )
    store.put(ok)
    assert (
        store.effective(StyleRef(style_id="sneaky", version="1.0.0")).lighting.text
        == "몰래 바꾼 조명"
    )
    assert (
        store.provenance_of(StyleRef(style_id="sneaky", version="1.0.0"))["lighting"]
        == "sneaky@1.0.0"
    )
    assert (
        store.provenance_of(StyleRef(style_id="sneaky", version="1.0.0"))["materials"]
        == "neutral_base@1.0.0"
    )


def test_versions_are_immutable_and_idempotent(store):
    doc = StyleDNADoc(
        style_id="mine",
        version="1.0.0",
        parent=StyleRef(style_id="neutral_base", version="1.0.0"),
        overrides=[Override(field="lighting", reason="내 조명을 쓴다")],
        lighting=Facet(text="내 조명"),
    )
    store.put(doc)
    store.put(doc)  # 같은 내용은 멱등
    changed = doc.model_copy(update={"lighting": Facet(text="바뀐 조명")})
    with pytest.raises(StyleError, match="불변"):
        store.put(changed)
    store.put(changed.model_copy(update={"version": "1.1.0"}))
    assert store.versions("mine") == ["1.0.0", "1.1.0"]
    assert store.resolve_ref("mine", "latest").version == "1.1.0"
    with pytest.raises(StyleError, match="시드"):
        store.put(seed_docs()[1])


def test_parent_must_exist_and_inheritance_cycles_are_detected(store):
    orphan = StyleDNADoc(
        style_id="orphan",
        version="1.0.0",
        parent=StyleRef(style_id="ghost", version="1.0.0"),
    )
    with pytest.raises(StyleError, match="스타일이 없습니다"):
        store.put(orphan)


def test_cute_3d_plus_horror_lighting_plus_sf_world_surfaces_explicit_conflicts(store):
    result = store.compose([CUTE, HORROR, SF])
    assert result.status == "unresolved" and result.dna is None
    labels = {c.label for c in result.conflicts}
    assert labels == {"lighting", "environment_rules", "sound_identity"}
    lighting = next(c for c in result.conflicts if c.field == "lighting")
    assert {c.source for c in lighting.candidates} == {
        "cute_3d@1.0.0",
        "horror_lighting@1.0.0",
    }


def test_explicit_resolutions_close_conflicts_and_record_provenance(store):
    resolutions = [
        Resolution(
            field="lighting",
            decision="take",
            winner="horror_lighting@1.0.0",
            rationale="공포 장면이라 저조도 광원을 우선한다",
            decided_by="user",
        ),
        Resolution(
            field="environment_rules",
            decision="take",
            winner="sf_world@1.0.0",
            rationale="세계관은 우주 SF 규칙을 따른다",
            decided_by="user",
        ),
        Resolution(
            field="sound_identity",
            decision="custom",
            custom={"text": "저주파 드론과 진공 무음", "params": {}},
            rationale="두 규칙을 합친 사운드 규칙을 직접 정했다",
            decided_by="user",
        ),
    ]
    result = store.compose([CUTE, HORROR, SF], resolutions)
    assert result.status == "resolved" and result.dna is not None
    assert result.dna.lighting.text.startswith("저조도")
    assert result.dna.visual_identity.text.startswith(
        "둥근 형태"
    )  # 충돌 없는 필드는 원본 유지
    assert result.dna.sound_identity.text == "저주파 드론과 진공 무음"
    via = {p.field: p.via for p in result.provenance}
    assert via["lighting"] == "resolution" and via["visual_identity"] == "claim"
    ids = {c.id for c in result.dna.quality_constraints}
    assert {"no_black_frames", "soft_shadows", "shadow_detail"} <= ids
    assert result.sha256 == store.compose([CUTE, HORROR, SF], resolutions).sha256
    assert any("호환" in w or "제외" in w for w in result.warnings)


def test_assistant_resolution_stays_pending_until_user_confirms(store):
    base = [
        Resolution(
            field="environment_rules",
            decision="take",
            winner="sf_world@1.0.0",
            rationale="세계관은 우주 SF 규칙을 따른다",
            decided_by="user",
        ),
        Resolution(
            field="sound_identity",
            decision="take",
            winner="horror_lighting@1.0.0",
            rationale="긴장 연출을 위해 공포 사운드를 쓴다",
            decided_by="user",
        ),
    ]
    guess = Resolution(
        field="lighting",
        decision="take",
        winner="cute_3d@1.0.0",
        rationale="어시스턴트가 추정한 선택",
        decided_by="assistant",
    )
    result = store.compose([CUTE, HORROR, SF], [*base, guess])
    assert result.status == "resolved_pending_confirmation"
    assert not guess.confirmed_by_user
    confirmed = guess.model_copy(update={"confirmed_by_user": True})
    assert store.compose([CUTE, HORROR, SF], [*base, confirmed]).status == "resolved"


def test_resolution_for_nonexistent_conflict_or_bad_winner_is_rejected(store):
    stray = Resolution(
        field="materials",
        decision="take",
        winner="cute_3d@1.0.0",
        rationale="충돌이 없는 필드에 대한 해결",
        decided_by="user",
    )
    with pytest.raises(StyleError, match="충돌이 없는 곳"):
        store.compose(
            [CUTE, HORROR],
            [
                stray,
                Resolution(
                    field="lighting",
                    decision="take",
                    winner="cute_3d@1.0.0",
                    rationale="충돌을 이 쪽으로 해결한다",
                    decided_by="user",
                ),
                Resolution(
                    field="sound_identity",
                    decision="take",
                    winner="cute_3d@1.0.0",
                    rationale="충돌을 이 쪽으로 해결한다",
                    decided_by="user",
                ),
            ],
        )
    bad = Resolution(
        field="lighting",
        decision="take",
        winner="sf_world@1.0.0",
        rationale="후보에 없는 winner",
        decided_by="user",
    )
    with pytest.raises(StyleError, match="후보에 없습니다"):
        store.compose([CUTE, HORROR], [bad])


def test_pin_work_refuses_unresolved_and_uses_revision_cas(store):
    work = "11111111-1111-4111-8111-111111111111"
    with pytest.raises(UnresolvedStyleConflict) as raised:
        store.pin_work(work, [CUTE, HORROR])
    assert {c.field for c in raised.value.conflicts} == {"lighting", "sound_identity"}
    res = [
        Resolution(
            field="lighting",
            decision="take",
            winner="horror_lighting@1.0.0",
            rationale="공포 조명을 우선한다",
            decided_by="user",
        ),
        Resolution(
            field="sound_identity",
            decision="take",
            winner="horror_lighting@1.0.0",
            rationale="공포 사운드를 우선한다",
            decided_by="user",
        ),
    ]
    first = store.pin_work(work, [CUTE, HORROR], res, expected_revision=0)
    assert first["revision"] == 1 and first["inputs"] == [
        "cute_3d@1.0.0",
        "horror_lighting@1.0.0",
    ]
    with pytest.raises(StyleError, match="다른 변경"):
        store.pin_work(work, [CUTE, HORROR], res, expected_revision=0)
    second = store.pin_work(work, [CUTE, HORROR], res, expected_revision=1)
    assert second["revision"] == 2 and second["sha256"] == first["sha256"]
    assert store.get_work(work)["dna"]["lighting"]["text"].startswith("저조도")


def test_new_version_does_not_change_a_pinned_work(store):
    work = "22222222-2222-4222-8222-222222222222"
    pinned = store.pin_work(work, [CUTE])
    changed = seed_docs()[1].model_copy(
        update={"version": "1.1.0", "lighting": Facet(text="완전히 다른 조명")}
    )
    store.put(changed)
    assert (
        store.get_work(work)["dna"]["lighting"]["text"]
        == pinned["dna"]["lighting"]["text"]
    )
    assert store.resolve_ref("cute_3d", "latest").version == "1.1.0"


def test_reference_assets_cover_image_video_code_render_settings_and_outcomes():
    sha = "a" * 64
    ReferenceAsset(kind="image", ref="refs/hero.png", sha256=sha)
    ReferenceAsset(kind="video", ref="refs/shot.mp4", sha256=sha)
    ReferenceAsset(kind="code", ref="refs/scene.tsx", sha256=sha, language="tsx")
    ReferenceAsset(
        kind="render_settings",
        ref="settings/eevee",
        params={"samples": 64.0, "engine": "EEVEE"},
    )
    ReferenceAsset(
        kind="outcome",
        ref="three-cinematic-v1-v2",
        polarity="failure",
        note="검은 프레임 5장",
    )
    for bad in (
        {"kind": "image", "ref": "refs/hero.png"},
        {"kind": "code", "ref": "a.tsx", "sha256": sha},
        {"kind": "render_settings", "ref": "settings/x"},
        {"kind": "outcome", "ref": "rec"},
        {"kind": "image", "ref": "../../etc/passwd", "sha256": sha},
        {"kind": "image", "ref": "/abs/path.png", "sha256": sha},
        {"kind": "image", "ref": "a\\b.png", "sha256": sha},
    ):
        with pytest.raises(ValidationError):
            ReferenceAsset(**bad)


def test_params_reject_nan_and_inf():
    with pytest.raises(ValidationError):
        Facet.model_validate_json('{"text":"x","params":{"kelvin":NaN}}')
    with pytest.raises(ValidationError):
        ReferenceAsset(kind="render_settings", ref="s", params={"x": float("inf")})


def test_conflicting_example_polarity_is_a_conflict(store):
    pos = StyleDNADoc(
        style_id="ex_pos",
        version="1.0.0",
        parent=StyleRef(style_id="neutral_base", version="1.0.0"),
        overrides=[Override(field="positive_examples", reason="좋은 예시를 추가한다")],
        positive_examples=[{"text": "느린 오비트"}],
    )
    neg = StyleDNADoc(
        style_id="ex_neg",
        version="1.0.0",
        parent=StyleRef(style_id="neutral_base", version="1.0.0"),
        overrides=[Override(field="negative_examples", reason="나쁜 예시를 추가한다")],
        negative_examples=[{"text": "느린 오비트"}],
    )
    store.put(pos)
    store.put(neg)
    result = store.compose(
        [
            StyleRef(style_id="ex_pos", version="1.0.0"),
            StyleRef(style_id="ex_neg", version="1.0.0"),
        ]
    )
    assert (
        result.status == "unresolved"
        and result.conflicts[0].field == "positive_examples"
    )


def test_composed_dna_is_json_serializable(store):
    result = store.compose([CUTE])
    json.dumps(result.model_dump(mode="json"), allow_nan=False)
    assert result.status == "resolved" and result.dna.style_id == "cute_3d"
