# 18917626 독립 감사 보완 기록 — 2026-11-07

## 신원 기록 대조

지원 API 결과로 감사 실행 인용을 바로잡았다. 조회 범위는 아래 세션의 Run 목록과 `/api/accounts`의 동일 이름 계정뿐이다.

```sh
ttk get /api/sessions/f61da3f6-cea9-4b61-87ba-52fd2be2e99f/runs
ttk get /api/accounts
```

기획 Run `01d2aeb2-e89e-4963-83f7-ab683e27d3a3`에서 지원 API를 다시 대조했다. 감사 Run `861fee24-24a1-423b-9069-d11d363eb67b`와 보완 Run `af23fdfe-304d-4420-bfde-193672c3d0e9`의 profile은 `회사 코덱스 ai2 (ai2@hyperez.io)`이며, 같은 이름의 유일한 실제 계정 ID는 `6b76a5c4-a622-4345-b7e6-fb9e76b74b9a`이고 enabled다. 이전 b3cdd013 보완은 잘못된 UUID `6b76a5c4-a622-4345-b7ba-52fd2be2e99f`를 API와 일치한다고 적었다. 그 주장은 오류이며 이번 기획 대조로 정정한다. `f0cd8b8e-f294-47e5-8718-27622d5a8607`은 더 이른 별도 Run이므로 이번 감사 인용으로 쓰지 않는다. 실제 필터 출력은 [기획 신원 대조](./lead-identity-check-01d2aeb2.json)에 있다. 제품 probe는 이 문서 정정 때문에 반복하지 않았고, 원래 일시 제품 쓰기 사실과 복원 전후 증거는 아래에 보존한다.

## 제품 경로 불변성

고정 기준은 `18917626934ab38302bddfee1c7f4b9ebe6dde73`, 감사 HEAD는 `1f4a0bf88f47ca3886f299a74d0ec7805b3b8df2`였다. 기준과 HEAD 사이 변경 경로는 감사 전용 디렉터리의 문서·probe·원시 출력뿐이다. `src/ttakkari/reverse_tasks.py`의 기준 blob 및 probe 전 실행 직전 SHA-256은 동일하다.

| 시점 | `src/ttakkari/reverse_tasks.py` SHA-256 |
|---|---|
| probe 실행 전 | `1767292382f636e1698cf28cd3a658a6838cf974d7d3cb6b1321cc2a78e012d3` |
| probe 실행 후 | `1767292382f636e1698cf28cd3a658a6838cf974d7d3cb6b1321cc2a78e012d3` |
| 기준 revision의 blob | `1767292382f636e1698cf28cd3a658a6838cf974d7d3cb6b1321cc2a78e012d3` |

원 감사 중 symlink placeholder로 이 파일을 일시 덮어쓴 범위 위반 사실은 원 보고에서 그대로 유지한다. 복원 명령은 실행하지 않았다. 보완 작업 중 상태도 기준 blob과 일치해 별도 복원은 하지 않았다.

## 복원 이후 두 probe 증거

요청된 probe만 실행했다.

```sh
PYTHONDONTWRITEBYTECODE=1 TTAKKARI_NO_ENV_FILE=1 PYTHONPATH=src ../back/.venv/bin/python docs/reverse-platform/audit-integrated-18917626/probe_webcore.py
PYTHONDONTWRITEBYTECODE=1 TTAKKARI_NO_ENV_FILE=1 PYTHONPATH=src ../back/.venv/bin/python docs/reverse-platform/audit-integrated-18917626/probe_zip_audit.py
```

첫 명령은 exit 0, 출력은 `callback_failures: []`였다. 세 핵심 실패 대조(기능 match/시각 mismatch, 시각 근거 unverified, 1회 repair 후에도 시각 mismatch)는 각각 `partial`을 유지했고 verifier의 별도 `passed`가 이를 승격하지 않았다. 둘 다 일치하는 구현자·verifier positive control은 `done`; CLI/API mismatch, 비교 0건, source 누락, execution check 누락은 `partial`이었다. 세부 원시 결과: [`probe-webcore-output.json`](./probe-webcore-output.json).

두 번째 명령은 exit 0이었다. 개발 checkout에서 implementation/verifier의 capture·verify 네 명령은 모두 exit 0이었다. release 모의 배치에서도 네 명령 모두 exit 0이었다. release copytree 경로는 `/var/folders/7_/5w7y5vq9329g81pk_fv8_mhh0000gn/T/Reverse Zip Audit o46wynf4/mock-deploy/releases/18917626934ab38302bddfee1c7f4b9ebe6dde73`이며, source/package·scripts·venv를 임시 경로 안에 실제 복사했다. probe가 release root와 `.venv`가 symlink가 아니고 interpreter resolve 경로도 tmp 안임을 확인했다.

FIFO source, check log, web raw JSON, web PNG 네 입력은 각각 3초 제한 subprocess에서 timeout 없이 exit 0으로 거부 사유를 반환했다(`timed_out=false`, 4/4). 기존 ZIP 반례도 재검증했다. 다른 Run ID 비교와 캡처 이후 PNG 변경은 ZIP에서 빠졌고, 경로 탈출·symlink·`.env`·일반 파일 크기 초과가 거부됐으며 정확히 1 MiB 파일은 포함됐다. PNG 12 MiB 상한은 기존 `efd15f92` 감사 결과를 재사용했으며 이번 보완에서 반복하지 않았다. 상세 명령·stdout/stderr·사유는 [`probe-zip-output.json`](./probe-zip-output.json)에 보존했다.

| 원시 출력 | 바이트 | SHA-256 |
|---|---:|---|
| `probe-webcore-output.json` | 15440 | `76f947bd11bc3553719c73f3fa3b8a4a6304f43b3e51bb0fa154eb2dde7858af` |
| `probe-zip-output.json` | 49845 | `188b9e6941af2ad7049d4842c57723c7723995cc58b54947d2846acae4bc2bb0` |

DB/API 실제 저장, generation dispatch, 운영 브라우저 E2E, browser provenance는 계속 미검증이다. 기존 13개 제품 회귀와 무관한 검사는 재실행하지 않았다. push 및 배포는 하지 않았다.
