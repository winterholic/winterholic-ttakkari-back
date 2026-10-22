# KB-E0 완료 기록

- **상태: KB-E0 로직 완료 승인, 제품검색 미승인.** (2026-10-22)
- 승인 근거: [독립 확인 4회차](kb-e0-independent-review-v4.md)가 main `1e9bb1ad4ad5dcecefbb1e1b7555dcba4d688711`의 E0 로직을 passed로 판정했다. 승인 범위는 DB·UI·추론을 제외한 E0 로직이다.
- 4회차의 정정 요청 두 가지(동등 변이 오표기, CI 폭 하한의 n 의존)는 승인 직후 반영했다. 내용은 아래 "승인 뒤 정정"에 있다.

## 판정 이력

| 회차 | 대상 SHA | 판정 | 원문 |
|---|---|---|---|
| 1 | `416cf22` | 부분 통과, 독립 승인 보류(기능 결함 4건) | [kb-e0-independent-review.md](kb-e0-independent-review.md) |
| 2 | `2e62817` | failed(N1 퇴화 CI, N2 테스트 약화). 결함 1~4는 막힘 확인 | 검증 보고서는 저장소 밖. 대응은 `reports/kb-e0/review-fixes-2/REPORT.md` |
| 3 | `5b788a3` | passed(DB·UI·추론 제외 E0 로직) | [kb-e0-independent-review-v3.md](kb-e0-independent-review-v3.md) |
| 4 | `1e9bb1a` | passed(E0 로직 완료 승인) | [kb-e0-independent-review-v4.md](kb-e0-independent-review-v4.md) |

검토 원문은 검증 clone의 파일을 그대로 복사했다. sha256은 3회차 `ab745c0c…6b20`, 4회차 `7ff8e9fb…e7a3`이다.

## 완료된 범위 (E0 로직)

- 평가 schema, runner, CLI, 고정 manifest
- source revision·quote·hash·time anchor와 invalid/ambiguous 보고
- split·group·중복·번역 누수 검사. positive gold 소유 문서도 연결 graph에 들어간다.
- Recall, nDCG, MRR, abstain의 두 오류 분모
- 문서 연결 군집 bootstrap, Holm, power 부족 inconclusive
- AB/BA 지연 판정. 절대 SLO와 p95 ratio CI를 분리한다.
- 정보량 기준 퇴화 판정
- 자원 수집 인터페이스. 지원하지 않으면 unverified다.
- cache·replay 무결성과 중단·재개
- 권리 fail-closed(fixture가 아닌 입력은 hold)
- reviewer audit 위조 차단
- 판정 이유·조건·가드 mutation 점검 도구 `kb-eval/mutation/`

## 미실행 잔여 (E0 완료 범위 밖)

- 실제 DB export adapter와 DB manifest·관측 fixture 검증
  - DB 기반 KB·knowledge·collector 회귀와 main 계약 묶음은 통과했다.
  - 이것은 통합이 기존 회귀를 깨지 않았다는 증거일 뿐 adapter 검증이 아니다.
- 모바일 확인 UI(320/390px)와 브라우저 검증. 현재는 unmounted API 골격이다.
- 실추론, 실서버 지연·자원·GPU 계측
- 인증된 reviewer·권리 검토 flow. 현재는 거부 경로만 있다.
- 낮음 위험: 로컬 artifact에 서명이 없다. fixture가 아닌 입력을 모두 hold하므로 E1a 시작에는 권리 검토 flow가 먼저 필요하다. 둘 다 `kb-eval/README.md`에 적었다.

## 승인 뒤 정정 (4회차 요청)

1. **mutation 오표기:** 동등 변이로 적었던 Q1(`low > -margin`), Q2(Holm 조건), Q5(power trial 하한 200)는 동등하지 않았다. 반례를 회귀 테스트로 옮기고 `expect="kill"`로 바꿨다. Q8(power 손실 CI 상한)은 반례도 충분한 동등성 근거도 없어 `undecided`로 표기했다.
2. **n 의존 퇴화 하한 제거:** ratio CI 폭 하한(5e-4)과 품질 CI 폭 하한(1e-3)은 표본이 늘면 1/√n으로 좁아지는 양에 기댔다. 그래서 같은 trace에 데이터를 더하면 pass가 unverified로 뒤집혔다(지연 CV 0.3%·5,000 block, 품질 SD 0.01·4,000 cluster). 정책 v4는 정보량 기준을 쓴다.
   - 지연은 block별 짝지은 log 비율의 표준편차 하한 1e-4를 쓴다.
   - 품질은 질문 단위 delta 표준편차 하한 1e-3을 쓴다.
   - 폭이 정확히 0이거나 NaN인 구간은 별도 가드로 남겼다.

## 다음 단위

설계 v2 §9.2 순서는 `E0 → E1a/E0.5와 KB3 → KB4 …`다. 다음 단위는 배정 전까지 시작하지 않는다.
