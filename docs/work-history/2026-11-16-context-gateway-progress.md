# Context Gateway 진행 기록 · 2026-11-16

상태: 구현·통합 검증 진행 중. 전체 완료·운영 배포 아님.

## 최신 main 병합 및 대표 회귀

- backend `d21343cb2f0805156e6d6c678479c3d8f2d50e88`와 frontend의 feat checkout에 최신 main을 충돌 없이 병합했다. backend upstream은 계정 login/check 경로, frontend upstream은 계정 재로그인 버튼이다.
- 통합 backend core는29 passed, public UI는93 passed/5 live skipped이며 production build 통과다. Private·migration·route·orchestrator·account·security 관련15개파일 실행은259 passed/1 failed였다. 실패는 기존 offline SQL 테스트의 `uv run alembic` 하위명령 exit2였으며 상세 stderr가 출력되지 않아 원인은 단정하지 않는다. 인접 realdb 테스트와 같이 설치된 `sys.executable -m alembic`을 사용하도록 고쳤고 해당파일16건 재검사 pass다. 마이그레이션 head는0079 하나다.
- 바뀐 Python56개경로 Ruff 검사에서 live helper의 비활성 E402 noqa1개를 발견해 제거했고 최종 전부 pass다. production 동작 변경은 없다.
- frontend 전체는 최초1805 passed/1 failed로 `/context-sharing`이 Markdown 링크 분류 허용목록에 빠진 것을 찾았다. 해당목록을 보완해 전체1806 passed/10 skipped와 production build를 확인했다. 공유 CSS는 기존토큰·부모gap으로 정리했고 자기범위 UI lint지적0건, 변경17개JS/TS경로 ESLint도 pass다.
- 전체 frontend lint는 기존main과 동일한 StoryboardCreateFlow.tsx의 set-state-in-effect 오류1개/기존warning2개 및 기존chat·creative·reverse CSS 지적으로 실패한다. 해당파일들은 `git diff --exit-code origin/main -- 경로들`이0으로 이작업 변경이 아님을 확인했다. 기준선을 올리거나 타기능을 고치지 않았다.
- 다음은 이결과를 커밋하고 기존pre-push게이트를 실행한 뒤 push 및 backend fullSHA 예약이다. 외부 Gateway 격리배포/HTTPS는 이검증 범위 밖이다.

## owner 최종 HTTP 검증 대조 및 main 통합 착수

- frontend `be2316c95308616763cc54bdd46bd2544bfc79ae`의 정본 기록을 직접 읽었다. backend `4ad5ca30`의 실제 공유 라우터를 붙인 최소 시험 호스트에서 키 첫 교환200/재사용401, TTL3600, 프로젝트 철회200/기존 세션 context401, MCP list/context200, JSON export200와 문서 verified_at null을 관측한 범위를 수락한다. 전체 Private 앱 기동·실제 배포를 검증한 것은 아니다.
- backend/front origin fetch가 모두 성공했다. 합류할 upstream은 backend `ed44e9a6`/`ceaf222e`와 frontend `3770a6a`의 계정 재로그인 확인 변경이다. 원래 main의 다른 작업 변경은 보존하고 이 두 feat worktree에서 순서대로 main을 병합한다.
- 다음은 통합 revision의 Private·route·account·migration 및 core 대표 회귀, frontend 테스트/빌드, 명시적 경로 커밋·push와 backend full SHA 배포 예약이다. Gateway의 Docker·외부 HTTPS·DNS/Tunnel과 이전 차단 서버 종료 미확인 경계는 유지한다.

## Private 혼합 검증 회귀 수락 · 4ad5ca30

- `4ad5ca30b8fc7cec547a07dbdf9c386b49b96f19`는 문서1개·테스트2개만 강화했고 서비스 변경은 없다. 기획이 `test_cg_private_verify_2c861cfc`에서 API의 미검증/혼합 상태 게시와 실제 Gateway manifest·프로젝트 철회 선택3건을 실행해3 passed (1.39s),0skip을 확인했다.
- `postgres.pg_database` 이름 metadata의 실행 전후 차이에서 `gateway_050ecb73` 추가를 관측했다. 시험DB는 삭제하지 않는다. owner 실제HTTP/browser `23daeb7c`의 기존 실행 소유를 유지하며 그 결과 뒤 통합·push·배포 예약 단계로 이어간다.

## Private 보안 후속 수락 · a9270859

- `a92708595990c4a679f470a6d400205040776995`의6개파일을 직접 대조했다. 잘못된/중복 문서 영수증 거부, API TTL 생략/3600 고정, 보상 키 폐기 실패의 식별자·오류 코드 감사 저장이 변경 범위다.
- 기획 재실행: DB 없는 `tests/test_context_sharing_gateway.py` →31 passed (0.11s). 기획 전용 `test_cg_private_verify_2c861cfc`에서 API TTL·cleanup audit·불완전 receipt·잘못된 만료시각 관련5건 →5 passed /45 deselected (1.27s). `git show --check a9270859`와 `git diff --check` 오류없음. 이 좁은 변경 검증을 수락하고 영향 없는 기존 검사는 재사용한다.
- owner 실제HTTP/browser Run `23daeb7c`는 계속 running이다. 런타임이 로드한 revision은 결과 보고에서 대조한다. 이번 보완을 owner 성공 여정이나 외부 배포 완료로 확대하지 않는다.
- worker가 만든 별도 시험DB `test_ctxshare_codex_a2fa_20261116`은 보고에 기록됐다. 운영 DB와 구분하고 삭제하지 않는다. 기존 initdb/shmget 거부 경로는 재시도 대상이 아니다.

## 11:12 KST Private 선행 조건 검증과 owner 실통합 재개

- Private `11bbe4f5847a8f079608a29f00762ae847932f85`의7개파일 diff를 대조했다. `verification_required` 강제 게이트 제거, 문서별 provenance/null 전달, project revoke receipt 확인을 확인했다.
- 기획이 새 `test_cg_private_verify_2c861cfc` DB를 CREATE/COMMENT한 뒤 `TTAKKARI_TEST_DB=해당URL PYTHONPATH=context_gateway/src:src .venv/bin/python -m pytest -q tests/test_context_sharing_api.py tests/test_context_sharing_gateway.py tests/test_context_sharing_mcp.py tests/test_context_sharing_real_gateway.py`를 실행했다. 결과83 passed in4.65s,0skip. 이 범위에 verification passed를 기록했다.
- owner `21ec6b7f`는 기존 컨텍스트92%였으므로 fresh_context와 task_type verification/auto로 재개했다. 새 Run `23daeb7c-8b45-4788-9d47-b1a0eeb26824`는 실제 HTTP/browser에서 미검증 문서 승인 게시→1시간 key 발급·교환·재사용 거부→폐기·프로젝트 철회, 자연어 agent/MCP 초안 도구 경계를 검증한다. 이전422/409는 해결 전 관측이다.
- 이 작업용 새 `test_cg_owner_2c861cfc`와 `test_cg_owner_gateway_2c861cfc`도 기획이 CREATE/COMMENT 성공을 확인했다. owner는 이 두DB만 사용하며 main 검증DB와 분리한다. 운영DB·실제 문서·기존 차단 서버 종료 대상을 건드리지 않고 DB를 삭제하지 않는다.
- core29건/public39건/owner합성62건은 영향이 없어 재사용한다. 다음은 owner 실제 실행 결과의 원래 요구 대조와 단일revision 통합·push·backend 배포 예약이다. Docker/외부HTTPS 및 이전 시험서버 종료는 미확인 경계를 유지한다.

## 11:09 KST 외부 화면 문서별 출처 교정 수락

- public UI `b7f545366d3f2a48a9fa1c88e358d941bf759735`의 web 7개 파일과 단건/전체 context 소비 경로를 대조했다. `mergeProvenance` 상속을 없애 문서의 null·미지정·빈 출처가 snapshot 값으로 채워지지 않는다.
- 기획 재실행 `npm test -- --run src/api/adapters.test.ts src/App.test.tsx` → 2 files / 39 passed (3.89s), `npm run typecheck` → exit0. `git show --check b7f54536`과 `git diff --check`에도 오류가 없었다. 이 좁은 교정에 verification passed를 기록했다.
- 코어 전체29건 검증은 core 파일 변경이 없어 재사용한다. 기존 browser/live 관측과 종료 미확인은 그대로이며 반복하지 않았다.
- Private nullable/provenance 변경은 현재 작업 트리에 보이지만 아직 보고·커밋·실행 증거와 대조하기 전이다. Private 기존 직렬 후속을 유지한다. 그 완료 뒤 owner worker `21ec6b7f`를 fresh context로 재개해 새 시험 DB에서 실제 게시·키 발급/폐기·프로젝트 철회 및 browser를 확인한다.

## 10:54 KST Private 첫 보고와 대기열 진단

- Private `185ea8bdb708f8a4cdf11739b930ea9abba86c7e`의 7개 소유 경로와 현재 service를 직접 대조했다. receipt 필수 필드/문서 hash·TTL 3600 계약 보완은 확인했지만 `verification_required` 게이트와 문서별 payload metadata 누락은 남아 있다. 따라서 이 보고의 완료는 해당 커밋 범위에만 적용한다.
- 기획 재실행 `TTAKKARI_NO_ENV_FILE=1 .venv/bin/python -m pytest --noconftest -q tests/test_context_sharing_gateway.py` → 24 passed (0.10s). DB fixture를 로드하지 않는 control 어댑터 단위 검사이며 worker 보고 198건 전체를 재실행한 것은 아니다.
- 지연 알림 4건을 현재 dispatch와 대조했다. 선행 `b239d536`은 succeeded이고 `9e83f98e`는 running, lease owned, task_exists=true다. 나머지 `fde29ca2`/`a2fa0047`/`1322fdf0`는 같은 worker의 직렬 후속이다. worker lane은 5/16, 최신 사용량에서 개인 Codex 두 계정은 잔여량이 관측됐다. 계정 부재나 전체 슬롯 포화의 증거는 없으므로 중복 지시·무작정 재배정하지 않는다.
- 기존 후속이 nullable 승인·문서별 metadata 전송을 소유한다. core `0f788cf7`, verifier `daef2531`, 두 UI 후속도 계속한다.

## 10:52 KST 코어·독립 검증 보고 대조

- 웹 wrapper `c429113c`의 공개 OpenAPI/docs 보완은 기획 재실행 `tests/test_web_serving.py` → 4 passed (0.29s)로 해당 범위만 수락했다.
- 독립 verifier는 실제 시험 DB에서 core 21 passed / 4 failed / 0 skipped, Private API/MCP/real-Gateway ASGI 56 passed / 0 skipped, REST↔MCP parity 1 passed를 보고했다. 이는 당시 dirty tree의 실행 결과이며 현재 커밋만의 재현이나 운영 배포 증거는 아니다.
- 실패 4건의 격리·ACL fixture·`pytest.raises` 사용 보완은 verifier `281975f1`의 fresh-context 후속 `daef2531`이 맡는다. 기획은 비동기 `pytest.raises` 사용의 TypeError를 직접 재현했다. 별도 DB `test_cg_verify_141e23a3`의 CREATE/COMMENT 성공을 확인했다.
- core `bba4990b4df1f026e3fed4687caa3b846e1340c5`는 manifest version/경로 및 project revoke를 추가했다. core 보고의 17 passed / 8 skipped는 DB 실행 증거가 아니며, 별도 verifier의 실제 DB 검사와 대조한다.
- 문서별 `source_revision`·`generated_at`·`verified_at`은 아직 입력 모델과 manifest에 없다. 기획의 `SnapshotDocumentInput.model_validate(...).model_dump()` 반례에서 세 필드 모두 보존 여부 False였다. core 컨텍스트 81%이므로 fresh-context 후속 `0f788cf7`에 이 누락과 rate-limit/audit DB 회귀를 맡겼다. 시험 DB `test_cg_metadata_29d22061`도 CREATE/COMMENT 성공을 확인했다.
- Private 후속 `1322fdf0`에 nullable 승인과 문서별 전체 metadata 전달 지시가 이미 대기 중임을 `read_session`으로 확인했다. 같은 지시를 추가하지 않는다. 기존 head `b239d536` 뒤 직렬 대기다.
- 위 두 추가 DB도 합성 시험 전용이며 삭제하지 않는다. core/독립 verifier의 테스트 파일 소유 범위와 DB가 분리되어 있다. 최종 통합 때 변경 revision과 무효화된 검사만 재실행한다.

## 10:41 KST 보고 대조 및 후속 실행

- owner UI 커밋 `e5f7fbeb2de0dd7ec512403807beb572ca3067b9`와 웹 연결 커밋 `87b87335`를 실제 checkout에서 확인했다.
- 기획 재실행: front의 화면/API/진입점/오류 안내 vitest 5 files / 104 passed (2.91s). backend `context_gateway/.venv/bin/python -m pytest -q tests/test_web_serving.py` → 3 passed (0.31s). 각각 합성 UI와 DB lifespan 없는 ASGI 범위다.
- owner 화면은 `unverified.length`로 게시를 차단하고 있었다. 이는 미검증 문서를 null provenance로 게시할 수 있다는 기존 결정과 다르므로 전체 UI 수락은 보류했다. worker `21ec6b7f`를 fresh context로 재시작했고, 후속 Run `af9c18c3`이 선택적 원본 대조·명시적 게시 승인 분리와 실제 Private 계약 검증을 담당한다.
- wrapper가 `/openapi.json`과 `/docs`를 404로 차단해 원래 API 문서 요구가 빠져 있었다. worker `d8713456` 후속 Run `3cd27915`에 public schema·로컬 문서 제공 및 control 비노출 회귀를 맡겼다. 이 발견은 기존 정적 serving 검사 3개가 전체 MVP 완료를 의미하지 않음을 보여 준다.
- core `a6be70b2`, Private 직렬 후속, public live UI `d46a119f`, 독립 verifier `7fd118ce`의 실행은 유지한다. 같은 지시를 중복 추가하지 않는다. 아직 코드 변경 중이므로 origin/main 통합·push·배포는 최종 검증 뒤 진행한다.

## 목표와 기준 문서

승인한 프로젝트의 불변 스냅샷만 외부 Web/REST/MCP에 제공한다. Public은 Private DB·원본·RAG·LLM에 접근하지 않는다. 기본 256비트 키는 1시간 만료이며 1회 교환 뒤 원래 만료를 넘지 않는 읽기 세션으로 쓴다. 게시 승인과 원본 대조는 구별하고 미검증 `verified_at`은 null이다. 구현 권한은 실제 개인·회사 문서 게시 승인과 다르므로 실제 문서는 게시하지 않는다.

- 영속 요구/완료 증거/소유자: 이 기획 세션의 `manage_work get`이 정본이다.
- [Gateway 계약](../context-gateway-contract.md), [Private 계약](../context-sharing-private-contract.md).
- [독립 검증 기록](../context-gateway-independent-verification.md)은 이전 static-only 판정이 남아 있을 수 있다. 최신 실행·revision을 확인한다.
- [외부 UI 검증](../../context_gateway/web/README.md), [웹·배포 연결](../context-gateway-web-deploy.md).

## 위치와 변경 경계

- Backend 격리 checkout: `/Users/winterholic/development/projects/winterholic-ttakkari/back-context-gateway-20261116`.
- Frontend 격리 checkout: `/Users/winterholic/development/projects/winterholic-ttakkari/front-context-gateway-20261116`.
- 두 checkout의 branch: `feat/context-gateway-20261116`.
- 기존 `back`/`front` main은 타 작업 dirty 상태다. reset/stash/일괄 add하지 않는다. 최종 통합은 격리 branch에서 최신 origin/main을 병합하고 검증한 뒤 자기 변경만 push한다.
- 확인한 backend 커밋: public UI `886dcfd1`, Private `8a48d684`, core `0c53924a`, live fixture `2c5df2e3`, nullable provenance `27750677`, UI 검증 문서 `e3000c28`/`766ce3cb`/`f654054f`. 이후 commit/dirty 변화는 다시 확인한다.
- `docs/context-gateway-discovery.md` 및 독립 검증 문서는 기획이 읽었으나 아직 미추적일 수 있다. 허가 없이 버리지 않는다.

## 실행 소유자와 다음 작업

| worker_id | 범위와 남은 단계 |
|---|---|
| `cf76b3b5-f593-45ec-b205-585359f0e990` | core. `a6be70b2` 후속 Run이 manifest 전체 필드·provenance·project revoke를 보완 중. 문서별 source_revision/generated_at/verified_at까지 모두 보존되는지 최종 대조 필요. |
| `77a65403-5666-4e97-ac05-6a29cd485b5d` | Private. 원래 구현 뒤 후속 여러 개가 직렬 대기 중. 실제 control 계약 정합성, receipt 필수 UUID/hash/문서목록 검사, TTL 3600만 허용, 폐기 실패 감사, nullable 미검증 문서 승인 게시를 지시함. latest queued 표시는 실제 실행 head와 다를 수 있다. |
| `21ec6b7f-93fa-418a-b5a0-9af2b0866fbf` | front owner UI. 문서 선택·편집·digest 승인·1회 키 표시·폐기·철회. 실제 API/service를 참고하며 원본 대조를 자동 선택하지 않음. |
| `6e1f6fc4-9a95-4bd9-a7a2-d99e43a4e231` | public UI. 합성 UI/브라우저 확인 뒤 fresh context로 실제 DB/서버 UI·SDK 검증을 재개함. web/만 수정, 전용 UI DB 사용. |
| `281975f1-ec0a-4f7a-959c-d65d11bead88` | 독립 verifier. static-only 8 passed 뒤 fresh context로 새 전용 DB의 core/Private 실제 통합·동시성·REST/MCP·철회를 실행 중. 구현 파일은 수정하지 않음. |
| `d8713456-bf26-40dc-ac13-5140cb6b0b38` | web_public/web_serving 새 모듈·Dockerfile·Compose·전용 테스트. apps.py/config.py/core 파일과 web/는 수정하지 않음. 기존 Caddy 8787과 public host 포트 충돌 제거를 맡음. |

`message_worker`는 현재 실행에 즉시 끼워 넣는 것이 아니라 후속 Run을 줄 세웠다. 알림의 queued 시간만 보고 멈춤으로 단정하지 않는다. 10:27 KST `read_session`에서 실제 core head는 running, lease/PID/dispatch가 있었고 worker lane은 5/16이었다. 같은 지시를 계속 추가하지 말고 보고·실제 코드와 대조한다.

## 유효한 검증과 한계

- 기획 재실행: public UI `npm test` → 8 files/94 tests passed, typecheck와 production build 성공(10:16 KST). 합성 API 기준이다.
- 기획 재실행: core auth/import-boundary/file-integrity 선택 검사 → 5 passed. DB 통합 증거가 아니다.
- 독립 verifier 첫 실행: DB 없는 8 passed. 당시 DB/MCP/REST 통합 미실행. static 결함 두 개는 Private 후속에 전달했다.
- public worker: configured browser로 합성 API의 390px/1280px 로그인·provenance·503·키보드·Markdown/ZIP 다운로드를 확인했다고 보고. 실제 최신 core 연결은 새 후속 Run의 검증 대상이다. 원본 screenshot은 다른 Run outbox라 기획이 파일로 직접 열지 않았다.
- core 첫 보고의 `8 passed, 6 skipped`, provenance 보고의 `12 passed, 6 skipped`를 전체 통과로 취급하지 않는다. DB skip 때문에 부분이었다.

## 안전한 시험 DB 경로와 정리 소유

차단된 core `initdb`의 SysV `shmget: Operation not permitted`를 반복하지 않았다. 대신 기획의 read-only `pg_isready` 및 psql metadata SELECT로 기존 `127.0.0.1:5432`, user `winterholic` 접속을 확인한 뒤 별도 시험 DB를 새로 만들었다. 운영 DB `ttakkari`는 사용하지 않는다.

| 새 DB | 소유/용도 |
|---|---|
| `test_cg_core_01cb5ce6` | verifier core 테스트. `CONTEXT_GATEWAY_TEST_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_core_01cb5ce6` |
| `test_cg_private_01cb5ce6` | verifier Private. `TTAKKARI_TEST_DB=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_private_01cb5ce6` |
| `test_cg_ui_d40c9adf` | public UI live 검증. `postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_ui_d40c9adf` |

세 DB 모두 CREATE DATABASE와 Run 소유 COMMENT 성공을 기획이 직접 확인했다. 사용 중이므로 삭제 금지. 최종 테스트 뒤 이 이름과 해당 실행이 만든 `gateway_*` DB만 소유 근거로 정리한다. 일반 prefix를 쓸어 지우지 않는다.

## 배포 제약과 최종 완료 조건

- 현재 Codex `docker version`은 `/Users/winterholic/.orbstack/run/docker.sock` 연결에서 `no such file or directory`로 실패했다. 컨테이너 실행은 미검증이며 daemon 기동·보안 변경·같은 소켓 우회는 하지 않는다. Compose config 검사만 컨테이너 실행으로 확대하지 않는다.
- DNS/Tunnel/운영 config·harness·보호 로그는 직접 읽거나 수정하지 않는다. 운영 로그/상태는 지원 MCP를 쓴다.
- 검증이 끝나면 최신 origin/main 통합·관련 회귀·커밋·push, backend full SHA 배포 예약을 사용자 standing authorization에 따라 진행한다. acceptance는 deployment success가 아니다.
- 독립 Gateway의 실제 격리 배포/외부 HTTPS 연결이 지원 경로로 불가하면 정확한 미검증 경계를 남긴다. Private만 배포하고 원래 MVP 전체 완료라고 하지 않는다.
- 최종 판정은 manage_work R1–R5와 실제 증거를 전부 대조한다. 테스트 개수·작업자 완료 문구·문서 완료는 전체 완료가 아니다.

## 외부 UI 실제 런타임 보고 대조 및 소비자 교정

- public UI worker `6e1f6fc4`의 실제 DB·built UI·REST 다운로드·401/429·SDK MCP 보고와 `context_gateway/web/README.md`를 대조했다. `git show --stat 8e12b0d7`은 README 한 파일, 45 insertions/27 deletions였다. 실제 서버는 시작 시 working tree를 로드했으므로 이 관측을 단일 SHA나 배포 빌드의 검증으로 확대하지 않는다. 다른 Run outbox의 원본 브라우저 캡처는 직접 열지 않았다.
- 시험 서버 종료는 Codex worker sandbox에서 `ps`와 `os.kill(SIGTERM)`이 `PermissionError: Operation not permitted`로 차단되어 미확인이다. `/private/tmp/context-gateway-ui-qh8kajyy/` PID 파일과 snapshot 경로, `test_cg_ui_d40c9adf` DB를 보존한다. 같은 종료 대상을 다른 명령·엔진으로 재시도하지 않는다. 이미 소비된 합성 키의 도구 응답 에코와 임시 secret 파일 삭제도 worker 관측 범위로 남긴다.
- core `2f5f2e45`의 변경 파일 5개와 최신 문서별 provenance 계약을 확인했다. core worker의 9 passed는 전용 DB의 좁은 검사 보고이며 전체 Gateway suite 결과는 독립 verifier가 담당한다.
- 최신 계약과 달리 `web/src/api/adapters.ts`의 `mergeProvenance`가 문서 null을 snapshot 값으로 상속한다. 기획이 Node `registerHooks`로 실제 TypeScript 함수를 로드해 반례를 실행했다: `documentVerifiedAt:null`, `uiVerifiedAt:"2026-11-16T01:00:00Z"`, `contractPreserved:false`, assertion exit 1. 첫 재현 스크립트는 TypeScript 7의 없는 `ModuleKind` API에서 실패했으며 제품 실패 증거로 쓰지 않는다.
- 넓은 UI 검증을 반복하지 않고 이 소비자 불일치만 fresh-context 후속 `3abbeca3-cd8e-4174-bffc-93151204ddbf`로 시작했다. 기존 public worker를 task_type general/auto로 재배정했으며, 쓰기는 web/에 한정한다. 문서 null/미지정 유지, A/B별 metadata, 단일 문서·전체 context 표시를 RED→GREEN 및 typecheck/build로 검증한다. 시험 서버 종료 재시도는 금지했다.
- 기존 Private `1322fdf0`, owner UI `af9c18c3`, 독립 verifier `daef2531`의 소유를 유지한다. R1–R5 전체 완료·push·배포는 아직이며, 현재 사용자 조치는 없다.

## 11:03 KST 코어·내부 화면 보고 대조

- 코어 `2f5f2e4546091114c13b8530a6dff2491d22b67a`의 5개 파일을 대조하고 기획이 전용 `test_cg_metadata_29d22061` DB에서 좁은 통합 검사를 재실행했다. `CONTEXT_GATEWAY_TEST_DATABASE_URL`과 `CONTEXT_GATEWAY_DATABASE_URL`을 이 DB로 지정하고 `context_gateway/.venv/bin/python -m pytest -q -o asyncio_default_fixture_loop_scope=function tests/test_provenance.py tests/test_rest_mcp_parity.py tests/test_rate_limit_audit.py` → `9 passed in 0.92s`. 문서별 출처·미검증 null·REST/MCP parity·rate/audit 후속 범위만 수락하고 core worker에 verification passed를 기록했다. 최초 root `../.venv/bin/python`은 import 단계 `ModuleNotFoundError: context_gateway`로 실패했으며 제품 실패로 취급하지 않는다.
- 내부 화면 `43adb346b14641b6c099addff7ca02c513846ef1`의 5개 파일을 대조했다. 미대조 문서 게이트 제거·명시적 공유 승인 유지·서버 고정 TTL 요청을 확인했다. 기획 재실행 `npm test -- --run src/pages/ContextSharingPage.test.tsx src/api/contextSharing.test.ts` → `2 files / 62 passed`, 2.77s. 합성 응답 기반 프런트 회귀이며 실제 게시·키 수명주기 통과 증거가 아니다.
- owner worker의 실제 시험 HTTP·인증 browser 기록에서 `publish_unverified=422 verification_required`를 확인했다. 키·프로젝트 철회는 미게시 오류409까지만 확인됐고 실제 발급·폐기·철회는 미실행이다. worker가 사용한 시험 DB는 worker 보고상 삭제됐으므로 다음 통합 검증은 새 전용 DB를 써야 한다. 원본 타 Run browser 캡처는 직접 열지 않았다.
- Private 진단 `read_session(last=4)`에서 head `fde29ca2`는 running, lease_owned=true, task_exists=true였다. `a2fa0047`·`1322fdf0`는 그 뒤 직렬 queued이며 worker lane은2/16이다. 이 관측은 전체 슬롯 부족이나 멈춤의 증거가 아니므로 같은 지시를 중복하지 않는다.
- 다음 의존성은 Private nullable 승인·문서 metadata 완료 보고 → 새 컨텍스트 owner UI에서 새 시험 DB의 실제 게시·1시간 키 발급/폐기·프로젝트 철회·browser 확인이다. owner 컨텍스트92%라 기존 엔진 대화에 새 통합 단위를 쌓지 않는다. Private 보고가 선행 조건을 충족하면 기획이 fresh_context로 재개한다. public UI `3abbeca3`와 독립 verifier `daef2531`은 기존 실행 소유를 유지한다.

## 코어 전체 검증 수락 (6b1e3ced)

- 독립 verifier `6b1e3ced3cb5fa12fe53aa9a52bb6bd2f0c81043`의 fixture 교정과 보고서를 대조했다. DB 격리, Project/ACL fixture 추가, 올바른 동기 `pytest.raises` 사용으로 네 실패를 교정했으며 기존 assertion을 약화하지 않았다. `git show --check`는 오류가 없었다.
- 기획이 같은 HEAD의 core src/tests에 미커밋 변경이 없음을 확인하고 전체를 재실행했다. 명령: `CONTEXT_GATEWAY_TEST_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_metadata_29d22061 CONTEXT_GATEWAY_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_metadata_29d22061 .venv/bin/python -m pytest -q -o asyncio_default_fixture_loop_scope=function tests` (cwd `context_gateway`). 결과: `29 passed in 1.86s`, 실패·skip0. 이 DB는 core 보완 worker가 종료한 뒤 기획이 쓰는 전용 DB다.
- 해당 Gateway 코어 검증 단위에 verification passed를 기록했다. 이전 전체21 pass/4 fail과 DB 미실행 결과는 해결 전 이력이다. Private의 기존56 pass는 이전 revision의 scoped 증거이며, 현재 작성 중인 Private nullable/provenance 변경에는 최종 재검증이 필요하다.
- Private 구현·테스트와 public UI 파일은 현재 다른 worker의 미커밋 변경이다. 보존하고 단일 revision 통합·push·배포는 이 작업들이 안정된 뒤 진행한다. owner 실제 게시·키 수명주기, 자연어 초안 연결, Docker·외부 HTTPS는 아직 전체 완료 근거가 없다.

## 배포 재개: 실제 실행 경로 교정

- 사용자 재개 지시 뒤 dd861485 예약의 지원 로그를 확인했다. 배포 준비 pytest에서 `test_single_head_preserves_both_published_branch_identities`가 최신 0079를 기존 0078과 비교해 실패했다. 같은 원인의 admission 검사도 로컬 RED로 확인하고, 0078은 online 승격 대상·0079는 current로 검증하도록 갱신했다. 계보의 0079→0078→0077 연결은 모두 검사한다.
- 기존 hs-ops/hs-edge 경로에서 Docker 29.1.2·Compose 5.0.0 및 제한된 edgectl 권한을 실제 확인했다. Mac Docker/차단 프로세스 종료를 재시도하지 않았다. Gateway만 포함한 소스와 별도 0600 자격 파일로 새 홈서버 서비스를 구성했다. 실제 사용자 문서는 게시하지 않았다.
- 실제 image build/기동에서 Node Blob 생성자 차이, 잘못 파싱된 tmpfs 목록, 이동된 venv의 실행 파일 경로를 확인해 교정했다. 수정 뒤 이미지가 빌드되고 세 컨테이너가 healthy가 됐다. 내부망의 호스트 포트 공개가 비활성인 관측에 따라 홈서버 전용 override는 포트를 공개하지 않고 nginx가 예약 내부 IP로 접속하게 한다. 최종 외부 검증과 source revision 고정은 후속 단계다.
- 새 production-only 자격 파일은 Gateway 연결 세 필드만 허용한다. RED 6건을 관측한 뒤 구현했고, 해당 보안/스키마/기존 어댑터 검사 72건이 통과했다. Ruff와 diff 검사를 통과했다.
- Codex sandbox의 unit_tests 전체는 2610 passed/29 failed/1 skipped였다. 실패에는 process table 열거가 불완전한 host resource 검사와 시험 DB 비밀번호 인증 실패가 포함됐다. 전체 통과를 주장하지 않으며 보호 정책을 바꾸거나 해당 환경 실패를 우회하지 않는다. 배포 컨트롤러의 호스트 환경 전체 검증을 별도 유지한다.

## INSERT-only 감사 권한의 실제 운영 회귀

- Gateway 9289e176의 HTTPS 키 교환에서 500을 관측했다. 공개 컨테이너 로그는 `auth.exchange_key → session.flush → INSERT gateway_audit_events ... RETURNING created_at`의 `permission denied`를 보였다. 운영 계정은 감사 INSERT만 갖지만 기존 코어 테스트는 admin DB 계정이라 이 차이를 놓쳤다.
- 감사 읽기 권한을 추가하지 않고 `AuditEvent`만 `eager_defaults=False`로 바꿔 INSERT가 서버 시각을 반환 조회하지 않도록 했다. DB의 `created_at` 기본값은 유지된다.
- 이번 Run 전용 홈서버 DB `test_cg_deploy_4277e61d`와 SSH loopback forward를 사용했다. `test_audit_insert_with_insert_only_role_does_not_require_select`에서 NOLOGIN 임시 역할에 INSERT만 부여해 실제 ORM flush를 재현했고 RED `InsufficientPrivilegeError`를 확인했다. 수정 뒤 Gateway 전체 `30 passed in 20.97s`. 임시 역할은 외부 트랜잭션 rollback으로 제거하고 SSH 자식 프로세스는 종료했다. 운영 Gateway DB는 시험용으로 drop/create하지 않았다.
- Public 컨테이너의 실제 검증: 외부 1.1.1.1:443 및 홈서버 Tailscale DB 주소 연결은 errno 101, snapshot write는 EROFS(30), project UPDATE는 42501, control 토큰은 환경에 없었다. 이 관측은 해당 주소·권한의 실행 증거이며 모든 가능한 목적지의 전수 검사는 아니다.
- Public HTTPS 화면·health 200, 무자격 프로젝트 401, control 경로 404를 관측했다. 모바일 390×844와 데스크톱 1280×900의 접속 폼은 configured browser로 렌더했고 닫았다. console에는 Cloudflare analytics script CSP 차단과 favicon 404가 있었으며 CSP를 완화하지 않았다. 인증된 UI 전체 여정의 운영 브라우저 검증은 이번 범위에 포함하지 않았다.

## 14:14 KST 외부 HTTPS 검증 결과

- 최종 Gateway 컨테이너 image·revision label과 current 링크는 `515b565dc652e67e806daff921fa26e86695d581`이다. control/public 모두 running, root FS read-only, public snapshot mount read-only, 호스트 publish port 없음, 내부 브리지 `172.31.0.0/24`를 실제 inspect했다. public에는 Docker HEALTHCHECK가 없으므로 running과 외부 `/healthz` HTTP 200으로 확인한다(control/DB는 Docker healthcheck도 있음).
- 원본 프로젝트를 게시하지 않고 실제 `share.winterholic.net` 및 `share-control.winterholic.net`에서 합성 검증을 수행했다. 결과 정본: `docs/work-history/2026-11-16-context-gateway-external-verification.json`의 status passed. 발급 TTL 3600초, 교환 200·재사용 401·세션 만료 연장 없음, 프로젝트 ACL 목록·타 프로젝트404, MD/JSON/ZIP·ZIP 무결성, 검색·개별 문서, 문서 verified_at null, 실제 Python MCP SDK의 읽기 도구 4개 및 REST/MCP 동일 스냅샷을 확인했다.
- read-only 키의 export는404, 키 폐기 뒤 기존 세션은401이었다. control 직접 프로젝트 철회 뒤 REST 프로젝트 조회는404로 존재를 숨긴다. 앞선 검사에서401을 기대한 것은 Private의 키 폐기까지 포함한 흐름과 control 단독 프로젝트 철회를 혼동한 시험 기대값이었다. 이404는 제품 변경 없이 현재 ACL 계약과 대조해 확인했다. 합성 프로젝트 6개는 모두 철회 상태이며 활성 프로젝트 수는0이었다.
- 초기 검사 스크립트가 잘못된 빈 projects 요청을 보내422를 받은 것도 확인했다. 유효한 본문으로 무자격 control 요청을 다시 검사해401을 관측했다. 실패한 시험 기대값을 운영 성공 증거로 취급하지 않는다.
- 기존 gosu HTTPS는200이었다. 전체 기존 홈서버 서비스의 전수 회귀 검증은 아니다. 브라우저는 닫았다. 이번 전용 시험 DB는 검증 뒤 정리하고, 이전 Run의 정리 차단 대상은 건드리지 않는다.
- 두 소스 커밋은 기존 pre-push gate를 유지하고 main에 push했다. 9289e176 gate: Ruff, contract87, 변경unit33 pass. 515b565d gate: Ruff, contract87 pass; Gateway 별도 패키지 검증은 위30건과 외부 검증으로 보완했다.
- Private backend 예약: 기존 dd861485는 schema-head 검사 실패로 종료됐다. 수정본 예약 ff7de520 뒤 core 감사 수정까지 담은 최신 예약 `e7e8252a-5fce-4669-b89a-9e43031889eb`(515b565d, skip_tests=false)는 requested로 접수됐다. 컨트롤러가 전체 검사와 세대 전환을 담당한다. 이 Run에서 결과를 기다리거나 polling하지 않는다. 운영 Private API의 gateway configured 상태와 실제 owner UI 게시 연결은 해당 배포 결과 뒤 최종 확인해야 하므로 R5 전체 완료를 선언하지 않는다.

## 16:01 KST Gateway 사이트 일시 중지/재개 제어 구현

- `context_gateway.gateway_site_control` singleton row에 `enabled`를 저장한다. 초기화 시 기존 DB는 `INSERT ... ON CONFLICT DO NOTHING`으로 enabled=true를 채우고, 상태 row가 아직 없는 경우에도 읽기는 true로 처리한다. 스냅샷·키·세션 row는 닫기/재개에서 변경하지 않는다.
- 최종 API 계약:
  - Private owner JWT `GET /api/context-sharing/site`은 Gateway에 실제 조회를 보내 `{ "enabled": true, "public_url": "https://share.winterholic.net" }`를 반환한다. 기존 `GET /api/context-sharing/status`는 연결 설정 검사 전용이다.
  - Private owner JWT `PATCH /api/context-sharing/site` body는 `{ "enabled": false }` 또는 `{ "enabled": true }`; 성공 응답은 같은 두 필드다. Gateway 미설정은 503 `gateway_not_configured`, 연결 실패는 503 `gateway_unavailable`, Gateway 거절/계약 오류는 각각 502 `gateway_rejected`/`gateway_bad_response`다.
  - Gateway control bearer `GET /control/v1/site`, `PATCH /control/v1/site`가 상태 조회·변경을 수행한다. Public `GET /api/v1/site` 응답은 문서/프로젝트를 노출하지 않고 `{ "enabled": false }`만 준다. Public UI는 이 조회를 사용해 임시 중지 화면을 표시할 수 있다.
  - disabled면 인증된 `/api/v1/*` 요청과 기존 MCP 요청은 공통 ASGI 경계에서 503 `site_temporarily_unavailable` 및 `Retry-After: 60`으로 닫힌다. 미교환 키도 exchange에서 거부된다. `/healthz`와 최소 site status 조회는 계속 열린다.
- control site 상태 조회와 변경은 `gateway_site_control_audit`에 기록한다. 기존 `context_gateway_public` 역할에는 상태 테이블 SELECT만 더하고 UPDATE/INSERT/DELETE를 명시적으로 거부하며 감사 테이블 접근은 주지 않는다.
- 변경 파일: `context_gateway/src/context_gateway/{apps.py,models.py}`, `context_gateway/tests/test_rest_mcp_parity.py`, `src/ttakkari/api/context_sharing.py`, `src/ttakkari/context_sharing/gateway.py`, `tests/{context_sharing_support.py,test_context_sharing_api.py,test_context_sharing_real_gateway.py}`. 요청 범위대로 frontend·운영 설정·키·게시 데이터는 수정하지 않았다.
- RED/GREEN: 새 Gateway 테스트는 구현 전 `/api/v1/site`와 `/control/v1/site`에서 404로 실패했다. 새 Private route 테스트도 구현 전 `/api/context-sharing/site`에서 404로 실패했다. 구현 후 통과했다.
- 검증(합성 자료, 기존 전용 시험 DB): 저장소 루트에서 `.venv/bin/python -m pytest -q tests/test_context_sharing_api.py tests/test_context_sharing_gateway.py tests/test_context_sharing_real_gateway.py` → `90 passed in 3.92s`. 여기에는 Private owner API부터 실제 Gateway control/public ASGI 앱까지 개폐 통합, 기존 REST 세션 거부, 재개, snapshot id·session 만료 보존, 미교환 키 일회 교환 회귀가 포함된다.
- 검증(합성 자료, Gateway `test_cg_metadata_29d22061`): `CONTEXT_GATEWAY_TEST_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_metadata_29d22061 CONTEXT_GATEWAY_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_metadata_29d22061 .venv/bin/python -m pytest -q -o asyncio_default_fixture_loop_scope=function tests` (`cwd=context_gateway`) → `33 passed in 1.92s`. REST/MCP/export/기존 세션·미교환 키 차단, 다시 열기, TTL·snapshot 유지, control 인증, public DB 역할의 SELECT-only site 권한과 감사 접근 거부를 확인했다.
- lint/공백: Gateway `.venv/bin/ruff check src/context_gateway/apps.py src/context_gateway/models.py tests/test_rest_mcp_parity.py` 및 Private `.venv/bin/ruff check src/ttakkari/api/context_sharing.py src/ttakkari/context_sharing/gateway.py tests/test_context_sharing_api.py tests/context_sharing_support.py tests/test_context_sharing_real_gateway.py` 모두 `All checks passed!`; 저장소 루트 `git diff --check` 종료 0, 출력 없음.
- 기존 외부 HTTPS 38건 및 Gateway 코어 30건은 앞선 14:14 기록의 revision 증거로 재사용했다. 이 작업에서는 테스트 DB에서 현재 코드의 Gateway 전체 33건을 추가 실행했다. 운영 HTTPS 재검증·푸시·배포는 지시대로 미실행이다.

## 16:20 KST Gateway 사이트 상태 누락 fail-closed 교정

- 구현 전 회귀 RED: 새 `test_missing_site_state_stays_closed_after_restart_until_explicit_resume`를 추가하고 `CONTEXT_GATEWAY_TEST_DATABASE_URL` 및 `CONTEXT_GATEWAY_DATABASE_URL` 양쪽을 `postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_site_4aea5321`로 지정해 해당 테스트를 실행했다. 기존 구현은 singleton row 삭제 뒤 public `GET /api/v1/site`에서 `{"enabled": true}`를 반환해 기대한 false와 달라 `1 failed`였다.
- 교정: `gateway_site_control_audit`가 처음 도입되는 DB에서만 초기 enabled=true row를 생성한다. 이미 audit 테이블이 있는 DB에서 singleton row가 사라지면 읽기는 false이며 재시작 초기화도 row를 복구하지 않는다. control PATCH가 명시적으로 true를 기록하면 재개된다. 기존 row가 있는 정상 enabled 사이트와 최초 도입 호환을 유지한다.
- 캐시: public `/api/v1/site` 상태 응답에 `Cache-Control: no-store`를 설정했다. 닫힌 상태의 public 보호 경로 503도 기존 `no-store`와 `Retry-After: 60`을 유지하며 테스트로 확인한다.
- 실제 PostgreSQL: 허용된 로컬 PostgreSQL 17 서버의 새 전용 DB `test_cg_site_4aea5321`을 사용했다. `psql 'postgresql://winterholic@127.0.0.1:5432/postgres' -c 'CREATE DATABASE test_cg_site_4aea5321'` → `CREATE DATABASE`; 버전 확인은 PostgreSQL 17.11, 접속 role `winterholic`. 회귀 테스트가 `SELECT current_database()` 값을 해당 이름과 assert한다. 기존 DB는 재사용·삭제하지 않았다.
- GREEN 및 범위: Gateway의 기존 사이트 제어·권한·감사 검사를 새 DB에서 선택 실행했다. `CONTEXT_GATEWAY_TEST_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_site_4aea5321 CONTEXT_GATEWAY_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_site_4aea5321 ./.venv/bin/python -m pytest -q -o asyncio_default_fixture_loop_scope=function tests/test_rest_mcp_parity.py::test_site_pause_blocks_exchange_existing_rest_mcp_and_export_then_resumes_without_ttl_reset tests/test_rest_mcp_parity.py::test_site_control_requires_control_token_and_public_role_cannot_change_site tests/test_rest_mcp_parity.py::test_public_database_role_can_read_site_switch_but_cannot_write_or_read_audit tests/test_rest_mcp_parity.py::test_missing_site_state_stays_closed_after_restart_until_explicit_resume` (`cwd=context_gateway`) → `4 passed in 0.69s`. 별도 미교환 원문 키는 닫힘 중 exchange 503, 재개 뒤 200이며 PostgreSQL `APIKey.expires_at`의 전후 값이 같았다. 기존 session 만료, 기존 키 TTL, control 인증 누락 401, public role site SELECT 성공·write 거부·감사 조회 거부도 확인했다.
- 정적 검사: `./.venv/bin/ruff check src/context_gateway/apps.py tests/conftest.py tests/test_rest_mcp_parity.py` (`cwd=context_gateway`) → `All checks passed!`; `git diff --check -- context_gateway/src/context_gateway/apps.py context_gateway/tests/conftest.py context_gateway/tests/test_rest_mcp_parity.py docs/work-history/2026-11-16-context-gateway-progress.md` 종료 0, 출력 없음. 전체 Gateway·Private·HTTPS 검사는 재실행하지 않았다.
- 범위 경계: 상태 API와 새 요청의 차단/재개는 확인했다. 열린 MCP/HTTP 스트림의 강제 종료, 이미 진행 중인 요청의 취소·롤백은 검사하지 않았다. 운영 site 상태·키·DB·게시물, Private/API/MCP 정책 파일은 변경하지 않았고 push/deploy도 하지 않았다. 이 기록 파일에는 앞서 들어온 15:44 관측 추가를 보존한다.
