# Context Sharing v2 최종 계약 독립 검토

- 일자: 2026-11-19 (Asia/Seoul)
- 작업 위치: `back-context-sharing-v2-20261119`
- 기준 HEAD / `origin/main`: `978cb5ace5115743b32f375ce065120e8d068d82` (일치)
- 검토 결론: 운영 안내와 현재 코드의 정적 계약은 일치한다. 기존 assertion 공백은 허용된 신규 반례 2건으로 보완했다. 공개 Reader 수락 1건은 다른 작업자 교정 중으로 보류한다. 확인된 운영 노출이나 실행 코드 결함은 없다.
- 소유 경계: 기존 제품 소스·기존 테스트·운영 문서는 읽기 전용. 이 기록과 신규 `tests/test_context_sharing_v2_final_contract_counterexamples.py`만 작성했다. Gateway 공개 Reader `context_gateway/web/`는 교정 담당 소유라 읽기·실행·브라우저 검수를 하지 않았다.

## 판정 요약

| 항목 | 결과 | 영향 / 다음 조치 |
|---|---|---|
| 설정·route·REST/MCP·배포 안내 | 정적 대조 일치 | 실제 운영 연결은 별도 승인 및 환경 확인 전까지 미검증 |
| A/B/C 및 core·Private 보안 계약 | 기존 독립 증거와 현재 SHA 연결 | 합성 DB/ASGI 범위에서 유효. 운영 TLS·운영 role·배포 상태는 증명하지 않음 |
| 테스트 diff의 보안 assertion | 두 제한 assertion이 교체된 사실 확인 후 개인 publish 경계 신규 반례 2건 추가 | count/size 경계 각각 RED 재현 후 전용 DB에서 GREEN `2 passed`; 기존 테스트는 읽기 전용 |
| 공개 Reader | 검토 대상에서 제외 | 다른 작업자 교정 중. 새 acceptance/evidence 전까지 최종 통합 수락 보류 |
| 배포 적용·외부 활성화 | 하지 않음 | nginx/Tunnel, migration, 서비스 기동, 운영 key·문서 발급, push/deploy 금지 준수 |

## 1. 운영 안내와 실제 코드 대조

- **설정 이름·기본값:** `context_gateway/config.py`의 Gateway 설정은 `CONTEXT_GATEWAY_DATABASE_URL`, `CONTEXT_GATEWAY_SNAPSHOT_DIR`, `CONTEXT_GATEWAY_CONTROL_TOKEN`, `CONTEXT_GATEWAY_RATE_LIMIT`, `CONTEXT_GATEWAY_INITIALIZE_SCHEMA`, `CONTEXT_GATEWAY_MCP_ALLOWED_HOSTS`와 일치한다. 기본 snapshot 경로, 분당 120, schema 기본 true, MCP host 목록도 안내와 같다. Private 설정은 `src/ttakkari/config.py`의 `TTAKKARI_` prefix 및 `context_gateway_timeout_seconds` 기본 10초/`0 < x ≤ 60`과 일치한다.
- **Publisher credential 파일:** `context_sharing/gateway.py::_connection_settings`가 prod에서 세 연결 설정이 모두 비었을 때만 `publisher.json`을 읽고, 소유자·regular file·권한·8 KiB·정확한 필드·HTTPS origin·token 형식을 검사한다. 일부 명시 설정과 파일 값을 섞지 않는 fail-closed 설명이 코드와 일치한다. 실제 credential 파일은 읽지 않았다.
- **경로:** Gateway control route는 기존 `/control/v1/*`와 신규 `/control/v2/credentials`, `/control/v2/credentials/status`다. Private `GatewayControl._send`의 `/v2/` 선택이 control v2로 보낸다. Private owner 및 agent API는 `/api/context-sharing` 및 `/api/agent/context-sharing` 아래 `/keys`, `/links`, 각 revoke route를 사용한다. Public REST는 `/api/v1/*`, MCP는 `/mcp`; 사람용 링크는 `/s#token=...`, cookie 전용 share exchange/context/export/logout 경로다.
- **REST/MCP 예시:** Private 발급 결과의 `connect.project_id`와 public origin을 쓰며 Private `entity_id`와 혼동하지 않는다고 안내한다. MCP `.mcp.json`의 `${TTAKKARI_CONTEXT_GATEWAY_PUBLIC_URL}` 및 별도 `CONTEXT_GATEWAY_API_KEY`는 문서의 사용자 지정 환경 변수 예시다. MCP tool 목록은 `list_projects`, `get_project_context`, `search_project_context`, `get_document`, `export_project_context`이며 코드/계약 테스트와 일치한다. 실제 설치 Claude Code 접속은 하지 않았다.
- **배포 순서:** 저장소 절차는 full SHA release 자료 전송 → Gateway Compose `config --quiet`/control build/control+public 기동 → Gateway DDL 및 role 확인 → Private DB backup 및 Alembic `0080` → Private backend 배포 → 별도 승인 활성화 순서다. Gateway와 Private DB·release가 분리되고 기존 DB volume에서는 role init script가 재실행되지 않는다는 주의가 맞다. `control`이 initializer 실행 주체이고 homeserver public initializer가 꺼진 점도 Compose와 맞다.
- **DB role·migration:** initializer가 public role이 존재할 때 제한된 `SELECT`, API key의 필요한 column `UPDATE`, session `INSERT/UPDATE`, audit `INSERT`만 부여하고 site control write 및 audit read를 회수한다. role 생성은 최초 DB volume init 스크립트 책임이다. `0080_ctx_share_v2_keys`는 `0079_context_sharing` 위 additive columns이며 down_revision, 기본 `single_use_v1`, `latest`, scope backfill을 확인했다. v2 행을 남긴 채 migration downgrade 금지 및 Gateway runtime DDL 자동 downgrade 부재 설명이 실제 downgrade와 맞다.
- **폐기 장애 복구:** Private에서 `revoking` 의도를 commit한 뒤 Gateway 전달, 미확인 때 완료를 표시하지 않고 maintenance에서 재시도한다. 코드의 interval은 10–60초 clamp 및 최대 50개 batch이고, Gateway 미도달 동안 부모 인증이 유효할 수 있다. remote revoke 뒤 Private commit 실패도 pending 대사한다. 문서는 즉시 차단이라고 과장하지 않는다.
- **보안 응답·로그:** 문서의 no-store/CSP/no-referrer, cookie flags, `401` detail 종류, `404` snapshot 숨김, 부모 rate limit 공유 및 site `503`은 코드와 tests가 뒷받침한다. 저장소 logger 정적 검색은 운영 stdout/Cloudflare 분석 헤더·body 보존을 검증하지 않는다.

## 2. A/B/C 및 필수 보안·호환성 근거

아래 실행 결과는 기존 work-history에서 재사용했다. 이번 검토에서 같은 suite를 반복 실행하지 않았다.

| 요구 | 수락 근거 / assertion | 실행 증거 및 대상 revision/SHA |
|---|---|---|
| A: pinned | `test_v2_metadata_auth_regressions.py`의 pinned 후속 publish metadata isolation; core 독립 수락의 project/list/context/snapshot 경계. pinned grant가 발급 시 snapshot과 metadata만 반환 | `docs/work-history/2026-11-19-context-gateway-v2-independent.md`: 명시적 `test_cg_v2_verify_20261119_7c4e`, 전체 Gateway `48 passed in 3.20s`; 후속 수정 없는 SHA 확인 후 `48 passed in 3.01s`. 현재 `apps.py` `13678e8…`, `auth.py` `c7c4c40…`, `models.py` `3b8e23d…`, `publishing.py` `e43542c…`, `web_serving.py` `92f9a35…`, metadata regression `f1a7034…`가 독립 기록 SHA와 일치 |
| B: latest | 새 승인 publish 후 latest resolver가 새 snapshot으로 이동하고 다른 snapshot ID는 404; pinned는 이동하지 않음 | 위 Gateway 48건 및 `test_v2_control_contract.py`; 현재 test SHA `eea7e9a…`. Private 실제 Gateway 통합 수락 3건도 A/B 여정을 포함 |
| C: metadata-only republish | 동일 project 이름에서 설명만 바꾼 새 publication이 새 manifest/Project 설명에 반영됨; 새 v2 pinned는 발급 시 metadata에 고정됨 | core metadata regression 및 독립 기록 legacy 보충; `test_v2_metadata_auth_regressions.py` SHA `f1a7034…`; legacy test는 helper 경로이지 과거 게시 metadata 복원 증거가 아님 |
| 링크/API key 분리·1회 원문·project UUID | Private 197건 중 실제 HTTP 발급/list/revoke와 `connect.project_id`, search-only/export-only parameterized assertion; 별도 Private 실제 Gateway acceptance에서 반복 REST/MCP, 링크 교환/cookie/revoke | `docs/work-history/2026-11-19-context-sharing-v2-private.md`: 8파일 집중 명령 `197 passed in 21.64s`, 합성 Gateway control. `docs/work-history/2026-11-19-context-sharing-v2-private-verification.md`: 전용 DB URL 및 `PGPORT=5432`, 수락 `3 passed in 2.00s`; 최종 lead 재실행 `3 passed in 1.89s`. 현재 service `e615d76…`, gateway adapter `068ee28…`, model `700f1f2…`, API `79d0a0d…`, MCP `984c07b…`, acceptance `73854b3…` |
| REST/MCP scope·반복 사용·rate limit | core acceptance는 read/search/export 단일 scope raw key 및 반복 exchange session 허용/거부 matrix; REST/MCP 같은 resolver·share audience reject; 부모와 자식 세션 rate counter 합산 | `test_v2_independent_acceptance.py` + metadata regression, 전체 Gateway 48건. 현재 acceptance SHA `20141eb…`; package contract SHA `2ede216…`; deployment contract SHA `168a3ef…` |
| 만료·부모/세션/프로젝트/snapshot 폐기·global pause | core 독립 tests의 expiry/revoke detail 및 열린 session 재검사; 기존 REST/MCP pause-resume/TTL assertions; Private acceptance의 미전달 폐기에서는 remote session 유지, retry 뒤 401 | core 48건; Private acceptance 3건. Private 기록은 maintenance callback과 commit-failure 대사 포함. 합성 Gateway/격리 PostgreSQL 범위 |
| 민감정보·digest·게시 불변성 | Private 기존 API/scanner suite의 `test_risky_body_is_previewed_blocks_publish_and_can_be_edited_away`, title/summary 검사, stale digest 409 및 document SHA; Gateway snapshot integrity/provenance/idempotent operation tests 및 commit failure 원복 | Private 197건; core 48건에는 publish failure 및 idempotency/runtime DDL. 현재 `test_context_sharing_api.py` SHA `af3d554…`, `test_snapshot_integrity.py` `b2dd2f2…`, `test_publish_idempotency.py` `514f612…`. 운영 실파일시스템 장애 발생률 증거는 아님 |
| 회사(work) 신규 공유 차단, v1 보존 | API acceptance가 v1/v2/link/agent v1 모두 `403 work_sharing_policy_required`, Gateway 호출 증가 없음, 기존 v1 행 active 유지; Private independent acceptance가 v1 3600초/단회 동작 확인 | Private 197건 및 acceptance 3건. work 공유 정책 없음/우회 차단은 조직 정책 계약과 일치 |
| 관리자 UI | lead 기록의 5파일 Vitest `5 files / 98 passed`, 4.19s, exit 0. 95 기존 + 독립 수락 3 포함 | front `HEAD=f6dfe5b553552cfabddd4749e74f2c47332488ec`; 현재 수락 대상 파일 SHA: Page `8692d0b…`, Page test `c4f41f3…`, ShareLinkPanel `240df54…`, API `821e0b4…`, API test `63847b0…`, errors test `c403f1c…`. front worktree에는 이 검토에서 건드리지 않은 dirty 변경이 있으나 해당 run 이후 동등성은 lead의 현재 증거로만 판단. 테스트 재실행 안 함 |

## 3. 기존 테스트 diff와 정책 대조

- 기준은 저장소의 v2 구현 계약이다. `tests/test_context_sharing_mcp.py`에서 과거 `test_work_scope_publish_with_confirmation_issues_key_and_audits_agent_confirmation`은 work 확인 후 publish/key 발급 성공을 기대했으나 현재 계약은 조직 정책 증명 없는 외부 공유를 금지한다. 교체된 테스트는 `work_sharing_policy_required`, Gateway 호출 0건, draft/publish audit 0건을 검사한다. 이 변경은 계약에 맞는 강한 차단이다.
- 같은 MCP 테스트의 `test_confirmed_work_scope_keeps_scanner_and_document_limits`는 과거 work 경로에서 `sensitive_content_blocked`, `too_many_documents`, `draft_too_large`를 각각 기대했다. 현재는 세 경우 모두 게시 시도 전 `work_sharing_policy_required`를 검사한다. 별도 개인 범위 test가 민감정보 scanner와 digest mismatch를 계속 검증한다. 개인 범위 `too_many_documents`/`draft_too_large` assertion은 없었다.
- 코드 `service.py::create_draft`에는 `MAX_DOCUMENTS`, `MAX_DOCUMENT_CHARS/MAX_DRAFT_CHARS` checks가 남고 work publish도 정책 차단된다. 실행 코드의 우회 결함은 찾지 못했다. 신규 독립 반례 `test_context_sharing_v2_final_contract_counterexamples.py`가 개인 publish에서 51개 문서 및 200,001자 문서를 각각 422로 거부하고 Gateway 호출이 없음을 확인해 기존 evidence gap을 메웠다.
- Rate-limit test diff는 count assertion을 API key row로 옮기고 session row count `0`을 확인한다. 현재 `authenticate_session`이 부모 key counter에 합산하는 코드 및 새 v2 key+session 429 assertion과 맞아 보안 완화가 아니다. site/pause, audit permission, control auth, expiry assertions는 삭제되지 않았고 formatting만 바뀌었다.
- 삭제 assertion 집계는 `git diff --unified=0 -- context_gateway/tests tests | rg '^-(?!-).*assert|^-async def test_' --pcre2`로 검토했다. 의미 있는 제거는 work publish 성공 기대를 신규 policy deny로 교체하고 그와 함께 work fixture의 scanner/문서 수/크기 결과 세 개를 제거한 부분이다. 테스트 파일에서 이를 숨기거나 삭제한 다른 security assertion은 찾지 못했다.

## 4. 배포 게이트와 실행 금지 경계

- `context_gateway/tests/test_deployment_contract.py`는 현재 control app route 전부에 대해 nginx `/control/v1/`, `/control/v2/` prefix가 같은 upstream으로 연결되는지, Host/HTTPS forwarded header, public 블록의 control upstream 부재, control access log off, fallback 404를 검사한다. `test_package_contract.py`는 public OpenAPI에서 control route 부재, control 앱 미마운트, REST/MCP export 도구 계약을 검사한다. 현재 assertion/file SHA는 각각 `168a3ef8…`, `2ede2162…`다.
- 과거 lead의 proxy 교정 증거는 변경 전 `Control route blocked by homeserver proxy: /control/v2/credentials` 1 fail 후 nginx location 추가, 명시적 Gateway DB로 `test_deployment_contract.py test_package_contract.py` **5 passed in 0.64s**, ruff 통과, diff-check exit 0이다. 현재 location 및 tests SHA가 확인되지만 `nginx -t` 실행 파일은 당시 환경에 없어 미실행이었다.
- `context-gateway-web-deploy.md`는 별도 배포 문서다. 운영 안내가 이 checkout의 `nginx.homeserver.conf` v2 proxy 변경을 확인하고 아직 적용되지 않았다고 기록한 내용과 충돌하지 않는다. 운영 TLS/DNS/Tunnel 현황은 조회하지 않았다.
- 필수 배포 게이트는 승인된 적용 단계에서 full revision 확인, Compose `config --quiet`, build 및 control/public ready, DB additive DDL·public role/grant 조회, Private backup/Alembic `current`와 v1 행 조회, private backend 별도 배포, nginx `-t`·`edgectl` 정적/route 확인, 제한된 health/control route check다. public Reader 교정/acceptance가 끝나기 전 v2 공개 발급·외부 활성화는 열지 않는다.
- 이번 검토에서 운영 nginx/Tunnel 변경·reload, database/실제 documents/secrets 읽기, 운영/공유 DB reset, 서비스 재시작, 외부 공유 활성화, push/deploy는 하지 않았다. `nginx -t`, compose build/up, live browser/TLS/runtime role은 미실행이며 운영 상태 확인으로 표시하지 않는다.

## 5. 실행 증거와 파일 SHA

이번 검토에서 실행한 명령과 결과:

```text
$ git rev-parse HEAD && git rev-parse origin/main
978cb5ace5115743b32f375ce065120e8d068d82
978cb5ace5115743b32f375ce065120e8d068d82

$ git pull --ff-only
Already up to date.

$ git diff --check
exit 0 (출력 없음)

$ PGPORT=5432 TTAKKARI_TEST_DB='postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_v2_final_contract_20261119' TTAKKARI_DATABASE_URL='postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_v2_final_contract_20261119' PYTHONPATH=src ../back/.venv/bin/python -m pytest -q tests/test_context_sharing_v2_final_contract_counterexamples.py
.. [100%]
2 passed in 1.27s; db reset: 2 selective, 0 forced full, 0 fell back to full

$ ../back/.venv/bin/ruff check tests/test_context_sharing_v2_final_contract_counterexamples.py
All checks passed!

$ ../back/.venv/bin/ruff format --check tests/test_context_sharing_v2_final_contract_counterexamples.py
1 file already formatted
```

RED/GREEN receipt: 전용 DB `test_cg_v2_final_contract_20261119`를 만들고 `current_database(), current_user, inet_server_addr(), inet_server_port()`로 `test_cg_v2_final_contract_20261119|winterholic|127.0.0.1|5432`를 확인했다. 신규 테스트에만 임시 monkeypatch로 `MAX_DOCUMENTS=1000`, `MAX_DOCUMENT_CHARS=300000`을 주입한 RED 실행은 두 assertion이 모두 실패했다(한도 비활성화 후 publish가 각각 `200`, 기대 `422`; `2 failed in 1.72s`). 주입을 제거한 뒤 GREEN은 `2 passed in 1.27s`. 첫 작성 시 `gateway` fixture를 잘못 요청해 setup error가 났고, owner draft 요청은 schema가 먼저 422 `validation`으로 막는다는 경로 확인 뒤 실제 auto-publish 경로로 교체했다. 이 두 탐색 실행은 최종 GREEN으로 계산하지 않는다. 이 시험은 합성 Private 앱/fixture와 별도 DB 범위다.

SHA-256 (현재 worktree 기준, 경로는 repository root relative):

```text
context_gateway/src/context_gateway/apps.py                         13678e87285ae8811b225f2e930dba24f6003e34551b2e6a770cc1949f2e2d56
context_gateway/src/context_gateway/auth.py                         c7c4c40e5ff65ee7bac242375b9573de0ba6a079961ca48407a156a46c29422b
context_gateway/src/context_gateway/models.py                       3b8e23d123d43047cddf3e35c63131928f375d3cb0bfac5d610cbac307e642e5
context_gateway/src/context_gateway/publishing.py                   e43542cdce9c83f0bda8e82d5bf25a84771f9c2171d2cd8e915303025300ff58
context_gateway/src/context_gateway/web_serving.py                  92f9a35498f4e1791bb2e4840d0db9630b0ceddc3dbb034228f6705206751a3f
context_gateway/tests/test_v2_independent_acceptance.py              20141eb846b997b6f9b5246a3dd0ece36d3886d752c4e88dfec20cbc69d44f30
context_gateway/tests/test_v2_metadata_auth_regressions.py           f1a7034620580e1d0c23634a5ebc1b1be697c011de2d1b6a359fcbc72049dd3a
context_gateway/tests/test_deployment_contract.py                    168a3ef8ccc6e1b2516f39f18149d49d0281f94b3098a04a5cc49592020b7984
context_gateway/tests/test_package_contract.py                       2ede2162a5c8c17cc9bb74f40838fc1495551b675b9195338b6b2565ba162f8c
context_gateway/tests/test_v2_control_contract.py                    eea7e9ab34a130199261920cc3a4ba2d7f7da1de2c03834a2c41a904c7ef7dc1
context_gateway/tests/test_runtime_migration_v2.py                   e83ef6c75dec1d0fc11162bd1021e6b448cc99ddf8cd5c706cf302a56fda2296
context_gateway/tests/test_snapshot_integrity.py                     b2dd2f2ccea16796bc20d22e37121775da6e1e5a76972c13a3a52c91ef733a98
context_gateway/tests/test_publish_idempotency.py                   514f612b19585477606a612e109fa44556eb294859390401fc6a960f1b073d75
src/ttakkari/context_sharing/service.py                              e615d766c5370ae1d82de039e75eb13b6fc89621eea4f4972eecf35d0d26cc4a
src/ttakkari/context_sharing/gateway.py                              068ee28dd94983f540f9bd14fcb11e808f4d8a29e55f77d4fcfa4c304b7bc71f
src/ttakkari/context_sharing/models.py                               700f1f26e469b64a270aaeaf37c6713b7a05e646a8dc19967fdc3b8b4420a38a
src/ttakkari/api/context_sharing.py                                  79d0a0d565e9675db4b26b645122e5947c09651f12a516c1a411712ceb0f6854
src/ttakkari/orchestrator/mcp_server.py                              984c07b4af093d04870466f318cae73aaf590e55c7abf0a121497db62eb22c73
tests/test_context_sharing_v2_private_acceptance.py                  73854b3e6352fb362159c680bbafd4166a5a8a1bbd7863ae1101221b227ae205
tests/test_context_sharing_api.py                                    af3d554cfa97f560da085df58a2d016edd4e76c686c34e2a00933ebd82993cf2
tests/test_context_sharing_mcp.py                                    3f69ad1cfae456ad2e10464eb45c7c1a050585b80942d9a89a6c5a1a7301f1b8
tests/test_context_sharing_v2_final_contract_counterexamples.py      643ec39c94b8a247a4238853e612cbc5ec74bd0a04b8292c3de8173aeded351e
deploy/context-gateway/compose.yaml                                 f964f5a9a46b87422562495b4a1f37d4d312ebaec3b8f4cae5162d1f9468c096
deploy/context-gateway/compose.homeserver.yaml                      495948bd2f7c50bd42b280cb821a0368d8f359ef4b1849cd36e0f0aa1bd8b35a
deploy/context-gateway/nginx.homeserver.conf                        d8705d8c2d9b226f4554496926e2e82d7df6fd139bc9200281e0f61eafd7b1e9
deploy/context-gateway/01-create-public-role.sh                     af64c250cee015f3c9129c7d8387527558207b85620d33103f04036e6bd6b8bc
alembic/versions/0080_ctx_share_v2_keys.py                          cc2fa6d80c9b5a94b6453ed3e604adbc834883cfb769bff34264083fd85ece73
docs/context-sharing-v2-contract.md                                 31791976e5cf7eb36d772ab9e903a99fc39c5513b1ce08b7595eb19da794fa04
docs/context-sharing-v2-operations.md                               9d5825830e8a51a53ddaa60e15e73fd7ecfe0ef6464a25a4feb09eaf8dbb645b
docs/context-gateway-web-deploy.md                                  0fb369ea89c4ccf3fb7e1b848460800b76f40995bb5c04033631f54f361fbf30
```

미실행: 관리 UI 98건, Gateway core 48건, Private 197건 및 독립 수락 3건은 유효한 기록 결과를 재사용하고 반복하지 않았다. 기존 제품 소스·테스트는 수정하지 않았다. 공개 Reader browser/acceptance, 운영 DB·TLS/role/route 및 nginx syntax/runtime은 별도 소유/운영 환경 경계라 미검증이다.

## 남은 일

1. 공개 Reader 교정 담당의 새 acceptance/evidence를 fresh context에서 읽고 해당 기능 수락 전에는 통합 수락을 보류한다.
2. 운영 활성화 단계에서만 승인된 Gateway/Private release, DB role, migration, nginx/Tunnel, TLS 및 route health를 실행 확인한다. 이 검토의 범위가 아니다.
