# 최종 UI 보완·main 통합 영수증 (2026-10-26)

대상: backend 564d497ccee2222cfede9c1dfa1fd687c3603716 (e08ea82d + origin/main 320e351의 merge), frontend 5a2650fde8d5c46f50c9f08ce67f14917f14da98.

## 인수 범위

독립 검증자 7213770b의 e08ea82d 보고서와 JSON을 직접 대조했다. k=5 원본 충실 재사용·k=7 명시 변경, 성공/합성 실패행 해석 저장·reload, 최선 결과 채택·reload는 해당 보고의 실제 UI/API/PG 범위로 인수한다. 실패행은 synthetic fixture이며 실제 adapter 실패 실증으로 승격하지 않는다. 과거 API15·0073/FK·runner 등록실패 검증을 반복하지 않았다.

누락된 파일 연결은 리드가 통합 revision에서 직접 보완했다. 표준 주입 Run DB를 database=ttakkari_test/user=ttk_test/loopback으로 단언한 뒤 실제 retrieval_eval CPU preset ko_tech_docs_v1의 BM25/lexical_overlap A/B를 k=5 동조건에서 실행했다. 두 Run은 fresh API에서 succeeded였다. 채택 API로 설정/Run/목표/지표/제약을 저장한 뒤 실제 브라우저 로그인→기록·결과물→리포트 만들기→파일 보관함에도 보관→생성→전체 reload→열기·내려받기를 수행했다. 실제 adapter 결과의 Markdown 리포트이며 WAV를 만들었다고 주장하지 않는다. 단일 반복 bounded fixture로 일반 성능 향상을 주장하지 않는다.

새 프로세스/API 요청/DB 세션으로 BestResult 보존, experiment ResearchArtifact 연결, Artifact area=files 및 keep_reason, metadata/content 각 HTTP200을 확인했다. API content·저장파일·브라우저 다운로드의 662바이트와 SHA256이 일치했다: 4964ec0cfabebfd8735b6b912c94b0595ff1221472883c1d27f4657136ccffb6. 식별자와 원시 영수증은 lead-artifact-ui-564d497.json.

브라우저: http://127.0.0.1:57173 / API57172, lifespan off의 실제 앱, 격리 home/fixture 인증, 정확 CORS. 서버 두 개와 브라우저는 종료했다. 새 로그인 onboarding dialog는 정상 건너뛰기로 닫았다. Vite /sw.js MIME 오류는 개발 서버에서만 관측했고 운영 빌드는 sw.js를 생성했다. 운영 브라우저/PWA 동작 보증은 아니다.

## 통합과 실행한 검증

- git fetch origin; git merge-tree --write-tree HEAD origin/main → exit0. git merge --no-ff origin/main → 564d497, 충돌 없음. 연구 본체/API/host_gpu는 e08ea82d 대비 diff 없음.
- scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest -q --timeout=30 --tb=short tests/test_research_v2_core.py tests/test_research_v2_api_contract.py tests/test_research_v2_e2e.py tests/test_research_adapters.py tests/test_stage2_defects_fix.py tests/test_scenario_b_artifacts.py tests/test_research_v2_migration.py tests/test_host_gpu_device_logging.py tests/test_route_inventory.py tests/test_run_db_pool_budget.py → 174 passed in 10.58s.
- scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest -q --timeout=30 --tb=short -m contract → 84 passed, 5307 deselected in 7.31s.
- frontend pnpm exec vitest run src/pages/research → 75 passed; pnpm build → exit0 (tsc-b+Vite, precache93/sw.js生成); pnpm lint → exit0 (ui-lint baseline138, 새 증가0).

## 실패·미확인 범위

Codex command sandbox의 추가 host_operations 회귀는 63 passed/10 failed/39 deselected (19.12s). tests/test_host_operations.py::test_runner_sigkill_before_child_identity_is_recorded_is_recovered_from_child_pidfile은 psutil.children→sysctl malloc에서 PermissionError Operation not permitted를 출력했다. 여러 runner test는 subprocess.Popen preexec_fn에서 SubprocessError로 중단했다. _child_setup은 setpriority 및 pidfile 기록이지만 정확한 실패 줄은 출력에 없어 원인은 추론이다. inline report OSError 및 backup/snapshot 결과실패도 있어 전부 동일 원인으로 단정하지 않는다. 이 소스는 통합한 origin/main 내용이며 이번 연구 변경이 아니다. 다른 엔진/명령으로 해당 거부를 우회하거나 같은 실패를 다시 실행하지 않았다.

별도 tests/test_host_gpu.py::test_supervisor_runs_until_stopped_and_cleans_up은 ready 대기에서 pytest-timeout15초로 실패했다. 실제 GPU를 쓰지 않는 fake supervisor 시험이며 원인은 미확정이다. 처음 넓은 테스트 묶음도 이 파일에서 여러 F 이후 중단(exit130)했으므로 전체 통과로 세지 않는다. 최초 잘못된 tests/test_research_core.py 경로 지정은 no tests ran(exit4); 올바른 v2_core를 쓴 위174건만 채택한다. helper의 첫 seed는 별세션 실행 뒤 반환 ORM Run 상태를 그대로 검사해 실패했다. fresh API로 상태를 재조회하게 바꾼 후 위실측을 수집했다. helper실패는 제품실패로 분류하지 않는다.

main fast-forward push와 실제 배포 완료 판정은 이 문서 작성 시 미실행. backend 검증 후보를 정상 feature branch로 push하고 독립 배포 controller의 호스트 gate에 제출할 예정이다. 알려진 sandbox 실패가 포함된 main pre-push 검사를 우회하지 않는다. controller의 수락은 성공이 아니다. frontend main/Vercel 배포, A 실제 Metal/RuntimeLease/raw, 운영 스모크, PMS 미완 대조는 후속 필수다. 전체 목표는 active로 유지한다.
