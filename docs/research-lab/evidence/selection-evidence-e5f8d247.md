# 선택 근거의 출처 표시와 링크 수 편향 제거

기준: e5f8d24707572eac47532a5a517cb761efcd7f88.
2026-10-26 리드 Codex의 명령 환경에서 직접 구현·검증했다.
독립 검증자, 운영, 브라우저, GPU 검증 결과가 아니다.

## 변경과 범위

동일한 적용 조건·비용·검토 상태에서도 기존 순위는 evidence_count가 큰 방법을
앞세웠다. 인용 개수는 진실성·효과·익숙함의 타당한 점수가 아니므로 이 보너스를
제거했다. 후보/검토 필요/제외 구분을 먼저 적용하고 같은 구분에서는 제목·ID로
정렬한다. 링크 수와 기존 후보는 남긴다. 신기한 방법에 별도 보너스도 주지 않는다.

선택 문맥에 원래 인용과 evidence/revision ID, locator, source_key, 원본 바이트
해시와 인용 구간 해시, 구간 무결성, 현재 리비전 여부를 연결했다. source hash와
quote hash는 서로 다른 대상이다. KB의 기존 관측 시각 기반 리비전 필터를 재사용해
A→B→A 재관측과 늦게 도착한 과거 B도 처리한다. 별도 수집기·스키마는 없다.

선택 이벤트는 당시의 근거 문맥을 보존한다. 이후 출처가 바뀌어도 이전 이벤트를
덮어쓰지 않는다. 현재 리비전이나 해시 일치는 출처의 진실성을 보증하지 않는다.
evidence_quality/information_value는 unassessed, expected_effect는
unknown_until_measured다. 오래된 인용만으로 방법을 자동 무효화하지 않는다.

## 실행 증거

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_selection_evidence.py -q --timeout=30 --tb=short
2 failed in 0.80s
familiar precedes new; selection research_context lacks evidence
```

초기 교정의 current 값은 과거 버전에 NULL을 반환했다. 호출 경로는
selection_evidence→KB current predicate였다. 같은 Run PG에서 직접 실행한
`NULL OR FALSE / NULL OR TRUE / coalesce(NULL OR FALSE,FALSE)` 결과는
`None / True / False`였다(DB ttakkari_test, role ttk_test; 주입 URL identity 대조).
WHERE 용 기존 KB 필터를 SELECT 값으로 사용할 때 생기는 SQL 삼가 논리였으며,
이 SELECT 결과만 coalesce(False)로 바꿨다. KB의 전역 필터는 변경하지 않았다.

시험 fixture 준비 중 private source의 locator 누락은 422였고 보완했다. 이후
기존 Node 수정 시도는 실제 DB의 불변 이력 trigger가 거부했다(1 failed,43 passed).
trigger를 해제하지 않고 정상 HTTP scope 검사와 **저장하지 않은** 잘못된 참조의
helper 입력으로 경계를 분리했다. invalid stored row 실행 성공으로 표현하지 않는다.

```text
$ RESEARCH_EVIDENCE_RECEIPT=docs/research-lab/evidence/selection-evidence-e5f8d247.json PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_selection_evidence.py tests/test_research_selection_failures.py tests/test_research_selection_context.py tests/test_research_source_refresh_acceptance.py tests/test_research_lab.py unit_tests/test_research_rules.py -q --timeout=30 --tb=short
44 passed in 2.12s
db reset: 25 selective (avg 4.8/115 tables), 0 forced full, 0 fell back to full
$ .venv/bin/ruff check src/ttakkari/research/rules.py tests/test_research_selection_evidence.py
All checks passed!
```

service.py의 HEAD/현재 JSON 진단을 code/message로 비교해 기존 unused import 3개와
동일함을 확인했다. git diff --check exit0. 저장된 실제 선택 3건의 current 값
True→False→True, quote hash·별도 source hash·미측정 표시는 Python으로 재대조했고
현재 제품·시험 파일 3개의 SHA256을 JSON에 보존했다. ASGI·주입 PG 인수이며,
텍스트·연구 노트·재관측 ledger는 합성 입력이다. 실제 연구 성과 실측이 아니다.

## 배포와 남은 요구

이번 조회의 기존 afdbdd60 예약은 e5f8d247/requested/error=null, 로그 없음이었다.
추가 polling 없이 독립 잔여 작업을 진행했다. 새 변경은 누적 main push 후 기존
controller에 전달한다. 로컬 사전검사 생략 정책은 이전 evidence와 동일하며,
controller의 전체 test/readiness/schema/ownership/drain gate는 유지한다.

PMS7.2의 근거질·정보가치 **calibration**, source_node 없는 Registry 추천과
전용 UI, 독립 인수는 여전히 미완료다. 다른 PMS7.x 원래 조건도 유지한다.
backend 활성 revision, frontend734 main/Vercel, A Metal/RuntimeLease/raw,
운영 MCP baseline/원문 UX10/R4·7·12~16 인수를 이 결과로 대체하지 않는다.

[실제 선택·이벤트 JSON](selection-evidence-e5f8d247.json)
