# Research Lab selection 방향·조건 독립 검증 — 2da5482

## 판정

**지정된 방향·조건 교정 범위: PASS.** 새 격리 Run의 실제 ASGI/PostgreSQL 선택 응답에서 기존 두 반례와 방향·조건 경계를 확인했다. 이는 PMS 7.2 전체 완료 판정이 아니다.

## 고정 대상과 경계

- Branch: `research-v2`
- HEAD: `2da5482cd0320e7177165964b6949d1cff72efc6`
- 해당 커밋은 제품 교정 `8ec7586`과 `main`의 `4053ee12`를 합친 `e1b27975` 뒤에 있다.
- 실행 전 working tree는 깨끗했다. 실행 후 임시 probe 테스트는 제거했고, 증거 JSON과 이 보고서만 추가했다.
- 원 독립 verifier `f76`의 FAIL은 기존 보고서·raw 근거와 이번 API 판정 형태가 일치해 정확한 것으로 인수했다. 이번 실행은 그 보고를 대체하는 새 독립 검증이며 이전 PASS를 재사용하지 않았다.
- 변경 코드, service/API 연결, selection context 저장 흐름은 수정하지 않았다. 기존 서비스/API 구현 경로로 API 요청을 보냈다.

## 실행 환경

`TTAKKARI_TEST_DB` 환경 변수가 주입돼 있었다. DSN은 출력하지 않고 SQLAlchemy `make_url`로 데이터베이스 부분이 `ttakkari_test`인지 확인했다. 실제 ASGI 요청 중 `select current_database(), current_user` 결과는 `("ttakkari_test", "ttk_test")`였고 주입 URL과 일치했으며 운영 DB `ttakkari`가 아니었다. `tests/conftest.py`의 명시 DB 경로와 테스트 DB 격리/reset을 사용했다. 별도 `initdb`는 실행하지 않았다.

## 실제 API 결과

실행한 범위는 임시 독립 ASGI probe 1개(서로 다른 실제 요청 8건)와 `unit_tests/test_research_rules.py`뿐이다. probe는 합성 연구 메모를 저장해 `/api/research/.../select`를 호출하고 응답의 후보 상태, 관계 방향, 양 조건 원문, 각각의 applicability, 합성 applicability, `affects_selection`을 확인했다.

| 실제 API 입력 | 관찰값 | 판정 |
|---|---|---|
| `challenges_under_conditions`, method가 `from_id`, dataset B 일치 | `candidate`; `affects_selection=false`; edge와 Node 조건 모두 보존 | outbound challenge가 method를 검토 대상으로 만들지 않음 |
| Node dataset A, Relation dataset B, 환경 dataset B | Relation `applicable`, Node `incompatible`, 합성 `incompatible`; `candidate`; `affects_selection=false` | Node 조건 불일치가 edge 일치에 덮이지 않음 |
| Node dataset B, Relation dataset A, 환경 dataset B | Node `applicable`, Relation `incompatible`, 합성 `incompatible`; `candidate`; `affects_selection=false` | Relation 조건 불일치도 독립 제약으로 적용 |
| 빈 Relation 조건, Node dataset A 일치 | Relation `unknown`/unspecified, Node 및 합성 `applicable`; `needs_review`; `affects_selection=true` | 한쪽 빈 조건은 추가 제약이 아님 |
| 양쪽 조건 모두 빈 상태 | 합성 `unknown`/unspecified; `needs_review`; `affects_selection=true` | 양쪽 빈 조건을 미확정으로 보존 |
| Node dataset A + hardware m5, 요청에서 hardware 누락 | Relation `applicable`, Node 및 합성 `unknown`, missing `hardware`; `needs_review` | 누락 입력은 검토 필요 |
| `contradicts` 관계가 method에서 나감, 조건 일치 | `needs_review`; `affects_selection=true` | 대칭 상충은 양 endpoint에서 영향 |
| `conflicts_with` 관계가 method에서 나감, 조건 일치 | `needs_review`; `affects_selection=true` | 대칭 상충은 양 endpoint에서 영향 |

모든 응답에서 관계 원문과 Node 조건이 남았다. 응답의 `expected_effect`는 `unknown_until_measured`, `information_value`와 `evidence_quality`는 `unassessed`였다. 이 검증은 효과나 정보가치를 예측·평가하지 않았다.

원시 응답별 실제 UUID, 조건 및 applicability JSON은 [selection-direction-review-2da5482-raw.json](selection-direction-review-2da5482-raw.json)에 보존했다. 파일에는 DSN/credential이 없다.

## 실행 명령과 출력

```text
$ SELECTION_DIRECTION_RAW=docs/research-lab/evidence/selection-direction-review-2da5482-raw.json PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 180 -- .venv/bin/python -m pytest tests/test_selection_direction_independent_temp.py unit_tests/test_research_rules.py -q -s --tb=short
DB_IDENTITY=["ttakkari_test", "ttk_test"]
....................
db reset: 1 selective (avg 3.0/115 tables), 0 forced full, 0 fell back to full
20 passed in 0.92s
```

보고된 20 passed는 위 지정 API probe와 규칙 단위 모듈만 포함한다. 기존 verifier의 FAIL 원문과 이번 요청 8건은 JSON에 남아 있다. 전체 suite 누적 PASS로 간주하지 않는다.

## 미실행·미검증

- 전체 suite 및 기존 보존증거 재실행
- 이전 UI/browser, Metal, TTS, host GPU, 배포 및 다른 제품 checkout 변경
- 실패 Run 자동 수집, 근거 질 평가, 효과·정보가치 calibration, registry 추천, UI 구현
- PMS 7.2 전체 완료 및 운영 성과

위 범위는 사용자 지시에 따라 재실행하지 않았다. 제품 수정·commit·push·merge·배포는 하지 않았다.
