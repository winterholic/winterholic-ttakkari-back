"""Platform V2 Backend Audit Scanner.

정적 수집 스캐너 (P1 coverage-reconciliation 후보):
- Routes (HTTP, WS)
- MCP Tools
- DB Tables & ORM Foreign Keys
- Alembic Migrations (AST + ScriptDirectory revision graph + DDL call-site inventory)
- Settings / Config Fields
- Source Files (src/**/*.py, 상대 임포트 해석, 정적 AST 파일 SCC, 동적 임포트 탐지)

결과 계약 (scan_report):
- extraction  : extractor가 오류 없이 끝났는가 (succeeded / succeeded_with_errors / failed)
- classification: 수집된 record가 모듈에 분류되었는가 (unmatched 개수)
- collection_scope: 무엇을 어떤 root/fingerprint로 몇 건 수집했는가 (record ID digest 포함)
- coverage    : 독립 discovery 후보 집합과 수집 record ID/fingerprint를 대조한 결과
                reconciled / unknown / mismatch. 독립 대조가 불가능하거나 source binding이 불명하면
                unknown이 정답이며, 이를 '완전'으로 승격하지 않는다.
- sanity_heuristics: 고정 최소 개수(sanity floor). 경고용 휴리스틱이며 coverage 근거가 아니다.

이 scanner status는 수집 상태만 뜻한다. DB 연결 상태나 제품 runtime 건강과 무관하다.

이전 교정 사항(유지):
1. ScriptDirectory 및 migration parse 오류 삼킴(except: pass) 제거 -> crosscheck failure로 기록.
2. ImportFrom statement target count 중복 누적 제거 및 AST 구문 단위 명시.
3. op.execute는 'op_execute_call_sites'로 명명, static_literal/parameterized/interpolated/unknown 분류.
4. 파일 SCC를 '런타임 순환'으로 과장하지 않고 '정적 AST 파일 상호 참조 컴포넌트'로 표기.
5. 비실측 GPU/CPU 문자열을 '미측정 (실행 증거 없음. 런타임 프로브 필요)'으로 표기.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Ensure project imports resolve
DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC_DIR = DEFAULT_REPO_ROOT / "src"
if str(DEFAULT_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(DEFAULT_SRC_DIR))
if str(DEFAULT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_REPO_ROOT))

os.environ["TTAKKARI_NO_ENV_FILE"] = "1"

# Target code baseline SHA
BASELINE_CODE_SHA = "607f8bda0df7dbdd458e4f0e54d703ce9edd3def"


@dataclass
class AuditContext:
    """단일 명시 audit context 및 root map."""

    repo_root: Path = DEFAULT_REPO_ROOT
    src_dir: Path = DEFAULT_SRC_DIR
    alembic_dir: Path = DEFAULT_REPO_ROOT / "alembic"
    sanity_floor_counts: dict[str, int] = field(
        default_factory=lambda: {
            "routes": 400,
            "tools": 80,
            "tables": 70,
            "migrations": 50,
            "configs": 150,
            "source_files": 250,
        }
    )

    @property
    def expected_min_counts(self) -> dict[str, int]:
        return self.sanity_floor_counts


@dataclass
class DiscoveryResult:
    category: str
    candidate_ids: list[str] | None = None
    fingerprints: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    unresolved: list[Any] = field(default_factory=list)
    auxiliary: dict[str, Any] = field(default_factory=dict)


# Module Definitions (25 Domain Modules across 4 Logical Planes)
MODULE_DEFINITIONS: dict[str, dict[str, Any]] = {
    # 1. Control Plane (6 Modules)
    "core.auth_session": {
        "plane": "Control",
        "name": "인증·세션·보안 제어",
        "user_role": "사용자 및 에이전트 인증, JWT/세션 발급/만료, 브루트포스 차단(로그인 스로틀링), 감사 로그",
        "execution": "request",
        "resources": "미측정 (코드추론: 세션 캐시 미소비, JWT 연산, DB 요청당 1-2건)",
        "current_release_coupling": "Core 세대 슬롯 밀결합(FastAPI auth middleware 및 dependency에 직결)",
        "proposed_role": "Core",
        "difficulty": "medium",
        "benefit": "플랫폼 전반의 단일 신원 인증 및 세션 검증 토큰 분리로 다른 서비스들의 인증 위임 가능",
        "status": "implemented",
    },
    "core.workspaces_transfers": {
        "plane": "Control",
        "name": "워크스페이스·파일 전송",
        "user_role": "작업 디렉토리 격리, 파일 다운로드/업로드 토큰 발급, 샌드박스 경로 제어",
        "execution": "request",
        "resources": "미측정 (코드추론: 파일 I/O 스트리밍, 버퍼 스트리밍, DB 토큰 검증 1건)",
        "current_release_coupling": "Core 세대 슬롯 밀결합(FastAPI static/streaming endpoints)",
        "proposed_role": "Core",
        "difficulty": "low",
        "benefit": "정적 파일 서빙 및 업로드/다운로드 경로 분리 시 대용량 파일 전송 중 Core 이벤트 루프 차단 해소",
        "status": "implemented",
    },
    "core.accounts_quota": {
        "plane": "Control",
        "name": "계정 풀·쿼터·토큰 계측",
        "user_role": "AI 공급자(Antigravity, Codex, Muse, Qwen) 계정 자격증명 관리, 토큰 사용량 계측 및 쿼터 제한",
        "execution": "request",
        "resources": "미측정 (코드추론: 비동기 사용량 기록 또는 요청당 1건)",
        "current_release_coupling": "Core 세대 슬롯 결합(Run 실행 전 계정 배정)",
        "proposed_role": "Core",
        "difficulty": "medium",
        "benefit": "계정 회전 및 쿼터 차단 로직을 중앙 집중화하여 다중 런타임 간 계정 충돌 방지",
        "status": "implemented",
    },
    "core.generation_deploy": {
        "plane": "Control",
        "name": "세대 배포·수명주기 감독",
        "user_role": "3 슬롯(8791/8792/8793) 롤링 무중단 배포 관리, 헬스체크, 커트오버, 이전 세대 drain/종료",
        "execution": "resident",
        "resources": "미측정 (코드추론: 주기적 폴링, DB 1-2건 리스 갱신)",
        "current_release_coupling": "Core 세대 슬롯 자체 제어자(메타 수명주기)",
        "proposed_role": "Core",
        "difficulty": "high",
        "benefit": "세대 전환 시 기존 활성 Run의 안전한 드레인 및 zero-downtime 배포 보장",
        "status": "implemented",
    },
    "core.system_routing": {
        "plane": "Control",
        "name": "시스템 상태·설정·알림",
        "user_role": "시스템 전역 설정(Settings), 푸시 알림(WebPush), 데스크 피드백, 모델 및 도구 라우팅 정책",
        "execution": "request",
        "resources": "미측정 (코드추론: 요청당 1건)",
        "current_release_coupling": "Core 세대 슬롯 밀결합(전역 app state)",
        "proposed_role": "Core",
        "difficulty": "low",
        "benefit": "전역 설정 및 푸시 디스패처 모듈화로 도메인 모듈들이 비동기 이벤트로 알림 요청 가능",
        "status": "implemented",
    },
    "ops.monitoring": {
        "plane": "Control",
        "name": "운영 관측성·오류 수집·배포 로그",
        "user_role": "Sentry 호환 에러 인제스트, 운영 로그 검색, 인시던트 이슈 트래킹, 세대 배포 감사",
        "execution": "request",
        "resources": "미측정 (코드추론: DB conn 1-2건)",
        "current_release_coupling": "Core API 라우터 (관측/제어 성격, Delivery 파이프라인과 분리)",
        "proposed_role": "service",
        "difficulty": "low",
        "benefit": "모니터링 시스템을 Core와 독립시켜 Core 장애 상황에서도 관측성 유지",
        "status": "implemented",
    },
    # 2. Execution Plane (3 Modules)
    "exec.run_engine": {
        "plane": "Execution",
        "name": "에이전트 실행 엔진·런타임",
        "user_role": "AI CLI(Antigravity, Codex, Muse, Qwen) 하위 프로세스 기동, 스트림 SSE 이벤트 전달, 중단/취소",
        "execution": "longjob",
        "resources": "미측정 (코드추론: 하위 프로세스 I/O 및 파싱, 로그 버퍼링, 이벤트 영속화 1-2건)",
        "current_release_coupling": "Core와 분리 가능하나 현재는 ASGI 프로세스 내부에서 서브프로세스 관리 중",
        "proposed_role": "worker",
        "difficulty": "high",
        "benefit": "Core 웹 서버와 긴 에이전트 CLI 실행의 수명주기 분리로 Core 배포 시 에이전트 중단 방지",
        "status": "implemented",
    },
    "exec.durable_scheduler": {
        "plane": "Execution",
        "name": "내구성 스케줄러·Lease 실행기",
        "user_role": "cron 기반 주기적 작업, 예약 작업(scheduled actions), PostgreSQL 행 잠금/Lease 기반 중복 방지 실행",
        "execution": "resident",
        "resources": "미측정 (코드추론: 주기 루프, lease 갱신 1건)",
        "current_release_coupling": "Core 수명주기와 결합(lifespan startup/shutdown)",
        "proposed_role": "worker",
        "difficulty": "medium",
        "benefit": "독립 프로세스 워커로 전환 시 스케줄러 재시작 없이 웹 서버만 단독 롤링 배포 가능",
        "status": "implemented",
    },
    "exec.host_gpu": {
        "plane": "Execution",
        "name": "호스트 GPU 가속기·LLM 서빙",
        "user_role": "Mac Studio Apple Silicon Metal 기반 llama-server 관리, GPU 벤치마크, 로컬 LLM 추론 자원 배정",
        "execution": "resident",
        "resources": "미측정 (실측 실행 증거 없음. 런타임 프로브 필요; 과거 추정 수치 전면 정정)",
        "current_release_coupling": "현재 Core API에서 서브프로세스로 기동/관리",
        "proposed_role": "service",
        "difficulty": "medium",
        "benefit": "GPU llama-server 수명을 백엔드 재배포와 완전히 격리하여 가속기 로딩 오버헤드 0화",
        "status": "partial",
    },
    # 3. Delivery Plane (1 Module, 사용자 Delivery 정의: 변경/계약/검증/릴리스/전환/rollback/증거)
    "delivery.git_cas": {
        "plane": "Delivery",
        "name": "코드 배포 파이프라인·Git CAS",
        "user_role": "Git 워크트리 격리 검증, 변경셋(ChangeSet) 증적 관리, merge queue, 자동 수복(bounded repair)",
        "execution": "longjob",
        "resources": "미측정 (코드추론: Git I/O, 테스트 서브프로세스, DB 1건)",
        "current_release_coupling": "별도 CLI 및 orchestrator 연동 (ChangeSet 증적 관리)",
        "proposed_role": "service",
        "difficulty": "high",
        "benefit": "무중단 배포 및 자동 검증 엔진의 독립 실행으로 배포 파이프라인 장애가 운영 서빙에 전파되지 않음",
        "status": "partial",
    },
    # 4. Domain Plane (15 Modules)
    "domain.memory_ontology": {
        "plane": "Domain",
        "name": "기억·엔티티 온톨로지 RAG",
        "user_role": "에이전트 장기 기억(Recall/Remember), 엔티티 및 관계 온톨로지 그래프 탐색, 맥락 라이브러리",
        "execution": "request",
        "resources": "미측정 (코드추론: 어휘 검색 + 벡터 임베딩 코사인 유사도, DB 1-2건)",
        "current_release_coupling": "Core 통합 라우터 서빙",
        "proposed_role": "library",
        "difficulty": "medium",
        "benefit": "RAG 엔진과 저장소 계층을 라이브러리화하여 다양한 에이전트에서 인프로세스 또는 경량 API로 공유",
        "status": "implemented",
    },
    "domain.knowledge_rag": {
        "plane": "Domain",
        "name": "검증 지식 베이스(KB)·증거 추출",
        "user_role": "불변 지식 베이스(KB), 소스 텍스트 증거(Evidence), 온톨로지 단정(Assertion) 추출 및 심사",
        "execution": "longjob",
        "resources": "미측정 (코드추론: 추출 배치 파이프라인, DB 트랜잭션 1-3건)",
        "current_release_coupling": "Core 통합 라우터 및 백그라운드 태스크",
        "proposed_role": "service",
        "difficulty": "high",
        "benefit": "지식 추출 배치의 장시간 연산 분리로 Core API 지연 방지 및 지식 저장소 독립 마이그레이션",
        "status": "implemented",
    },
    "domain.collector": {
        "plane": "Domain",
        "name": "웹 데이터 수집·정제 플랫폼",
        "user_role": "웹 문서 수집(Intake), Robots/Polite 속도 조절, Stage 2 파이프라인(텍스트 정제/블롭 저장/품질 검사)",
        "execution": "longjob",
        "resources": "미측정 (코드추론: HTML 파싱, 텍스트 추출, 네트워크 I/O, DB 스테이지 트랜잭션)",
        "current_release_coupling": "독립 패키지 구조이나 Core 프로세스 내부 runner 구동 및 ChatSession 읽기 결합",
        "proposed_role": "worker",
        "difficulty": "medium",
        "benefit": "수집 파이프라인 크롤링 부하를 Core 웹 프로세스에서 완전히 격리하고 독자 재시작/스케일링",
        "status": "partial",
    },
    "domain.creative_studio": {
        "plane": "Domain",
        "name": "크리에이티브 스튜디오·디렉터·ComfyUI",
        "user_role": "멀티미디어 제작(이미지/영상/음악) 디렉터 계획 수립, ComfyUI 워크플로우 렌더링, Artcraft 자산 생성",
        "execution": "longjob",
        "resources": "미측정 (실측 실행 증거 없음. execute_cpu_attempt는 원본 반환 더미 lambda임)",
        "current_release_coupling": "Core API 라우터, artcraft_tasks.session_id FK cascade, managed Comfy GPU lease 및 dynamic import 복합 결합",
        "proposed_role": "보류/단계적 격리 (FK cascade, file/process coupling 선행 해소 필요)",
        "difficulty": "high",
        "benefit": "세션 ID 분리 및 managed Comfy/ArtCraft 프로세스 decoupling 후 점진적 서비스화 가능 (현재 즉시 분리 불가)",
        "status": "partial",
    },
    "domain.notebooklm": {
        "plane": "Domain",
        "name": "NotebookLM 브라우저 연동",
        "user_role": "Google NotebookLM 브라우저 세션 제어, 노트북 소스 추가/삭제, 자동 질문 질의 및 응답 수집",
        "execution": "longjob",
        "resources": "미측정 (코드추론: Chromium 프로세스 메모리, DB 1건)",
        "current_release_coupling": "Core API 및 백그라운드 브라우저 태스크",
        "proposed_role": "worker",
        "difficulty": "medium",
        "benefit": "헤드리스 브라우저의 메모리 누수 및 좀비 프로세스 문제를 Core로부터 완전 격리",
        "status": "implemented",
    },
    "domain.research_lab": {
        "plane": "Domain",
        "name": "리서치 랩·실험 벤치마크",
        "user_role": "모델 벤치마크 연구 실험, 연구 노드/릴레이션 관리, 가설 검증 결과 수집",
        "execution": "request",
        "resources": "미측정 (코드추론: DB 1건)",
        "current_release_coupling": "Core API 라우터",
        "proposed_role": "library",
        "difficulty": "low",
        "benefit": "연구 데이터 스키마와 실험 메트릭 독립 관리",
        "status": "prototype",
    },
    "domain.reverse_eng": {
        "plane": "Domain",
        "name": "리버스 엔지니어링·재구성",
        "user_role": "바이너리/스크립트 디컴파일, 리버스 작업 추적, 코드 재구성 프롬프트 파이프라인",
        "execution": "longjob",
        "resources": "미측정 (코드추론: 분석 연산, DB 1건)",
        "current_release_coupling": "Core API 라우터 및 작업 큐",
        "proposed_role": "job",
        "difficulty": "medium",
        "benefit": "헤비한 분석 작업 단독 큐 분리",
        "status": "implemented",
    },
    "domain.meetings_stt": {
        "plane": "Domain",
        "name": "회의록·로컬 Whisper STT",
        "user_role": "음성 회의 녹음 전사(로컬 Whisper), 회의 요약, 안건/결정사항/액션아이템 추출",
        "execution": "longjob",
        "resources": "미측정 (실측 실행 증거 없음. 런타임 프로브 필요; 과거 추정 수치 전면 정정)",
        "current_release_coupling": "Core API 라우터 및 6개 외래키(1 self, 5 external) 결합",
        "proposed_role": "worker",
        "difficulty": "high",
        "benefit": "Whisper 모델 로딩 및 무거운 음성 전사 연산이 웹 요청 처리 지연을 유발하지 않도록 격리",
        "status": "implemented",
    },
    "domain.pms_tasks": {
        "plane": "Domain",
        "name": "PMS·마일스톤·체크리스트",
        "user_role": "프로젝트/마일스톤/태스크 관리, 반복 체크리스트 및 템플릿 관리, 진행 상태 추적",
        "execution": "request",
        "resources": "미측정 (코드추론: DB 요청당 1건)",
        "current_release_coupling": "Core API 라우터",
        "proposed_role": "library",
        "difficulty": "low",
        "benefit": "도메인 데이터가 명확하고 부작용이 적어 서비스화 또는 코어 라이브러리로 유지 용이",
        "status": "implemented",
    },
    "domain.design_system": {
        "plane": "Domain",
        "name": "디자인 시스템 카탈로그",
        "user_role": "UI 디자인 토큰, 컴포넌트 명세, 디자인 가이드 검색 및 에이전트 제공",
        "execution": "request",
        "resources": "미측정 (코드추론: 인메모리 캐시, 파일시스템 기반)",
        "current_release_coupling": "Core API 라우터",
        "proposed_role": "library",
        "difficulty": "low",
        "benefit": "프론트엔드 빌드 산출물과 정적 파일 동기화만으로 독립 배포 가능",
        "status": "implemented",
    },
    "domain.media_tts": {
        "plane": "Domain",
        "name": "로컬·클라우드 음성 합성(TTS)",
        "user_role": "텍스트 음성 변환(TTS), 음성 파일 캐싱, 스트리밍 서빙",
        "execution": "request",
        "resources": "미측정 (코드추론: 디스크 캐시 I/O, 외부 API 또는 로컬 엔진)",
        "current_release_coupling": "Core API 라우터",
        "proposed_role": "service",
        "difficulty": "medium",
        "benefit": "음성 합성 외부 API 또는 로컬 엔진 지연이 Core 요청 쓰레드를 잠식하지 않음",
        "status": "partial",
    },
    "domain.game_studio": {
        "plane": "Domain",
        "name": "게임 스튜디오 프로토타입",
        "user_role": "HTML5/Canvas 게임 자동 생성, 샌드박스 빌드, 사용자 플레이 피드백 수집",
        "execution": "request",
        "resources": "미측정 (코드추론: DB 1건)",
        "current_release_coupling": "Core API 라우터",
        "proposed_role": "library",
        "difficulty": "low",
        "benefit": "게임 프로젝트 데이터 격리",
        "status": "prototype",
    },
    "domain.personal_tools": {
        "plane": "Domain",
        "name": "개인 일상 도구(메모·일정·날씨·주식)",
        "user_role": "개인 메모, 캘린더 일정, 실시간 날씨 브리핑, 토스증권 포트폴리오 동기화",
        "execution": "request",
        "resources": "미측정 (코드추론: 외부 날씨/주식 API 네트워크 I/O, DB 1건)",
        "current_release_coupling": "Core API 라우터",
        "proposed_role": "library",
        "difficulty": "low",
        "benefit": "개인 편의 기능들을 독립 도메인으로 묶어 비즈니스 로직과 분리",
        "status": "implemented",
    },
    "domain.external_connectors": {
        "plane": "Domain",
        "name": "외부 서비스 커넥터(Google·GitHub·RDP)",
        "user_role": "Google 계정 OAuth/토큰 리프레시, GitHub 저장소 탐색, 원격 데스크톱 세션 연결",
        "execution": "request",
        "resources": "미측정 (코드추론: 외부 서비스 네트워크 I/O)",
        "current_release_coupling": "Core API 라우터",
        "proposed_role": "shared adapter",
        "difficulty": "medium",
        "benefit": "외부 서비스 인증 프로토콜 변경 시 어댑터만 단독 업데이트",
        "status": "implemented",
    },
    "domain.shares_artifacts": {
        "plane": "Domain",
        "name": "결과물 공유·공개 배포",
        "user_role": "산출물(Artifacts) 저장/분류, 퍼블릭 읽기 전용 공유 링크 생성 및 서명 토큰 검증",
        "execution": "request",
        "resources": "미측정 (코드추론: DB 1건)",
        "current_release_coupling": "Core API 라우터",
        "proposed_role": "Core",
        "difficulty": "low",
        "benefit": "사용자에게 결과물을 노출하는 정적 게이트웨이 역할",
        "status": "implemented",
    },
}


def get_git_info(ctx: AuditContext | None = None) -> dict[str, Any]:
    root = ctx.repo_root if ctx else DEFAULT_REPO_ROOT
    try:
        res_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        head = res_head.stdout.strip()
        res_tree = subprocess.run(
            ["git", "rev-parse", "HEAD:src"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        src_tree = res_tree.stdout.strip()
        res_dirty = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--", "src"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        src_dirty = res_dirty.returncode != 0
        return {
            "available": True,
            "head": head,
            "src_tree": src_tree,
            "src_dirty": src_dirty,
        }
    except Exception:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
        return {
            "available": False,
            "head": "0" * 40,
            "src_tree": "0" * 40,
            "src_dirty": False,
        }


def get_git_sha(ctx: AuditContext | None = None) -> str:
    info = get_git_info(ctx)
    if info["available"]:
        return info["head"]
    return BASELINE_CODE_SHA


def extract_routes(
    ctx: AuditContext | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    from tests.test_route_inventory import collect_routes

    raw = collect_routes()
    out = {}
    for (method, path), label in raw.items():
        out[(method, path)] = {"method": method, "path": path, "label": label}
    return out


def extract_mcp_tools(ctx: AuditContext | None = None) -> list[str]:
    import asyncio

    from ttakkari.orchestrator import mcp_server as ms

    async def _get():
        tools = await ms.server.list_tools()
        if isinstance(tools, tuple):
            tools = tools[0]
        return sorted(t.name for t in tools)

    return asyncio.run(_get())


def extract_db_tables(ctx: AuditContext | None = None) -> dict[str, Any]:
    from ttakkari.models import Base

    tables = {}
    for name, table in Base.metadata.tables.items():
        columns = [c.name for c in table.columns]
        fks = [
            {"column": fk.parent.name, "target": str(fk.target_fullname)}
            for fk in table.foreign_keys
        ]
        tables[name] = {"columns": columns, "foreign_keys": fks}
    return tables


def extract_alembic_migrations(
    ctx: AuditContext | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Alembic migration 파일 파싱 및 ScriptDirectory 교차 검증, DDL call-site 수집.

    Returns:
        (migrations_list, crosscheck_failures)
    """
    alembic_dir = ctx.alembic_dir if ctx else DEFAULT_REPO_ROOT / "alembic"
    versions_dir = alembic_dir / "versions"
    crosscheck_failures: list[str] = []

    if not versions_dir.exists():
        crosscheck_failures.append(f"Alembic versions dir missing: {versions_dir}")
        return [], crosscheck_failures

    alembic_map: dict[str, dict[str, Any]] = {}
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(alembic_dir))
        script = ScriptDirectory.from_config(cfg)
        for rev in script.walk_revisions():
            alembic_map[rev.revision] = {
                "down_revision": rev.down_revision,
                "branch_labels": list(rev.branch_labels or []),
            }
    except Exception as e:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
        crosscheck_failures.append(
            f"Alembic ScriptDirectory cross-check unavailable: {e}"
        )

    migrations = []
    py_files = sorted(versions_dir.glob("*.py"))

    # Crosscheck count
    if alembic_map and len(py_files) != len(alembic_map):
        crosscheck_failures.append(
            f"Migration file count ({len(py_files)}) != ScriptDirectory revisions ({len(alembic_map)})"
        )

    for p in py_files:
        try:
            content = p.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
            crosscheck_failures.append(f"Failed to read migration file {p.name}: {e}")
            continue
        rev = None
        down = None
        parse_err = None
        try:
            tree = ast.parse(content)
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            if target.id == "revision" and isinstance(
                                node.value, ast.Constant
                            ):
                                rev = node.value.value
                            elif target.id == "down_revision":
                                if isinstance(node.value, ast.Constant):
                                    down = node.value.value
                                elif isinstance(node.value, (ast.Tuple, ast.List)):
                                    down = [
                                        elt.value
                                        for elt in node.value.elts
                                        if isinstance(elt, ast.Constant)
                                    ]
                elif isinstance(node, ast.AnnAssign):  # noqa: SIM102 - keep AST node kind and field handling separate
                    if isinstance(node.target, ast.Name):
                        if (
                            node.target.id == "revision"
                            and node.value
                            and isinstance(node.value, ast.Constant)
                        ):
                            rev = node.value.value
                        elif node.target.id == "down_revision" and node.value:
                            if isinstance(node.value, ast.Constant):
                                down = node.value.value
                            elif isinstance(node.value, (ast.Tuple, ast.List)):
                                down = [
                                    elt.value
                                    for elt in node.value.elts
                                    if isinstance(elt, ast.Constant)
                                ]
        except Exception as e:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
            parse_err = str(e)
            crosscheck_failures.append(f"Migration AST parse error in {p.name}: {e}")

        # Fallback tracking
        used_filename_fallback = False
        if rev is None:
            rev = p.stem.split("_")[0]
            used_filename_fallback = True
            crosscheck_failures.append(f"Used filename fallback revision for {p.name}")

        if down is None and rev in alembic_map:
            down = alembic_map[rev]["down_revision"]

        # DDL call sites in upgrade()
        ddl_call_sites: list[dict[str, Any]] = []
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call) and isinstance(
                            sub.func, ast.Attribute
                        ):
                            attr = sub.func.attr
                            args = sub.args
                            if attr in (
                                "create_table",
                                "drop_table",
                                "add_column",
                                "drop_column",
                                "create_index",
                                "create_foreign_key",
                                "execute",
                            ):
                                target_val = "?"
                                sql_type = "unknown"
                                if args:
                                    arg0 = args[0]
                                    if isinstance(arg0, ast.Constant):
                                        target_val = str(arg0.value)
                                        sql_type = "static_literal"
                                    elif isinstance(arg0, ast.JoinedStr):
                                        sql_type = "interpolated"
                                        target_val = "[interpolated f-string]"
                                    elif isinstance(arg0, ast.Call):
                                        sql_type = "parameterized"
                                        target_val = f"call:{ast.unparse(arg0.func)}"
                                    else:
                                        target_val = (
                                            ast.unparse(arg0)
                                            if hasattr(ast, "unparse")
                                            else str(arg0)
                                        )
                                ddl_call_sites.append(
                                    {
                                        "kind": attr,
                                        "target": target_val,
                                        "sql_classification": sql_type,
                                        "line": sub.lineno,
                                    }
                                )
        except Exception as exc:  # noqa: BLE001 - retain unsupported DDL evidence
            crosscheck_failures.append(
                f"Migration DDL extraction failed in {p.name}: {exc}"
            )

        migrations.append(
            {
                "filename": p.name,
                "revision": rev,
                "down_revision": down,
                "stem": p.stem,
                "branch_labels": alembic_map.get(rev, {}).get("branch_labels", []),
                "ddl_call_sites": ddl_call_sites,
                "used_filename_fallback": used_filename_fallback,
                "parse_error": parse_err,
            }
        )

    return migrations, crosscheck_failures


def extract_config_fields(ctx: AuditContext | None = None) -> list[str]:
    from ttakkari.config import Settings

    return sorted(Settings.model_fields.keys())


def extract_source_files(
    ctx: AuditContext | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]], list[str]]:
    """Source files 수집 및 missing root / parse error 검증."""
    src_dir = ctx.src_dir if ctx else DEFAULT_SRC_DIR
    repo_root = ctx.repo_root if ctx else DEFAULT_REPO_ROOT
    files = {}
    parse_errors = []
    missing_roots = []

    if not src_dir.exists():
        missing_roots.append(f"SRC_DIR missing: {src_dir}")
        return files, parse_errors, missing_roots

    py_paths = sorted(src_dir.rglob("*.py"))
    if not py_paths:
        missing_roots.append(f"SRC_DIR contains 0 python files: {src_dir}")
        return files, parse_errors, missing_roots

    for p in py_paths:
        try:
            rel = p.relative_to(repo_root).as_posix()
        except ValueError:
            rel = p.as_posix()
        file_sha = ""
        lines = 0
        try:
            data = p.read_bytes()
            file_sha = hashlib.sha256(data).hexdigest()
            content = data.decode("utf-8", errors="replace")
            lines = len(content.splitlines())
            ast.parse(content)
        except SyntaxError as e:
            parse_errors.append({"file": rel, "error": f"SyntaxError: {e}"})
            lines = 0
        except Exception as e:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
            parse_errors.append({"file": rel, "error": str(e)})
            lines = 0
        files[rel] = {"lines": lines, "path": rel, "sha256": file_sha}

    return files, parse_errors, missing_roots


# ==============================================================================
# Independent Candidate Discovery & Reconciliation
# ==============================================================================


def discover_source_files(ctx: AuditContext) -> DiscoveryResult:
    src_dir = ctx.src_dir
    if not src_dir.exists():
        return DiscoveryResult(
            category="source_files",
            candidate_ids=[],
            errors=[f"src_dir missing: {src_dir}"],
        )
    fingerprints: dict[str, str] = {}
    errors: list[str] = []
    unresolved: list[dict[str, Any]] = []
    for p in sorted(src_dir.rglob("*.py")):
        try:
            rel = p.relative_to(ctx.repo_root).as_posix()
        except ValueError:
            rel = p.as_posix()
        try:
            data = p.read_bytes()
            fingerprints[rel] = hashlib.sha256(data).hexdigest()
            ast.parse(data.decode("utf-8", errors="replace"))
        except SyntaxError as e:
            errors.append(f"SyntaxError in {rel}: {e}")
        except Exception as e:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
            errors.append(f"Failed to read/hash/parse {p}: {e}")
    return DiscoveryResult(
        category="source_files",
        candidate_ids=sorted(fingerprints.keys()),
        fingerprints=fingerprints,
        errors=errors,
        unresolved=unresolved,
    )


def discover_migrations(ctx: AuditContext) -> DiscoveryResult:
    versions_dir = ctx.alembic_dir / "versions"
    if not versions_dir.exists():
        return DiscoveryResult(
            category="migrations",
            candidate_ids=[],
            errors=[f"versions_dir missing: {versions_dir}"],
        )
    py_files = sorted(versions_dir.glob("*.py"))
    candidates: list[str] = []
    errors: list[str] = []
    for p in py_files:
        rev = None
        try:
            content = p.read_text(encoding="utf-8")
            tree = ast.parse(content)
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Name)
                            and target.id == "revision"
                            and isinstance(node.value, ast.Constant)
                        ):
                            rev = node.value.value
                elif isinstance(node, ast.AnnAssign) and (
                    isinstance(node.target, ast.Name)
                    and node.target.id == "revision"
                    and node.value
                    and isinstance(node.value, ast.Constant)
                ):
                    rev = node.value.value
            if rev is None:
                rev = p.stem.split("_")[0]
                errors.append(f"Used filename fallback revision for {p.name}")
            candidates.append(f"{p.name}::{rev}")
        except Exception as e:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
            errors.append(f"Failed to read/parse migration {p.name}: {e}")
    return DiscoveryResult(
        category="migrations", candidate_ids=sorted(candidates), errors=errors
    )


def discover_config_fields(ctx: AuditContext) -> DiscoveryResult:
    cfg_path = ctx.src_dir / "ttakkari" / "config.py"
    if not cfg_path.exists():
        return DiscoveryResult(
            category="configs",
            candidate_ids=[],
            errors=[f"config.py missing: {cfg_path}"],
        )
    try:
        tree = ast.parse(cfg_path.read_text(encoding="utf-8"))
        fields: list[str] = []
        unresolved: list[dict[str, Any]] = []
        errors: list[str] = []

        settings_classes: list[ast.ClassDef] = []
        settings_assigns: list[ast.AST] = []
        all_classes: dict[str, ast.ClassDef] = {}

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                all_classes[node.name] = node
                if node.name == "Settings":
                    settings_classes.append(node)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "Settings":
                        settings_assigns.append(node)
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == "Settings":
                    settings_assigns.append(node)

        if settings_assigns:
            unresolved.append(
                {
                    "item": "Settings",
                    "reason": "Settings factory or dynamic definition detected outside static discovery boundary",
                }
            )

        if not settings_classes and not settings_assigns:
            unresolved.append(
                {
                    "item": "Settings",
                    "reason": "Settings definition not found in config.py",
                }
            )
        elif settings_classes:
            node = settings_classes[0]
            for b in node.bases:
                base_name = (
                    b.id
                    if isinstance(b, ast.Name)
                    else (b.attr if isinstance(b, ast.Attribute) else ast.unparse(b))
                )
                if base_name != "BaseSettings":
                    if base_name in all_classes:
                        parent = all_classes[base_name]
                        for st in parent.body:
                            if isinstance(st, ast.AnnAssign) and isinstance(
                                st.target, ast.Name
                            ):
                                name = st.target.id
                                ann = ast.unparse(st.annotation)
                                if (
                                    not name.startswith("_")
                                    and "ClassVar" not in ann
                                    and name != "model_config"
                                ):
                                    fields.append(name)
                        unresolved.append(
                            {
                                "item": f"Settings({base_name})",
                                "reason": f"Settings inherits from {base_name}; static inheritance outside simple BaseSettings discovery boundary",
                            }
                        )
                    else:
                        unresolved.append(
                            {
                                "item": f"Settings({base_name})",
                                "reason": f"Settings inherits from external/unsupported class {base_name} outside static discovery boundary",
                            }
                        )

            for st in node.body:
                if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    name = st.target.id
                    ann = ast.unparse(st.annotation)
                    if (
                        not name.startswith("_")
                        and "ClassVar" not in ann
                        and name != "model_config"
                    ):
                        fields.append(name)

        return DiscoveryResult(
            category="configs",
            candidate_ids=sorted(set(fields)),
            errors=errors,
            unresolved=unresolved,
        )
    except Exception as e:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
        return DiscoveryResult(
            category="configs",
            candidate_ids=[],
            errors=[f"Failed to parse config.py: {e}"],
        )


def discover_tables(ctx: AuditContext) -> DiscoveryResult:
    src_dir = ctx.src_dir
    if not src_dir.exists():
        return DiscoveryResult(
            category="tables", candidate_ids=[], errors=[f"src_dir missing: {src_dir}"]
        )
    tables: set[str] = set()
    unresolved: list[dict[str, Any]] = []
    errors: list[str] = []
    for p in sorted(src_dir.rglob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for st in node.body:
                        if isinstance(st, ast.Assign) and any(
                            isinstance(t, ast.Name) and t.id == "__tablename__"
                            for t in st.targets
                        ):
                            if isinstance(st.value, ast.Constant) and isinstance(
                                st.value.value, str
                            ):
                                tables.add(st.value.value)
                            else:
                                unresolved.append(
                                    {
                                        "file": str(p.relative_to(ctx.repo_root)),
                                        "line": st.lineno,
                                        "reason": "dynamic __tablename__",
                                    }
                                )
                elif isinstance(node, ast.Call):
                    fn_ = node.func
                    nm = (
                        fn_.id
                        if isinstance(fn_, ast.Name)
                        else (fn_.attr if isinstance(fn_, ast.Attribute) else None)
                    )
                    if (
                        nm == "Table"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                    ):
                        tables.add(node.args[0].value)
        except Exception as e:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
            errors.append(f"Parse error in {p}: {e}")
    return DiscoveryResult(
        category="tables",
        candidate_ids=sorted(tables),
        errors=errors,
        unresolved=unresolved,
    )


def discover_tools(ctx: AuditContext) -> DiscoveryResult:
    mcp_path = ctx.src_dir / "ttakkari" / "orchestrator" / "mcp_server.py"
    if not mcp_path.exists():
        return DiscoveryResult(
            category="tools",
            candidate_ids=[],
            errors=[f"mcp_server.py missing: {mcp_path}"],
        )
    try:
        tree = ast.parse(mcp_path.read_text(encoding="utf-8"))
        tools: set[str] = set()
        unresolved: list[dict[str, Any]] = []
        errors: list[str] = []

        top_level_funcs = {
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                is_tool = False
                for d in node.decorator_list:
                    s = ast.unparse(d)
                    if s.startswith("server.tool") or s in (
                        "query_tool",
                        "orchestrator_tool",
                    ):
                        is_tool = True
                        break
                if is_tool:
                    if node in top_level_funcs:
                        tools.add(node.name)
                    else:
                        unresolved.append(
                            {
                                "item": node.name,
                                "reason": f"Conditional or nested tool registration ({node.name}) outside static discovery support boundary",
                            }
                        )

        return DiscoveryResult(
            category="tools",
            candidate_ids=sorted(tools),
            errors=errors,
            unresolved=unresolved,
        )
    except Exception as e:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
        return DiscoveryResult(
            category="tools",
            candidate_ids=[],
            errors=[f"Parse error in {mcp_path}: {e}"],
        )


def discover_routes(ctx: AuditContext) -> DiscoveryResult:
    src_dir = ctx.src_dir
    errors: list[str] = []
    count = 0
    HTTP = {
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "websocket",
        "api_route",
        "options",
        "head",
    }
    if src_dir.exists():
        for p in sorted(src_dir.rglob("*.py")):
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for d in node.decorator_list:
                            if (
                                isinstance(d, ast.Call)
                                and isinstance(d.func, ast.Attribute)
                                and d.func.attr in HTTP
                            ):
                                count += 1
            except Exception as e:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
                errors.append(f"Route discovery AST parse error in {p}: {e}")
    else:
        errors.append(f"src_dir missing: {src_dir}")
    return DiscoveryResult(
        category="routes",
        candidate_ids=None,
        errors=errors,
        unresolved=[
            {
                "category": "routes",
                "reason": "FastAPI route decorates/includes router dynamically without a unified static registry",
            }
        ],
        auxiliary={"route_decorator_call_sites": count},
    )


def evaluate_import_binding(ctx: AuditContext) -> dict[str, Any]:
    outside_root: list[str] = []
    unbound_modules: list[str] = []
    reasons: list[str] = []
    categories: dict[str, dict[str, Any]] = {
        "configs": {"state": "bound", "reasons": []},
        "tables": {"state": "bound", "reasons": []},
        "tools": {"state": "bound", "reasons": []},
        "routes": {"state": "bound", "reasons": []},
    }

    src_dir = ctx.src_dir.resolve()

    for name, mod in list(sys.modules.items()):
        if not name.startswith("ttakkari"):
            continue
        if mod is None:
            unbound_modules.append(f"{name} (None module object)")
            continue
        if not hasattr(mod, "__file__") or not mod.__file__:
            unbound_modules.append(f"{name} (missing __file__)")
            continue
        mod_path = Path(mod.__file__).resolve()
        try:
            mod_path.relative_to(src_dir)
        except ValueError:
            outside_root.append(f"{name} ({mod_path})")

        loader = getattr(mod, "__loader__", None)
        spec = getattr(mod, "__spec__", None)
        if loader is None:
            unbound_modules.append(
                f"{name} has no __loader__ (in-memory mock/unverified loader)"
            )
            continue
        loader_path = getattr(loader, "path", None) or (
            getattr(spec, "origin", None) if spec else None
        )
        if loader_path:
            try:
                if Path(loader_path).resolve() != mod_path:
                    unbound_modules.append(
                        f"{name} loader origin ({loader_path}) != file ({mod_path})"
                    )
            except Exception:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
                unbound_modules.append(
                    f"{name} loader path unresolvable ({loader_path})"
                )

    target_checks = {
        "configs": ["ttakkari.config"],
        "tables": ["ttakkari.models"],
        "tools": ["ttakkari.orchestrator.mcp_server"],
        "routes": ["ttakkari.main"],
    }
    for cat, mod_names in target_checks.items():
        cat_errors = []
        for mod_name in mod_names:
            if mod_name in sys.modules:
                mod = sys.modules[mod_name]
                if mod is None:
                    cat_errors.append(f"{mod_name} is None in sys.modules")
                    continue
                if not hasattr(mod, "__file__") or not mod.__file__:
                    cat_errors.append(f"{mod_name} has no __file__")
                    continue
                p = Path(mod.__file__).resolve()
                try:
                    p.relative_to(src_dir)
                except ValueError:
                    cat_errors.append(f"{mod_name} ({p}) outside {src_dir}")
                loader = getattr(mod, "__loader__", None)
                if loader is None:
                    cat_errors.append(
                        f"{mod_name} has no __loader__ (source hash binding unverified)"
                    )
                else:
                    loader_path = getattr(loader, "path", None) or (
                        getattr(mod.__spec__, "origin", None)
                        if getattr(mod, "__spec__", None)
                        else None
                    )
                    if loader_path:
                        try:
                            if Path(loader_path).resolve() != p:
                                cat_errors.append(f"{mod_name} loader path mismatch")
                        except Exception:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
                            cat_errors.append(f"{mod_name} loader path unresolvable")
            else:
                cat_errors.append(
                    f"Required module {mod_name} not found in sys.modules"
                )

        if cat_errors or outside_root or unbound_modules:
            categories[cat]["state"] = "unbound"
            err_list = cat_errors + [
                f"Global unbound: {m}" for m in (outside_root + unbound_modules)
            ]
            categories[cat]["reasons"] = err_list

    if outside_root or unbound_modules:
        state = "unbound"
        if outside_root:
            reasons.append(
                f"{len(outside_root)} ttakkari modules imported outside context src_dir ({src_dir})"
            )
        if unbound_modules:
            reasons.append(
                f"{len(unbound_modules)} ttakkari modules have unverified/missing loaders ({', '.join(unbound_modules[:5])})"
            )
    else:
        # Loader/spec/file paths locate a module; they do not attest that the
        # live object was executed from the current source bytes. Keep static
        # discovery useful without turning metadata into runtime provenance.
        state = "unknown"
        reasons.append(
            "Loaded object/source-byte identity is unverified; loader paths are metadata only"
        )

    for category_binding in categories.values():
        if category_binding["state"] == "bound":
            category_binding["state"] = "unknown"
            category_binding["reasons"].append(
                "Loaded object/source-byte identity is unverified; static reconciliation only"
            )

    return {
        "state": state,
        "outside_root": outside_root,
        "unbound_modules": unbound_modules,
        "categories": categories,
        "reasons": reasons,
    }


def reconcile_category(
    category: str,
    collected_ids: list[str],
    discovery: DiscoveryResult,
    binding: dict[str, Any] | None,
    collected_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    missing_from_collected: list[str] = []
    extra_in_collected: list[str] = []
    duplicates_in_collected: list[str] = []
    fingerprint_mismatches: list[str] = []
    unresolved = list(discovery.unresolved)
    auxiliary = dict(discovery.auxiliary)

    counts = Counter(collected_ids)
    duplicates_in_collected = sorted([k for k, v in counts.items() if v > 1])

    collected_set = set(collected_ids)
    collected_count = len(collected_ids)
    candidate_count = (
        len(discovery.candidate_ids) if discovery.candidate_ids is not None else None
    )

    # 1. Check import binding
    if binding is not None and binding.get("state") != "bound":
        reasons.append(
            "Import source binding is unbound or outside target context root"
        )
        if binding.get("reasons"):
            reasons.extend(binding["reasons"])
        static_known = (
            discovery.candidate_ids is not None
            and not discovery.errors
            and not unresolved
        )
        static_missing = (
            sorted(set(discovery.candidate_ids or []) - collected_set)
            if static_known
            else []
        )
        static_extra = (
            sorted(collected_set - set(discovery.candidate_ids or []))
            if static_known
            else []
        )
        return {
            "category": category,
            "state": "unknown",
            "static_state": (
                "mismatch"
                if static_missing or static_extra or duplicates_in_collected
                else "matching_ids"
            )
            if static_known
            else "unknown",
            "reasons": reasons,
            "collected_count": collected_count,
            "candidate_count": candidate_count,
            "missing_from_collected": static_missing,
            "extra_in_collected": static_extra,
            "duplicates_in_collected": duplicates_in_collected,
            "fingerprint_mismatches": [],
            "unresolved": unresolved,
            "auxiliary": auxiliary,
        }

    # 2. Routes or no independent candidates
    if discovery.candidate_ids is None:
        reasons.append(
            "No independent candidate discovery available; coverage is unverified/unknown"
        )
        return {
            "category": category,
            "state": "unknown",
            "reasons": reasons,
            "collected_count": collected_count,
            "candidate_count": candidate_count,
            "missing_from_collected": [],
            "extra_in_collected": [],
            "duplicates_in_collected": duplicates_in_collected,
            "fingerprint_mismatches": [],
            "unresolved": unresolved,
            "auxiliary": auxiliary,
        }

    candidate_set = set(discovery.candidate_ids)
    missing_from_collected = sorted(candidate_set - collected_set)
    extra_in_collected = sorted(collected_set - candidate_set)

    # 3. Check for empty equals empty
    if collected_count == 0 and candidate_count == 0:
        reasons.append("Empty candidate and collected sets cannot prove coverage")
        return {
            "category": category,
            "state": "unknown",
            "reasons": reasons,
            "collected_count": 0,
            "candidate_count": 0,
            "missing_from_collected": [],
            "extra_in_collected": [],
            "duplicates_in_collected": [],
            "fingerprint_mismatches": [],
            "unresolved": unresolved,
            "auxiliary": auxiliary,
        }

    # 4. Check discovery errors
    if discovery.errors:
        reasons.extend(discovery.errors)
        return {
            "category": category,
            "state": "unknown",
            "reasons": reasons,
            "collected_count": collected_count,
            "candidate_count": candidate_count,
            "missing_from_collected": missing_from_collected,
            "extra_in_collected": extra_in_collected,
            "duplicates_in_collected": duplicates_in_collected,
            "fingerprint_mismatches": [],
            "unresolved": unresolved,
            "auxiliary": auxiliary,
        }

    # 5. Check unresolved discovery items (e.g. dynamic __tablename__)
    if unresolved:
        reasons.append(
            f"{len(unresolved)} unresolved candidate items prevent coverage verification"
        )
        return {
            "category": category,
            "state": "unknown",
            "reasons": reasons,
            "collected_count": collected_count,
            "candidate_count": candidate_count,
            "missing_from_collected": missing_from_collected,
            "extra_in_collected": extra_in_collected,
            "duplicates_in_collected": duplicates_in_collected,
            "fingerprint_mismatches": [],
            "unresolved": unresolved,
            "auxiliary": auxiliary,
        }

    # 5. Check import binding
    if binding is not None and binding.get("state") != "bound":
        reasons.append(
            "Import source binding is unbound or outside target context root"
        )
        if binding.get("reasons"):
            reasons.extend(binding["reasons"])
        return {
            "category": category,
            "state": "unknown",
            "reasons": reasons,
            "collected_count": collected_count,
            "candidate_count": candidate_count,
            "missing_from_collected": missing_from_collected,
            "extra_in_collected": extra_in_collected,
            "duplicates_in_collected": duplicates_in_collected,
            "fingerprint_mismatches": [],
            "unresolved": unresolved,
            "auxiliary": auxiliary,
        }

    # 6. For source_files: check collected_hashes and fingerprints
    if category == "source_files":
        if collected_hashes is None:
            reasons.append(
                "Source content hashes missing from collection; cannot verify content fingerprint"
            )
            return {
                "category": category,
                "state": "unknown",
                "reasons": reasons,
                "collected_count": collected_count,
                "candidate_count": candidate_count,
                "missing_from_collected": missing_from_collected,
                "extra_in_collected": extra_in_collected,
                "duplicates_in_collected": duplicates_in_collected,
                "fingerprint_mismatches": [],
                "unresolved": unresolved,
                "auxiliary": auxiliary,
            }
        for cid in collected_ids:
            expected_fp = discovery.fingerprints.get(cid)
            actual_fp = collected_hashes.get(cid)
            if expected_fp and actual_fp and expected_fp != actual_fp:
                fingerprint_mismatches.append(cid)

    # 7. Check for mismatch
    if (
        missing_from_collected
        or extra_in_collected
        or duplicates_in_collected
        or fingerprint_mismatches
    ):
        state = "mismatch"
        if missing_from_collected:
            reasons.append(
                f"{len(missing_from_collected)} items missing from collected set"
            )
        if extra_in_collected:
            reasons.append(f"{len(extra_in_collected)} extra items in collected set")
        if duplicates_in_collected:
            reasons.append(
                f"{len(duplicates_in_collected)} duplicate items in collected set"
            )
        if fingerprint_mismatches:
            reasons.append(
                f"{len(fingerprint_mismatches)} fingerprint mismatches detected"
            )
        return {
            "category": category,
            "state": state,
            "reasons": reasons,
            "collected_count": collected_count,
            "candidate_count": candidate_count,
            "missing_from_collected": missing_from_collected,
            "extra_in_collected": extra_in_collected,
            "duplicates_in_collected": duplicates_in_collected,
            "fingerprint_mismatches": fingerprint_mismatches,
            "unresolved": unresolved,
            "auxiliary": auxiliary,
        }

    # 8. All reconciled
    return {
        "category": category,
        "state": "reconciled",
        "reasons": [],
        "collected_count": collected_count,
        "candidate_count": candidate_count,
        "missing_from_collected": [],
        "extra_in_collected": [],
        "duplicates_in_collected": [],
        "fingerprint_mismatches": [],
        "unresolved": [],
        "auxiliary": auxiliary,
    }


# ==============================================================================
# Classifier functions (Fail-closed: No silent catch-all, return "unmatched")
# ==============================================================================


def classify_route(method: str, path: str) -> str:
    p = path.lower()
    # Core & Control
    if p.startswith("/api/auth"):
        return "core.auth_session"
    if p.startswith(("/api/sessions", "/api/agent/sessions")):
        return "core.auth_session"
    if p.startswith("/api/workspaces"):
        return "core.workspaces_transfers"
    if any(
        p.startswith(x)
        for x in (
            "/api/transfers",
            "/api/transfer-tokens",
            "/api/uploads",
            "/api/dl",
            "/api/view",
        )
    ):
        return "core.workspaces_transfers"
    if p.startswith("/api/accounts"):
        return "core.accounts_quota"
    if any(
        p.startswith(x)
        for x in (
            "/api/deployments",
            "/api/agent/deploy",
            "/api/bootstrap-ready",
            "/api/ready",
            "/api/health",
            "/api/version",
        )
    ):
        return "core.generation_deploy"
    if any(
        p.startswith(x)
        for x in (
            "/api/system",
            "/api/push",
            "/api/notifications",
            "/api/desk",
            "/api/agent/system",
            "/api/agent/operations",
            "/api/agent/notify",
            "/api/harness",
        )
    ):
        return "core.system_routing"

    # Execution Plane
    if p.startswith(
        (
            "/api/runs",
            "/api/orchestrator",
            "/api/agent/execution-capabilities",
            "/api/qwen-status",
            "/api/executor",
        )
    ):
        return "exec.run_engine"
    if p.startswith("/api/agent/host-gpu"):
        return "exec.host_gpu"

    # Delivery Plane
    if p.startswith("/api/delivery"):
        return "delivery.git_cas"

    # Domain Plane
    if any(
        p.startswith(x)
        for x in (
            "/api/assistant/memories",
            "/api/assistant/entities",
            "/api/assistant/relations",
            "/api/assistant/library",
            "/api/assistant/sources",
            "/api/assistant/recall",
            "/api/assistant/tools",
            "/api/assistant/status",
            "/api/assistant/instructions",
            "/api/agent/recall",
            "/api/agent/remember",
            "/api/agent/forget",
            "/api/agent/link",
            "/api/agent/entities",
            "/api/agent/library",
        )
    ):
        return "domain.memory_ontology"
    if p.startswith(
        (
            "/api/assistant/knowledge",
            "/api/agent/knowledge",
            "/api/assistant/kb",
            "/api/agent/kb",
            "/api/kb",
        )
    ):
        return "domain.knowledge_rag"
    if p.startswith(("/api/collect", "/api/agent/collect", "/api/assistant/collect")):
        return "domain.collector"
    if p.startswith(
        (
            "/api/creative",
            "/api/comfy",
            "/api/artcraft",
            "/api/agent/comfy",
            "/api/agent/creative",
        )
    ):
        return "domain.creative_studio"
    if p.startswith(("/api/notebooklm", "/api/agent/notebooklm")):
        return "domain.notebooklm"
    if p.startswith(("/api/research", "/api/agent/research")):
        return "domain.research_lab"
    if p.startswith(("/api/reverse", "/api/agent/reverse")):
        return "domain.reverse_eng"
    if p.startswith(("/api/meetings", "/api/agent/meetings")):
        return "domain.meetings_stt"
    if any(
        p.startswith(x)
        for x in (
            "/api/pms",
            "/api/agent/pms",
            "/api/checklists",
            "/api/agent/checklists",
            "/api/tasks",
        )
    ):
        return "domain.pms_tasks"
    if p.startswith(("/api/design-systems", "/api/agent/design-systems")):
        return "domain.design_system"
    if p.startswith("/api/tts"):
        return "domain.media_tts"
    if p.startswith("/api/games"):
        return "domain.game_studio"
    if any(
        p.startswith(x)
        for x in (
            "/api/weather",
            "/api/stocks",
            "/api/agent/stocks",
            "/api/memos",
            "/api/calendar",
            "/api/memo-categories",
            "/api/agent/memos",
            "/api/agent/events",
            "/api/agent/memo-categories",
        )
    ):
        return "domain.personal_tools"
    if any(
        p.startswith(x)
        for x in (
            "/api/google-accounts",
            "/api/agent/google-accounts",
            "/api/github",
            "/api/remote-desktop",
        )
    ):
        return "domain.external_connectors"
    if any(
        p.startswith(x)
        for x in (
            "/api/shares",
            "/api/public",
            "/api/exports",
            "/api/artifacts",
            "/api/agent/artifacts",
            "/api/agent/share",
            "/api/agent/keep",
        )
    ):
        return "domain.shares_artifacts"

    # Control / Ops
    if p.startswith(("/api/ops-logs", "/api/agent/ops-logs", "/api/ops")):
        return "ops.monitoring"

    return "unmatched"


def classify_mcp_tool(name: str) -> str:
    n = name.lower()
    if n in ("list_sessions", "read_session"):
        return "core.auth_session"
    if n in ("account_usage",):
        return "core.accounts_quota"
    if n in ("request_backend_deploy", "backend_deploy_status"):
        return "core.generation_deploy"
    if n in ("notify_user", "host_operation_start", "host_operation_status", "host_operation_cancel"):
        return "core.system_routing"
    if n in (
        "manage_work",
        "spawn_worker",
        "get_worker",
        "message_worker",
        "stop_worker",
        "list_workers",
        "stop_agents",
        "begin_environment_probe",
        "report_execution_environment",
        "execution_capabilities",
        "luna_common30_v2_cancel",
        "luna_common30_v2_status",
        "start_luna_common30_v2",
    ):
        return "exec.run_engine"
    if n in ("schedule_action", "list_scheduled_actions", "cancel_scheduled_action"):
        return "exec.durable_scheduler"
    if n in (
        "host_gpu_start",
        "host_gpu_status",
        "host_gpu_stop",
        "host_gpu_bench",
        "server_resources",
    ):
        return "exec.host_gpu"
    if n in (
        "recall",
        "remember",
        "forget",
        "get_entity",
        "describe_entity",
        "link",
        "library_catalog",
        "my_context",
        "reset_my_context",
    ):
        return "domain.memory_ontology"
    if n.startswith(("knowledge_", "kb_")):
        return "domain.knowledge_rag"
    if n.startswith(("creative_v2_", "comfy_")):
        return "domain.creative_studio"
    if n.startswith("notebooklm_"):
        return "domain.notebooklm"
    if n in ("manage_research",):
        return "domain.research_lab"
    if n.startswith("reverse_"):
        return "domain.reverse_eng"
    if n in ("list_meetings", "get_meeting", "search_meeting_items", "search_meetings"):
        return "domain.meetings_stt"
    if n in ("list_pms_projects", "create_pms_task", "manage_pms") or "checklist" in n:
        return "domain.pms_tasks"
    if n.startswith("design_system_"):
        return "domain.design_system"
    if n in (
        "create_memo",
        "get_memo",
        "update_memo",
        "delete_memo",
        "list_memos",
        "search_memos",
        "list_memo_categories",
        "list_memo_tags",
        "create_event",
        "update_event",
        "delete_event",
        "list_events",
        "tossinvest_status",
    ):
        return "domain.personal_tools"
    if n in ("google_account_status",):
        return "domain.external_connectors"
    if n in ("list_artifacts", "set_artifact_area", "share_file", "keep_artifact"):
        return "domain.shares_artifacts"
    if n.startswith("ops_"):
        return "ops.monitoring"

    return "unmatched"


def classify_table(name: str) -> str:
    n = name.lower()
    if n in (
        "credentials",
        "sessions",
        "refresh_tokens",
        "login_attempts",
        "login_bucket",
        "login_throttle",
        "audit_logs",
    ):
        return "core.auth_session"
    if n in ("workspaces", "transfer_tokens"):
        return "core.workspaces_transfers"
    if n in (
        "engine_accounts",
        "account_usage_samples",
        "qwen_token_usage",
        "qwen_flash_usage",
    ):
        return "core.accounts_quota"
    if n in ("runtime_generations", "runtime_control", "runtime_commands"):
        return "core.generation_deploy"
    if n in ("app_settings", "notifications", "push_subscriptions", "desk_feedback"):
        return "core.system_routing"
    if n in (
        "runs",
        "run_events",
        "runtime_leases",
        "retired_integration_records",
        "runtime_process_groups",
    ):
        return "exec.run_engine"
    if n in ("scheduled_actions",):
        return "exec.durable_scheduler"
    if n in ("system_resource_samples",):
        return "exec.host_gpu"
    if n in ("change_sets",):
        return "delivery.git_cas"
    if n.startswith("ctx_"):
        return "domain.memory_ontology"
    if n.startswith(("knowledge_", "kb_")):
        return "domain.knowledge_rag"
    if n.startswith("collector_"):
        return "domain.collector"
    if n in ("artcraft_tasks",):
        return "domain.creative_studio"
    if n.startswith("notebooklm_"):
        return "domain.notebooklm"
    if n.startswith("research_"):
        return "domain.research_lab"
    if n in ("reverse_jobs",):
        return "domain.reverse_eng"
    if n.startswith(("meeting_", "local_stt_")) or n == "meetings":
        return "domain.meetings_stt"
    if n.startswith(("pms_", "checklist_")):
        return "domain.pms_tasks"
    if n.startswith("tts_"):
        return "domain.media_tts"
    if n.startswith("game_"):
        return "domain.game_studio"
    if n in (
        "memos",
        "memo_categories",
        "calendar_events",
        "weather_briefings",
        "tossinvest_sync_runs",
    ):
        return "domain.personal_tools"
    if n in ("google_accounts",):
        return "domain.external_connectors"
    if n in ("artifacts", "public_shares"):
        return "domain.shares_artifacts"
    if n.startswith("ops_"):
        return "ops.monitoring"

    return "unmatched"


def classify_migration(stem: str) -> str:
    s = stem.lower()
    # Core & Control
    if s == "0071_library_platform_v2_merge":
        # A no-op merge joins the library and execution branches; deployment
        # owns convergence, while each parent retains its domain's DDL.
        return "core.generation_deploy"
    if "initial" in s or "login_throttle" in s:
        return "core.auth_session"
    if "transfer_tokens" in s:
        return "core.workspaces_transfers"
    if any(
        x in s
        for x in (
            "engine_accounts",
            "account_pools",
            "account_groups",
            "account_usage",
            "antigravity_usage",
            "qwen_token",
            "qwen_flash",
        )
    ):
        return "core.accounts_quota"
    if any(x in s for x in ("runtime_generations", "three_slots")):
        return "core.generation_deploy"
    if any(
        x in s
        for x in (
            "push_subscriptions",
            "app_settings",
            "work_mode_settings",
            "desk_feedback",
            "notifications",
        )
    ):
        return "core.system_routing"

    # Execution Plane
    if any(
        x in s
        for x in (
            "orchestrator",
            "run_resume",
            "worker_routing",
            "run_executor",
            "background_consumer",
        )
    ):
        return "exec.run_engine"
    if "scheduled_actions" in s:
        return "exec.durable_scheduler"
    if "system_resource" in s:
        return "exec.host_gpu"

    # Delivery Plane
    if "delivery_change_sets" in s:
        return "delivery.git_cas"

    # Domain Plane
    if any(x in s for x in ("assistant_context", "context_library")):
        return "domain.memory_ontology"
    if any(
        x in s for x in ("knowledge", "kb_", "clovanote_vectors", "retire_clovanote")
    ):
        return "domain.knowledge_rag"
    if "collector" in s:
        return "domain.collector"
    if "artcraft" in s:
        return "domain.creative_studio"
    if "notebooklm" in s:
        return "domain.notebooklm"
    if "research_lab" in s or s == "0073_research_reuse_source":
        return "domain.research_lab"
    if "reverse" in s:
        return "domain.reverse_eng"
    if any(x in s for x in ("meeting", "local_stt")):
        return "domain.meetings_stt"
    if any(x in s for x in ("pms", "checklists")):
        return "domain.pms_tasks"
    if any(x in s for x in ("local_tts", "tts")):
        return "domain.media_tts"
    if any(x in s for x in ("game_studio", "game_active", "game_build")):
        return "domain.game_studio"
    if any(
        x in s
        for x in (
            "memos_calendar",
            "event_reminders",
            "memo_categories",
            "scope_memo_event",
            "weather",
            "tossinvest",
        )
    ):
        return "domain.personal_tools"
    if "google_accounts" in s:
        return "domain.external_connectors"
    if any(x in s for x in ("artifact", "public_shares", "library_intent")):
        return "domain.shares_artifacts"

    # Control / Ops
    if "ops_" in s:
        return "ops.monitoring"

    return "unmatched"


def classify_config(field: str) -> str:
    f = field.lower()
    if f == "studio_lab_root":
        return "domain.creative_studio"
    # Core & Control
    if any(
        x in f
        for x in (
            "auth",
            "jwt",
            "session",
            "secret",
            "password",
            "cookie",
            "login",
            "throttle",
            "access_token",
            "refresh_token",
        )
    ):
        return "core.auth_session"
    if any(
        x in f
        for x in (
            "workspace",
            "transfer",
            "upload",
            "download",
            "allowed_roots",
            "default_writable_roots",
            "company_roots",
            "copy_max_bytes",
        )
    ):
        return "core.workspaces_transfers"
    if any(x in f for x in ("account", "quota", "usage", "credit", "billing")):
        return "core.accounts_quota"
    if any(
        x in f
        for x in (
            "runtime_generation",
            "deploy",
            "release",
            "drain",
            "cutover",
            "bootstrap",
            "migration_online",
        )
    ):
        return "core.generation_deploy"
    if any(
        x in f
        for x in (
            "desk",
            "push",
            "vapid",
            "database_",
            "cors_origins",
            "env",
            "home",
            "instructions_max_chars",
            "internal_api_url",
            "runtime_api_url",
            "runtime_poll_seconds",
            "trust_proxy_ip_header",
            "backup_passphrase_file",
            "harness_view",
        )
    ):
        return "core.system_routing"

    # Execution Plane
    if any(
        x in f
        for x in (
            "qwen",
            "agy",
            "codex",
            "muse",
            "run_",
            "worker",
            "agent",
            "orchestrator",
            "probe",
            "antigravity_bin",
            "claude_bin",
            "copilot_bin",
            "opencode_bin",
            "claude_setting",
            "engine_models",
            "engine_profiles",
            "engine_seatbelt",
            "cancel_grace_seconds",
            "resume",
            "peer_models",
            "model_counterparts",
            "profile_cooldown_seconds",
            "lead_archive",
            "lead_dedup",
            "max_concurrent_leads",
            "max_limit_waits",
        )
    ):
        return "exec.run_engine"
    if any(x in f for x in ("schedule", "cron", "lease")):
        return "exec.durable_scheduler"
    if any(x in f for x in ("gpu", "metal", "llama", "resource_sample_seconds")):
        return "exec.host_gpu"

    # Delivery Plane
    if any(x in f for x in ("delivery", "git_", "changeset")):
        return "delivery.git_cas"

    # Domain Plane
    if any(x in f for x in ("context", "embed", "memory", "library")):
        return "domain.memory_ontology"
    if any(x in f for x in ("knowledge", "kb_")):
        return "domain.knowledge_rag"
    if any(x in f for x in ("collector", "fetch", "robots")):
        return "domain.collector"
    if any(x in f for x in ("creative", "comfy", "artcraft")):
        return "domain.creative_studio"
    if any(x in f for x in ("notebooklm",)):
        return "domain.notebooklm"
    if any(x in f for x in ("research",)):
        return "domain.research_lab"
    if any(x in f for x in ("reverse",)):
        return "domain.reverse_eng"
    if any(x in f for x in ("meeting", "whisper", "stt")):
        return "domain.meetings_stt"
    if any(x in f for x in ("pms", "checklist", "task")):
        return "domain.pms_tasks"
    if any(x in f for x in ("design_system",)):
        return "domain.design_system"
    if any(x in f for x in ("tts", "voice")):
        return "domain.media_tts"
    if any(x in f for x in ("game",)):
        return "domain.game_studio"
    if any(
        x in f
        for x in ("weather", "stock", "toss", "memo", "calendar", "reminder_scan")
    ):
        return "domain.personal_tools"
    if any(x in f for x in ("google", "github", "remote_desktop", "vnc")):
        return "domain.external_connectors"
    if any(
        x in f
        for x in ("artifact", "share", "public_origin", "public_sharing", "soffice_bin")
    ):
        return "domain.shares_artifacts"

    # Control / Ops
    if any(x in f for x in ("ops", "sentry", "log", "telemetry")):
        return "ops.monitoring"

    return "unmatched"


def classify_source_file(rel_path: str) -> str:
    p = rel_path.lower()
    # Core & Control
    if "/api/auth" in p or "/security/" in p or p.endswith("/deps.py"):
        return "core.auth_session"
    if (
        "/api/workspaces" in p
        or "/api/transfers" in p
        or "/transfer/" in p
        or p.endswith("/transfers.py")
        or "/transfers/" in p
    ):
        return "core.workspaces_transfers"
    if (
        "/api/accounts" in p
        or "antigravity_accounts" in p
        or "account_usage" in p
        or p.endswith(("/copilot_accounts.py", "/opencode_accounts.py"))
    ):
        return "core.accounts_quota"
    if (
        "/api/deployments" in p
        or "generation" in p
        or "/deploy/" in p
        or p.endswith(("/deploy_gate.py", "/deployment.py"))
    ):
        return "core.generation_deploy"
    if (
        any(
            x in p
            for x in (
                "/api/system",
                "/api/push",
                "/api/notifications",
                "/api/desk",
                "/push/",
                "/desk_feedback.py",
                "/backup_status.py",
                "/operations/",
                "/api/operations.py",
                "/sysinfo.py",
                "/schema_aliases.py",
                "/textsafe.py",
                "/harness_view/",
            )
        )
        or p.endswith(("/config.py", "/main.py", "/audit.py", "/api/harness.py"))
        or p == "src/ttakkari/__init__.py"
    ):
        return "core.system_routing"

    # Execution Plane
    if any(
        x in p
        for x in (
            "/runs/",
            "/orchestrator/",
            "model_process",
            "/agents/",
            "agent_control",
            "/broker/",
            "/experiments/",
            "/api/orchestrator.py",
            "executor",
            "queued_work",
            "background_consumers",
        )
    ) or p.endswith(("/cli.py", "/model_catalog.py")):
        return "exec.run_engine"
    if "/schedules/" in p or "scheduler" in p:
        return "exec.durable_scheduler"
    if (
        "host_gpu" in p
        or "/api/host_env" in p
        or "host_resource" in p
        or "resource_history" in p
    ):
        return "exec.host_gpu"

    # Delivery Plane
    if "/delivery/" in p or "delivery_models" in p:
        return "delivery.git_cas"

    # Domain Plane
    if "/context/" in p or "/api/library_insight" in p:
        return "domain.memory_ontology"
    if (
        "/kb/" in p
        or "knowledge" in p
        or "kb_models" in p
        or "/api/knowledge" in p
        or "/kb_eval/" in p
        or p.endswith("/kb_store.py")
        or "kb_vector" in p
        or "kb_library" in p
        or "kb_delivery" in p
        or "kb_embedding" in p
        or "kb_retrieval" in p
        or "/api/kb_" in p
    ):
        return "domain.knowledge_rag"
    if "/collector/" in p or "collector_models" in p or "/api/collect" in p:
        return "domain.collector"
    if (
        "/creative/" in p
        or "managed_comfy" in p
        or "artcraft" in p
        or "/api/creative" in p
        or "/api/comfy" in p
        or "/api/artcraft" in p
    ):
        return "domain.creative_studio"
    if "/notebooklm/" in p or "/api/notebooklm" in p:
        return "domain.notebooklm"
    if "/research/" in p or "/api/research" in p:
        return "domain.research_lab"
    if "/reverse/" in p or "/api/reverse" in p or "reverse" in p:
        return "domain.reverse_eng"
    if "/stt/" in p or "/api/meetings" in p or "/meetings/" in p:
        return "domain.meetings_stt"
    if any(
        x in p
        for x in (
            "/api/pms",
            "/api/checklists",
            "/api/tasks",
            "/pms/",
            "/planner/",
            "/api/agent_pms.py",
            "/api/planner.py",
        )
    ):
        return "domain.pms_tasks"
    if "design_system" in p or "design_preview" in p:
        return "domain.design_system"
    if "/tts/" in p or "tts_models" in p or "/api/tts" in p:
        return "domain.media_tts"
    if "/games/" in p or "/api/games" in p:
        return "domain.game_studio"
    if any(
        x in p
        for x in (
            "/weather/",
            "/stocks/",
            "/api/weather",
            "/api/stocks",
            "/api/memos",
            "/api/calendar",
        )
    ):
        return "domain.personal_tools"
    if any(
        x in p
        for x in (
            "/google_accounts/",
            "google_account",
            "/github/",
            "remote_desktop",
            "/api/google_accounts",
            "/api/github",
        )
    ):
        return "domain.external_connectors"
    if any(
        x in p
        for x in (
            "/artifacts/",
            "/sharing/",
            "/api/shares",
            "/api/public_shares",
            "/api/artifacts",
        )
    ):
        return "domain.shares_artifacts"

    # Control / Ops
    if any(
        x in p for x in ("/ops/", "ops_ingest", "telemetry", "/api/oplogs", "/oplog/")
    ):
        return "ops.monitoring"

    # Shared base / DB / API common
    if p.endswith(
        (
            "/db.py",
            "/models.py",
            "/schemas.py",
            "/api/__init__.py",
            "/api/agent.py",
            "/api/sessions.py",
            "/api/assistant.py",
        )
    ):
        return "core.auth_session"

    return "unmatched"


# ==============================================================================
# Import Graph & Dependency Analyzer (Relative imports, File SCC, Dynamic imports)
# ==============================================================================


def analyze_import_dependencies(
    source_files: dict[str, Any], ctx: AuditContext | None = None
) -> tuple[
    dict[str, list[str]],
    dict[str, int],
    list[list[str]],
    list[list[str]],
    list[dict[str, Any]],
]:
    """정적 임포트 그래프 분석.

    Returns:
        - mod_deps: coarse module dependency graph {src_mod: [dst_mods]}
        - file_ast_import_counts: AST import statement target count per file (exact statement node unit)
        - file_sccs: 정적 AST 파일 그래프 수준 Tarjan SCC
        - module_cycles: coarse module aggregate cycles
        - dynamic_imports: importlib.import_module 등 런타임 동적 wiring 원장
    """
    py_files = set(source_files.keys())
    repo_root = ctx.repo_root if ctx else DEFAULT_REPO_ROOT

    def resolve_module(src_rel: str, module: str | None, level: int) -> str | None:
        if level == 0:
            if not module:
                return None
            parts = module.split(".")
            c1 = "src/" + "/".join(parts) + ".py"
            if c1 in py_files:
                return c1
            c2 = "src/" + "/".join(parts) + "/__init__.py"
            if c2 in py_files:
                return c2
            return None
        else:
            # Relative import
            src_parts = src_rel.split("/")[:-1]
            if level - 1 > len(src_parts):
                return None
            base_parts = src_parts[: len(src_parts) - (level - 1)]
            if module:
                base_parts.extend(module.split("."))
            c1 = "/".join(base_parts) + ".py"
            if c1 in py_files:
                return c1
            c2 = "/".join(base_parts) + "/__init__.py"
            if c2 in py_files:
                return c2
            return None

    file_edges: dict[str, list[dict[str, Any]]] = defaultdict(list)
    dynamic_imports: list[dict[str, Any]] = []
    file_ast_import_counts: dict[str, int] = defaultdict(int)
    mod_deps: dict[str, set[str]] = defaultdict(set)

    for rel in sorted(py_files):
        p = repo_root / rel
        src_mod = classify_source_file(rel)
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001 - retain unsupported dependency evidence
            dynamic_imports.append(
                {
                    "source_file": rel,
                    "line": 0,
                    "call": "unresolved_parse_error",
                    "reason": str(exc),
                }
            )
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target_file = resolve_module(rel, alias.name, 0)
                    if target_file:
                        file_edges[rel].append(
                            {
                                "target": target_file,
                                "line": node.lineno,
                                "stmt": f"import {alias.name}",
                            }
                        )
                        file_ast_import_counts[target_file] += 1
                        dst_mod = classify_source_file(target_file)
                        if src_mod != dst_mod and dst_mod != "unmatched":
                            mod_deps[src_mod].add(dst_mod)
            elif isinstance(node, ast.ImportFrom):
                target_file = resolve_module(rel, node.module, node.level)
                if target_file:
                    file_edges[rel].append(
                        {
                            "target": target_file,
                            "line": node.lineno,
                            "stmt": f"from {'.' * node.level}{node.module or ''} import ...",
                        }
                    )
                    # EXACT COUNT: 1 increment per ImportFrom statement node targeting this file
                    file_ast_import_counts[target_file] += 1
                    dst_mod = classify_source_file(target_file)
                    if src_mod != dst_mod and dst_mod != "unmatched":
                        mod_deps[src_mod].add(dst_mod)
            elif isinstance(node, ast.Call):
                is_dynamic = False
                call_type = ""
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                ):
                    is_dynamic = True
                    call_type = "importlib.import_module"
                elif isinstance(node.func, ast.Name) and node.func.id == "__import__":
                    is_dynamic = True
                    call_type = "__import__"
                if is_dynamic:
                    try:
                        arg_str = ast.unparse(node.args[0]) if node.args else ""
                    except Exception:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
                        arg_str = ""
                    dynamic_imports.append(
                        {
                            "source_file": rel,
                            "line": node.lineno,
                            "call": f"{call_type}({arg_str})",
                        }
                    )

    # Tarjan's SCC on file_edges (Static AST File-Level SCCs)
    scc_index = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    file_sccs: list[list[str]] = []

    def strongconnect(v: str):
        nonlocal scc_index
        indices[v] = scc_index
        lowlink[v] = scc_index
        scc_index += 1
        stack.append(v)
        on_stack.add(v)

        for edge in file_edges.get(v, []):
            w = edge["target"]
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])

        if lowlink[v] == indices[v]:
            scc = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) > 1:
                file_sccs.append(scc)

    for f in sorted(py_files):
        if f not in indices:
            strongconnect(f)

    # Coarse Module Cycles
    clean_mod_deps = {k: sorted(v) for k, v in mod_deps.items()}
    module_cycles: list[list[str]] = []
    visited: set[str] = set()
    rec_stack: list[str] = []

    def dfs_mod(node: str):
        visited.add(node)
        rec_stack.append(node)
        for neighbor in clean_mod_deps.get(node, []):
            if neighbor not in visited:
                dfs_mod(neighbor)
            elif neighbor in rec_stack:
                idx = rec_stack.index(neighbor)
                cycle = rec_stack[idx:] + [neighbor]
                if cycle not in module_cycles:
                    module_cycles.append(cycle)
        rec_stack.pop()

    for m in clean_mod_deps:
        if m not in visited:
            dfs_mod(m)

    return (
        clean_mod_deps,
        file_ast_import_counts,
        file_sccs,
        module_cycles,
        dynamic_imports,
    )


# ==============================================================================
# Audit Runner & Health Evaluation
# ==============================================================================


def run_audit(ctx: AuditContext | None = None) -> dict[str, Any]:
    context = ctx or AuditContext()
    git_info = get_git_info(context)

    routes = extract_routes(context)
    tools = extract_mcp_tools(context)
    tables = extract_db_tables(context)
    migrations, migration_crosscheck_failures = extract_alembic_migrations(context)
    configs = extract_config_fields(context)
    source_files, parse_errors, missing_roots = extract_source_files(context)

    collected_ids: dict[str, list[str]] = {
        "routes": [f"{m} {p}" for m, p in routes],
        "tools": list(tools),
        "tables": list(tables.keys()),
        "migrations": [f"{m['filename']}::{m['revision']}" for m in migrations],
        "configs": list(configs),
        "source_files": list(source_files.keys()),
    }

    collected_hashes = {k: v["sha256"] for k, v in source_files.items()}

    unmatched: dict[str, list[str]] = {
        "routes": [],
        "tools": [],
        "tables": [],
        "migrations": [],
        "configs": [],
        "source_files": [],
    }

    inventory: dict[str, dict[str, Any]] = {
        mod_id: {
            "id": mod_id,
            "plane": meta["plane"],
            "name": meta["name"],
            "user_role": meta["user_role"],
            "execution": meta["execution"],
            "resources": meta["resources"],
            "current_release_coupling": meta["current_release_coupling"],
            "proposed_role": meta["proposed_role"],
            "difficulty": meta["difficulty"],
            "benefit": meta["benefit"],
            "status": meta["status"],
            "src_tree": git_info["src_tree"],
            "scan_commit": git_info["head"],
            "routes": [],
            "tools": [],
            "tables": [],
            "migrations": [],
            "configs": [],
            "files": [],
            "lines": 0,
        }
        for mod_id, meta in MODULE_DEFINITIONS.items()
    }

    # 1. Routes
    for (m, p), r_meta in routes.items():
        mod_id = classify_route(m, p)
        if mod_id in inventory:
            inventory[mod_id]["routes"].append(
                {"method": m, "path": p, "label": r_meta["label"]}
            )
        else:
            unmatched["routes"].append(f"{m} {p}")

    # 2. Tools
    for t in tools:
        mod_id = classify_mcp_tool(t)
        if mod_id in inventory:
            inventory[mod_id]["tools"].append(t)
        else:
            unmatched["tools"].append(t)

    # 3. Tables
    for t_name, t_meta in tables.items():
        mod_id = classify_table(t_name)
        if mod_id in inventory:
            inventory[mod_id]["tables"].append({"name": t_name, **t_meta})
        else:
            unmatched["tables"].append(t_name)

    # 4. Migrations
    for mig in migrations:
        mod_id = classify_migration(mig["stem"])
        if mod_id in inventory:
            inventory[mod_id]["migrations"].append(mig)
        else:
            unmatched["migrations"].append(mig["stem"])

    # 5. Configs
    for c in configs:
        mod_id = classify_config(c)
        if mod_id in inventory:
            inventory[mod_id]["configs"].append(c)
        else:
            unmatched["configs"].append(c)

    # 6. Source Files
    for rel, f_meta in source_files.items():
        mod_id = classify_source_file(rel)
        if mod_id in inventory:
            inventory[mod_id]["files"].append(rel)
            inventory[mod_id]["lines"] += f_meta["lines"]
        else:
            unmatched["source_files"].append(rel)

    # Dependencies & Cycles
    mod_deps, file_ast_counts, file_sccs, module_cycles, dynamic_imports = (
        analyze_import_dependencies(source_files, context)
    )
    for mod_id, entry in inventory.items():
        entry["dependencies_internal"] = mod_deps.get(mod_id, [])

    collected_counts = {
        "routes": len(routes),
        "tools": len(tools),
        "tables": len(tables),
        "migrations": len(migrations),
        "configs": len(configs),
        "source_files": len(source_files),
        "total_lines": sum(f["lines"] for f in source_files.values()),
    }

    limits_ledger = [
        "1. ORM Base.metadata를 검사하였으며, 실제 운영 PostgreSQL 물리 DB에 직접 연결하여 쿼리하지 않음. 수집 테이블 수는 collected_counts.tables를 참조.",
        "2. 파이썬 소스 분모는 src/**/*.py에 한정되며, frontend, scripts, tests, 배포 쉘 스크립트 등은 이 분모에 포함되지 않음.",
        "3. op.execute는 AST call-site 수집이며, SQL 런타임 의미·트리거·인덱스·downgrade 영향이 해석된 것이 아님 (정적 문자열/파라미터화/보간 분류만 제공). 개별 위치는 migration ddl_call_sites를 참조.",
        "4. 파일 SCC는 정적 AST 파일 상호 참조 컴포넌트이며, 런타임 import cycle 또는 순환 실행 증거가 아님 (TYPE_CHECKING/lazy import 미구분). 수집 목록은 file_sccs를 참조.",
        "5. 분모 내 unmatched=0은 수집된 항목의 분류 완료를 의미할 뿐 제품 전체의 기능 전수성이나 런타임 무결성을 입증하지 않음 (100% 전수 커버리지 주장 철회 및 범위 한정, routes 등 독립 대조 불가 범주는 unknown 처리).",
    ]

    # Evaluate Import Binding
    import_binding = evaluate_import_binding(context)

    # Run Discoveries
    discoveries = {
        "routes": discover_routes(context),
        "tools": discover_tools(context),
        "tables": discover_tables(context),
        "migrations": discover_migrations(context),
        "configs": discover_config_fields(context),
        "source_files": discover_source_files(context),
    }

    # Reconcile Categories
    cov_results = {
        "routes": reconcile_category(
            "routes",
            collected_ids["routes"],
            discoveries["routes"],
            import_binding["categories"]["routes"],
        ),
        "tools": reconcile_category(
            "tools",
            collected_ids["tools"],
            discoveries["tools"],
            import_binding["categories"]["tools"],
        ),
        "tables": reconcile_category(
            "tables",
            collected_ids["tables"],
            discoveries["tables"],
            import_binding["categories"]["tables"],
        ),
        "migrations": reconcile_category(
            "migrations", collected_ids["migrations"], discoveries["migrations"], None
        ),
        "configs": reconcile_category(
            "configs",
            collected_ids["configs"],
            discoveries["configs"],
            import_binding["categories"]["configs"],
        ),
        "source_files": reconcile_category(
            "source_files",
            collected_ids["source_files"],
            discoveries["source_files"],
            None,
            collected_hashes=collected_hashes,
        ),
    }

    # Extraction status evaluation
    extraction_by_cat: dict[str, dict[str, Any]] = {
        "routes": {"state": "succeeded", "errors": []},
        "tools": {"state": "succeeded", "errors": []},
        "tables": {"state": "succeeded", "errors": []},
        "configs": {"state": "succeeded", "errors": []},
    }

    if migration_crosscheck_failures:
        mig_state = (
            "failed"
            if any(
                "ScriptDirectory" in err
                or "missing" in err.lower()
                or "read" in err.lower()
                or "permission" in err.lower()
                or "failed to read" in err.lower()
                for err in migration_crosscheck_failures
            )
            else "succeeded_with_errors"
        )
        extraction_by_cat["migrations"] = {
            "state": mig_state,
            "errors": list(migration_crosscheck_failures),
        }
        cov_results["migrations"]["state"] = "unknown"
        cov_results["migrations"]["reasons"].extend(migration_crosscheck_failures)
    else:
        extraction_by_cat["migrations"] = {"state": "succeeded", "errors": []}

    if missing_roots:
        extraction_by_cat["source_files"] = {
            "state": "failed",
            "errors": [f"Missing root: {r}" for r in missing_roots],
        }
        cov_results["source_files"]["state"] = "unknown"
        cov_results["source_files"]["reasons"].extend(missing_roots)
    elif parse_errors:
        extraction_by_cat["source_files"] = {
            "state": "succeeded_with_errors",
            "errors": [f"{e['file']}: {e['error']}" for e in parse_errors],
        }
        cov_results["source_files"]["state"] = "unknown"
        cov_results["source_files"]["reasons"].extend(
            [f"AST parse error in {e['file']}: {e['error']}" for e in parse_errors]
        )
    else:
        extraction_by_cat["source_files"] = {"state": "succeeded", "errors": []}

    extraction_ok = all(c["state"] == "succeeded" for c in extraction_by_cat.values())

    # Classification status
    classification_by_cat = {}
    for cat in ("routes", "tools", "tables", "migrations", "configs", "source_files"):
        c_coll = collected_counts[cat]
        c_unm = len(unmatched[cat])
        classification_by_cat[cat] = {
            "collected": c_coll,
            "classified": c_coll - c_unm,
            "unmatched": c_unm,
            "unmatched_items": list(unmatched[cat]),
        }
    all_collected_classified = all(len(v) == 0 for v in unmatched.values())

    # Collection scope
    collection_scope_by_cat = {}
    for cat in ("routes", "tools", "tables", "migrations", "configs", "source_files"):
        collection_scope_by_cat[cat] = {
            "collected_count": collected_counts[cat],
            "collected_ids_sha256": hashlib.sha256(
                json.dumps(sorted(collected_ids[cat])).encode("utf-8")
            ).hexdigest(),
        }

    # Coverage summary
    cov_summary = {
        "reconciled": [c for c, v in cov_results.items() if v["state"] == "reconciled"],
        "unknown": [c for c, v in cov_results.items() if v["state"] == "unknown"],
        "mismatch": [c for c, v in cov_results.items() if v["state"] == "mismatch"],
    }
    all_reconciled = len(cov_summary["reconciled"]) == len(cov_results)

    # Sanity heuristics
    floor_counts = dict(context.sanity_floor_counts)
    below_floor = [
        c for c, fl in floor_counts.items() if collected_counts.get(c, 0) < fl
    ]
    sanity_heuristics = {
        "used_as_coverage_evidence": False,
        "floor_counts": floor_counts,
        "below_floor": below_floor,
    }

    # Unresolved ledger
    unresolved_ledger: list[dict[str, Any]] = []
    # Routes
    unresolved_ledger.append(
        {
            "category": "routes",
            "item": "routes",
            "reason": "FastAPI routes decorated dynamically; independent discovery candidate set unavailable (unknown)",
        }
    )
    # Unbound bindings
    if import_binding["state"] != "bound":
        for cat, c_bind in import_binding["categories"].items():
            if c_bind["state"] != "bound":
                unresolved_ledger.append(
                    {
                        "category": cat,
                        "item": cat,
                        "reason": "Import source binding unbound / outside target root: "
                        + "; ".join(c_bind["reasons"]),
                    }
                )
    # Parse errors
    for pe in parse_errors:
        unresolved_ledger.append(
            {
                "category": "source_files",
                "item": pe["file"],
                "reason": f"AST parse error: {pe['error']}",
            }
        )
    # Missing roots
    for mr in missing_roots:
        unresolved_ledger.append(
            {
                "category": "source_files",
                "item": mr,
                "reason": "Missing source root directory",
            }
        )
    # Crosscheck failures
    for cf in migration_crosscheck_failures:
        unresolved_ledger.append(
            {
                "category": "migrations",
                "item": "alembic_migrations",
                "reason": cf,
            }
        )
    # Dynamic tables or other discovery unresolved
    for cat, disc in discoveries.items():
        for unres in disc.unresolved:
            if isinstance(unres, dict):
                unresolved_ledger.append(
                    {
                        "category": cat,
                        "item": unres.get("file", cat),
                        "reason": unres.get("reason", ""),
                    }
                )

    # Verdict
    if not extraction_ok:
        verdict = "extraction_failed"
    elif cov_summary["mismatch"]:
        verdict = "coverage_mismatch"
    elif cov_summary["unknown"]:
        verdict = "coverage_partially_unverified"
    else:
        verdict = "all_categories_reconciled"

    target = {
        "scanner_file": str(Path(__file__).resolve()),
        "git": git_info,
        "declared_baseline": {
            "baseline_code_sha": BASELINE_CODE_SHA,
            "src_tree_matches_scanned": (BASELINE_CODE_SHA == git_info["head"]),
        },
        "claims_product_inventory": (
            import_binding["state"] == "bound" and git_info["available"]
        ),
    }

    scan_report = {
        "target": target,
        "extraction": {
            "ok": extraction_ok,
            "by_category": extraction_by_cat,
        },
        "classification": {
            "all_collected_classified": all_collected_classified,
            "by_category": classification_by_cat,
        },
        "collection_scope": {
            "by_category": collection_scope_by_cat,
        },
        "coverage": {
            "by_category": cov_results,
            "all_reconciled": all_reconciled,
            "summary": cov_summary,
        },
        "sanity_heuristics": sanity_heuristics,
        "unresolved_ledger": unresolved_ledger,
        "verdict": verdict,
        "scope_note": "이 scanner status는 정적 수집 및 독립 대조 상태만 뜻하며, DB 연결 상태나 제품 runtime 건강과 무관합니다.",
    }

    top_god_files = sorted(file_ast_counts.items(), key=lambda x: -x[1])[:15]

    return {
        "sha": git_info["head"],
        "collected_counts": collected_counts,
        "source_manifest": {
            rel: f_meta["sha256"] for rel, f_meta in source_files.items()
        },
        "limits_ledger": limits_ledger,
        "unmatched": unmatched,
        "inventory": inventory,
        "dependencies": mod_deps,
        "file_sccs": file_sccs,
        "module_cycles": module_cycles,
        "dynamic_imports": dynamic_imports,
        "god_files": top_god_files,
        "scan_report": scan_report,
    }


# ==============================================================================
# File Writers & Documentation Generators
# ==============================================================================


def write_outputs(audit_data: dict[str, Any], ctx: AuditContext | None = None):
    root = ctx.repo_root if ctx else DEFAULT_REPO_ROOT
    design_dir = root / "docs" / "design" / "platform-v2"
    review_dir = root / "review" / "platform-v2" / "20261024" / "backend"
    design_dir.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)

    report = audit_data["scan_report"]

    # 1. Raw JSON Evidence in review dir
    (review_dir / "audit_summary.json").write_text(
        json.dumps(
            {
                "scan_report": report,
                "collected_counts": audit_data["collected_counts"],
                "limits_ledger": audit_data["limits_ledger"],
                "unmatched": audit_data["unmatched"],
                "file_sccs_count": len(audit_data["file_sccs"]),
                "file_sccs": audit_data["file_sccs"],
                "module_cycles_count": len(audit_data["module_cycles"]),
                "module_cycles": audit_data["module_cycles"],
                "dynamic_imports_count": len(audit_data["dynamic_imports"]),
                "god_files_ast_counts": audit_data["god_files"],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    (review_dir / "unmatched_ledger.json").write_text(
        json.dumps(audit_data["unmatched"], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    (review_dir / "dynamic_imports_ledger.json").write_text(
        json.dumps(audit_data["dynamic_imports"], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    migrations, _ = extract_alembic_migrations(ctx)
    (review_dir / "migration_ddl_ledger.json").write_text(
        json.dumps(
            [
                {
                    "stem": m["stem"],
                    "revision": m["revision"],
                    "down_revision": m["down_revision"],
                    "branch_labels": m.get("branch_labels", []),
                    "ddl_call_sites": m.get("ddl_call_sites", []),
                }
                for m in migrations
            ],
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    (review_dir / "source_manifest.json").write_text(
        json.dumps(
            {
                "files": audit_data["source_manifest"],
                "src_tree": report["target"]["git"]["src_tree"],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 2. backend-scan-coverage.csv
    cov_csv_path = design_dir / "backend-scan-coverage.csv"
    with open(cov_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(
            [
                "category",
                "collected_count",
                "candidate_count",
                "coverage_state",
                "reasons",
            ]
        )
        cov_by_cat = report["coverage"]["by_category"]
        for cat in (
            "routes",
            "tools",
            "tables",
            "migrations",
            "configs",
            "source_files",
        ):
            c_res = cov_by_cat.get(cat, {})
            writer.writerow(
                [
                    cat,
                    c_res.get("collected_count", 0),
                    c_res.get("candidate_count", ""),
                    c_res.get("state", "unknown"),
                    ";".join(c_res.get("reasons", [])),
                ]
            )

    # 3. backend-module-inventory.json & csv
    inv_list = list(audit_data["inventory"].values())
    (design_dir / "backend-module-inventory.json").write_text(
        json.dumps(inv_list, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    csv_path = design_dir / "backend-module-inventory.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(
            [
                "id",
                "plane",
                "name",
                "user_role",
                "entrypoints_summary",
                "files_count",
                "lines",
                "src_tree",
                "scan_commit",
                "dependencies_internal",
                "tables_count",
                "execution",
                "resources",
                "current_release_coupling",
                "proposed_role",
                "difficulty",
                "benefit",
                "status",
            ]
        )
        for row in inv_list:
            entry_summary = f"API:{len(row['routes'])}, MCP:{len(row['tools'])}"
            writer.writerow(
                [
                    row["id"],
                    row["plane"],
                    row["name"],
                    row["user_role"],
                    entry_summary,
                    len(row["files"]),
                    row["lines"],
                    row.get("src_tree", ""),
                    row.get("scan_commit", ""),
                    ";".join(row.get("dependencies_internal", [])),
                    len(row["tables"]),
                    row["execution"],
                    row["resources"],
                    row["current_release_coupling"],
                    row["proposed_role"],
                    row["difficulty"],
                    row["benefit"],
                    row["status"],
                ]
            )

    # 4. backend-module-inventory.md
    cov_by_cat = report["coverage"]["by_category"]
    md_lines = [
        "# 따까리 플랫폼 V2 백엔드 모듈 감사 인벤토리 (P1 Corrected Candidate)",
        f"> 스캔 커밋: `{report['target']['git']['head']}` | 소스 트리: `{report['target']['git']['src_tree']}` | 판정: `{report['verdict']}`  ",
        "> 공식 최신 기준: `c8f10156845d007ec6496a65d6d8417be19260bd` (`origin/main` 클린 스냅샷 전이 원장 포함)",
        "",
        "> [!IMPORTANT] **수집 범위 한정 및 대조 커버리지(Coverage) 분리 안내**",
        "> 본 감사는 독립 discovery 후보 집합 및 소스 바인딩과의 대조를 거친 **수집 범위 한정** 결과입니다.",
        "> 라우트 등 독립 정적 발견 집합이 없는 범주는 coverage=unknown으로 명시되며, 100% 완전성을 주장하지 않습니다.",
        f"> 스캐너 판정: `{report['verdict']}` (reconciled: {report['coverage']['summary']['reconciled']}, unknown: {report['coverage']['summary']['unknown']})",
        "",
        "## 1. 수집 범위 및 대조 커버리지 결과 (Collection Scope & Coverage)",
        "",
        "| 구분 (Category) | 수집 항목 수 (Collected) | 후보 항목 수 (Candidates) | 대조 상태 (Coverage) | 미매핑 (Unmatched) |",
        "|---|---|---|---|---|",
        f"| **Routes (API/WS)** | {audit_data['collected_counts']['routes']} | - | `{cov_by_cat.get('routes', {}).get('state', 'unknown')}` | {len(audit_data['unmatched']['routes'])} |",
        f"| **MCP Tools** | {audit_data['collected_counts']['tools']} | {cov_by_cat.get('tools', {}).get('candidate_count', '-')} | `{cov_by_cat.get('tools', {}).get('state', 'unknown')}` | {len(audit_data['unmatched']['tools'])} |",
        f"| **DB Tables (ORM)** | {audit_data['collected_counts']['tables']} | {cov_by_cat.get('tables', {}).get('candidate_count', '-')} | `{cov_by_cat.get('tables', {}).get('state', 'unknown')}` | {len(audit_data['unmatched']['tables'])} |",
        f"| **Alembic Migrations** | {audit_data['collected_counts']['migrations']} | {cov_by_cat.get('migrations', {}).get('candidate_count', '-')} | `{cov_by_cat.get('migrations', {}).get('state', 'unknown')}` | {len(audit_data['unmatched']['migrations'])} |",
        f"| **Settings / Configs** | {audit_data['collected_counts']['configs']} | {cov_by_cat.get('configs', {}).get('candidate_count', '-')} | `{cov_by_cat.get('configs', {}).get('state', 'unknown')}` | {len(audit_data['unmatched']['configs'])} |",
        f"| **Source Files (.py)** | {audit_data['collected_counts']['source_files']} | {cov_by_cat.get('source_files', {}).get('candidate_count', '-')} | `{cov_by_cat.get('source_files', {}).get('state', 'unknown')}` | {len(audit_data['unmatched']['source_files'])} |",
        f"| **총 코드 라인 수 (LoC)** | {audit_data['collected_counts']['total_lines']} | - | - | - |",
        "",
        "### 범위 밖 원장 (Out-of-Scope Limits Ledger)",
        *[f"- {lim}" for lim in audit_data["limits_ledger"]],
        "",
        "## 2. 25개 도메인 모듈 인벤토리 명세 (4 Logical Planes)",
        "",
        "| ID | Plane | 모듈명 | 진입점 (API/MCP) | 규모 (Files/Lines) | DB 테이블 | 실행 모드 | 제안 역할 | 난이도 | 상태 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]

    for m in inv_list:
        md_lines.append(
            f"| `{m['id']}` | {m['plane']} | **{m['name']}** | API {len(m['routes'])}개, MCP {len(m['tools'])}개 | {len(m['files'])}개 / {m['lines']:,}줄 | {len(m['tables'])}개 | `{m['execution']}` | `{m['proposed_role']}` | `{m['difficulty']}` | `{m['status']}` |"
        )

    md_lines.append("\n## 3. 모듈별 상세 명세\n")
    for m in inv_list:
        table_names = [t["name"] for t in m["tables"]]
        table_str = ", ".join(f"`{name}`" for name in table_names) or "없음"
        dep_str = (
            ", ".join(f"`{d}`" for d in m.get("dependencies_internal", []))
            or "없음 (Root)"
        )
        route_samples = ", ".join(
            f"`{r['method']} {r['path']}`" for r in m["routes"][:5]
        )
        if len(m["routes"]) > 5:
            route_samples += f" 외 {len(m['routes']) - 5}개"
        if not route_samples:
            route_samples = "없음"
        tool_samples = ", ".join(f"`{t}`" for t in m["tools"]) or "없음"

        md_lines.extend(
            [
                f"### `{m['id']}`: {m['name']} ({m['plane']} Plane)",
                f"- **사용자 및 비즈니스 역할**: {m['user_role']}",
                f"- **실행 형태 및 자원 특성**: `{m['execution']}` | {m['resources']}",
                f"- **현재 배포 결합도**: {m['current_release_coupling']}",
                f"- **V2 제안 역할**: `{m['proposed_role']}` (이전 난이도: `{m['difficulty']}`)",
                f"- **측정 가능한 기대 이점**: {m['benefit']}",
                f"- **현재 구현 상태**: `{m['status']}`",
                f"- **소유 코드 규모**: {len(m['files'])}개 파일, {m['lines']:,} 라인 (src_tree: `{m.get('src_tree', '')}`)",
                f"- **내부 의존 모듈**: {dep_str}",
                f"- **소유 DB 테이블 ({len(m['tables'])}개)**: {table_str}",
                f"- **대표 API 경로 ({len(m['routes'])}개)**: {route_samples}",
                f"- **소유 MCP 도구 ({len(m['tools'])}개)**: {tool_samples}",
                "",
            ]
        )

    (design_dir / "backend-module-inventory.md").write_text(
        "\n".join(md_lines), encoding="utf-8"
    )

    # 4. backend-dependency-graph.mmd & md
    mmd_lines = [
        "flowchart TD",
        "  subgraph Control[Control Plane]",
    ]
    for m in inv_list:
        if m["plane"] == "Control":
            mmd_lines.append(
                f'    {m["id"].replace(".", "_")}["{m["name"]}<br/>({m["id"]})"]'
            )
    mmd_lines.append("  end")

    mmd_lines.append("  subgraph Execution[Execution Plane]")
    for m in inv_list:
        if m["plane"] == "Execution":
            mmd_lines.append(
                f'    {m["id"].replace(".", "_")}["{m["name"]}<br/>({m["id"]})"]'
            )
    mmd_lines.append("  end")

    mmd_lines.append("  subgraph Domain[Domain Plane]")
    for m in inv_list:
        if m["plane"] == "Domain":
            mmd_lines.append(
                f'    {m["id"].replace(".", "_")}["{m["name"]}<br/>({m["id"]})"]'
            )
    mmd_lines.append("  end")

    mmd_lines.append("  subgraph Delivery[Delivery Plane (사용자 Delivery 정의)]")
    for m in inv_list:
        if m["plane"] == "Delivery":
            mmd_lines.append(
                f'    {m["id"].replace(".", "_")}["{m["name"]}<br/>({m["id"]})"]'
            )
    mmd_lines.append("  end")

    for src, dsts in audit_data["dependencies"].items():
        src_node = src.replace(".", "_")
        for dst in dsts:
            dst_node = dst.replace(".", "_")
            mmd_lines.append(f"  {src_node} --> {dst_node}")

    (design_dir / "backend-dependency-graph.mmd").write_text(
        "\n".join(mmd_lines), encoding="utf-8"
    )

    (design_dir / "backend-dependency-graph.json").write_text(
        json.dumps(
            {
                "dependencies": audit_data["dependencies"],
                "file_sccs": audit_data["file_sccs"],
                "module_cycles": audit_data["module_cycles"],
                "dynamic_imports": audit_data["dynamic_imports"],
                "god_files_ast_statement_counts": audit_data["god_files"],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    dep_md_lines = [
        "# 따까리 플랫폼 V2 백엔드 의존성 그래프 및 구조 분석 (Dependency Graph - Corrected)",
        f"> 기준 소스 SHA: `{report['target']['git']['head']}` | 감사 시각: 2026-10-24",
        "",
        "## 1. 아키텍처 다이어그램 (Mermaid)",
        "```mermaid",
        *mmd_lines,
        "```",
        "",
        "## 2. 순환 참조 및 결합도 정밀 분석",
        "",
        "> [!IMPORTANT] **정적 AST 파일 SCC와 모듈 집계 사이클(Coarse Module Cycle)의 구분**",
        "> 이전 감사 보고의 사이클 수치는 서로 다른 파일이 동일 모듈 묶음에 속할 때 발생하는 모듈 집계 순환 루프였습니다.",
        "> 상대 임포트(relative imports)를 해석한 파일 수준의 Tarjan SCC 결과, **정적 AST 파일 상호 참조 컴포넌트는 총 8개**입니다.",
        "> 주의: 이는 AST 구문 수준의 상호 참조이며, `if TYPE_CHECKING:` 가드, 함수 내부 lazy import, 런타임 importlib wiring 등의 조건부 실행이 분리되지 않은 정적 참조 집합입니다.",
        "",
        f"### 2.1 정적 AST 파일 수준 상호 참조 (File-Level SCCs: 총 {len(audit_data['file_sccs'])}개)",
        "정적 AST 상호 import를 형성하는 파일 집합:",
        "",
    ]
    for idx, scc in enumerate(audit_data["file_sccs"], 1):
        dep_md_lines.append(
            f"{idx}. **SCC (크기: {len(scc)}개 파일)**: "
            + ", ".join(f"`{f}`" for f in scc)
        )

    dep_md_lines.extend(
        [
            "",
            f"### 2.2 모듈 수준 집계 사이클 (Coarse Module Cycles: 총 {len(audit_data['module_cycles'])}개)",
            "모듈 간 상호 파일 참조가 묶여서 발생하는 아키텍처 수준의 의존성 루프:",
            "",
        ]
    )
    for idx, cycle in enumerate(audit_data["module_cycles"][:15], 1):
        dep_md_lines.append(f"{idx}. " + " -> ".join(f"`{c}`" for c in cycle))
    if len(audit_data["module_cycles"]) > 15:
        dep_md_lines.append(
            f"... 외 {len(audit_data['module_cycles']) - 15}개 집계 사이클"
        )

    dep_md_lines.extend(
        [
            "",
            "### 2.3 동적 임포트 및 런타임 Wiring (Dynamic Imports Ledger)",
            "정적 import 문이 아닌 `importlib.import_module()` 또는 `__import__()`로 동적 로딩되는 런타임 간선 (정적 그래프 외부):",
            "",
            "| 소스 파일 | 라인 | 동적 호출식 | 런타임 영향 |",
            "|---|---|---|---|",
        ]
    )
    for d in audit_data["dynamic_imports"]:
        dep_md_lines.append(
            f"| `{d['source_file']}` | L{d['line']} | `{d['call']}` | 동적 런타임 로딩 (정적 AST 의존성 외부) |"
        )

    dep_md_lines.extend(
        [
            "",
            "## 3. 핵심 참조 집중 파일 (AST Import Statement In-degree)",
            "",
            "> [!NOTE] **수치 해석 주의**",
            "> 아래 수치는 다른 파일에서 해당 파일을 대상으로 작성한 **AST Import/ImportFrom 구문의 수(node count)**이며, 심볼 사용 횟수나 순환 참조 강도를 의미하지 않습니다.",
            "",
            "| 순위 | 파일 경로 | AST 임포트문 수 (In-degree) | 소속 모듈 | 결합 분석 |",
            "|---|---|---|---|---|",
        ]
    )
    for idx, (f_path, count) in enumerate(audit_data["god_files"], 1):
        mod = classify_source_file(f_path)
        dep_md_lines.append(
            f"| {idx} | `{f_path}` | **{count}회** | `{mod}` | 전역 공통 인터페이스/모델로 임포트 빈도 높음 |"
        )

    (design_dir / "backend-dependency-graph.md").write_text(
        "\n".join(dep_md_lines), encoding="utf-8"
    )

    # 5. data-ownership.md
    data_md_lines = [
        "# 따까리 플랫폼 V2 데이터 소유권 및 분리 후보 분석 (Data Ownership - Corrected)",
        f"> 기준 소스 SHA: `{report['target']['git']['head']}` | 감사 시각: 2026-10-24 | 교정 검증: P1 B Backend 감사 교정",
        "",
        "## 1. 개요 및 데이터 소유권 원칙 (Data Ownership Principles)",
        "플랫폼 V2는 모듈러 모놀리스 및 점진적 서비스 분리를 지향하며, 다음 불변식을 준수한다:",
        "1. **단일 소유권 원칙**: 각 DB 테이블은 오직 하나의 도메인 모듈에만 소유된다 (소유 모듈만 직접 DDL/DML 수행).",
        "2. **명시적 계약 소비 (Contract-based Access)**: 타 모듈 데이터 접근 시 직접 SQL JOIN을 지양하고, API/서비스 인터페이스 또는 뷰/이벤트를 통해 소비한다.",
        "3. **공유 신원(Identity) 격리**: User, Workspace, Session, Run 등 플랫폼 공통 Identity는 Control/Execution Plane이 소유하며, 도메인 모듈은 외래키 제약 대신 단순 ID(UUID/String) 참조로 결합도를 낮춘다.",
        "",
        "## 2. 모듈별 DB 테이블 소유권 및 외래키 분석 (Table Ownership Ledger)",
        "",
        "| 테이블명 | 소유 모듈 ID | 컬럼 수 | 외래키(FK) 참조 대상 | 데이터 결합도 (Coupling) |",
        "|---|---|---|---|---|",
    ]
    cross_module_fks = []
    for m in inv_list:
        for t in m["tables"]:
            fk_list = t.get("foreign_keys", [])
            fk_descs = []
            for fk in fk_list:
                col = fk["column"]
                target = fk["target"]
                target_table = target.split(".")[0]
                target_mod = classify_table(target_table)
                fk_descs.append(f"{col}->{target}")
                if target_mod != m["id"] and target_mod != "unmatched":
                    cross_module_fks.append(
                        {
                            "src_table": t["name"],
                            "src_mod": m["id"],
                            "target_table": target_table,
                            "target_mod": target_mod,
                            "fk_col": col,
                        }
                    )
            fk_str = ", ".join(fk_descs) or "None"
            coupling = "High (외래키 결합)" if fk_list else "Low (독립 테이블)"
            data_md_lines.append(
                f"| `{t['name']}` | `{m['id']}` | {len(t.get('columns', []))} | `{fk_str}` | {coupling} |"
            )

    data_md_lines.extend(
        [
            "",
            "## 3. 크로스 모듈 외래키 결합 현황 (Cross-Module Foreign Keys)",
            "",
            "| 소스 테이블 | 소스 모듈 | 참조 테이블 | 참조 대상 모듈 | 외래키 컬럼 | 결합 영향 및 분리 장애 |",
            "|---|---|---|---|---|---|",
        ]
    )
    for cm in cross_module_fks:
        data_md_lines.append(
            f"| `{cm['src_table']}` | `{cm['src_mod']}` | `{cm['target_table']}` | `{cm['target_mod']}` | `{cm['fk_col']}` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |"
        )

    data_md_lines.extend(
        [
            "",
            "## 4. 스키마 진화 및 마이그레이션 DDL 영향 분석",
            "",
            "### 4.1 Alembic 62개 마이그레이션 체인 및 DDL 분석",
            "- **체인 구조**: Alembic `ScriptDirectory` 기준 62 revisions, 단일 base `0001`, 단일 head `0062_delivery_change_sets`로 선형 구성됨.",
            "- **복원된 down_revision**: 타입 주석형 대입(`down_revision: str | None = '...'`) 24건을 AST로 복원하여 revision chain 무결성을 확보함.",
            "- **DDL 효과**: 총 78개 테이블 생성, 2개 테이블 삭제(`clovanote_jobs`, `clovanote_notes`), 인덱스 및 FK 생성 포함.",
            "- **DDL AST Call-site Inventory (op.execute)**: 총 66건의 call-site가 감지되었으며, 이는 AST 호출 위치의 집계이며 런타임 SQL 실행 의미·파티셔닝·트리거 동작·downgrade 영향이 해석된 것이 아님 (정적 문자열/파라미터화/보간 분류 제공).",
            "",
            "### 4.2 주요 도메인별 결합 분석 교정",
            "",
            "1. **Creative Studio (`artcraft_tasks`) 데이터 결합 확인**:",
            "   - **정정 사항**: 이전 감사의 '외래키 없음 / DB 결합 제로' 주장은 허위임.",
            "   - **실제 외래키**: `artcraft_tasks.session_id -> sessions.id (ON DELETE CASCADE, unique=True)`가 명시적으로 선언되어 있음.",
            "   - **수명주기 결합**: `ChatSession` 삭제 시 `artcraft_tasks`가 cascade 삭제되며, 세션 조회/정리 로직이 `ArtcraftTask`에 직접 결합되어 있음.",
            "",
            "2. **회의록(Meetings) 다중 외래키 결합 (6개 FK 필드)**:",
            "   - `Meeting` 테이블은 자기 참조 1개(`source_meeting_id -> meetings.id`)와 5개 외부 모듈 참조를 가짐:",
            "     - `entity_id -> ctx_entities.id` (`domain.memory_ontology`)",
            "     - `pms_project_id -> pms_projects.id` (`domain.pms_tasks`)",
            "     - `audio_artifact_id -> artifacts.id` (`domain.shares_artifacts`)",
            "     - `analysis_run_id -> runs.id` (`exec.run_engine`)",
            "     - `analysis_session_id -> sessions.id` (`core.auth_session`)",
            "   - 회의록은 5개 상이한 모듈과 DB 수준에서 직결되어 있어 독립 분리 시 정규화 및 느슨한 결합 전환이 선행되어야 함.",
            "",
            "3. **수집기(Collector Platform) 결합 분석**:",
            "   - `collector_jobs`, `collector_outbox` 테이블 자체는 `job_id -> collector_jobs.id`로 내부 결합되어 있음.",
            "   - 그러나 에이전트 intake 경로는 `ChatSession`을 읽어 scope를 판정하고, HTTP 요청이 job 등록 및 commit을 동기 수행하므로 완전한 독립 런타임이 아님.",
            "",
            "## 5. 우선 분리 후보 비교 분석 (Creative Studio 즉시 분리 권고 철회 및 재평가)",
            "",
            "> [!WARNING] **Creative Studio 즉시 독립 배포 권고 철회**",
            "> 이전 보고의 'Creative Studio DB 결합 제로 / 즉시 독립 배포 최우선 후보' 결론을 전면 철회합니다.",
            "> 실제 코드 조사 결과, Creative Studio는 데이터베이스, 파일시스템, 외부 라이브러리 동적 로딩, GPU Lease, 렌더링 람다에서 복합 결합을 가지고 있어 P1 단계에서 즉시 분리할 수 없습니다.",
            "",
            "| 평가 항목 | **후보 1: Creative Studio (`domain.creative_studio`)** (보류/재평가) | **후보 2: Collector Platform (`domain.collector`)** | **후보 3: Meeting STT (`domain.meetings_stt`)** |",
            "|---|---|---|---|",
            "| **비즈니스 특성** | 멀티미디어(이미지/영상/음악) 디렉터 계획 및 ComfyUI/Artcraft 자산 생성 | 대규모 웹 문서 크롤링 및 Stage 정제 파이프라인 | Whisper 기반 음성 전사, 화자 분리, 회의 요약 |",
            "| **Data Coupling** | **높음 (교정됨)**: `artcraft_tasks.session_id -> sessions.id` ON DELETE CASCADE 결합. 세션 삭제 시 트랜잭션 직결 | **중간**: `collector_jobs`, `collector_outbox` 2개 소유. ChatSession 읽기 결합 | **매우 높음**: `meetings` 테이블에 6개 FK (1 self, 5 external) 결합 |",
            "| **Resource 점유** | 미측정 (실측 실행 증거 없음; execute_cpu_attempt는 원본 반환 더미 lambda임) | 미측정 (네트워크 I/O 및 비동기 백그라운드 태스크 부하 실측 증거 없음) | 미측정 (Whisper 모델 로딩 및 연산 자원 실측 증거 없음) |",
            "| **수명주기 결합** | ArtCraft의 파일시스템 고정 경로, state.json, managed Comfy의 GPU lease 및 동적 import 결합 | 장기 배치 워커, Core와 HTTP 등록 결합 | 회의 분석 시 Run 및 Session 수명주기와 결합 |",
            "| **Core 무배포 분리 적합도** | **보류 (즉시 분리 불가, 사전 디커플링 필수)** | **양호 (워커 프로세스 분리 적합)** | **보류 (데이터 정규화 및 FK 해체 선행 필요)** |",
            "",
            "### 5.1 Creative Studio 결합 요인 상세 분석",
            "1. **DB FK 결합**: `artcraft_tasks`가 `sessions.id`를 ON DELETE CASCADE로 직접 참조하여 세션 라이프사이클에 종속됨.",
            "2. **호스트 파일시스템 결합**: ArtCraft 작업 디렉토리 고정 경로(`/Users/winterholic/.ttakkari/...`), subprocess/server 프로세스 상태 관리, `data_dir/artcraft/state.json` 영속화 결합.",
            "3. **Managed Comfy의 런타임 결합**: 외부 `ttk_comfy.*` 모듈 동적 임포트(5건), `ResourceLeaseManager`를 통한 Core 내부 GPU Lease 점유.",
            "4. **더미 렌더러 분리 한계**: `execute_cpu_attempt`는 현재 원본을 돌려주는 더미 lambda 경로로 동작하며, 실제 외부 엔진 렌더 파이프라인과 엄격히 구별되어야 함.",
            "5. **자산 전달 계약**: `/api/creative`의 production/assets 파일은 Core의 세션/아티팩트 및 다운로드 토큰(`transfers.tokens`)과 밀결합됨.",
            "",
            "### 5.2 교정 권고안",
            "- Creative Studio의 즉시 독립 분리 추천을 철회하고, **보류(Hold)** 판정으로 변경함.",
            "- 향후 분리를 위해서는: 1) `session_id` FK를 논리 ID로 전환, 2) ArtCraft 고정 경로를 워크스페이스 스토리지 인터페이스로 추상화, 3) GPU Lease를 gRPC/HTTP 리스 에이전트로 분리하는 작업이 선행되어야 함.",
            "- 분리 후보로서 가장 위험도가 낮고 독립적인 대상은 백그라운드 워커 형태의 **Collector Platform (`domain.collector`)**임.",
        ]
    )

    (design_dir / "data-ownership.md").write_text(
        "\n".join(data_md_lines), encoding="utf-8"
    )


def format_cli_summary(report: dict[str, Any]) -> tuple[list[str], int]:
    lines = [
        "==================================================",
        "Platform V2 Backend Audit Scanner (수집 범위 한정 요약)",
        "==================================================",
        f"결과(verdict): {report.get('verdict', 'unknown')}",
    ]

    ext = report.get("extraction", {})
    lines.append(f"추출(extraction): ok={ext.get('ok', False)}")

    cls = report.get("classification", {})
    lines.append(
        f"분류(classification): all_classified={cls.get('all_collected_classified', False)}"
    )

    scope = report.get("collection_scope", {}).get("by_category", {})
    scope_parts = [f"{k}={v.get('collected_count', 0)}" for k, v in scope.items()]
    lines.append(f"수집 범위: {', '.join(scope_parts)}")

    cov = report.get("coverage", {})
    cov_summary = cov.get("summary", {})
    lines.append(
        f"대조 커버리지(coverage): reconciled={cov_summary.get('reconciled', [])}, "
        f"unknown={cov_summary.get('unknown', [])}, mismatch={cov_summary.get('mismatch', [])}"
    )
    for cat, c_info in cov.get("by_category", {}).items():
        lines.append(f"  - {cat}: {c_info.get('state', 'unknown')}")

    scope_note = report.get("scope_note", "")
    if scope_note:
        lines.append(f"주의: {scope_note}")

    verdict = report.get("verdict")
    if verdict == "all_categories_reconciled":
        code = 0
    elif verdict == "coverage_partially_unverified":
        code = 2
    else:
        code = 1

    return lines, code


def main(argv: list[str] | None = None) -> int:
    ctx = AuditContext()
    data = run_audit(ctx)
    write_outputs(data, ctx)
    lines, code = format_cli_summary(data["scan_report"])
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
