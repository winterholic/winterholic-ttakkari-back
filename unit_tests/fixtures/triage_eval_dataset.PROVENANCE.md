# triage_eval_dataset.json 라벨 출처

- 35건(좋음 13, 애매 8, 나쁨 14)은 규칙 기반 triage를 구현한 같은 작업(commit 2028504)에서 함께 작성됐다.
  원문 URL, 라벨러, 인간 검수 기록이 남아 있지 않다. 따라서 `gold_label`은 인간 정답이 아니다.
- 용도는 규칙 회귀 기대값(리팩터링 후 같은 판정이 나오는지)이며, 선별 정확도의 근거가 아니다.
  같은 규칙을 이 데이터로 평가한 수치(오판 보류율 0%, 재현율 100% 등)는 순환 평가라서 성능 주장에 쓰지 않는다.
- 외부 라벨 평가는 `unit_tests/fixtures/triage_ml/`와 `scripts/eval_triage_ml.py`가 맡는다(FineWeb-C 한국어 사람 주석, Mr. TyDi 한국어).
