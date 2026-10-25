"""엔터티 동일성 호환 규칙: 동음이의어 표제어, 도메인·종류(kind) 호환, 충돌 슬롯 키.

이 규칙은 '자동 병합을 허용할 수 있는가'만 판단한다. 불호환이면 병합하지 않고 후보로 남긴다.
"""

from __future__ import annotations

# 동음이의어(Homonym) 표제어: 등록된 도메인끼리 서로 다르면 같은 이름이어도 다른 대상이다.
# key: norm_name, value: {도메인: 개념 설명}. 시드 사전 검토(KB3-301 교정)에서 일반어와 겹치는 표제어를 더했다.
KNOWN_HOMONYMS: dict[str, dict[str, str]] = {
    "python": {"dev": "Python 프로그래밍 언어", "science": "비단뱀 (파이톤 파충류)", "general": "비단뱀 또는 일반 용어"},
    "파이썬": {"dev": "Python 프로그래밍 언어", "science": "비단뱀 (파이톤 파충류)", "general": "비단뱀 또는 일반 용어"},
    "go": {"dev": "Go 프로그래밍 언어 (Golang)", "general": "동사 go 또는 바둑(Baduk/Go)"},
    "고": {"dev": "Go 프로그래밍 언어 (Golang)", "general": "동사 go 또는 바둑(Baduk/Go)"},
    "rust": {"dev": "Rust 시스템 프로그래밍 언어", "science": "녹, 철의 산화물 (Rust)", "general": "녹 또는 부식"},
    "러스트": {"dev": "Rust 시스템 프로그래밍 언어", "science": "녹, 철의 산화물 (Rust)", "general": "녹 또는 부식"},
    "service": {"infra": "쿠버네티스 Service 리소스", "business": "고객 서비스 또는 비즈니스 용역"},
    "서비스": {"infra": "쿠버네티스 Service 리소스", "business": "고객 서비스 또는 비즈니스 용역"},
    "java": {"dev": "Java 객체지향 언어", "general": "자바 섬 또는 자바 커피"},
    "자바": {"dev": "Java 객체지향 언어", "general": "자바 섬 또는 자바 커피"},
    # --- 시드 검토로 추가 ---
    "swift": {"dev": "Swift 프로그래밍 언어", "business": "SWIFT 국제 금융 메시징망", "general": "빠른 또는 칼새"},
    "스위프트": {"dev": "Swift 프로그래밍 언어", "business": "SWIFT 국제 금융 메시징망", "general": "빠른 또는 칼새"},
    "ruby": {"dev": "Ruby 프로그래밍 언어", "general": "루비 보석"},
    "루비": {"dev": "Ruby 프로그래밍 언어", "general": "루비 보석"},
    "pulsar": {"infra": "Apache Pulsar 메시징", "science": "펄서(회전 중성자별)"},
    "펄사": {"infra": "Apache Pulsar 메시징", "science": "펄서(회전 중성자별)"},
    "spark": {"dev": "Apache Spark 분석 엔진", "general": "불꽃 또는 일반 용어"},
    "스파크": {"dev": "Apache Spark 분석 엔진", "general": "불꽃 또는 일반 용어"},
    "flux": {"infra": "Flux GitOps 도구", "science": "플럭스(물리량)", "general": "흐름 또는 일반 용어"},
    "플럭스": {"infra": "Flux GitOps 도구", "science": "플럭스(물리량)", "general": "흐름 또는 일반 용어"},
    "helm": {"infra": "Kubernetes Helm 패키지 매니저", "general": "배의 키 또는 일반 용어"},
    "헬름": {"infra": "Kubernetes Helm 패키지 매니저", "general": "배의 키 또는 일반 용어"},
    "loki": {"infra": "Grafana Loki 로그 시스템", "general": "북유럽 신화의 로키"},
    "로키": {"infra": "Grafana Loki 로그 시스템", "general": "북유럽 신화의 로키"},
    "secret": {"infra": "쿠버네티스 Secret 리소스", "general": "비밀 또는 일반 용어"},
    "시크릿": {"infra": "쿠버네티스 Secret 리소스", "general": "비밀 또는 일반 용어"},
    "job": {"infra": "쿠버네티스 Job 리소스", "business": "직업 또는 일자리"},
    "잡": {"infra": "쿠버네티스 Job 리소스", "business": "직업 또는 일자리"},
    "transformer": {"ai": "Transformer 신경망 구조", "science": "변압기(전기)", "general": "변환기 또는 일반 용어"},
    "트랜스포머": {"ai": "Transformer 신경망 구조", "science": "변압기(전기)", "general": "변환기 또는 일반 용어"},
    "gemini": {"ai": "Google Gemini 모델", "science": "쌍둥이자리", "general": "쌍둥이자리 또는 일반 용어"},
    "제미나이": {"ai": "Google Gemini 모델", "science": "쌍둥이자리", "general": "쌍둥이자리 또는 일반 용어"},
    "mistral": {"ai": "Mistral AI 모델", "science": "지중해 북서풍 미스트랄", "general": "지중해 북서풍"},
    "미스트랄": {"ai": "Mistral AI 모델", "science": "지중해 북서풍 미스트랄", "general": "지중해 북서풍"},
    "express": {"dev": "Express.js 웹 프레임워크", "general": "급행 또는 일반 용어"},
    "익스프레스": {"dev": "Express.js 웹 프레임워크", "general": "급행 또는 일반 용어"},
    "chroma": {"ai": "Chroma 벡터 DB", "general": "색채 또는 일반 용어"},
    "크로마": {"ai": "Chroma 벡터 DB", "general": "색채 또는 일반 용어"},
}


def is_domain_compatible(d1: str, d2: str, key_norm: str = "") -> bool:
    """두 도메인 간의 호환성 검사.

    동음이의어 사전(KNOWN_HOMONYMS)에 속한 표제어는 서로 다른 등록 도메인이면 비호환이다.
    일반적인 경우 'general'은 모든 도메인과 호환되고, dev/infra/ai는 기술 용어끼리 호환된다.
    """
    if d1 == d2:
        return True

    if key_norm and key_norm in KNOWN_HOMONYMS:
        homonym_domains = KNOWN_HOMONYMS[key_norm]
        if d1 in homonym_domains and d2 in homonym_domains:
            return False

    if d1 == "general" or d2 == "general":
        return True

    if {d1, d2} <= {"dev", "infra"}:
        return True

    return {d1, d2} <= {"dev", "ai"}


def is_kind_compatible(k1: str, k2: str) -> bool:
    """entity_kind 호환성. 종류가 다르면(technology vs concept 등) 자동 병합하지 않는다."""
    return k1 == k2


def disambiguated_norm(norm: str, domain: str) -> str:
    """kb_entities 유일 키(tenant, visibility, kind, norm_name) 슬롯이 이미 점유된 동음이의 엔터티용 norm_name.

    norm_name 은 정규화 과정에서 '~' 를 제거하지 않으므로 일반 표기와 충돌하지 않는다.
    """
    return f"{norm}~{domain}"
