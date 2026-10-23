"""Limited pilot sources (CP-S2-10): a few public, officially licensed feeds/docs, teacher off, rule-only quality.

Each entry's rights were read from the primary text on 2026-10-23 (KST) and are recorded with the exact observation in
`basis`. This is an operator reading, not legal advice: anything outside a stated grant (third-party content inside a
page, trademarks, images) is not covered and the owner can pause a source or revoke the grant at any time. `train`
is never granted here, so nothing collected by the pilot can feed a model.

Seeding is idempotent and happens once: the `pilot` runtime flag records it, so a source the owner paused or edited is
never re-created or silently re-enabled. Registration passes through the same validation as the owner API.
"""
from __future__ import annotations

from ttakkari.collector import control, runtime
from ttakkari.collector.contracts import AuthContext

CHECKED_AT = "2026-10-23T16:30:00+09:00"
# Live smoke (2026-10-23): every kubernetes.io page was held as tech_docs_version_unknown because these pages are a
# rolling "latest" snapshot with no version in the URL. The pilot opts out of that gate only for these two sources;
# the document revision (raw/text hash, fetch time) still pins what was collected, and version_hint stays recorded.
_NO_VERSION_GATE = {"require_version": False}
_K8S_EXCLUDED = ["/legacy/", "/v1.0/", "/v1.1/", "/404"]
_K8S_POLICY = {
    "allowed_hosts": ["kubernetes.io"],
    "permissions": {"store": "allowed", "knowledge": "allowed", "export": "allowed", "train": "unknown"},
    "license_id": "CC-BY-4.0",
    "policy_url": "https://github.com/kubernetes/website/blob/main/LICENSE",
    "basis": ("kubernetes/website 저장소 LICENSE 원문이 'Attribution 4.0 International'(CC BY 4.0)임을 2026-10-23 직접 확인. "
              "출처 표시 의무는 문서 URL과 수집 시각을 근거 메타데이터로 보존해 충족. 상표·로고·제3자 자료는 제외. "
              "robots.txt는 /legacy/ /v1.0/ /v1.1/ /404 만 막으며 그 경로는 제외 접두어로 같이 둔다. 학습 이용은 확인하지 않아 unknown."),
    "checked_at": CHECKED_AT,
    "min_delay_seconds": 2.0,
    "excluded_path_prefixes": _K8S_EXCLUDED,
    "default_profile": "tech_docs",
}

PILOT_SOURCES: tuple[dict, ...] = (
    {"source_key": "pilot:kubernetes-blog-feed", "title": "Kubernetes 블로그 (공식 피드)",
     "seeds": ["https://kubernetes.io/feed.xml"], "interval_minutes": 1440, "max_pages": 10, "max_depth": 0,
     "profile": "tech_docs", "profile_params": _NO_VERSION_GATE, "auto_import": True, "policy": _K8S_POLICY},
    {"source_key": "pilot:kubernetes-docs-ko", "title": "Kubernetes 한국어 공식 문서 (핵심 개념 6쪽)",
     "seeds": ["https://kubernetes.io/ko/docs/concepts/overview/",
               "https://kubernetes.io/ko/docs/concepts/workloads/pods/",
               "https://kubernetes.io/ko/docs/concepts/services-networking/service/",
               "https://kubernetes.io/ko/docs/concepts/configuration/configmap/",
               "https://kubernetes.io/ko/docs/concepts/storage/persistent-volumes/",
               "https://kubernetes.io/ko/docs/concepts/workloads/controllers/deployment/"],
     "interval_minutes": 10080, "max_pages": 6, "max_depth": 0, "profile": "tech_docs",
     "profile_params": _NO_VERSION_GATE, "auto_import": True, "policy": _K8S_POLICY},
    {"source_key": "pilot:govuk-news-atom", "title": "GOV.UK 뉴스·공지 (공식 Atom 피드)",
     "seeds": ["https://www.gov.uk/search/news-and-communications.atom"], "interval_minutes": 1440, "max_pages": 10,
     "max_depth": 0, "profile": "news",
     # Pages can embed third-party material that OGL does not cover, so results wait for the owner's review.
     "auto_import": False,
     "policy": {
         "allowed_hosts": ["www.gov.uk"],
         "permissions": {"store": "allowed", "knowledge": "allowed", "export": "allowed", "train": "unknown"},
         "license_id": "OGL-UK-3.0",
         "policy_url": "https://www.gov.uk/help/terms-conditions",
         "basis": ("GOV.UK 이용 약관이 사이트 콘텐츠를 Crown copyright 아래 Open Government Licence로 공개한다고 밝힘을 2026-10-23 확인"
                   "(약관이 가리키는 OGL 버전 표기는 v1 링크라 v3.0 호환 조건으로 읽음: 출처 표시 필요). "
                   "제3자 저작물·로고·개인정보는 면책. 개별 페이지의 별도 표기는 수집기가 판별하지 못해 자동 적재하지 않고 소유자 검토를 거친다. "
                   "robots.txt는 */print 와 /search/all* 만 막는다."),
         "checked_at": CHECKED_AT,
         "min_delay_seconds": 2.0,
         "excluded_path_prefixes": ["/search/all"],
         "default_profile": "news",
     }},
)


async def seed(db, *, tenant_id: str = "owner", scope: str = "personal") -> list[str]:
    """Register the pilot once. Returns the keys created now; an existing flag or source makes this a no-op."""
    if await runtime.get(db, runtime.PILOT) is not None:
        return []
    ctx = AuthContext(tenant_id=tenant_id, scope=scope, principal_id="owner", kind="owner")
    created: list[str] = []
    for spec in PILOT_SOURCES:
        try:
            async with db.begin_nested():
                await control.register_source(db, ctx, **{k: v for k, v in spec.items()})
            created.append(spec["source_key"])
        except control.SourceKeyExists:
            continue
    await runtime.put(db, runtime.PILOT, {"seeded": created, "tenant_id": tenant_id, "scope": scope,
                                          "rights_checked_at": CHECKED_AT})
    return created
