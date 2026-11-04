"""Named collection targets and UI/UX topics that a mission goal can mention.

A goal such as "IBM Carbon, Material, Apple HIG ... 를 수집해줘" names its targets. Turning those names into
official entry points is a lookup, not a search problem, so it is deterministic and verifiable: every root below was
fetched on 2026-11-05. A target without a root is still a target; the research phase discovers its official site.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Target:
    key: str
    name: str
    kind: str  # design_system | enterprise_ui | ux_research | standard | public_sector
    aliases: tuple[str, ...]
    roots: tuple[str, ...] = ()
    blog: tuple[str, ...] = ()  # engineering/design blog entry points that carry the authors' own reasoning

    @property
    def hosts(self) -> tuple[str, ...]:
        return tuple(sorted({(urlsplit(u).hostname or "").lower() for u in (*self.roots, *self.blog)} - {""}))


KIND_LABEL = {
    "design_system": "디자인 시스템", "enterprise_ui": "업무용 UI", "ux_research": "UX 연구·이론",
    "standard": "접근성·표준", "public_sector": "공공 디자인 시스템",
}

TARGETS: tuple[Target, ...] = (
    Target("carbon", "IBM Carbon", "design_system", ("ibm carbon", "carbon design system", "carbon 디자인", "카본"), ("https://www.carbondesignsystem.com/",)),
    Target("material", "Google Material Design", "design_system", ("material design", "material 3", "머티리얼"), ("https://m3.material.io/",)),
    Target("hig", "Apple Human Interface Guidelines", "design_system", ("apple hig", "human interface guidelines", "apple human interface", "hig"), ("https://developer.apple.com/design/human-interface-guidelines/",)),
    Target("fluent", "Microsoft Fluent", "design_system", ("microsoft fluent", "fluent design", "fluent 2", "fluent ui"), ("https://fluent2.microsoft.design/",)),
    Target("atlassian", "Atlassian Design System", "design_system", ("atlassian",), ("https://atlassian.design/",)),
    Target("spectrum", "Adobe Spectrum", "design_system", ("adobe spectrum", "spectrum"), ("https://spectrum.adobe.com/",)),
    Target("primer", "GitHub Primer", "design_system", ("github primer", "primer"), ("https://primer.style/",)),
    Target("fiori", "SAP Fiori", "enterprise_ui", ("sap fiori", "fiori"), ("https://www.sap.com/design-system/fiori-design-web/",)),
    Target("lightning", "Salesforce Lightning", "enterprise_ui", ("salesforce lightning", "lightning design system", "salesforce"), ("https://www.lightningdesignsystem.com/",)),
    Target("redwood", "Oracle Redwood", "enterprise_ui", ("oracle redwood", "redwood"), ("https://redwood.oracle.com/",)),
    Target("cloudscape", "AWS Cloudscape", "enterprise_ui", ("aws cloudscape", "cloudscape"), ("https://cloudscape.design/",)),
    Target("antd", "Ant Design", "enterprise_ui", ("ant design", "antd", "앤트 디자인"), ("https://ant.design/docs/spec/introduce",)),
    Target("polaris", "Shopify Polaris", "enterprise_ui", ("shopify polaris", "polaris"), ("https://shopify.dev/docs/api/polaris",)),
    Target("patternfly", "Red Hat PatternFly", "enterprise_ui", ("patternfly", "red hat patternfly"), ("https://www.patternfly.org/",)),
    Target("blueprint", "Palantir Blueprint", "enterprise_ui", ("palantir blueprint", "blueprintjs", "blueprint"), ("https://blueprintjs.com/docs/",)),
    Target("eui", "Elastic UI", "enterprise_ui", ("elastic ui", "elastic eui", "eui"), ("https://eui.elastic.co/",)),
    Target("clarity", "VMware Clarity", "enterprise_ui", ("vmware clarity", "clarity design"), ("https://clarity.design/",)),
    Target("base", "Uber Base", "design_system", ("uber base", "base web"), ("https://base.uber.com/",)),
    Target("gestalt", "Pinterest Gestalt", "design_system", ("pinterest gestalt",), ("https://gestalt.pinterest.systems/",)),
    Target("garden", "Zendesk Garden", "design_system", ("zendesk garden", "garden"), ("https://garden.zendesk.com/",)),
    Target("paste", "Twilio Paste", "design_system", ("twilio paste",), ("https://github.com/twilio-labs/paste",)),
    Target("pajamas", "GitLab Pajamas", "design_system", ("gitlab pajamas", "pajamas"), ("https://design.gitlab.com/",)),
    Target("nngroup", "Nielsen Norman Group", "ux_research", ("nielsen norman", "nn/g", "nngroup", "nn group"), ("https://www.nngroup.com/articles/",)),
    Target("ixdf", "Interaction Design Foundation", "ux_research", ("interaction design foundation", "ixdf"), ("https://ixdf.org/literature",)),
    Target("baymard", "Baymard Institute", "ux_research", ("baymard",), ("https://baymard.com/blog",)),
    Target("lawsofux", "Laws of UX", "ux_research", ("laws of ux", "ux laws", "ux 법칙"), ("https://lawsofux.com/",)),
    Target("wcag", "W3C WCAG", "standard", ("wcag", "web content accessibility"), ("https://www.w3.org/WAI/standards-guidelines/wcag/",)),
    Target("aria", "WAI-ARIA Authoring Practices", "standard", ("wai-aria", "aria authoring", "aria apg"), ("https://www.w3.org/WAI/ARIA/apg/",)),
    Target("uswds", "U.S. Web Design System", "public_sector", ("uswds", "u.s. web design system"), ("https://designsystem.digital.gov/",)),
    Target("govuk", "GOV.UK Design System", "public_sector", ("gov.uk", "govuk"), ("https://design-system.service.gov.uk/",)),
    Target("nhs", "NHS Design System", "public_sector", ("nhs design", "nhs service manual"), ("https://service-manual.nhs.uk/design-system",)),
    Target("toss", "토스 디자인 시스템", "design_system", ("toss design", "토스 디자인", "토스디자인", "toss tds", "tds"), (), ("https://toss.tech/",)),
    Target("naver", "네이버 디자인 시스템", "design_system", ("naver design", "네이버 디자인", "네이버디자인", "nds"), ()),
    Target("kakao", "카카오 디자인 시스템", "design_system", ("kakao design", "카카오 디자인", "카카오디자인"), ()),
    Target("baemin", "우아한형제들 디자인", "design_system", ("우아한형제들", "배민 디자인", "baemin"), ()),
    Target("carrot", "당근 Seed 디자인 시스템", "design_system", ("당근 디자인", "seed design"), ()),
)

# The UI/UX areas a goal can ask for. `en` feeds the search query, `words` decide whether a collected document belongs.
TOPICS: tuple[dict[str, Any], ...] = (
    {"key": "typography", "label": "타이포그래피", "en": "typography type scale font size", "words": ("typography", "type scale", "font size", "타이포", "글꼴", "폰트")},
    {"key": "spacing", "label": "간격·여백", "en": "spacing scale padding margin layout", "words": ("spacing", "padding", "margin", "whitespace", "간격", "여백", "패딩")},
    {"key": "layout", "label": "레이아웃·그리드", "en": "layout grid breakpoints", "words": ("layout", "grid", "breakpoint", "레이아웃", "그리드")},
    {"key": "color", "label": "색상", "en": "color palette contrast tokens", "words": ("color", "colour", "palette", "contrast", "색상", "컬러")},
    {"key": "hierarchy", "label": "시각적 위계·게슈탈트", "en": "visual hierarchy gestalt principles", "words": ("hierarchy", "gestalt", "proximity", "위계", "게슈탈트")},
    {"key": "uxlaws", "label": "UX 법칙", "en": "UX laws Fitts Hick cognitive load", "words": ("fitts", "hick", "cognitive load", "jakob", "ux law", "법칙")},
    {"key": "a11y", "label": "접근성", "en": "accessibility WCAG ARIA guidelines", "words": ("accessib", "wcag", "aria", "screen reader", "접근성")},
    {"key": "responsive", "label": "반응형", "en": "responsive design breakpoints adaptive", "words": ("responsive", "adaptive", "mobile first", "반응형")},
    {"key": "interaction", "label": "인터랙션", "en": "interaction design states feedback motion", "words": ("interaction", "motion", "animation", "feedback", "인터랙션", "모션")},
    {"key": "components", "label": "컴포넌트", "en": "component design guidelines button form table", "words": ("component", "button", "input", "form", "컴포넌트")},
    {"key": "tokens", "label": "디자인 토큰", "en": "design tokens naming theming", "words": ("design token", "tokens", "theming", "토큰")},
    {"key": "ia", "label": "정보 구조", "en": "information architecture navigation", "words": ("information architecture", "navigation", "taxonomy", "정보 구조", "내비게이션")},
    {"key": "dataviz", "label": "데이터 시각화", "en": "data visualization chart guidelines", "words": ("data visualization", "chart", "dataviz", "데이터 시각화", "차트")},
    {"key": "enterprise", "label": "업무용 UI(ERP·CRM·대시보드)", "en": "enterprise UI data table dashboard admin density", "words": ("erp", "crm", "dashboard", "admin", "data table", "density", "enterprise", "대시보드", "관리자")},
)

_URL = re.compile(r"https?://\S+")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", _URL.sub(" ", text).lower())


def _mentioned(haystack: str, alias: str) -> bool:
    alias = alias.lower()
    if re.fullmatch(r"[a-z0-9 .\-/]+", alias):
        # Short Latin aliases (hig, eui, tds) must stand alone, or "highlight" would match "hig".
        return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", haystack) is not None
    return alias in haystack


def targets_in(text: str) -> list[Target]:
    """Targets the text names, in catalog order. Bare generic words (spectrum, garden, base) need a design context."""
    haystack = _norm(text)
    generic = {"spectrum", "garden", "primer", "redwood", "lightning", "polaris", "clarity", "blueprint", "eui", "atlassian"}
    design_context = any(word in haystack for word in ("design system", "디자인 시스템", "ui/ux", "design", "디자인"))
    found = []
    for target in TARGETS:
        for alias in target.aliases:
            if _mentioned(haystack, alias) and (alias not in generic or design_context):
                found.append(target)
                break
    return found


def topics_in(text: str) -> list[dict[str, Any]]:
    """Topics the goal names. Text that names none gets no topic: the caller keeps its generic research dimensions."""
    haystack = _norm(text)
    return [topic for topic in TOPICS if any(word in haystack for word in topic["words"])]


def target_for_host(host: str) -> Target | None:
    host = host.lower()
    for target in TARGETS:
        if any(host == h or host.endswith("." + h) for h in target.hosts):
            return target
    return None


def topics_matching(text: str) -> list[str]:
    """Topic keys whose words occur in a collected document, used for per-topic coverage."""
    haystack = text.lower()
    return [topic["key"] for topic in TOPICS if sum(haystack.count(word) for word in topic["words"]) >= 2]


# Crawl boundary per target when the root sits inside a larger host (developer.apple.com, w3.org, ant.design).
PATH_PREFIX = {
    "hig": "/design/", "antd": "/docs/spec/", "nngroup": "/articles/", "ixdf": "/literature", "baymard": "/blog",
    "wcag": "/WAI/", "aria": "/WAI/ARIA/apg/", "nhs": "/design-system", "polaris": "/docs/api/polaris", "fiori": "/design-system/",
    "blueprint": "/docs/", "material": "/",
}

EXTRA_RESEARCH = (
    ("google research", "Google Research human computer interaction user interface design study"),
    ("acm chi", "ACM CHI user interface design guidelines empirical study"),
)
INITIAL_QUERIES = 30
CATALOG_SEARCH_BUDGET = 80


def plan_parts(goal: str, targets: list[Target], topics: list[dict[str, Any]]) -> dict[str, Any]:
    """Sources, queries and coverage for a goal that names targets and/or UI/UX topics.

    Sources are the verified roots. Queries are clean: topic or target phrases, never the goal sentence itself.
    Coverage has one entry per named target and per named topic, so the screen can say which of them are still empty."""
    sources, queries, query_topics = [], [], {}

    def add_query(query: str, key: str) -> None:
        if query.lower() not in {q.lower() for q in queries}:
            queries.append(query)
            query_topics[query] = key

    for topic in topics:
        add_query(f"{topic['en']} design system guidelines", topic["key"])
        add_query(f"{topic['label']} 디자인 시스템 가이드라인 근거", topic["key"])
    for target in targets:
        for url in target.roots:
            adapter = "github" if (urlsplit(url).hostname or "") == "github.com" else "website"
            sources.append({"url": url, "adapter": adapter, "provenance": "catalog", "topics": [f"t:{target.key}"], "target": target.key,
                            **({"path_prefix": PATH_PREFIX[target.key]} if target.key in PATH_PREFIX else {})})
        for url in target.blog:
            sources.append({"url": url, "adapter": "website", "provenance": "catalog", "topics": [f"t:{target.key}"], "target": target.key})
        if not target.roots:
            add_query(f"{target.name} documentation guidelines", f"t:{target.key}")
        add_query(f"{target.name} design philosophy principles engineers designers", f"t:{target.key}")
    haystack = _norm(goal)
    for needle, query in EXTRA_RESEARCH:
        if needle in haystack:
            add_query(query, "ux_research")
    coverage = [{"topic": t.name, "key": f"t:{t.key}", "kind": t.kind, "state": "unexplored", "sources": 0, "documents": 0, "hosts": 0} for t in targets]
    coverage += [{"topic": t["label"], "key": t["key"], "kind": "topic", "state": "unexplored", "sources": 0, "documents": 0, "hosts": 0} for t in topics]
    if any(key == "ux_research" for key in query_topics.values()):
        coverage.append({"topic": "UX 연구(Google Research·ACM CHI)", "key": "ux_research", "kind": "topic", "state": "unexplored", "sources": 0, "documents": 0, "hosts": 0})
    ordered = list(queries)
    plan_topics = [{"key": c["key"], "label": c["topic"], "subject": "", "subject_en": ""} for c in coverage]
    return {"sources": sources, "topics": plan_topics, "queries": ordered[:INITIAL_QUERIES], "deferred_queries": ordered[INITIAL_QUERIES:],
            "query_topics": query_topics, "coverage": coverage}
