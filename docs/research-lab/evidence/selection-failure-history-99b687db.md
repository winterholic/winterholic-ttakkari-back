# 실패 이력을 다음 방법 선택에 연결

기준 revision: `99b687db52d04244e1fa0b096bdff63f08c207c6`.
2026-10-26 리드 Codex의 표준 명령 환경에서 직접 구현·검증했다.
별도 독립 검증자의 인수나 운영/GPU/UI 검증 결과가 아니다.

## 문제와 변경

기존 `/studies/{id}/select`는 같은 scope의 Method Node와 Relation만 읽었다.
실행 실패가 Experiment/Run에 보존돼 있어도 후속 연구 선택에는 나타나지 않았다.
실제 legacy CPU 평가의 잘못된 qrels와 v2 retrieval 실행의 text 누락 KeyError를
만들어 저장한 뒤 선택해도 둘 다 candidate로 반환되는 2개 RED를 확인했다.

선택 문맥에서 같은 scope의 실패 Experiment/Run을 직접 조회한다. 새 collector,
자동 해석 Node, schema, daemon은 추가하지 않는다. legacy method_ids와 v2
Registry의 source_node_id→주/비교 Method 연결을 지원한다. 원본 상세 경로,
Experiment/Run/Configuration ID, 오류, 선언 환경과 관측 환경을 따로 유지하며
원본 raw는 해당 상세 API에서 조회한다. 같은 Run은 중복하지 않는다.

현재 조건에 적용되거나 조건을 모르면 needs_review, 명확히 다른 조건이면
기록을 남긴 채 candidate를 허용한다. 선언·관측 환경이 충돌하면 unknown이다.
실패의 원인·방법의 무효성은 추론하지 않는다(causal_status=unassessed).
기존 효과/근거질/정보가치 미측정 표시는 유지한다. selection_recorded event와
fresh resume에 이 문맥이 보존된다. Registry 연결·legacy 실패·Run 조회는 각각
최대 200행이며 상한 초과는 truncated/needs_review다. 관계 방향·조건 규칙은 유지한다.

## 실행 증거

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_selection_failures.py -q --timeout=30 --tb=short
2 failed in 0.80s: v1/v2 both candidate != needs_review (실제 CPU 실패 후)
```

초기 교정 뒤 관계/규칙 포함 25PASS1.11s. 추가 환경 충돌 반례는 candidate
오판정을 검출했다. 같은 단계의 scope fixture는 summary 누락으로 먼저 실패했고
fixture 필수값을 보완했다(제품 실패로 합산하지 않음). 이후 27PASS1.15s.
Registry lineage 상한 반례 2FAIL0.64s→조회 상한/불완전성 표시를 추가했다.

최종 명령과 출력:

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_selection_failures.py tests/test_research_selection_context.py tests/test_research_lab.py unit_tests/test_research_rules.py -q --timeout=30 --tb=short
41 passed in 1.70s
db reset: 22 selective (avg 4.5/115 tables), 0 forced full, 0 fell back to full
$ .venv/bin/ruff check src/ttakkari/research/rules.py tests/test_research_selection_failures.py
All checks passed!
```

service.py의 기존 unused import 3개는 HEAD와 현재 JSON 진단의 code/message를
대조해 증가 0을 확인했다. `git diff --check` exit0.
테스트는 실제 ASGI와 주입 Run PG에서 supplied DB/user identity를 대조하고
운영 DB 이름을 거부한다. GPU/model 추론은 실행하지 않았다.

v1/v2 실패→동일 A/다른 B/미입력 조건의 실제 선택 6건과 fresh resume는
[raw JSON](selection-failure-history-99b687db.json)에 있다. 이 응답은 27PASS
단계 캡처이며 최종 41PASS의 Registry 상한/lineage 검사는 추가 실제 DB
합성행 검사다. JSON은 최종 source SHA를 별도 필드로 구분한다.
합성 lineage/범위/상한 행을 실제 모델 실행으로 표현하지 않는다.

## 남은 범위와 배포

PMS7.2 전체 완료가 아니다. Node 연결이 없는 Registry 항목의 추천, 근거질·
정보가치 calibration, 전용 추천 UI는 남는다. 이번 변경은 기존 Node 선택 경로와
명시된 Registry lineage만 연결한다. 기존 25b77 방향·조건 독립 검증은 과거
소스 범위로 유지하며 새 서비스/규칙 변경의 독립 인수로 확대하지 않는다.

이번 Run 1회 조회한 d74df05f는 99b687db/requested/skip_tests=false/error=null이었다.
배포 활성, frontend734 main/Vercel, host Metal/RuntimeLease/raw, 운영 MCP 및
PMS6·7·8 원래 완료 조건은 미검증/미완료다. 새 검증 변경은 일반 누적 push 후
별도 full SHA 배포 예약으로 전달하며 controller의 모든 gate를 유지한다.
