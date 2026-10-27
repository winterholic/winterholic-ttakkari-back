# 에이전트 보상 복구와 회사 세션 도구 권한 (2026-10-28)

## 요청과 운영 근거

사용자는 다양한 실행 오류로 중단된 에이전트와 오래된 quota reset 날짜를 기다리는 세션의 자동 감시·복구, 회사 세션의 불필요한 작업 권한 제한 제거를 요청했다. 개인·회사 화면 구분과 회사 AI 계정 선택 정책은 유지한다. 사용자 취소를 임의로 취소하는 요청으로 해석하지 않는다.

기준 revision은 c70bce7ba8ab888ea4989f2ab3ef887ff41f0809, 작업공간은 `/Users/winterholic/development/ttakkari-recovery-permissions-20261028`이다. 기존 `projects/winterholic-ttakkari/back`의 다른 작업 변경은 보존했다.

2026-10-28 07:03 KST `host_operation_start(kind=runtime_report)`는 active local API c70bce7b와 offline generation f84f2b63의 lease/process-group 각각 2개를 반환했다. 해당 Run의 재개 성공은 아직 확인하지 않았다. 첨부 IMG_3783.jpeg는 회사 세션의 ops 로그 403 차단을 보여 준다. 소스 `api/oplogs.py`의 pool 조건과 일치한다.

## 변경한 동작

- active coordinator가 기존 queue dispatch와 함께 보상 처리를 수행한다. 보상 한 건의 오류가 일반 queue 처리를 막지 않는다. standby/draining은 새 보상을 수행하지 않는다.
- 기존 executor orphan의 owner PID/start-time, lease token, process-group ledger, 제어행 잠금을 재사용한다. 소유자가 죽고 그룹 종료가 확인된 경우만 소유권을 회수한다. UNKNOWN은 유지한다. 프로세스 종료 여부를 heartbeat 나이만으로 단정하지 않는다.
- 기존 회수기는 running만 처리했다. queued/cancelled/terminal 상태 저장 직후 소유자가 죽은 경우에도 상태 자체는 보존하고 잔류 lease를 회수한다. running인 경우에만 interrupted로 전환한다.
- 일시 연결/서버/DB 풀 오류와 확인된 중단은 같은 Run ID로 30/60/120초 뒤 최대 3회 재시도한다. checkpoint, 공급자 대화 키, 최근 진행과 이전 인계를 보존한다. 재시도 전에 이미 적용된 파일·외부 부수효과를 확인하도록 인계한다. 프로바이더/도구 동작의 exactly-once를 주장하지 않는다.
- 취소 명령이 있는 Run, 새 지시가 있는 세션, 보관된 세션, 완료/일시정지 계약과 그 자식은 되살리지 않는다. 정책·인증·도구 이름·모델 지정 오류를 quota로 위장하지 않는다. 영속 예산 소진 뒤에는 실패 원인이 남는다.
- quota 대기 당시보다 나중의 신선한 사용 가능 관측은 과거의 계정/모델 풀 reset 힌트를 해제한다. stale/unknown/exhausted 관측과 별도 model-capacity cooldown은 보존한다. 다른 적합 계정/모델의 현재 가용성은 기존 라우팅 정책으로 재평가한다.
- 회사 세션의 운영 로그·배포 예약/조회·환경/GPU·운영 도구·디자인 자료·역분석·NotebookLM·Google 상태·주식 상태·Creative/Comfy 도구의 일괄 403 차단을 제거했다. 세션 인증, 고정 입력·서비스 소유권, 로그 마스킹·비밀 보호는 유지한다. 역분석 Run은 요청 세션의 AI 계정 pool을 이어받으며 디렉터리 위치로 덮지 않는다.
- `github_accounts`와 `github_switch_account(account)`는 양쪽 세션 그룹에 제공된다. 등록된 github.com login만 받으며 backend가 고정 `gh auth switch`를 실행한다. 전환은 DB advisory lock으로 직렬화하고 활성 계정 재조회가 확인돼야 성공이다. 자격증명은 응답하지 않는다. GitHub 탐색 API는 계속 요청별 계정 인증을 사용한다. 전역 활성 계정은 다른 세션의 명시 전환으로 이후 바뀔 수 있다.
- runtime_report에는 활동 중인 Run의 소유권·대기시각·보상 횟수 메타데이터를 최대 40개 보여 준다. prompt/result/토큰 원문은 포함하지 않는다.

## 검증 범위와 남은 작업

새 테스트에서 기능 부재·구버전 동작의 실패를 먼저 관측했다. 보상/회사 서비스/GitHub 전환/기존 quota·세대·프로세스 소유권·역분석·NotebookLM·도구 인벤토리 회귀를 격리 PostgreSQL 및 합성 엔진으로 검사한다. 최종 실행 수치는 아래에 추가한다.

Codex nested command sandbox에서 넓힌 호스트 검사 일부는 psutil/sysctl EPERM, runner preexec_fn 및 GPU supervisor OSError로 실행되지 않았다. 이는 해당 sandbox 범위이며 배포 backend나 host GPU 경로의 불가를 뜻하지 않는다. 같은 차단을 그대로 재시도하지 않는다. 기존 source worktree에 없던 `.venv`는 허용된 프로젝트 환경을 연결해 정적·격리 DB 검사에 사용한다.

운영 배포 후 실제 dead-owner lease 2개와 미래 quota wait가 해소됐는지 확인해야 한다. unknown 프로세스, 현재 소유자가 살아 있지만 메모리 task를 잃은 lease, 비재시도 오류는 별도로 남을 수 있으므로 정상화 여부를 단정하지 않는다. 기존 개인/회사 자료의 명시 cross-scope 접근·다른 세션 작업 제어처럼 남은 권한 조건도 사용자 의도와 대조할 후속 범위다. 전체 목표는 아직 active다.

## 실행 기록

- `PYTHONPATH=src .venv/bin/python -m pytest -q`에 route/security inventory, compensation, work service, GitHub switch, worker quota, host scope, reverse/reconstruction, deployment, oplog, creative MCP, NotebookLM, generations/processes, executor/reconcile, GitHub accounts 및 변경한 host/Google 권한 사례 23개 파일/노드 묶음을 지정: **311 passed in 34.45s**. 테스트 입력/회수 대상은 격리 DB와 합성 프로세스 증거이며 운영 복구 성공 증거는 아니다.
- 변경 Python 파일 전체 `ruff check`: `All checks passed!`. `git diff --check`: exit 0.
- source origin/main이 작업 중 `038067ae`로 진전했다. collection recovery API 변경을 보존하여 통합한다. 통합 후 계약/관련 회귀를 다시 검사한다.

- origin/main `038067ae`의 collection mission recovery API와 이번 변경을 통합했다. shared operations 인증 함수가 이제 owner의 양쪽 세션 그룹을 허용하므로 collection 진단/기존 임무 재시도도 같은 권한이 적용된다. 과거 038067ae의 개인 한정 결정은 이번 사용자의 명시적 제한 완화 요청으로 대체되며 tenant=owner·활성 작업 보존·사용자 pause/cancel·예상 회차 검사는 유지한다. MCP 역할/읽기전용 목록 및 화면 도구 분류도 이에 맞춘다.
- runtime_report의 실제 SQL 및 회사 도구 권한·보상 회귀 31 passed in 1.61s.

- 통합 뒤 `tests/test_route_inventory.py tests/test_security_gate_inventory.py tests/test_run_compensation.py tests/test_work_service_access.py tests/test_github_account_switch.py tests/test_collection_agent.py tests/test_worker_quota_recovery.py tests/test_orchestrator.py` 재검사: **108 passed in 16.10s**. 겹치지 않는 기존 기능 검증(311/31)과 묶어 검토한다. 마지막 변경 Python ruff 및 diff whitespace 통과.

- 두 번째 origin/main 진전 `0730559a`의 Muse binding/발견 guard/인자 provenance 수정도 보존하여 재통합했다. 겹치는 런타임 경계와 MCP 역할·권한·새 Muse 회귀 묶음: **122 passed in 21.32s**.
- Codex nested command 환경에서 `git -c credential.username=winterholic push origin HEAD:main`과 표준 gh git-credential helper 지정은 모두 `could not read Password ... Device not configured`로 끝났다. 이 결과는 credential 공급 실패이며 sandbox 권한 거부나 해당 저장소의 GitHub 403을 관측한 것은 아니다. `gh auth status`의 토큰 없는 metadata는 daeseon-dev active/winterholic inactive, 둘 다 success/keyring을 반환했다. 등록된 winterholic 자격을 Git 프로토콜에만 전달하는 임시 계정별 helper를 준비했다. 파일에는 자격증명 값을 저장하지 않는다. 전역 활성 계정 전환은 수행하지 않았다.

- 계정별 credential helper 사용 시 인증은 pre-push 단계까지 진행됐다. 첫 pre-push의 기본 uv cache는 Codex command sandbox에서 초기화되지 않아, 다음 시도는 허용된 `/private/tmp/ttakkari-uv-cache-ec779f15`를 사용했다. 같은 거부 경로를 다시 열지 않았다.
- pre-push impacted 모드의 ruff와 계약 묶음 **84 passed**. 변경 테스트 파일 전체는 **381 passed, 24 failed**. 1건은 Google 도구 표시 대상의 낡은 개인 한정 기대값이어서 모든 실행으로 수정했다. 나머지 23건은 이 Codex nested 환경에서 호스트 process/sysctl·GPU supervisor·operation runner 실행 경계 검사에 실패했다. sysctl EPERM·preexec_fn 예외·supervisor OSError는 관측됐으며, 나머지 집계 실패까지 같은 제약의 영향이라는 설명은 코드·경로에 근거한 추론이다. 해당 범위는 미검증으로 기록한다. 해당 검사를 같은 환경에서 그대로 반복하지 않는다.
- 검증된 변경을 게시하기 위해 저장소가 문서화한 로컬 pre-push 생략 옵션을 사용하고, 배포 컨트롤러의 전체 pytest·보안·프로토콜·ready/ownership 검증은 정상 실행하도록 예약한다. 이는 운영 검증 통과 주장이 아니다. 전역 GitHub 활성 계정은 바꾸지 않으며 자격증명은 Git 프로토콜 안에서만 전달한다.

- Google 계정 회귀 전체(설명 기대값 교정 포함): **27 passed in 4.47s**.
