# 하네스 조회 2차 개편: 7개 그룹 › 관심사 › 레이어 (ADR 0070, 2026-10-23)

## 요청과 결과
- 사용자 피드백: 초안(평면 목록 + 카테고리 13종 필터)은 어렵다. Claude·Codex·Muse·Qwen Code·Antigravity 메인 하네스, 따까리, 프로젝트별의 7개 그룹을 1차 내비게이션으로 하고, 그룹 안에서 관심사별·레이어(얹히는 순서)별로 보게 해 달라.
- 작업 위치: 메인 작업트리(남의 미커밋 WIP)는 건드리지 않고 origin/main 위의 worktree `back-harness-view`, `front-harness-view`에서 작업했다.
- 시작 시 배포 예약 상태 확인: `de124fae-…`(SHA 08739d5)는 `superseded`였고 `2a8625cf-…`(SHA 474e034a…)가 `succeeded`(2026-10-23 04:03 UTC). 08739d5 는 474e034 와 origin/main 에 포함돼 있어 하네스 1차 API 는 이미 배포돼 있었다.
- 구성: `harness_view/taxonomy.py`(그룹·관심사·레이어 정의와 분류·검증·요약), `extras.py`(Muse·Qwen·Antigravity·Codex 보강 수집), `projects.py`(프로젝트별 수집), `api/harness.py`(그룹·프로젝트 라우트와 필터), `service.py`.

## 결정
- 분류는 수집기 hint 우선, 없으면 category·scope·engine 으로 파생. `taxonomy.validate`가 "각 항목이 정확히 한 그룹·그 그룹의 레이어에 속하고 id 가 유일"함을 시험에서 확인한다.
- Muse 위치는 어댑터와 Muse 번들 스킬 문서에서 확인한 `~/.config/muse`(설정·신뢰·로그인)와 `~/.local/share/muse`(번들 플러그인 스킬)다. 로그인 파일은 존재만 확인한다. 규칙·개인 메모리·사용자 훅·MCP 위치는 확인하지 못해 "없음 + 근거"로 표시했다.
- Qwen Code 는 `qwen-local-harness`(Claude Code `--bare` 를 로컬 Qwen 에 연결)다. `runtime.py` 를 AST 로만 읽어 `--agents` 정의와 고정 인자를 가져온다(변수는 `<이름>` 으로 대체, 실행 안 함). 따까리 qwen-local 어댑터 항목(짧은 계약·설정·활성화)은 세션 주입 레이어로 묶었다.
- Codex `.rules`는 권한·설정, `~/.agents`(공용)는 Antigravity 그룹(Codex 가 읽는지는 확인 필요), Qwen 하네스 저장소는 프로젝트에서 제외(그룹 중복 방지).
- 레이어 순서의 근거는 code/external 로 표시한다. 사용자 → 프로젝트 순서는 엔진 CLI 동작이라 external(확인 필요).
- 대응 항목은 파일형 관심사(지침·설정)만 항목 링크, 나머지는 관심사 링크. 메인 하네스끼리의 지침 짝은 대표 지침 파일만(참조 문서 제외).

## 검증
- 새 시험 `tests/test_harness_view_groups.py` 12개(보안 시험 묶음 `security/gate.py` 에 등록): 소유자 로그인·에이전트 토큰 401/403(새 3개 경로), 모든 항목이 정확히 한 그룹·정의된 레이어에 속함과 카드 합계 = 전체, 그룹 소속 규칙 30여 건(Muse·Qwen·프로젝트·따까리 포함), Muse 로그인 파일 본문 비노출과 Qwen `.runtime/profile` 미열람, 프로젝트별 묶음(프로젝트 → 엔진 → 관심사, 회사 구분, git worktree 제외, 하네스 없는 프로젝트 목록, 키 형식 422), 카나리 10종(Muse auth, Qwen env·profile·conf, 프로젝트 settings env·local·.mcp.json·.env·심볼릭 탈출, Antigravity MCP)이 그룹·프로젝트·검색·상세 전 응답에 없음, 그룹 상세의 레이어 순서·근거·링크·세션 주입, 필터·crumb, 상세의 레이어·대응 항목, 프로젝트 위치 계산.
- 관련 묶음: `pytest tests/test_harness_view_safe.py tests/test_harness_view_api.py tests/test_harness_view_groups.py tests/test_route_inventory.py tests/test_security_gate*.py unit_tests/test_oplog_scrub.py tests/test_runs_events_masking.py tests/test_oplog.py tests/test_execution_capabilities.py tests/test_api_runs.py tests/test_antigravity_model_routing.py` → 통과(최종 숫자는 아래 최종 보고).
- 실데이터(홈의 ~/.claude 등과 개발 폴더, 따까리 홈은 임시): 항목 679개, 빌드 약 1.4초. 그룹별 항목(있음/없음/경고): Claude 328(324/4/29), Codex 18(17/1/0), Muse 34(29/5/1), Qwen Code 14(10/4/0), Antigravity 45(42/3/3), 따까리 194(191/3/14), 프로젝트별 46(프로젝트 16개 중 회사 3, 살펴본 60개 프로젝트, 경고 2).

## 미검증
- 운영 서버 응답과 로그인 UI, 실제 iOS Safari(Chrome 에뮬레이션만), 따까리 홈의 실제 비서 지침·계정·capability 측정(화면의 따까리 그룹은 임시 홈 기준).
- 엔진 CLI 의 로드 순서(external 표시 전부), Muse 규칙·개인 메모리 위치, Antigravity·Codex 의 `~/.agents` 로드 여부.
