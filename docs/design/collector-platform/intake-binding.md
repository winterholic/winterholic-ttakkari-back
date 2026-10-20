# Intake scope 결속과 멱등 판정 계약

2026-10-20 · 독립 설계 검증(PASSED_WITH_CORRECTIONS)의 지적 R01·R02 반영. 기계 판독 정본은 [contracts/intake-binding.json](contracts/intake-binding.json), 판정 fixture는 `contracts/fixtures/intake/`. CP-S2-01 런타임은 이 문서와 fixture를 테스트 기대값으로 삼는다. 이번 수정은 계약·설계 문서만이며 런타임 구현은 하지 않았다.

## 1. Scope 결속 (R01)

envelope의 `access.tenant_id`, `access.scope`, `access.principal_ids`는 요청 본문이므로 신뢰하지 않는다. collector intake는 인증 주체에서 scope를 산출한다.

| 입력 경로 | 산출 근거 |
|---|---|
| 채팅/agent 세션 | 인증된 session의 owner와 `session.pool`. 기존 agent API가 `scope=session.pool`로 기록하는 방식(`src/ttakkari/api/agent.py:144,174`)과 같다 |
| agent principal token | token에 묶인 principal과 그 principal에 허용된 scope. 본문으로 확대할 수 없다 |
| 주기 실행(SourcePolicy) | 등록 시점에 결속된 SourcePolicy의 tenant/scope/owner. 실행 시 envelope 값으로 바꾸지 않는다 |

규칙:

1. `tenant_id`, `scope`는 산출값과 정확히 같아야 한다. `principal_ids`는 집합으로 산출값과 같아야 한다. 부분집합·상위집합 모두 불일치다.
2. 불일치면 `403 scope_binding_mismatch`로 거부한다. job, outbox, 예산 예약을 만들지 않고, 다른 scope의 멱등 행 존재 여부도 응답으로 드러내지 않는다.
3. 판정 순서: schema → semantic → scope 결속 → payload hash → 멱등 조회. 결속 전에는 멱등 저장소를 조회하지 않는다.
4. 저장은 산출값을 기준으로 한다. envelope의 access는 산출값과 같음이 확인된 감사용 사본이다.

envelope 단독 fixture(`fixtures/*.json`)는 인증 맥락이 없으므로 tenant/principal 변조를 잡을 수 없다. 이 결함을 intake fixture가 메운다.

## 2. 멱등 판정 (R02)

- 멱등키: `(산출 tenant_id, 산출 scope, envelope.source.key, Idempotency-Key 헤더)`.
- 정규화: RFC 8785 JSON Canonicalization Scheme(JCS). 객체 key는 UTF-16 code unit 순, 숫자는 ECMAScript 직렬화, 공백 없음, UTF-8.
- `payload_hash = sha256(UTF-8(JCS(envelope − 제외 경로)))`.
- 제외 경로: `/recorded_at` 한 개. 재전송마다 바뀔 수 있는 전송 메타데이터이기 때문이다. 제외해도 `recorded_at ≥ source.collected_at` 의미 검증은 그대로 한다.
- 포함: 나머지 전부. `envelope_id`도 포함한다. 같은 요청의 재전송은 같은 envelope_id를 써야 하며, 새 ID를 발급하면 DeliveryOutbox의 `UNIQUE(sink,scope,envelope_id,adapter_version)`와 어긋나므로 409로 본다.
- 같은 tenant/scope에서 다른 request key로 같은 `envelope_id`가 오면 `409 envelope_id_conflict`다. DB 유일키는 `UNIQUE(tenant_id, sink, scope, envelope_id)`라 다른 tenant의 같은 ID는 독립이다. raw `IntegrityError`는 밖으로 나가지 않는다.
- 결과: 새 키는 `202 created`. 같은 키·같은 hash는 `202 replay`로 기존 job ID를 돌려주고 행을 늘리지 않는다. 같은 키·다른 hash는 `409 conflict`이며 부작용이 없다.
- 런타임은 RFC 8785 검증 벡터를 통과한 구현을 써야 한다. 현재 back venv에 `rfc8785` 패키지는 없다(`ModuleNotFoundError`, 2026-10-20 확인). CP-S2-01은 외부 패키지 없이 순수 Python 구현(`ttakkari.collector.contracts.jcs`)을 채택했고 RFC 8785 숫자 벡터 11건을 단위 테스트로 고정했다. 정수는 |n| ≤ 2**53만, lone surrogate·NaN·Infinity·U+0000(PostgreSQL이 저장 못 함)·깊이 64 초과는 `CollectorContractError`(422)다. checker의 `jcs()`는 설계 검사용 참조 구현이다.

## 3. Fixture 판정표

| 파일 | 상황 | 기대 |
|---|---|---|
| intake-valid-bound | 산출값과 일치하는 단일 제출 | created |
| intake-invalid-tenant-tamper | tenant_id 임의 변조 | reject:scope_binding_mismatch |
| intake-invalid-principal-tamper | principal_ids를 다른 주체로 변조 | reject:scope_binding_mismatch |
| intake-invalid-principal-escalation | 자기 주체에 admin 추가 | reject:scope_binding_mismatch |
| intake-invalid-scope-tamper | personal 세션이 work로 제출 | reject:scope_binding_mismatch |
| intake-replay-key-order | 모든 객체 key 순서만 반대인 재전송 | created → replay |
| intake-replay-recorded-at | recorded_at만 바뀐 재전송 | created → replay |
| intake-conflict-semantic | 같은 키로 decision 변경 | created → conflict:409 |
| intake-conflict-envelope-id | 같은 키로 envelope_id 새 발급 | created → conflict:409 |
| intake-conflict-cross-scope-replay | 같은 키·payload를 다른 scope로 재전송 | created → reject:scope_binding_mismatch |

checker는 각 case에서 job 행 수(0 또는 1)도 확인한다. checker의 신뢰 경로를 envelope 값으로 바꾸거나, recorded_at 제외를 빼거나, JCS 대신 순서 보존 직렬화를 쓰는 변이를 넣으면 각각 실패하는 것을 확인했다(`evidence/verification.txt`).

## 4. 검증 범위

CP-S2-01 재작업 기준(2026-10-20, 증거는 `reports/collector/s2-01/verification.txt`):

- 서비스 계층 `submit_envelope`와 일회용 PostgreSQL에서 확인한다: fixture 10건의 결과와 job/outbox 행 수, 동일 키 동시 10요청 job 1행, 키는 다르고 envelope_id가 같은 동시 10요청 1행(나머지 409), 다른 tenant/scope 독립, 롤백 잔여 0, 마이그레이션 왕복과 모델 일치.
- 예외 계층: `CollectorContractError`(422), `ScopeBindingMismatchError`(403), `CollectorConflictError`/`EnvelopeIdConflictError`(409)는 서로 하위 클래스가 아니다. 각 예외가 `http_status`·`error_code`를 가진다.
- **미검증(CP-S2-02로 이동)**: HTTP 라우트, 실제 session/agent 인증 경로에서 AuthContext 산출, HTTP 상태 코드 403/409/422 응답.
