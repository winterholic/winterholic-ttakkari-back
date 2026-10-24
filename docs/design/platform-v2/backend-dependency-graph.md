# 따까리 플랫폼 V2 백엔드 의존성 그래프 및 구조 분석 (Dependency Graph - Corrected)
> 기준 소스 SHA: `1edfc43acc95c4e5a55045300e0b23b9f29b506a` | 감사 시각: 2026-10-24

## 1. 아키텍처 다이어그램 (Mermaid)
```mermaid
flowchart TD
  subgraph Control[Control Plane]
    core_auth_session["인증·세션·보안 제어<br/>(core.auth_session)"]
    core_workspaces_transfers["워크스페이스·파일 전송<br/>(core.workspaces_transfers)"]
    core_accounts_quota["계정 풀·쿼터·토큰 계측<br/>(core.accounts_quota)"]
    core_generation_deploy["세대 배포·수명주기 감독<br/>(core.generation_deploy)"]
    core_system_routing["시스템 상태·설정·알림<br/>(core.system_routing)"]
    ops_monitoring["운영 관측성·오류 수집·배포 로그<br/>(ops.monitoring)"]
  end
  subgraph Execution[Execution Plane]
    exec_run_engine["에이전트 실행 엔진·런타임<br/>(exec.run_engine)"]
    exec_durable_scheduler["내구성 스케줄러·Lease 실행기<br/>(exec.durable_scheduler)"]
    exec_host_gpu["호스트 GPU 가속기·LLM 서빙<br/>(exec.host_gpu)"]
  end
  subgraph Domain[Domain Plane]
    domain_memory_ontology["기억·엔티티 온톨로지 RAG<br/>(domain.memory_ontology)"]
    domain_knowledge_rag["검증 지식 베이스(KB)·증거 추출<br/>(domain.knowledge_rag)"]
    domain_collector["웹 데이터 수집·정제 플랫폼<br/>(domain.collector)"]
    domain_creative_studio["크리에이티브 스튜디오·디렉터·ComfyUI<br/>(domain.creative_studio)"]
    domain_notebooklm["NotebookLM 브라우저 연동<br/>(domain.notebooklm)"]
    domain_research_lab["리서치 랩·실험 벤치마크<br/>(domain.research_lab)"]
    domain_reverse_eng["리버스 엔지니어링·재구성<br/>(domain.reverse_eng)"]
    domain_meetings_stt["회의록·로컬 Whisper STT<br/>(domain.meetings_stt)"]
    domain_pms_tasks["PMS·마일스톤·체크리스트<br/>(domain.pms_tasks)"]
    domain_design_system["디자인 시스템 카탈로그<br/>(domain.design_system)"]
    domain_media_tts["로컬·클라우드 음성 합성(TTS)<br/>(domain.media_tts)"]
    domain_game_studio["게임 스튜디오 프로토타입<br/>(domain.game_studio)"]
    domain_personal_tools["개인 일상 도구(메모·일정·날씨·주식)<br/>(domain.personal_tools)"]
    domain_external_connectors["외부 서비스 커넥터(Google·GitHub·RDP)<br/>(domain.external_connectors)"]
    domain_shares_artifacts["결과물 공유·공개 배포<br/>(domain.shares_artifacts)"]
  end
  subgraph Delivery[Delivery Plane (사용자 Delivery 정의)]
    delivery_git_cas["코드 배포 파이프라인·Git CAS<br/>(delivery.git_cas)"]
  end
  exec_run_engine --> core_auth_session
  exec_run_engine --> core_generation_deploy
  exec_run_engine --> core_system_routing
  exec_run_engine --> delivery_git_cas
  exec_run_engine --> domain_creative_studio
  exec_run_engine --> domain_game_studio
  exec_run_engine --> domain_knowledge_rag
  exec_run_engine --> domain_meetings_stt
  exec_run_engine --> domain_memory_ontology
  exec_run_engine --> domain_pms_tasks
  exec_run_engine --> domain_shares_artifacts
  exec_run_engine --> exec_durable_scheduler
  exec_run_engine --> ops_monitoring
  core_accounts_quota --> core_auth_session
  core_accounts_quota --> core_system_routing
  core_accounts_quota --> exec_run_engine
  core_auth_session --> core_generation_deploy
  core_auth_session --> core_system_routing
  core_auth_session --> domain_collector
  core_auth_session --> domain_knowledge_rag
  core_auth_session --> domain_memory_ontology
  core_auth_session --> domain_pms_tasks
  core_auth_session --> domain_research_lab
  core_auth_session --> domain_shares_artifacts
  core_auth_session --> exec_run_engine
  domain_pms_tasks --> core_auth_session
  domain_pms_tasks --> core_system_routing
  domain_pms_tasks --> core_workspaces_transfers
  domain_pms_tasks --> exec_run_engine
  domain_pms_tasks --> ops_monitoring
  domain_creative_studio --> core_auth_session
  domain_creative_studio --> core_generation_deploy
  domain_creative_studio --> core_system_routing
  domain_creative_studio --> core_workspaces_transfers
  domain_creative_studio --> domain_shares_artifacts
  domain_creative_studio --> exec_host_gpu
  domain_creative_studio --> exec_run_engine
  domain_shares_artifacts --> core_auth_session
  domain_shares_artifacts --> core_generation_deploy
  domain_shares_artifacts --> core_system_routing
  domain_shares_artifacts --> domain_meetings_stt
  domain_shares_artifacts --> exec_run_engine
  domain_collector --> core_auth_session
  domain_collector --> core_system_routing
  domain_collector --> domain_knowledge_rag
  domain_collector --> exec_run_engine
  core_generation_deploy --> core_auth_session
  core_generation_deploy --> core_system_routing
  core_generation_deploy --> core_workspaces_transfers
  core_generation_deploy --> domain_collector
  core_generation_deploy --> domain_creative_studio
  core_generation_deploy --> domain_knowledge_rag
  core_generation_deploy --> domain_media_tts
  core_generation_deploy --> domain_meetings_stt
  core_generation_deploy --> domain_memory_ontology
  core_generation_deploy --> domain_notebooklm
  core_generation_deploy --> domain_personal_tools
  core_generation_deploy --> domain_pms_tasks
  core_generation_deploy --> domain_shares_artifacts
  core_generation_deploy --> exec_durable_scheduler
  core_generation_deploy --> exec_host_gpu
  core_generation_deploy --> exec_run_engine
  core_generation_deploy --> ops_monitoring
  domain_design_system --> core_auth_session
  domain_design_system --> core_system_routing
  domain_design_system --> exec_run_engine
  core_system_routing --> core_auth_session
  core_system_routing --> core_generation_deploy
  core_system_routing --> core_workspaces_transfers
  core_system_routing --> domain_creative_studio
  core_system_routing --> domain_game_studio
  core_system_routing --> domain_media_tts
  core_system_routing --> domain_meetings_stt
  core_system_routing --> domain_memory_ontology
  core_system_routing --> domain_notebooklm
  core_system_routing --> domain_personal_tools
  core_system_routing --> domain_pms_tasks
  core_system_routing --> domain_reverse_eng
  core_system_routing --> domain_shares_artifacts
  core_system_routing --> exec_durable_scheduler
  core_system_routing --> exec_run_engine
  core_system_routing --> ops_monitoring
  domain_game_studio --> core_auth_session
  domain_game_studio --> core_system_routing
  domain_game_studio --> domain_shares_artifacts
  domain_game_studio --> exec_run_engine
  domain_external_connectors --> core_auth_session
  domain_external_connectors --> core_system_routing
  domain_external_connectors --> exec_run_engine
  domain_external_connectors --> ops_monitoring
  exec_host_gpu --> core_auth_session
  exec_host_gpu --> core_system_routing
  exec_host_gpu --> exec_run_engine
  domain_knowledge_rag --> core_auth_session
  domain_knowledge_rag --> core_system_routing
  domain_knowledge_rag --> domain_collector
  domain_knowledge_rag --> domain_memory_ontology
  domain_knowledge_rag --> exec_run_engine
  domain_memory_ontology --> core_auth_session
  domain_memory_ontology --> core_system_routing
  domain_memory_ontology --> domain_knowledge_rag
  domain_memory_ontology --> domain_meetings_stt
  domain_memory_ontology --> exec_run_engine
  domain_meetings_stt --> core_auth_session
  domain_meetings_stt --> core_generation_deploy
  domain_meetings_stt --> core_system_routing
  domain_meetings_stt --> core_workspaces_transfers
  domain_meetings_stt --> domain_memory_ontology
  domain_meetings_stt --> domain_shares_artifacts
  domain_meetings_stt --> exec_run_engine
  domain_notebooklm --> core_auth_session
  domain_notebooklm --> core_system_routing
  domain_notebooklm --> domain_external_connectors
  domain_notebooklm --> exec_run_engine
  ops_monitoring --> core_auth_session
  ops_monitoring --> core_generation_deploy
  ops_monitoring --> core_system_routing
  ops_monitoring --> exec_run_engine
  domain_research_lab --> core_auth_session
  domain_research_lab --> core_system_routing
  domain_research_lab --> domain_knowledge_rag
  domain_research_lab --> exec_run_engine
  domain_reverse_eng --> core_auth_session
  domain_reverse_eng --> core_system_routing
  domain_reverse_eng --> core_workspaces_transfers
  domain_reverse_eng --> exec_run_engine
  domain_personal_tools --> core_auth_session
  domain_personal_tools --> core_system_routing
  domain_personal_tools --> core_workspaces_transfers
  domain_personal_tools --> exec_run_engine
  core_workspaces_transfers --> core_auth_session
  core_workspaces_transfers --> core_system_routing
  core_workspaces_transfers --> domain_shares_artifacts
  core_workspaces_transfers --> exec_run_engine
  domain_media_tts --> core_auth_session
  domain_media_tts --> core_system_routing
  domain_media_tts --> exec_run_engine
  delivery_git_cas --> core_auth_session
  exec_durable_scheduler --> core_auth_session
  exec_durable_scheduler --> core_system_routing
  exec_durable_scheduler --> exec_run_engine
```

## 2. 순환 참조 및 결합도 정밀 분석

> [!IMPORTANT] **정적 AST 파일 SCC와 모듈 집계 사이클(Coarse Module Cycle)의 구분**
> 이전 감사 보고의 사이클 수치는 서로 다른 파일이 동일 모듈 묶음에 속할 때 발생하는 모듈 집계 순환 루프였습니다.
> 상대 임포트(relative imports)를 해석한 파일 수준의 Tarjan SCC 결과, **정적 AST 파일 상호 참조 컴포넌트는 총 8개**입니다.
> 주의: 이는 AST 구문 수준의 상호 참조이며, `if TYPE_CHECKING:` 가드, 함수 내부 lazy import, 런타임 importlib wiring 등의 조건부 실행이 분리되지 않은 정적 참조 집합입니다.

### 2.1 정적 AST 파일 수준 상호 참조 (File-Level SCCs: 총 10개)
정적 AST 상호 import를 형성하는 파일 집합:

1. **SCC (크기: 2개 파일)**: `src/ttakkari/generation.py`, `src/ttakkari/db.py`
2. **SCC (크기: 2개 파일)**: `src/ttakkari/runs/usage.py`, `src/ttakkari/agents/muse.py`
3. **SCC (크기: 4개 파일)**: `src/ttakkari/generation_deployment.py`, `src/ttakkari/generation_bootstrap.py`, `src/ttakkari/deploy_gate.py`, `src/ttakkari/deployment.py`
4. **SCC (크기: 2개 파일)**: `src/ttakkari/context/library.py`, `src/ttakkari/api/assistant.py`
5. **SCC (크기: 3개 파일)**: `src/ttakkari/creative/demo.py`, `src/ttakkari/creative/service.py`, `src/ttakkari/creative/render.py`
6. **SCC (크기: 7개 파일)**: `src/ttakkari/runs/usage_collector.py`, `src/ttakkari/api/sessions.py`, `src/ttakkari/schedules/service.py`, `src/ttakkari/schedules/__init__.py`, `src/ttakkari/generation_service.py`, `src/ttakkari/runs/agent_control.py`, `src/ttakkari/runs/manager.py`
7. **SCC (크기: 2개 파일)**: `src/ttakkari/design_preview.py`, `src/ttakkari/design_system.py`
8. **SCC (크기: 2개 파일)**: `src/ttakkari/meetings/providers.py`, `src/ttakkari/meetings/local_stt/jobs.py`
9. **SCC (크기: 2개 파일)**: `src/ttakkari/reverse.py`, `src/ttakkari/reverse_tasks.py`
10. **SCC (크기: 2개 파일)**: `src/ttakkari/harness_view/projects.py`, `src/ttakkari/harness_view/sources.py`

### 2.2 모듈 수준 집계 사이클 (Coarse Module Cycles: 총 68개)
모듈 간 상호 파일 참조가 묶여서 발생하는 아키텍처 수준의 의존성 루프:

1. `core.auth_session` -> `core.generation_deploy` -> `core.auth_session`
2. `core.auth_session` -> `core.generation_deploy` -> `core.system_routing` -> `core.auth_session`
3. `core.generation_deploy` -> `core.system_routing` -> `core.generation_deploy`
4. `core.auth_session` -> `core.generation_deploy` -> `core.system_routing` -> `core.workspaces_transfers` -> `core.auth_session`
5. `core.system_routing` -> `core.workspaces_transfers` -> `core.system_routing`
6. `core.auth_session` -> `core.generation_deploy` -> `core.system_routing` -> `core.workspaces_transfers` -> `domain.shares_artifacts` -> `core.auth_session`
7. `core.generation_deploy` -> `core.system_routing` -> `core.workspaces_transfers` -> `domain.shares_artifacts` -> `core.generation_deploy`
8. `core.system_routing` -> `core.workspaces_transfers` -> `domain.shares_artifacts` -> `core.system_routing`
9. `core.auth_session` -> `core.generation_deploy` -> `core.system_routing` -> `core.workspaces_transfers` -> `domain.shares_artifacts` -> `domain.meetings_stt` -> `core.auth_session`
10. `core.generation_deploy` -> `core.system_routing` -> `core.workspaces_transfers` -> `domain.shares_artifacts` -> `domain.meetings_stt` -> `core.generation_deploy`
11. `core.system_routing` -> `core.workspaces_transfers` -> `domain.shares_artifacts` -> `domain.meetings_stt` -> `core.system_routing`
12. `core.workspaces_transfers` -> `domain.shares_artifacts` -> `domain.meetings_stt` -> `core.workspaces_transfers`
13. `core.auth_session` -> `core.generation_deploy` -> `core.system_routing` -> `core.workspaces_transfers` -> `domain.shares_artifacts` -> `domain.meetings_stt` -> `domain.memory_ontology` -> `core.auth_session`
14. `core.system_routing` -> `core.workspaces_transfers` -> `domain.shares_artifacts` -> `domain.meetings_stt` -> `domain.memory_ontology` -> `core.system_routing`
15. `core.auth_session` -> `core.generation_deploy` -> `core.system_routing` -> `core.workspaces_transfers` -> `domain.shares_artifacts` -> `domain.meetings_stt` -> `domain.memory_ontology` -> `domain.knowledge_rag` -> `core.auth_session`
... 외 53개 집계 사이클

### 2.3 동적 임포트 및 런타임 Wiring (Dynamic Imports Ledger)
정적 import 문이 아닌 `importlib.import_module()` 또는 `__import__()`로 동적 로딩되는 런타임 간선 (정적 그래프 외부):

| 소스 파일 | 라인 | 동적 호출식 | 런타임 영향 |
|---|---|---|---|
| `src/ttakkari/google_account/registry.py` | L85 | `importlib.import_module('ttakkari.notebooklm.consumer')` | 동적 런타임 로딩 (정적 AST 의존성 외부) |
| `src/ttakkari/managed_comfy.py` | L250 | `importlib.import_module('ttk_comfy.server')` | 동적 런타임 로딩 (정적 AST 의존성 외부) |
| `src/ttakkari/managed_comfy.py` | L251 | `importlib.import_module('ttk_comfy.client')` | 동적 런타임 로딩 (정적 AST 의존성 외부) |
| `src/ttakkari/managed_comfy.py` | L252 | `importlib.import_module('ttk_comfy.runner')` | 동적 런타임 로딩 (정적 AST 의존성 외부) |
| `src/ttakkari/managed_comfy.py` | L253 | `importlib.import_module('ttk_comfy.workflow')` | 동적 런타임 로딩 (정적 AST 의존성 외부) |

## 3. 핵심 참조 집중 파일 (AST Import Statement In-degree)

> [!NOTE] **수치 해석 주의**
> 아래 수치는 다른 파일에서 해당 파일을 대상으로 작성한 **AST Import/ImportFrom 구문의 수(node count)**이며, 심볼 사용 횟수나 순환 참조 강도를 의미하지 않습니다.

| 순위 | 파일 경로 | AST 임포트문 수 (In-degree) | 소속 모듈 | 결합 분석 |
|---|---|---|---|---|
| 1 | `src/ttakkari/models.py` | **149회** | `core.auth_session` | 전역 공통 인터페이스/모델로 임포트 빈도 높음 |
| 2 | `src/ttakkari/config.py` | **119회** | `core.system_routing` | 전역 공통 인터페이스/모델로 임포트 빈도 높음 |
| 3 | `src/ttakkari/runs/__init__.py` | **96회** | `exec.run_engine` | 전역 공통 인터페이스/모델로 임포트 빈도 높음 |
| 4 | `src/ttakkari/__init__.py` | **85회** | `core.system_routing` | 전역 공통 인터페이스/모델로 임포트 빈도 높음 |
| 5 | `src/ttakkari/db.py` | **53회** | `core.auth_session` | 전역 공통 인터페이스/모델로 임포트 빈도 높음 |
| 6 | `src/ttakkari/security/auth.py` | **43회** | `core.auth_session` | 전역 공통 인터페이스/모델로 임포트 빈도 높음 |
| 7 | `src/ttakkari/api/deps.py` | **42회** | `core.auth_session` | 전역 공통 인터페이스/모델로 임포트 빈도 높음 |
| 8 | `src/ttakkari/audit.py` | **31회** | `core.system_routing` | 전역 공통 인터페이스/모델로 임포트 빈도 높음 |
| 9 | `src/ttakkari/collector/__init__.py` | **23회** | `domain.collector` | 전역 공통 인터페이스/모델로 임포트 빈도 높음 |
| 10 | `src/ttakkari/broker/policy.py` | **22회** | `exec.run_engine` | 전역 공통 인터페이스/모델로 임포트 빈도 높음 |
| 11 | `src/ttakkari/runs/manager.py` | **22회** | `exec.run_engine` | 전역 공통 인터페이스/모델로 임포트 빈도 높음 |
| 12 | `src/ttakkari/orchestrator/runtime.py` | **21회** | `exec.run_engine` | 전역 공통 인터페이스/모델로 임포트 빈도 높음 |
| 13 | `src/ttakkari/knowledge/__init__.py` | **17회** | `domain.knowledge_rag` | 전역 공통 인터페이스/모델로 임포트 빈도 높음 |
| 14 | `src/ttakkari/knowledge_models.py` | **17회** | `domain.knowledge_rag` | 전역 공통 인터페이스/모델로 임포트 빈도 높음 |
| 15 | `src/ttakkari/collector/contracts.py` | **14회** | `domain.collector` | 전역 공통 인터페이스/모델로 임포트 빈도 높음 |