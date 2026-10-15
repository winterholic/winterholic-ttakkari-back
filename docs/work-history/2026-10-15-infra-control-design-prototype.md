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

## 후속 사실조사 (2026-10-15)

- 저장소 문서의 5개 질문은 원문이 아니라 `spec.md §2.1`에서 I3.1/I3.3을 바탕으로 도출한 제안임을 확인했다. 원본 계획 CSV/HTML과 10/14 work-history, 지정 세션에서도 사용자 질문 문장 목록은 찾지 못했다.
- 알려진 `hs-llm-read` SSH alias로 Ubuntu에 읽기 접속해 running systemd unit 37개, Prometheus health/targets, Loki ready/config/labels를 확인했다. Prometheus targets 3개는 모두 `up`; Prometheus API rules는 0개였다. Docker API는 permission denied로 읽을 수 없어 우회하지 않았다.
- runtime Prometheus retention은 10y/100GiB, 로컬 compose는 30d/8GB. Loki runtime은 90d, 로컬 설정 파일은 14d, README는 90d다. 설정 불일치를 `docs/design/infra-control/prerequisites.md`에 기록했다.
- `/resources` UI/API/DTO, agent API, `server_resources` MCP와 오류/마지막 값 표시를 코드 근거로 매핑했다. 현재 Mac snapshot 전용이므로 I1.4만 정식 완료. I1.1~I1.3은 부분 조사, I2.1~I2.3은 미완 선행 때문에 todo 유지.
- 사용자 디자인/제품 코드, 인프라 설정, 서비스, 설치, 배포는 수정하지 않았다. 기존 미추적 `docs/creative-workspace/`는 보존했다.
- 조사 상세 및 Claude 후속 반영 항목(위 단락은 Luna 기록): `docs/design/infra-control/prerequisites.md` 및 outbox `infra-prerequisites/2026-10-15-infra-prerequisites.md`.

## Claude 후속 반영 (2026-10-15 22시 이후)

- 반영 근거: `prerequisites.md`(repo). 지정 outbox 사본(a7707ff7…)은 읽기 가드로 열지 못했고 repo 문서가 같은 내용이라고 명시돼 있어 repo 문서만 근거로 썼다.
- 시안 변경(`prototype.html`): 「부분 성공」을 **「부분 확인」**으로 바꿔 조사 표본(21:59~22:00 KST, 실시간 아님)과 일치시켰다. Docker 읽기 거부, 서비스 응답 원천 없음(수집 up·ready·200·307·빈 조회는 응답이지 정상이 아님), win-server 연결 안 함을 항목별 **사유 칩**으로 나눴다. 전체 상태에 중립색 **「일부 미확인」**을 추가해 장애·주의와 구분했다. 수집원 행 배지는 정상 대신 **응답**으로 바꿨다. 장애 예시는 실제 서비스 대신 `sample-api`로 바꿨고, 조사 근거가 없는 win-server는 정상 예시에서 뺐다.
- 명세 변경(`spec.md`): §1 확인/가정/미확인을 출처·관측 시각으로 재분리, §3 상태 규칙(우선순위 문제>주의>일부 미확인>정상, 미확인 사유 구분), §4 부분 확인, §6 남은 권한·확정 불가.
- 검증: `node scripts/verify.mjs prototype.html captures` → `captures=38 issues=0 consoleErrors=0 reflow320Overflow=0`(글자 1772개 AA 미달 0). 전 상태를 다시 캡처했고 부분 확인·문제 화면은 눈으로 확인했다.
- I3.1·I3.2는 doing 유지, done으로 올리지 않는다. 새 인프라 접속·추가 조사·운영 구현은 하지 않았다.
