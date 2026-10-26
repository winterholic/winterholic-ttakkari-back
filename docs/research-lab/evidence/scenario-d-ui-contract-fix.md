# 시나리오 D 실제 UI 인수와 프론트 계약 교정

2026-10-26. backend `12aaf56d30e04d0e6bfc52523963e05663759297`,
frontend 기존 `5a2650f` → 교정 `7341245fe230a14728ea61fa5075748827cf8ed1`.
이번 backend 배포 조회는 `02141811-90f9-4145-afc2-17e1ee98de01 testing / skip_tests=false / error=null` 1회다.
운영 활성화·Metal·Vercel 인수는 남아 있으며 배포 대기 폴링이나 신규 예약은 하지 않았다.

## 실제 관측과 최소 변경

격리 Run PostgreSQL `ttakkari_test/ttk_test`의 실제 identity를 확인하고,
실제 ASGI 앱(lifespan off)과 frontend Vite, 설정된 ttakkari_browser로 비밀번호 로그인했다.
운영 인증·DB는 사용하지 않았다. 빈 Run DB는 기존 Alembic을 `Config()`/절대 script_location으로 head까지 올렸다.

새 연구→가설 등록 직후 실행 상태가 `undefined`였다([before](scenario-d-create-before.png)).
POST/PATCH/promote 응답은 저장된 정의이며 런타임 readiness는 GET 상세에서만 계산된다.
프론트의 세 mutation wrapper가 결과를 완성된 Method로 취급한 계약 불일치였다.
각 mutation 뒤 저장된 ID를 같은 scope의 기존 getMethod로 읽도록 교정했다.
서버 상태값을 추정하거나 고정하지 않으며, 대기 중 범위가 바뀌어도 처음 scope를 유지한다.

외부 결과 폼은 기본 provenance가 external_benchmark인데 출처 title/locator 입력·전송이 없었다.
서버의 기존 제목+locator 필수 정책에 맞게 두 필드와 제출 가드를 추가했다.
manual_evaluation/imported_log로 바꾸면 이 필드는 전송하지 않는다.
백엔드 정책·DB 스키마·계측 코드는 변경하지 않았다.

## RED/GREEN 및 범위

```sh
pnpm exec vitest run src/pages/research/method-mutation-contract.test.ts src/pages/research/external-report-form.test.tsx --reporter=dot
# 교정 전: 5 failed (readiness undefined 3건, 필수출처 가드/입력 누락 2건)
# 교정 후: 5 passed, 558ms
pnpm exec vitest run src/pages/research --reporter=dot
# 80 passed in 3.89s
pnpm run build
# tsc 및 Vite exit0, sw.js 생성. chunk-size warning 잔존.
pnpm run lint
# exit0, ui-lint 138/기준선138. 기존 MethodPicker key warning은 test stderr에 잔존.
git diff --check
# exit0
```

## 원문 D의 저장·복원·재사용

1. 화면에서 연구 `75dbeb47-18a1-442e-bcda-fef1cb43f86e`와 커널 접근 가설을 등록.
   대상 LLM/계층 GPU kernel/목표 speed/가설/metric decode_tps를 폼에서 지정했다.
2. 직접 실행 미지원 안내와 외부 보고 설계를 확인. 외부 보고 실험 `e2ad7ec4-8736-45d3-9188-ee95e1ec353d`를 만들고
   수동 합성 값1 tok/s를 입력. 실제 하드웨어 측정이 아니라는 환경·메모를 보존했다.
3. 성공한 외부 보고를 근거로 판단 메모와 함께 후보 승격. UI는 ‘외부 결과만 있으므로 후보까지’이며 검증됨 선택을 제공하지 않았다.
   전체 navigation 후 [후보(외부 결과만)·어댑터 필요·실측 없음·미검증](scenario-d-candidate-reload.png)을 확인했다.
4. 별도 연구 `dbfdd5b1-0783-41b3-ba22-3f480b93f49c`에서 같은 카탈로그 method를 선택해 후속 실험
   `18f873bc-7535-4d98-9bfa-c717801a4ce1`을 화면으로 생성했다.
   새 출처 필드로 `Synthetic UI benchmark fixture (실제 논문 아님)`과
   `https://example.invalid/d-ui-benchmark#table-1`을 입력하고 보고를 등록했다.
   전체 navigation 뒤 실행 상세에서 출처/합성 환경/메모/1 tok/s 및 ‘따까리 실측 아님’을 읽었다([화면](scenario-d-source-reload.png)).
5. 수정된 새 가설 등록을 한 번 더 수행해 등록 직후 [가설 단계·미지원 사유·다음 행동](scenario-d-create-after.png)을 확인했다.
   첫 실제 브라우저가 변경 전 Vite 모듈을 계속 제공해서 소스와 HTTP module을 대조했고,
   이 Run 소유 Vite만 종료/재기동(TTAKKARI_NO_ENV_FILE=1)한 뒤 수정된 UI로 인수했다. 제품 오류로 확대하지 않는다.

별도 Python 프로세스의 fresh HTTP 8회와 새 SQLAlchemy DB connection으로 저장을 대조했다.
`PYTHONPATH=src .venv/bin/python -P /private/tmp/research-d-readback-5e1c.py` 실제 출력:

```json
{"fresh_http_reads":8,"candidate":"candidate","execution_status":"adapter_required","local_measured_runs":0,"followup_same_method":true,"source_raw_env_metric":true,"db":["ttakkari_test","ttk_test"]}
```

원문 응답/별도 DB rows는 [scenario-d-ui-readback-12aaf56.json](scenario-d-ui-readback-12aaf56.json),
상태/후보/출처 DOM은 `scenario-d-ui-{created,candidate,external}.txt`다.
source title/URL은 raw_output/environment_snapshot/measurement.meta에서 동일했다.
두 Run 모두 external_report이며 local measured count0, candidate의 evidence unverified와 decision Node가 저장됐다.
합성 fixture는 논문 진위·실A100 성능·가설 개선/검증됨의 증거가 아니다.

브라우저는 닫았다. 두 fixture 서버는 SIGINT로 exit130이며 backend dispose 중 CancelledError를 기록했다.
별도 socket connect_ex에서57441/57442 모두61(connection refused)이라 listener 종료를 확인했다.
Vite 개발 sw.js MIME 오류는 남는다. 종료를 정상 exit0/운영 PWA PASS로 재서술하지 않는다.

R3/D 및 R4의 등록 직후 UI 공백을 이 격리 범위에서 보완했다. R4/R7/R12의 A Metal과 운영 상태,
R13의 운영 baseline, R14/R15 controller 전체 gate·활성·frontend 배포,
R16/PMS6·7·8의 원래 미완 조건은 유지한다. 이전 B/C 인수를 반복하지 않았다.
