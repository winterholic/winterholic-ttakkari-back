# Context Gateway v2 독립 인수 검증

- 일자: 2026-11-19 (Asia/Seoul)
- 작업 위치: `back-context-sharing-v2-20261119`
- 시작 및 종료 HEAD: `978cb5ace5115743b32f375ce065120e8d068d82`
- 기준: `origin/main`은 같은 revision. 코어 구현과 기존 테스트는 읽기 전용으로 확인했고 이 문서 및 `context_gateway/tests/test_v2_independent_acceptance.py`만 추가했다.
- 검증 DB: `test_cg_v2_verify_20261119_7c4e` (host PostgreSQL 17, `127.0.0.1:5432`). 기존 worker DB와 분리했고 삭제하지 않았다.

## DB 및 실행 증거

DB 생성과 확인:

```text
$ /opt/homebrew/opt/postgresql@17/bin/createdb --no-password -h127.0.0.1 -p5432 test_cg_v2_verify_20261119_7c4e
$ /opt/homebrew/opt/postgresql@17/bin/psql --no-password -h127.0.0.1 -p5432 -d test_cg_v2_verify_20261119_7c4e -tAc 'select current_database(), current_user, inet_server_addr(), inet_server_port()'
test_cg_v2_verify_20261119_7c4e|winterholic|127.0.0.1|5432
```

독립 acceptance와 동시 변경된 core metadata 회귀를 최종 파일 기준으로 실행:

```text
$ CONTEXT_GATEWAY_TEST_DATABASE_URL='postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_v2_verify_20261119_7c4e' TTAKKARI_TEST_DB='test_cg_v2_verify_20261119_7c4e' PGPORT=5432 PYTHONPATH=context_gateway/src ../back/.venv/bin/python -m pytest -q context_gateway/tests/test_v2_independent_acceptance.py context_gateway/tests/test_v2_metadata_auth_regressions.py
........                                                                 [100%]
8 passed in 1.12s
```

최종 전체 Gateway suite (추가 acceptance 포함):

```text
$ CONTEXT_GATEWAY_TEST_DATABASE_URL='postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_v2_verify_20261119_7c4e' TTAKKARI_TEST_DB='test_cg_v2_verify_20261119_7c4e' PGPORT=5432 PYTHONPATH=context_gateway/src ../back/.venv/bin/python -m pytest -q context_gateway/tests
................................................ [100%]
48 passed in 3.20s
```

정적 검사:

```text
$ ../back/.venv/bin/ruff check context_gateway/tests/test_v2_independent_acceptance.py
All checks passed!
$ ../back/.venv/bin/ruff format --check context_gateway/tests/test_v2_independent_acceptance.py
1 file already formatted
$ git diff --check
exit 0
```

## SHA와 동시 변경

| 파일 | 시작 SHA-256 | 최종 SHA-256 | 판정 |
|---|---|---|---|
| `context_gateway/src/context_gateway/apps.py` | `13678e87285ae8811b225f2e930dba24f6003e34551b2e6a770cc1949f2e2d56` | 동일 | 변경 없음 |
| `context_gateway/src/context_gateway/auth.py` | `c7c4c40e5ff65ee7bac242375b9573de0ba6a079961ca48407a156a46c29422b` | 동일 | 변경 없음 |
| `context_gateway/src/context_gateway/models.py` | `3b8e23d123d43047cddf3e35c63131928f375d3cb0bfac5d610cbac307e642e5` | 동일 | 변경 없음 |
| `context_gateway/src/context_gateway/publishing.py` | `e43542cdce9c83f0bda8e82d5bf25a84771f9c2171d2cd8e915303025300ff58` | 동일 | 변경 없음 |
| `context_gateway/src/context_gateway/web_serving.py` | `92f9a35498f4e1791bb2e4840d0db9630b0ceddc3dbb034228f6705206751a3f` | 동일 | 변경 없음 |
| `context_gateway/tests/test_v2_metadata_auth_regressions.py` | `70bd9d412fca35a3119a422b2154a60b0c970c64e6e1dfc7d8fe399cd4d73299` | `f1a7034620580e1d0c23634a5ebc1b1be697c011de2d1b6a359fcbc72049dd3a` | core 담당자의 동시 테스트 추가. 최종 SHA에 대해 해당 모듈과 acceptance를 재실행해 8 passed 확인 |

시작 시점과 같은 HEAD/base를 유지했다. 기존 작업 트리에는 다른 Gateway, Private, UI의 미커밋 변경이 있었으며 되돌리거나 수정하지 않았다. 기존 `test_v2_metadata_auth_regressions.py`는 시작 이후 내용이 바뀌어 그 영향 테스트를 최종 SHA에서 다시 실행했다.

## 요구별 결과

1. **revision, SHA, 기존 suite** — HEAD/base와 파일별 SHA를 기록했다. 코어 5개 구현 SHA는 시작/끝이 같고, 동시에 추가된 metadata 회귀 테스트는 변경 후 표적 재검증했다. 전체 suite `48 passed`.
2. **동료 링크, TTL, 반복 사용, cookie, 원문 비노출** — 실제 Gateway control/public FastAPI 앱과 위 격리 DB에서 API key 및 링크 TTL 1/3/7/30일을 확인했다. 링크 기본값 3일, 반복 링크 교환·같은 쿠키 세션 열람, `Secure; HttpOnly; SameSite=Strict; Path=/api/v1/share`를 확인했다. DB에는 hash만 저장되고 control status에는 raw key가 없다. Gateway control 발급 응답은 Private adapter가 fragment URL을 만들도록 일회 raw token을 전달한다. Private `/keys` 목록 및 URL 구성은 별도 Private 검증 범위라 미실행.
3. **반복 API Key, REST/MCP, scope와 project 경계** — 각 read/search/export 단일 scope에 대해 raw bearer와 반복 교환 session의 REST 허용·거부, MCP 세 도구 허용·거부를 새 matrix test로 확인했다. multi-scope context/search/export parity, 임의 project ID와 snapshot ID 조작 거부는 기존 suite에서 확인했다. Share session 및 링크 raw token은 일반 REST/MCP audience에서 `401 audience_mismatch`로 거부된다.
4. **A/B/C 게시 및 메타데이터** — pinned(A)는 후속 게시 문서·프로젝트 이름·요약·latest ID/list가 노출되지 않고 pinned snapshot만 반환한다. latest(B)는 새 게시 snapshot을 반환한다. 같은 프로젝트 이름으로 설명만 변경한 게시(C)는 새 snapshot manifest와 Project 설명에 반영된다. 관련 core metadata/control 테스트가 전체 suite에서 통과했다.
5. **만료·폐기·pause/resume·오류** — 새 테스트가 동일 share cookie session에서 부모 링크 만료, 키 폐기, snapshot 폐기, project 폐기 뒤 읽기 거부를 확인했다. 만료와 키 폐기는 각각 `credential_expired`, `credential_revoked`. 기존 pause/resume 검사는 pause 동안 이미 열린 REST/MCP/export session이 차단되고 resume 뒤 TTL 연장 없이 재사용되는 경계를 확인했다. share session의 일반 REST/MCP 거부 및 만료/폐기 오류코드 구분도 통과했다.
6. **게시 실패/멱등성/v1/DDL/role** — 새 테스트가 snapshot 디렉터리 이동 뒤 DB commit을 의도적으로 실패시켜 HTTP 500, latest/DB 원복, 파일 잔여물 제거를 확인하고 같은 operation_id 재시도가 정상 게시되는 것을 확인했다. 이는 synthetic commit failure 주입이며 실제 파일시스템/DB 장애 발생률을 측정한 것은 아니다. 기존 suite가 operation_id 같은 payload 재시도/변경 payload 409, 구형 schema additive runtime DDL, v1 값/불변 trigger, 제한된 public DB role 권한을 확인했다. 운영 DB role 및 배포 migration은 미검증.
7. **기록 및 제한** — 본 기록과 독립 테스트를 저장했다. 브라우저 UI, Private 앱↔Gateway 네트워크 경로, Private Alembic version stamping/migration, 운영 role, 운영 데이터·키·게시, 외부 공개, 배포·재시작은 별도 단위 또는 금지 범위라 실행하지 않았다. 검증은 독립 PostgreSQL DB에 연결한 앱 직접 ASGI 호출이다.

## 초기에 수정한 테스트 기대

독립 테스트 첫 실행에서 Gateway control 발급 receipt도 링크 raw token을 반환한다고 예상한 assertion이 잘못되어 실패했고, 수정 후 통과했다. 다음 실행에서는 빈 문자열을 응답 본문에 포함 여부 확인하는 assertion 작성 실수로 실패했고 이를 조건부 검사로 수정했다. 두 실패는 구현 동작 결함이 아니며 최종 acceptance 및 전체 suite는 통과했다.

## 코어 교정 독립 재검수

- 요청에 따라 구현 및 테스트 파일은 수정하지 않았다. 기존 검증 DB `test_cg_v2_verify_20261119_7c4e`는 사용하지 않고 보존했다. 이번 재검수는 별도 `test_cg_v2_review_20261119_9d24` (`127.0.0.1:5432`)로 수행했다.
- 검수 시점 HEAD/base는 계속 `978cb5ace5115743b32f375ce065120e8d068d82`. `auth.py` `c7c4c40e5ff65ee7bac242375b9573de0ba6a079961ca48407a156a46c29422b`, `apps.py` `13678e87285ae8811b225f2e930dba24f6003e34551b2e6a770cc1949f2e2d56`, `models.py` `3b8e23d123d43047cddf3e35c63131928f375d3cb0bfac5d610cbac307e642e5`, `publishing.py` `e43542cdce9c83f0bda8e82d5bf25a84771f9c2171d2cd8e915303025300ff58`, metadata regression test `f1a7034620580e1d0c23634a5ebc1b1be697c011de2d1b6a359fcbc72049dd3a`; 모두 직전 검증 SHA와 일치했다.
- 재실행: 위 DB로 `pytest -q context_gateway/tests/test_v2_metadata_auth_regressions.py` → `4 passed in 0.65s`; 전체 `pytest -q context_gateway/tests` → `48 passed in 3.01s`.
- pinned regression은 후속 게시 뒤에도 REST project/list/context/snapshot list, MCP project/context, share-cookie context가 grant 시점 이름·요약·snapshot ID·본문만 노출함을 확인한다. latest 선택은 기존 control 테스트에서 새 snapshot 선택, 같은 이름의 설명 변경은 별도 regression에서 확인했다. detail 기반 인증 오류는 expired/revoked API key와 session, share key/session의 일반 REST/MCP audience 거부를 HTTP `detail`로 검사한다.
- Legacy fallback 판정: immutable metadata가 없는 legacy manifest는 실제 과거 게시 이름/설명을 복원할 수 없다. 현재 발급 시점 Project 값을 pinned ACL에 기록하고 이후 Project 변경이 serializer에 나타나지 않는다는 직접 DB/helper test가 통과했다. 이는 계약에 적힌 fallback과 일치하며 결함으로 판정하지 않는다. 단, 과거 게시 당시 metadata와의 동일성은 증명할 수 없고, 기존 기록이 말하듯 이번 legacy 전용 test 자체는 helper 직렬화 경로 검증이다.
- 검수 결론: 이번 범위의 코드 결함은 찾지 못했다. 운영 DB·브라우저·Private 통합은 별도 검증 경계로 남는다.
