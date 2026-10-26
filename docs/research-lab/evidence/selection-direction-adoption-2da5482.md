# Selection 방향·조건 교정 인수와 누적 배포 준비

2026-10-26 리드 인수 대상은 `2da5482cd0320e7177165964b6949d1cff72efc6`이다.

독립 검증 worker `25b77d19-5243-431f-a58b-a2c9e36e01c2`, Run
`4b2db7e5-3c49-4969-bc16-ff95c674c609`의 실제 routing은
`codex/gpt-6-luna/high`, task type `verification`이었다. 결과의 범위는
[검증 보고서](selection-direction-review-2da5482.md)와
[실제 응답 JSON](selection-direction-review-2da5482-raw.json)에 따른다.

리드는 원래 요구, 제품 변경 3파일, 독립 보고서의 명령·출력과 응답 8건을 직접 대조했다.
보고서의 API/Run PostgreSQL probe와 규칙 검사 결과는 `20 passed in 0.92s`다.
이는 전체 suite나 UI/GPU/운영 검증 결과가 아니다. 기존 f76의 두 반례 FAIL은 유효한
과거 결과로 보존하고, 이번 PASS는 그 교정 범위만 인수한다.

이번 Run에서는 Python으로 JSON의 8개 실제 관찰값과 예상 후보 상태를 대조했다.
outbound challenge 무영향, Node/Relation 각각의 불일치, 한쪽/양쪽 빈 조건,
hardware 누락, 두 대칭 관계의 영향 및 미측정 효과/근거질/정보가치 경계를 확인했다.
JSON의 `source_sha256` 3개가 현재 `rules.py`, `service.py`, API `research.py`와
모두 일치했다. 출력:

```text
receipt PASS: 8 API observations, 3 current source SHA256 matches; observed outputs only, no new runtime execution
review pytest receipt: 20 passed in 0.92s; db reset: 1 selective (avg 3.0/115 tables), 0 forced full, 0 fell back to full
```

`git diff --check`와 `git merge-base --is-ancestor origin/main HEAD`는 exit 0이었다.
이 SHA/응답 대조는 새 API 실행이 아니며 독립 검증의 유효 입력과 현재 소스를 연결한다.
동일 소스의 API/UI/모델 실험은 반복하지 않았다.

기존 controller `831bcd9a-98db-4475-b072-bccef6c70a29`는 후보 `4053ee12`의
`testing`, `skip_tests=false`, `error=null`로 조회됐다. 이 후보에는 선택 교정이 없다.
인수한 증거를 커밋하고 일반 fast-forward로 main에 누적 반영한 뒤 full SHA로
새 배포를 예약한다. 예약 수락은 활성 성공이 아니다. 문서화된 `TTK_SKIP_GATE=1`은
기존 Codex 환경 제한이 있는 로컬 중복 pre-push만 생략하며 controller의 전체
test/readiness/schema/ownership/drain gate는 유지한다.

남은 필수 요구: 새 후보 전체 gate·활성 revision, frontend 7341245 main/Vercel,
A 실제 host Metal/RuntimeLease/raw, 운영 MCP baseline 보존·원문 UX10,
PMS 6/7/8 잔여 조건. PMS 7.2의 실패 Run 자동 수집·근거 평가·calibration·Registry
추천·UI는 이 인수 범위에 없으며 전체 목표는 active다.
