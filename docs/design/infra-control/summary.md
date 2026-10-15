# 인프라 관제 시안 · 최종 요약 (2026-10-15, 조건부)

- 열기: `prototype.html#s=partial` (해시 s=normal|problem|loading|denied|failed|partial|empty|stale, t=light|dark, d=항목, chrome=0). 캡처는 `gallery.html`.
- 반영한 조사 사실(관측 시각 21:59~22:00 KST 표본, 실시간 아님): 수집 대상 up·Loki ready·Grafana 200·Dozzle 307은 **응답**일 뿐 서비스 정상이 아니다. Docker 읽기 거부는 **장애가 아닌 미확인(권한 없음)**. 빈 조회(series 0)는 정상도 0도 아니다. win-server는 연결하지 않아 미확인.
- 표기: 정상/주의/문제/미확인 외에 전체 상태 전용 「일부 미확인」(중립색, 장애·기준 초과 아님). 미확인은 사유 칩(권한 없음·확인 원천 없음·연결 안 함·값 오래됨·갱신 실패)으로 구분.
- 가정으로 남김: 3회/5분/30분 임계값, 서비스 응답 확인 원천, 정상·문제 예시 수치와 시각, Grafana/Dozzle 이동. 장애 예시는 실제 서비스가 아닌 sample-api.
- 검증: `captures=38 issues=0 consoleErrors=0 reflow320Overflow=0` (Chrome 155 headless).
- 정식 확정 불가: I2.2·I2.3 미완료(I1.1~I1.3 부분, I2.1~I2.3 todo), Docker 읽기 권한·서비스 health 원천·임계값·host ID 미결정, 5개 질문 사용자 원문 미확인. I3.1·I3.2는 doing 유지.
