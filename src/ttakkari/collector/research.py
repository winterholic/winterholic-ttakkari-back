"""Autonomous-research logic for topic-only missions: pure functions, no network and no database.

A plan only states intended coverage. Everything here that claims a state (coverage, relations, stop reason) takes
observed collector evidence as input, so the same code serves the live view and the durable follow-up rounds.
No model is called: decomposition, term learning and novelty checks are deterministic, which keeps the research
loop inside the per-run budget (`model_calls` is 0 by construction).
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

MAX_ROUNDS = 3
INITIAL_QUERY_CAP = 10
MAX_FOLLOWUP_QUERIES = 6
MAX_PAPERS = 20
COVERED_DOCUMENTS = 2
COVERED_HOSTS = 2

# Particles stripped from the end of a Korean token. Only applied when at least two characters remain.
_PARTICLES = ("에서는", "으로는", "에서", "으로", "에는", "이란", "라는", "에게", "까지", "부터", "와의", "과의", "에", "의", "과", "와", "을", "를", "은",
              "는", "이", "가", "로", "도", "만", "및")
_REQUEST_WORDS = {"조사", "수집", "정리", "분석", "찾아", "조사해줘", "수집해줘", "정리해줘", "해줘", "해주세요", "관련", "대한", "대해", "위한", "폭넓게",
                  "자료", "연구", "내용", "알려줘", "찾아줘", "조사해", "정리해", "하자", "좀", "및", "그리고", "에", "다양한", "여러", "전반", "전반적"}

# Aspects a user may ask for explicitly; each becomes its own coverage topic.
ASPECTS: dict[str, dict[str, Any]] = {
    "cost": {"label": "비용·효율", "words": ("비용", "효율", "가격", "토큰"), "ko": "비용 효율", "en": "cost efficiency",
             "alt_ko": "비용 절감 사례", "alt_en": "cost latency tradeoff"},
    "accuracy": {"label": "정확도·평가", "words": ("정확도", "정확성", "평가", "벤치마크", "성능"), "ko": "정확도 평가 벤치마크",
                 "en": "accuracy evaluation benchmark", "alt_ko": "평가 지표 비교 실험", "alt_en": "empirical evaluation results"},
    "comparison": {"label": "비교·대안", "words": ("비교", "대안", "차이", "장단점"), "ko": "비교 대안 차이", "en": "comparison alternatives",
                   "alt_ko": "장단점 비교 분석", "alt_en": "versus comparative study"},
    "security": {"label": "보안·안전", "words": ("보안", "안전", "취약점", "위험"), "ko": "보안 안전 위험", "en": "security safety risks",
                 "alt_ko": "취약점 공격 사례", "alt_en": "threat model vulnerabilities"},
    "scale": {"label": "확장성·운영", "words": ("확장", "운영", "배포", "대규모"), "ko": "확장성 운영 사례", "en": "scalability production deployment",
              "alt_ko": "운영 배포 경험", "alt_en": "production lessons learned"},
}
BASELINE: list[dict[str, str]] = [
    {"key": "theory", "label": "핵심 이론·용어", "ko": "개념 이론 정리", "en": "theory fundamentals survey",
     "alt_ko": "입문 개요 설명", "alt_en": "tutorial overview"},
    {"key": "papers", "label": "논문·원전", "ko": "논문", "en": "research paper arxiv",
     "alt_ko": "서베이 논문 리뷰", "alt_en": "survey paper review"},
    {"key": "practice", "label": "구현·실무 사례", "ko": "구현 사례 실무", "en": "open source implementation case study",
     "alt_ko": "github 예제 구현", "alt_en": "github example implementation"},
    {"key": "limits", "label": "설계 패턴·한계", "ko": "설계 패턴 한계", "en": "design patterns limitations",
     "alt_ko": "문제점 실패 사례", "alt_en": "pitfalls failure modes"},
]

# Small curated glossary. Terms outside it are learned from collected documents ("한글(English)" pairs), never guessed.
GLOSSARY: dict[str, str] = {
    "검색 증강 생성": "retrieval augmented generation", "대규모 언어 모델": "large language model", "언어 모델": "language model",
    "하네스 엔지니어링": "harness engineering", "에이전트": "agent", "프롬프트 엔지니어링": "prompt engineering", "프롬프트": "prompt",
    "임베딩": "embedding", "벡터 검색": "vector search", "강화 학습": "reinforcement learning", "미세 조정": "fine tuning",
    "월드 모델": "world model", "피지컬 AI": "physical AI", "환각": "hallucination", "지식 그래프": "knowledge graph",
    "트랜스포머": "transformer", "컨텍스트 윈도우": "context window", "컨텍스트 엔지니어링": "context engineering",
    "멀티 에이전트": "multi agent", "도구 호출": "tool calling", "추론": "reasoning", "로봇": "robot", "자율 주행": "autonomous driving",
    "기계 학습": "machine learning", "머신러닝": "machine learning", "딥러닝": "deep learning", "신경망": "neural network",
    "데이터베이스": "database", "분산 시스템": "distributed system", "소프트웨어 설계": "software design",
}

_LATIN_STOP = frozenset(
    ["the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "have", "has", "had", "not", "but", "can", "will", "would", "could", "should", "into", "over", "under", "about", "than", "then", "their", "there", "these", "those", "which", "while", "where", "when", "what", "who", "how", "why", "also", "such", "more", "most", "other", "some", "any", "all", "our", "out", "use", "used", "using", "may", "might", "one", "two", "new", "via", "per", "each", "both", "between", "within", "without", "across", "paper", "papers", "work", "results", "result", "show", "shows", "shown", "based"])
_KO_STOP = frozenset(
    ["있다", "있는", "있으며", "하는", "한다", "하여", "대한", "통해", "위해", "따라", "경우", "때문", "그리고", "하지만", "또한", "이를", "이는", "것이다", "수있다", "같은", "이러한", "다양한", "대하여", "관련", "사용", "기반", "제공", "가능", "필요", "결과", "방법", "문제", "이후", "이전", "우리", "본문", "다음", "아래", "위한", "에서", "으로", "이다", "입니다"])
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>\]\)\}]+)", re.IGNORECASE)
_ARXIV_RE = re.compile(r"(?:arxiv\.org/(?:abs|pdf|html)/|arxiv:\s*)((?:\d{4}\.\d{4,5})(?:v\d+)?|(?:[a-z\-]+(?:\.[A-Za-z]{2})?/\d{7})(?:v\d+)?)", re.IGNORECASE)
_PAIR_RE = re.compile(r"([가-힣][가-힣 ]{1,24}[가-힣])\s*[\(（]\s*([A-Za-z][A-Za-z0-9\- ]{2,60}[A-Za-z0-9])(?:\s*[,，]\s*[A-Z][A-Za-z0-9]{1,10})?\s*[\)）]")
_URL_RE = re.compile(r"https?://[^\s<>\[\]{}\"']+")


def _tokens_ko(text: str) -> list[str]:
    out = []
    for raw in re.findall(r"[가-힣]{2,}", text):
        token = raw
        for particle in _PARTICLES:
            if token.endswith(particle) and len(token) - len(particle) >= 2:
                token = token[: -len(particle)]
                break
        out.append(token)
    return out


def _clean_word(word: str) -> str:
    for particle in _PARTICLES:
        if word.endswith(particle) and len(word) - len(particle) >= 2 and re.fullmatch(r"[가-힣]+", word):
            return word[: -len(particle)]
    return word


def subject_of(goal: str) -> tuple[str, list[str]]:
    """The topic phrase with request wording and aspect words removed, plus the aspect keys the user named."""
    text = _URL_RE.sub(" ", goal)
    words = [word for word in re.split(r"[\s,·/]+", text.strip()) if word]
    aspects, kept = [], []
    for raw in words:
        word = raw if any(raw in spec["words"] for spec in ASPECTS.values()) else _clean_word(raw)
        key = next((name for name, spec in ASPECTS.items() if word in spec["words"]), None)
        if key:
            if key not in aspects:
                aspects.append(key)
            continue
        if word in _REQUEST_WORDS or word in {"이론", "논문", "실무", "구현", "사례", "한계", "설계", "패턴"} or re.fullmatch(r"[가-힣]+해(줘|주세요)\W*", word):
            continue
        kept.append(word.strip(".,!?"))
    subject = " ".join(word for word in kept if word).strip() or " ".join(_clean_word(word) for word in words)[:120]
    return subject[:120], aspects


def translate(subject: str, learned: dict[str, str] | None = None, *, partial: bool = False) -> str | None:
    """English form of the subject from the glossary and learned pairs.

    None while any Korean word is untranslated, unless `partial` keeps the Latin/translated words that remain."""
    table = {**GLOSSARY, **(learned or {})}
    out = subject
    for ko in sorted(table, key=len, reverse=True):
        out = out.replace(ko, table[ko])
    if re.search(r"[가-힣]", out):
        out = re.sub(r"[가-힣]+", " ", out) if partial else ""
    return " ".join(out.split()) or None


def decompose_topics(goal: str, *, context: str = "", learned: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Coverage topics: the baseline research dimensions plus every aspect the user asked about (listed first)."""
    subject, aspects = subject_of(goal)
    if context:
        aspects += [name for name in subject_of(context)[1] if name not in aspects]
    subject_en = translate(subject, learned, partial=True)
    topics = [{"key": name, "label": ASPECTS[name]["label"], "ko": ASPECTS[name]["ko"], "en": ASPECTS[name]["en"],
               "alt_ko": ASPECTS[name]["alt_ko"], "alt_en": ASPECTS[name]["alt_en"]} for name in aspects[:3]]
    topics = [BASELINE[0], *topics, *BASELINE[1:]]
    return [{**topic, "subject": subject, "subject_en": subject_en} for topic in topics]


def _query(subject: str | None, suffix: str) -> str | None:
    return f"{subject} {suffix}".strip() if subject else None


def queries_for_topics(topics: list[dict[str, Any]], *, alternate: bool = False) -> list[str]:
    """Korean then English query per topic, de-duplicated in topic order."""
    seen, queries = set(), []
    for topic in topics:
        suffix_ko, suffix_en = (topic["alt_ko"], topic["alt_en"]) if alternate else (topic["ko"], topic["en"])
        for query in (_query(topic["subject"], suffix_ko), _query(topic.get("subject_en"), suffix_en)):
            if query and query.lower() not in seen:
                seen.add(query.lower())
                queries.append(query)
    return queries


def initial_plan_parts(goal: str, context: str) -> dict[str, Any]:
    """Topics, the first-round queries and the deferred remainder for a topic-only mission."""
    topics = decompose_topics(goal, context=context)
    query_topics: dict[str, str] = {}
    ordered: list[str] = []
    for topic in topics:
        for query in queries_for_topics([topic]):
            if query not in query_topics:
                query_topics[query] = topic["key"]
                ordered.append(query)
    claims = context_claims(context)
    if claims:  # user-supplied statements become their own search topic so they are checked against outside sources
        terms = [term for term, _ in Counter(t for c in claims[:6] for t in content_terms(c["text"])).most_common(4)]
        if terms:
            query = " ".join([topics[0]["subject"], *terms])[:200]
            if query not in query_topics:
                topics.append({"key": "context", "label": "사용자 맥락 검증", "ko": "", "en": "", "alt_ko": "", "alt_en": "",
                               "subject": topics[0]["subject"], "subject_en": topics[0]["subject_en"]})
                query_topics[query] = "context"
                ordered.insert(min(len(ordered), 4), query)
    return {
        "topics": [{k: v for k, v in topic.items() if k in ("key", "label", "subject", "subject_en")} for topic in topics],
        "queries": ordered[:INITIAL_QUERY_CAP], "deferred_queries": ordered[INITIAL_QUERY_CAP:],
        "query_topics": query_topics,
        "coverage": [{"topic": topic["label"], "key": topic["key"], "state": "unexplored", "sources": 0, "documents": 0, "hosts": 0}
                     for topic in topics],
    }


def extract_paper_references(text: str) -> list[dict[str, str]]:
    """DOI and arXiv identifiers in order of appearance, canonicalised and de-duplicated."""
    found: list[tuple[int, dict[str, str]]] = []
    for match in _DOI_RE.finditer(text):
        doi = match.group(1).rstrip(".,;:)]}'\"")
        if doi.lower().startswith("10.48550/arxiv."):
            identifier = doi.split(".", 2)[2] if doi.count(".") >= 2 else doi
            found.append((match.start(), {"kind": "arxiv", "identifier": identifier.removeprefix("arXiv."), "canonical_url": ""}))
        else:
            found.append((match.start(), {"kind": "doi", "identifier": doi, "canonical_url": f"https://doi.org/{doi}"}))
    for match in _ARXIV_RE.finditer(text):
        found.append((match.start(), {"kind": "arxiv", "identifier": match.group(1), "canonical_url": ""}))
    result, seen = [], set()
    for _, ref in sorted(found, key=lambda pair: pair[0]):
        key = (ref["kind"], ref["identifier"].lower())
        if key in seen:
            continue
        seen.add(key)
        if ref["kind"] == "arxiv":
            ref = {**ref, "canonical_url": f"https://arxiv.org/abs/{ref['identifier']}"}
        result.append(ref)
    return result


def paper_reference_of_url(url: str) -> dict[str, str] | None:
    refs = extract_paper_references(url)
    return refs[0] if refs else None


def extract_term_pairs(text: str) -> dict[str, str]:
    """Korean terms that the documents themselves gloss in English, e.g. `검색 증강 생성(Retrieval-Augmented Generation)`.

    The Korean term is the last N Korean words before the parenthesis, N being the English word count, so leading
    particles and verbs ("…과 하네스 엔지니어링(Harness Engineering)") are not absorbed. Acronym-only glosses are skipped."""
    pairs: dict[str, str] = {}
    for ko, en in _PAIR_RE.findall(text):
        english = " ".join(en.replace("-", " ").lower().split())
        count = len(english.split())
        if not 1 <= count <= 6 or (count == 1 and en.isupper()):
            continue
        words = ko.split()[-count:]
        if len(" ".join(words)) >= 2:
            pairs.setdefault(" ".join(words), english)
    return pairs


def content_terms(text: str) -> list[str]:
    """Distinctive lowercase terms: Latin words/acronyms and Korean stems, without stop words."""
    terms = [word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text) if word.lower() not in _LATIN_STOP]
    terms += [token for token in _tokens_ko(text) if token not in _KO_STOP and token not in _REQUEST_WORDS]
    return list(dict.fromkeys(terms))


def _query_tokens(query: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9\-]+|[가-힣]+", query.lower()))


def is_novel_query(query: str, existing: Iterable[str], threshold: float = 0.75) -> bool:
    tokens = _query_tokens(query)
    if not tokens:
        return False
    for other in existing:
        theirs = _query_tokens(other)
        if theirs and len(tokens & theirs) / len(tokens | theirs) >= threshold:
            return False
    return True


def _anchored(text: str, anchors: list[str], radius: int = 300) -> str:
    """Only the stretches of a document close to the research subject, so filler vocabulary elsewhere is not mistaken for a keyword."""
    low, spans = text.lower(), []
    for anchor in anchors:
        start = 0
        while (index := low.find(anchor, start)) != -1 and len(spans) < 200:
            spans.append((max(0, index - radius), index + len(anchor) + radius))
            start = index + len(anchor)
    merged: list[list[int]] = []
    for begin, end in sorted(spans):
        if merged and begin <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([begin, end])
    return " ".join(text[begin:end] for begin, end in merged)


_TITLE_PHRASE_RE = re.compile(r"\b(?:[A-Z][a-z]{2,}[\s\-]){1,4}[A-Z][a-z]{2,}\b")
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Za-z]*[A-Z][A-Za-z0-9]*\b|\b[A-Z]{2,6}\b")


def candidate_terms(documents: list[dict[str, Any]], exclude: Iterable[str], *, anchors: Iterable[str] = (), limit: int = 6) -> list[dict[str, Any]]:
    """New terminology seen near the subject in at least two independent documents (or three times in a lone one).

    Precision over recall: only acronyms/camel-case names and Title-Case phrases qualify. Plain lowercase word pairs are
    not terminology evidence (templated boilerplate repeats across documents), and every query costs search budget."""
    skip = {term for text in exclude for term in content_terms(text)}
    anchor_list = [anchor.lower() for anchor in anchors if len(anchor) >= 3]
    df: Counter[str] = Counter()
    tf: Counter[str] = Counter()
    topics_of: dict[str, set[str]] = {}
    for doc in documents:
        text = _anchored(doc.get("title", "") + "\n" + doc["text"][:50_000], anchor_list) if anchor_list else doc["text"][:20_000]
        if not text:
            continue
        counts = Counter(t.lower() for t in _ACRONYM_RE.findall(text) if len(t) >= 3)
        counts.update(" ".join(m.lower().replace("-", " ").split()) for m in _TITLE_PHRASE_RE.findall(text))
        for term, count in counts.items():
            if term in skip or all(part in skip or part in _LATIN_STOP for part in term.split()):
                continue
            tf[term] += count
            df[term] += 1
            topics_of.setdefault(term, set()).update(doc.get("topics") or [])
    ranked = sorted((term for term in df if df[term] >= 2 or tf[term] >= 3), key=lambda t: (-df[t] * math.log1p(tf[t]), t))
    return [{"term": term, "documents": df[term], "topics": sorted(topics_of[term])} for term in ranked[:limit]]


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    parts = host.split(".")
    return ".".join(parts[-3:] if len(parts) >= 3 and parts[-2] in {"co", "ac", "or", "go", "com"} else parts[-2:]) if len(parts) >= 2 else host


def compute_coverage(plan: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-topic coverage from what was actually found and collected, never from what was planned.

    states: unexplored (no query ran), no_candidates (queries ran, nothing found), sources_found (found, nothing
    accepted yet), unavailable (found but every attempt held/failed/blocked), thin (accepted, but fewer than
    two documents or one host), covered (>= 2 accepted documents from >= 2 hosts)."""
    history = [entry for entry in plan.get("search_history") or [] if entry.get("status", "ok") == "ok"]
    ran = Counter(plan.get("query_topics", {}).get(entry["query"]) for entry in history)
    sources: dict[str, set[str]] = {}
    for source in plan.get("sources", []):
        for key in source.get("topics") or []:
            sources.setdefault(key, set()).add(source["url"])
    accepted: dict[str, list[dict[str, Any]]] = {}
    attempted: Counter[str] = Counter()
    for item in items:
        for key in item.get("topics") or []:
            attempted[key] += 1
            if item["status"] == "accepted":
                accepted.setdefault(key, []).append(item)
    out = []
    for entry in plan.get("coverage", []):
        key = entry.get("key") or entry["topic"]
        docs = accepted.get(key, [])
        hosts = {host_of(doc["url"]) for doc in docs}
        if docs:
            state = "covered" if len(docs) >= COVERED_DOCUMENTS and len(hosts) >= COVERED_HOSTS else "thin"
        elif attempted[key]:
            state = "unavailable"
        elif sources.get(key):
            state = "sources_found"
        elif ran[key]:
            state = "no_candidates"
        else:
            state = "unexplored"
        out.append({**entry, "state": state, "sources": len(sources.get(key, ())), "documents": len(docs), "hosts": len(hosts)})
    return out


def coverage_gaps(coverage: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"topic": entry["topic"], "state": entry["state"]} for entry in coverage if entry["state"] != "covered"]


def followups(plan: dict[str, Any], documents: list[dict[str, Any]], coverage: list[dict[str, Any]], *, budget_left: int) -> dict[str, Any]:
    """What to search next, derived from the collected documents and the coverage that is still missing."""
    known_queries = [*plan.get("queries", []), *(entry["query"] for entry in plan.get("search_history") or [])]
    topics = {topic["key"]: topic for topic in plan.get("topics", [])}
    weak = [entry["key"] for entry in coverage if entry["state"] not in ("covered", "unexplored") and entry.get("key") in topics]
    pairs: dict[str, str] = {}
    cited: dict[tuple[str, str], dict[str, Any]] = {}
    for doc in documents:
        pairs.update(extract_term_pairs(doc["text"]))
        for ref in extract_paper_references(doc["text"][:200_000]):
            entry = cited.setdefault((ref["kind"], ref["identifier"].lower()), {**ref, "topics": [], "cited_by": []})
            entry["topics"] = sorted({*entry["topics"], *(doc.get("topics") or [])})
            entry["cited_by"] = [*entry["cited_by"], doc["url"]][:5]
    refs = list(cited.values())
    learned = {**(plan.get("glossary") or {}), **pairs}
    new_pairs = {ko: en for ko, en in pairs.items() if ko not in (plan.get("glossary") or {})}
    subject = next(iter(topics.values()), {}).get("subject", "")
    subject_en = translate(subject, learned, partial=True)
    buckets: dict[str, list[tuple[str, str, str]]] = {"deferred_topic": [], "new_keyword": [], "learned_translation": [], "weak_topic_alt": []}

    def propose(query: str | None, topic: str, reason: str) -> None:
        taken = [q for bucket in buckets.values() for q, _, _ in bucket]
        if query and topic and is_novel_query(query, [*known_queries, *taken]):
            buckets[reason].append((query, topic, reason))

    for query in plan.get("deferred_queries", []):
        propose(query, plan.get("query_topics", {}).get(query, ""), "deferred_topic")
    anchors = [subject, *(w for w in (subject_en or "").split() if w.lower() not in _LATIN_STOP)]
    words = [w for w in (subject_en or "").split() if w.lower() not in _LATIN_STOP]
    initials = "".join(w[0] for w in words) if len(words) >= 2 else ""  # the subject's own acronym is not a new keyword
    terms = candidate_terms(documents, [plan.get("goal", ""), subject, initials, *known_queries], anchors=anchors) if subject else []
    for term in terms:
        latin = bool(re.fullmatch(r"[\x00-\x7f]+", term["term"]))
        base = subject_en if latin else subject
        propose(_query(base, term["term"]), (term["topics"] or weak or [next(iter(topics), "")])[0], "new_keyword")
    first = next(iter(topics.values()), {})
    if subject_en and not first.get("subject_en"):  # a gloss found in the documents makes English queries possible for every topic
        for key, topic in topics.items():
            spec = BASELINE_BY_KEY.get(key) or ASPECT_BY_LABEL.get(topic["label"])
            if spec:
                propose(_query(subject_en, spec["en"]), key, "learned_translation")
    for key in weak:  # an explored topic that still lacks collected material gets differently worded queries
        alt = BASELINE_BY_KEY.get(key) or ASPECT_BY_LABEL.get(topics[key]["label"])
        if alt and subject:
            propose(_query(subject, alt.get("alt_ko", "")), key, "weak_topic_alt")
            propose(_query(subject_en, alt.get("alt_en", "")), key, "weak_topic_alt")
    wanted = []  # round-robin so one source of ideas cannot crowd out the others
    while any(buckets.values()):
        for bucket in buckets.values():
            if bucket:
                wanted.append(bucket.pop(0))
    return {"queries": wanted[:min(MAX_FOLLOWUP_QUERIES, max(0, budget_left))], "papers": refs, "glossary": new_pairs, "terms": terms}


BASELINE_BY_KEY = {topic["key"]: topic for topic in BASELINE}
ASPECT_BY_LABEL = {spec["label"]: spec for spec in ASPECTS.values()}


def decide_stop(plan: dict[str, Any], state: dict[str, Any], *, now: datetime, started_at: datetime, coverage: list[dict[str, Any]],
                searches_used: int, new_documents: int | None, followup_count: int) -> str | None:
    """The reason a research run ends, or None to continue. Provider failure is never reported as a normal stop."""
    budget = plan["budget"]
    if all(entry["state"] == "covered" for entry in coverage) and coverage:
        return "coverage_satisfied"
    if (now - started_at).total_seconds() >= budget.get("seconds", 1800):
        return "time_budget"
    if searches_used >= budget.get("searches", 16):
        return "search_budget"
    if state.get("round", 0) >= MAX_ROUNDS:
        return "round_limit"
    if followup_count == 0:
        return "no_novel_queries"
    if state.get("round", 0) >= 1 and new_documents == 0:
        return "diminishing_returns"
    return None


# ---- user context -> collected material relations ---------------------------------------------------------------

def context_claims(text: str) -> list[dict[str, str]]:
    """The user's own statements, each kept as provenance=user_provided and unverified."""
    claims, seen = [], set()
    for part in re.split(r"(?<=[.!?。])\s+|\n+", text or ""):
        sentence = " ".join(part.split())
        if len(sentence) < 12:
            continue
        key = hashlib.sha256(sentence.lower().encode()).hexdigest()[:8]
        if key in seen:
            continue
        seen.add(key)
        claims.append({"id": f"c-{key}", "text": sentence[:500], "provenance": "user_provided", "fact_check_status": "unverified"})
        if len(claims) >= 40:
            break
    return claims


def link_context(claims: list[dict[str, str]], targets: list[dict[str, Any]], *, per_claim: int = 3) -> dict[str, Any]:
    """Relations between user claims and collected material.

    `user_cited` is exact (the claim names the source URL). `lexical_overlap` only says the document shares distinctive
    terms with the claim: a lead to check, never support or refutation, so every relation stays unverified."""
    relations: list[dict[str, Any]] = []
    linked: set[str] = set()
    prepared = [(target, set(content_terms(target.get("title", "") + " " + target["text"][:30_000]))) for target in targets]
    for claim in claims:
        urls = {url.rstrip(".,;:!?)）。，") for url in _URL_RE.findall(claim["text"])}
        for target in targets:
            if target["url"] in urls or target.get("final_url") in urls:
                relations.append(_relation(claim, target, "user_cited", [], None))
                linked.add(claim["id"])
        terms = set(content_terms(claim["text"]))
        if len(terms) < 2:
            continue
        scored = []
        for target, vocabulary in prepared:
            matched = sorted(terms & vocabulary)
            if len(matched) >= 2 and len(matched) / len(terms) >= 0.34:
                scored.append((len(matched), target, matched))
        for _, target, matched in sorted(scored, key=lambda row: (-row[0], row[1]["url"]))[:per_claim]:
            relations.append(_relation(claim, target, "lexical_overlap", matched[:8], _snippet(target["text"], matched)))
            linked.add(claim["id"])
    return {"claims": [{k: v for k, v in claim.items() if k in ("id", "text")} for claim in claims], "relations": relations[:120],
            "unlinked": [claim["id"] for claim in claims if claim["id"] not in linked], "provenance": "user_provided",
            "verification": "unverified", "documents": len(targets)}


def _relation(claim, target, kind, matched, snippet) -> dict[str, Any]:
    return {"claim_id": claim["id"], "type": kind, "matched_terms": matched, "snippet": snippet,
            "target": {k: target.get(k) for k in ("kind", "url", "title", "document_id", "identifier") if target.get(k)},
            "verification": "unverified"}


def _snippet(text: str, matched: list[str]) -> str | None:
    best, best_score = None, 0
    for sentence in re.split(r"(?<=[.!?。])\s+|\n+", text[:60_000]):
        lowered = sentence.lower()
        score = sum(term in lowered for term in matched)
        if score > best_score:
            best, best_score = sentence.strip(), score
    return best[:240] if best else None


def classify_search_error(exc: BaseException) -> dict[str, Any]:
    """A provider failure with a stable code. Distinct from an empty result, which is a legitimate (if useless) answer."""
    from ttakkari.collector.fetcher import FetchError
    from ttakkari.collector.netguard import UrlRejected

    if isinstance(exc, FetchError):
        # Anti-bot challenges cannot be resolved by automated retries; keep them non-retryable
        # so they do not exhaust attempt budgets and stall subsequent collection of valid sources.
        is_challenge = exc.code == "search_provider_challenge"
        return {"code": exc.code, "retryable": False if is_challenge else bool(exc.retryable)}
    if isinstance(exc, UrlRejected):
        return {"code": "search_url_rejected", "retryable": False}
    if isinstance(exc, TimeoutError):
        return {"code": "timeout", "retryable": True}
    if exc.__class__.__module__.startswith("lxml") or isinstance(exc, ValueError):
        return {"code": "provider_response_unparseable", "retryable": True}
    return {"code": exc.__class__.__name__[:60], "retryable": True}


def needs_rounds(plan: dict[str, Any]) -> bool:
    """Follow-up rounds exist for topic-only research and for missions that carry user context to relate to the results."""
    return plan.get("mode") in ("research", "mixed") and bool(plan.get("topics") or (plan.get("context") or {}).get("text"))
