# 2026-10-15 · 인프라 관제 I3.1/I3.2 조건부 디자인 시안

- 요청: Claude 한도 때문에 못 한 디자인 작업을 WBS에서 찾아 재개. 사용량 확인은 Claude 개인 계정 5시간 0%, 주간 7%, 상한 95%, fresh. 대상은 PMS 12.3.1(I3.1)·12.3.2(I3.2)이고 프로젝트는 e99a75de.
- 선행 판단: PMS 12.1.x·12.2.x 7개는 전부 todo 0%이고 코드·work-history·docs에 I1/I2 산출물이 없다. 계획서 CSV/JSON/HTML만 있다. 따라서 WBS가 낡은 것이 아니라 **선행이 실제로 미완료**다. 선행을 완료로 바꾸거나 건너뛰지 않았다.
- 한 일: 선행이 없어도 가능한 범위로 **조건부 검토용 시안**만 만들었다. 제품 코드·공통 셸(front)은 수정하지 않았다.
  - `docs/design/infra-control/prototype.html`: 정적 단일 파일, 디자인 시스템 토큰·컴포넌트만 사용. 정상/문제/로딩/권한 없음/갱신 실패/부분 성공/빈 값/오래된 값 8상태, 모바일 상세, 라이트/다크.
  - `docs/design/infra-control/spec.md`: 5개 질문 제안, 동선, 제외 항목, 상태 어휘, 상태별 명세, 접근성 검토, 확인/가정/미확인 표, 남은 게이트.
  - `docs/design/infra-control/scripts/verify.mjs`: 설치된 Chrome을 CDP로 제어하는 검증(도구 설치 없음). `build-outbox.py`는 사용자용 사본 생성.
- 검증(2026-10-15): `node scripts/verify.mjs prototype.html captures` → `captures=36 issues=0 consoleErrors=0 reflow320Overflow=0`. 글자 1670개 대비 AA 미달 0, 터치 영역 미달 0, 키보드 Tab→Enter→Esc 포커스 복귀 통과. 결과는 `captures/verify-report.json`. PNG는 `.gitignore`로 제외하고 outbox `infra-design/`에 둔다.
- 자동 검사로 잡힌 것: 갱신 실패 배너 버튼 24px, 비활성 버튼 대비. 눈으로 잡은 것: 히어로 숫자 불일치, 같은 Loki의 상태 엇갈림, 정상 화면이 길어짐, 갱신 실패 시각 중복, 로딩 스켈레톤이 배경에 묻힘, 권한 없음·로딩에서 호스트 이름 노출.
- **확정하지 않은 것**: 호스트·서비스 위치·임계값·링크 주소는 모두 예시이고 화면에 `가정` 표시를 달았다. 계획서 원문의 "5개 핵심 질문"은 읽기 가드로 확인하지 못해 I3.1·I3.3 문구에서 도출했다(확인 필요). Docker 읽기 권한은 미확인으로 유지했다.
- PMS: 12.3.1·12.3.2에 원문을 보존한 채 Claude 회복·수행 범위·잔여 선행을 덧붙이고 status만 doing으로 바꿨다(진행률 0%). 조건부 시안은 I3 done으로 세지 않는다. I3.3과 I1/I2는 건드리지 않았다.
- 남은 blocker: I1.1~I1.4 → I2.1 → I2.2·I2.3 → I3.1 완료 기준 → I3.2 완료 기준. 실기기(iPhone Safari·VoiceOver) 검증과 사용성 측정은 미실행.
- 원격 인프라 변경, 도구 설치, 추가 작업자 생성은 하지 않았다.
