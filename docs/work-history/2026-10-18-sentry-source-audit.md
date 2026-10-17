# Sentry 원본 대조와 운영 로그 기반 보완 — 2026-10-18

사용자 요청: 알려진 Sentry 기능을 이정표로 원본부터 분석하고, 따까리의 초기 기반에서 필요한 부분만 보완한다. 보존·삭제 정책은 기존 보류 결정을 유지한다.

## 원본 선택과 실제 확인 범위

최신 커밋을 조회한 뒤 아래 SHA로 고정해 raw 소스를 다운로드했다. 원본 코드는 제품에 복사하지 않았다. 전체 저장소를 분석했다거나 Sentry와 동등한 구현이라고 주장하지 않는다.

| 저장소 | 고정 커밋 |
|---|---|
| getsentry/sentry | `624fb343cb8e0f9c1b852e6860c426fb7804b739` |
| getsentry/sentry-python | `0f7acd5e8968b6b8b92154669112c647ec88abe3` |
| getsentry/relay | `83696ac63b9296bc9fd5ccfb26708e3dc84c06b4` |

| 원본 파일·직접 읽은 지점 | 따까리 판단 |
|---|---|
| [grouping/strategies/newstyle.py](https://github.com/getsentry/sentry/blob/624fb343cb8e0f9c1b852e6860c426fb7804b739/src/sentry/grouping/strategies/newstyle.py): `stacktrace`, `single_exception`, `chained_exception` | 앱 프레임과 예외 타입 우선, 값/줄 번호 제외는 유지. 최종 RuntimeError만 보던 체인 그룹핑은 원인별 fingerprint도 포함하도록 보완. 플랫폼별 정규화 전체를 옮기지는 않는다. |
| [grouping/api.py](https://github.com/getsentry/sentry/blob/624fb343cb8e0f9c1b852e6860c426fb7804b739/src/sentry/grouping/api.py): configuration version·fingerprint variants | 단일 예외 키는 유지하고 새 체인 키만 `exc-chain-v1`로 구분. 기존 이슈를 임의 재작성하지 않는다. |
| [event_manager.py](https://github.com/getsentry/sentry/blob/624fb343cb8e0f9c1b852e6860c426fb7804b739/src/sentry/event_manager.py): `save`, aggregate 생성 트랜잭션, `_handle_regression` | 저장 ID와 집계/상태 전이 구분, DB 원자성을 대조. PostgreSQL 고유 키로 중복 claim과 집계를 한 트랜잭션에 둔다. 재발의 첫 상세는 샘플 예산과 별도로 남긴다. SHA만 있는 따까리는 Sentry의 릴리스 순서 판정을 그대로 흉내 내지 않고 발생/해결 시각 판정을 유지한다. |
| [ingest/consumer/processors.py](https://github.com/getsentry/sentry/blob/624fb343cb8e0f9c1b852e6860c426fb7804b739/src/sentry/ingest/consumer/processors.py): `process_event` replay check·cache 저장 시점 | 재처리 ID 추적 개념을 적용. 원본 주석도 지적하는 cache 일관성/TTL 한계를 옮기지 않고 DB 영수증을 사용한다. DB timestamp/sequence가 commit 순서를 보장하지 않는 경우도 별도 재현했다. |
| [SDK scrubber.py](https://github.com/getsentry/sentry-python/blob/0f7acd5e8968b6b8b92154669112c647ec88abe3/sentry_sdk/scrubber.py): `scrub_dict`, request·breadcrumbs·frames 처리 | 민감한 키 아래의 값은 자료형에 관계없이 통째로 가린다. dict/list 값이 통과하던 결함을 보완. SDK의 전체 PII 정책을 복제한 것은 아니다. |
| [SDK serializer.py](https://github.com/getsentry/sentry-python/blob/0f7acd5e8968b6b8b92154669112c647ec88abe3/sentry_sdk/serializer.py): memo·depth/breadth·문자열 제한 | 예산을 두는 기존 구조 유지. 따까리의 도구 응답 예산은 독자 요구이며, 줄인 목록의 다음 커서를 실제 반환 수로 맞춘다. SDK의 임의 객체 serializer를 이식하지 않는다. |
| [Relay processor.rs](https://github.com/getsentry/relay/blob/83696ac63b9296bc9fd5ccfb26708e3dc84c06b4/relay-pii/src/processor.rs): `apply_all_rules`, 원본 메타값에도 같은 규칙 적용 | 원본뿐 아니라 파생 제목·breadcrumb·응답도 마스킹해야 한다는 기준으로 점검. 인용 문자열의 escaped quote/닫히지 않은 quote 처리, 마스킹 전에 제목·breadcrumb을 자르던 경로를 보완. |
| [Relay config.rs](https://github.com/getsentry/relay/blob/83696ac63b9296bc9fd5ccfb26708e3dc84c06b4/relay-pii/src/config.rs): 정규식 컴파일 예산·실패 처리 | 기존 마스킹 입력 상한·장문 성능 검사를 유지. 별도의 사용자 정규식 설정 엔진은 추가하지 않는다. |

## 재현으로 확정한 결함과 수정

| 결함 | 변경 전 실제 결과 | 보완 |
|---|---|---|
| 샘플 생략 후 중복 재수집 | 같은 ID를 다시 넣으면 `Recorded(...stored=False)`를 반환하고 카운트를 다시 올림 | 상세와 별개인 `ops_event_receipts` 고유 ID claim. 원문/비밀은 저장하지 않음 |
| 재발했지만 상세 예산 소진 | `regressed=True, stored=False`여서 최신 원인 근거 없음 | 재발 첫 이벤트는 상세 보존 |
| 크기 제한 목록 페이지 | 5개를 반환했는데 `next_cursor=10` | 최종 반환 수로 커서 재계산, 이슈/이벤트 검색 양쪽 적용 |
| 같은 종료 시각의 배치 경계 | 실패 Run 205개를 3회 동기화해도 200개만 집계 | 14일 범위의 미처리 ID를 선택. 원본 ID/receipt 인덱스로 중복 제외 |
| sequence와 commit 역순 | id=20을 처리한 뒤 늦게 commit한 id=10은 누락 | 같은 미처리 ID 선택 방식 적용 |
| 비밀값의 구조·escaping | password dict/list와 escaped quote 뒤 `private-tail`이 남음 | 민감 키 값 전체 제거, escape-aware 인용 문자열 처리 |
| 마스킹 전 파생값 자르기 | 긴 인용 비밀번호 일부가 실제 ops DB 제목/breadcrumb에 남음 | 마스킹 후 제한 적용. 이미 잘린 인용 문자열도 보수적으로 가림 |
| 같은 wrapper의 다른 원인 | ConnectionError/PermissionError를 감싼 RuntimeError가 같은 키 | cause/context를 그룹 키에 포함 |
| 저장 오류를 중복으로 오인 | NOT NULL 위반도 `IntegrityError`를 삼켜 정상 skip처럼 반환 | 예상 중복은 고유 claim으로 처리하고 실제 DB 오류는 전파·로그·재시도 |
| 소스 간 rollback 전파 | 뒤 소스 실패 시 앞 소스 성공까지 롤백하는 코드 경로 | 소스별 savepoint. 실패 소스만 취소하고 다른 성공을 커밋 |

동시 동일 ID 6회, 실패 트랜잭션 후 같은 ID 재시도, source별 부분 실패도 실제 DB 테스트로 확인한다. 모든 사례가 운영에서 실제 발생했다고 주장하지 않는다. 합성 데이터 재현과 소스 대조 근거다.

## 스키마와 운영 경계

- 0040은 새 영수증 테이블과 그 시간 인덱스만 추가한다. 기존 `ops_events.dedupe_key`를 영수증으로 채운다. 0039와 online 호환을 명시했고 프로토콜은 1을 유지한다.
- 과거 샘플 생략 원본은 ID가 없어 과거 집계를 정확히 복원할 수 없다. 임의 수정을 하지 않는다. 첫 재수집에서 한 번 더 집계될 가능성은 남고 이후 중복은 방지한다.
- 과거 체인 이슈를 강제로 나누지 않는다. 새 체인 이벤트부터 원인별 키가 적용된다.
- 일반 상세 샘플 30건/이슈/시간은 유지하되 재발 첫 상세는 예외다. 삭제 자동화는 추가하지 않는다. 나중의 보존 정책은 상세와 영수증을 구분해야 한다.
- 기존 파일 백필/블록/발췌 길이 예산, 시간 추정 태그, release 추정 태그, 회사 세션 차단은 유지한다. 전체 원문 보존이나 Sentry의 모든 언어·ExceptionGroup·트레이싱 지원을 의미하지 않는다.
- 작업은 `back-ops-sentry-audit` 독립 worktree. 실행 시 `PYTHONPATH=src`, 독립 DB `ttakkari_ops_audit_20261018`/`ttakkari_ops_audit_clip_20261018`를 지정했다. 실제 운영 로그 원문이나 인증 파일을 열지 않았다.

## 검증 기록

- 변경 전 실패 로그: `/private/tmp/ops-audit-red.log` (10 failed, 1 passed; 그중 잘못 고른 varchar 예외 검사는 NOT NULL 위반으로 고쳐 별도 재현), `/private/tmp/ops-audit-integrity-red.log` (1 failed), `/private/tmp/ops-audit-clip-red.log` (1 failed).
- 관련 최종 검사: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_oplog.py tests/test_oplog_migration.py unit_tests/test_oplog_scrub.py unit_tests/test_oplog_normalize.py unit_tests/test_oplog_limits.py` → **87 passed in 8.18s** (새 영수증 테이블까지 마스킹 검사 대상에 포함한 최종 실행). 세대 계약 포함 직전 검사도 **113 passed**.
- `ruff check` 변경 파일 → **All checks passed!**. `git diff --check` → exit 0.
- 최초 전체 검사는 별도 worktree의 `.venv/bin/alembic` 경로가 없어 624 passed 뒤 중단됐다. 제품 코드 실패로 판단하지 않았다. 테스트가 요구하는 worktree `.venv` 링크를 준비한 뒤 최종 코드와 origin/main(e817fc1)을 포함해 다시 실행했다. `TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_ops_audit_20261018 PYTHONPATH=src .venv/bin/python -m pytest -q -x` → **1453 passed, 4 skipped, 2 xfailed, 1 warning, 6 subtests passed in 324.03s**. warning은 기존 Starlette HTTP_422 상수의 deprecation이다.
- 관련 검사·전체 검사·스키마 계약/실제 마이그레이션 검사가 통과했다. 커밋·푸시 후 별도 배포 도구로 정확한 SHA를 예약한다. 예약 수락과 운영 반영은 다르며 새 운영 MCP/수집기 동작은 이번 로컬 검증에 포함하지 않는다.
