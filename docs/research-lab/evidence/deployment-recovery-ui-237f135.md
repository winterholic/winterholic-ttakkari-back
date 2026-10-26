# 누적 배포 교정 통합 및 남은 UI 인수 (2026-10-26)

대상: backend `237f1356d4061acfd2371ee2952bcb782d86f8a0`, frontend `5a2650fde8d5c46f50c9f08ce67f14917f14da98`.
backend는 기존 연구 branch fa7dfa2에 origin/main `77c1238a2b8ade01c096dc74b4f4da3a52c89849`를 충돌 없이 merge했다. `git diff --quiet e9e3260 -- src alembic deploy`는 exit0이었다. 제품·마이그레이션·배포 코드 변경 없이 테스트 격리 교정과 증거를 통합한 것이다.

## 배포 실패와 교정 범위

지원 도구 `backend_deploy_status(d3711123)` 및 `ops_deploy_log` summary/range65~101에서 e9e3260 후보의 release-stage pytest 실패를 확인했다. `1 failed, 2941 passed, 11 skipped, 2 xfailed, 3 warnings, 6 subtests passed in 942.19s`이며 실패는 `test_requeue_before_spawn_keeps_the_resume_handoff[column]`의 `calls.n=0 / succeeded / None`이다. 이는 오류 주입이 호출되지 않은 관측이며 운영 handoff 유실이 입증된 것이 아니다.

Codex 명령 환경/표준 Run PostgreSQL에서 해당 검사만 실행하면 `3 passed, 14 deselected in 1.92s`였다. 앞서 Copilot 테스트를 실행하는 순서로 좁힌 명령은 배포와 같은 값을 재현했다.

```sh
PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest -q --timeout=30 --tb=short tests/test_copilot_integration.py tests/test_run_db_pool_budget.py::test_requeue_before_spawn_keeps_the_resume_handoff -x
# 1 failed, 4 passed in 1.05s; calls.n=0 / succeeded / None
```

기존 main의 `615127d9` 및 `77c1238a`와 `docs/work-history/2026-10-26-p0-test-method-shadow.md` 전문을 대조했다. Copilot 테스트가 전역 manager instance의 상속 메서드를 monkeypatch하면 undo가 bound method를 instance에 남겨 다음 class patch를 가린다. 기존 교정은 defining class를 패치하며, 현재 모형과 달라진 시험 DB 제약은 reflection으로 재구축한다. 제품 코드나 통과 조건은 완화하지 않는다. 이 교정을 다시 만들지 않고 main의 기존 커밋을 통합했다.

```sh
PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest -q --timeout=30 --tb=short -x tests/test_fixture_reset.py tests/test_copilot_integration.py tests/test_run_db_pool_budget.py unit_tests/test_schema_head_admission.py
# 40 passed in 5.47s
```

지원 목록과 예약 조회에서 누적 후보 `77c1238a`의 기존 배포 `2f8896ee-fc2e-4af9-8693-6ebc5fbf179e`가 `testing / skip_tests=false / error=null`임을 확인했다. 이 후보가 e9e3260의 자손이며 위 테스트 교정을 포함한다. 중복 배포는 요청하지 않았다. 전체 controller gate, 활성 revision, frontend/Vercel 및 Metal은 아직 미확인이다.

## 원문 UX5: 두 방법의 조합 결과를 UI에서 읽고 복원

표준 주입 DB를 URL과 실제 SELECT 모두 `ttakkari_test / ttk_test / loopback`으로 단언했다. 현재 Run outbox 격리 home/fixture auth, 실제 앱 ASGI와 runner, 정확 CORS/API57341·frontend57342를 사용했다. 백엔드는 lifespan off여서 운영 background service를 시작하지 않았다. 설정된 ttakkari_browser로 일반 비밀번호 로그인을 하고 온보딩의 건너뛰기를 사용했다. 운영 인증은 쓰지 않았다.

이 Run UI용 연구에서 document_chunking_strategy + hybrid_retrieval의 실제 CPU 4조건을 생성·실행했다. 새 UI fixture를 채우는 실행이며 과거 전체 suite나 성능 벤치를 반복한 것이 아니다. 4개 Run은 실제 succeeded이며 BM25/Hybrid RRF × 없음/fixed_window의 네 입력과 원문이 대응한다. 실제 분석 UI에서 4설정/4실행, Recall@5의 0.0% 변화, n=1 변동성 미상, 설정 차이 128자·overlap16·k5 및 4조합, 청크32/53을 확인했다. 전체 navigation으로 새로고침한 뒤 같은 저장된 결과를 읽었다. 표는 모바일390px에서 가로 스크롤 region을 유지했다. 명시 resize1280px 뒤 데스크톱 표도 캡처했다.

원시 증거: [late-ui-grid-reload.txt](late-ui-grid-reload.txt), [late-ui-evidence-237f135.json](late-ui-evidence-237f135.json), [desktop](late-ui-grid-desktop.png), [mobile](late-ui-grid-mobile.png). 기존 fa7dfa2의 2회 repeat 검증은 재사용한다. 신경망 임베딩·일반 품질 개선·신뢰구간은 주장하지 않는다.

## 원문 UX7: 실제 실행 실패와 연구자 해석의 저장·복원

이전 UI의 synthetic 실패행과 구분하기 위해, 이 Run에서는 실제 retrieval adapter에 `text` 필드가 없는 custom corpus 문서를 전달했다. 정상 생성/실행 계약을 거친 runner가 `retrieval_eval.py:407 text = doc["text"]`에서 실제 `KeyError: 'text'`를 잡아 실패 Run으로 저장했다. DB 상태를 직접 주입하거나 adapter를 monkeypatch하지 않았다. 잘못된 입력의 실험이며 검색 알고리즘 품질 실패나 운영 장애는 아니다.

분석 UI에서 실패 Run의 `KeyError: 'text'`, 성공값이 없어 비교 차트를 그리지 않는 상태, 실패를 평균에 포함하지 않는 안내를 확인했다. 해당 행의 **실패 원인 해석 남기기**에서 다음 행동을 했다.

1. `실제 KeyError: 입력 코퍼스의 text 누락` 제목과 관측·후속 가설을 입력하고 해석 저장.
2. 전체 페이지 navigation으로 새로고침.
3. 같은 분석 화면에서 원문 실패 메시지와 연결 Run/설정 배지, `연구자 해석 (미검증)` 노트가 함께 보이는 것을 확인.
4. 별도 Python 프로세스/DB 세션과 새로운 HTTP resume 요청으로 Node 저장 및 연결을 재조회.

마지막 readback 출력: `{"notes": 1, "epistemic_status": "unverified", "error_code": "KeyError", "measurement_count": 0, "fresh_resume_http": 200}`. 자동 수치 요약이 원인을 해석한 것이 아니라는 UI 문구도 유지됐다. 원문: [late-ui-failure-reload.txt](late-ui-failure-reload.txt), [mobile screenshot](late-ui-failure-mobile.png). raw JSON에 해당 입력·실패·Node 세부·resume 근거와 screenshot SHA256을 보존했다. 브라우저 navigation 뒤 network 목록에서 resume/detail/compare GET200도 확인했다. 저장 POST 상태 코드는 navigation 전 별도로 수집하지 않아 단정하지 않는다.

Vite 개발 서버의 /sw.js가 text/html MIME으로 응답한 service-worker 오류 2개가 남았다. 기존 개발 서버 관측과 같은 범위이며 운영 PWA 정상 여부로 확대하지 않는다. 검증 후 브라우저를 닫고 두 fixture 서버의 종료(exit0)를 확인했다. 운영 DB 변경·실제 TTS 재합성·GPU start는 없었다.

## 다음 실행

기존 controller `2f8896ee`가 필수 전체 gate를 소유한다. 다음 보고/Run에서 결과를 한 번 복원하고, 활성 revision을 확인한 뒤 frontend main/Vercel → A 실제 Metal/RuntimeLease/raw → 운영 manage_research MCP → R1~R16/PMS 최종 대조를 잇는다. 원문 UX5/7의 위 격리 UI 공백은 보완했으며 운영 검증으로 확대하지 않는다. PMS7의 원래 확장 조건도 자동 done 처리하지 않는다.

후속 2026-10-26: 위 testing 표시는 당시 상태다. 2f8896ee는 3161pass 뒤 transfer 진단 로그 수집 검사에서 실패했다. migration→transfer 순서로 별개 로그 격리 결함을 재현하고 시험 helper만 교정한 근거는 [migration-logging-isolation-fix.md](migration-logging-isolation-fix.md)다. 이 문서의 기존 실제 UI 인수는 유효하다. 다음 예약 ID/실행 소유자는 manage_work 최신 revision에서 복원한다.
