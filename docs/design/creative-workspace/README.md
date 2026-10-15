# Creative Tool Workspace · 디자인 재개 산출물 (WBS 15.4 U01~U06)

2026-10-15, Claude Sonnet high. Claude 한도 회복 뒤 보류돼 있던 디자인 과제를 재개한 결과다.

## 상태: 검토용 시안 · 정식 설계 완료 아님

| 구분 | 내용 |
|---|---|
| 있는 것 | 4화면(작업실·결과물·실행 이력·도구)과 모바일·데스크톱 핵심 동선의 **클릭 가능한 시안**, 상태 명세(U01~U06), 검증 기록 |
| 없는 것 | 실제 앱 연결·검증, 백엔드 계약, 제품 코드 반영, 실제 임베드 왕복 |
| 정식 완료를 막는 것 | 선행 과제(A01·A02·D01·D02·D04·D05·B14·A03 중 해당분) 미완. 이번 실행은 선행 상태를 바꾸지 않았다 |

모든 도구 이름·버전·상태·파일·실행 기록은 가상 예시이고, 프로토타입 화면에도 "가상 예시"가 표시된다. 13개 앱은 **후보·미검증**이다.

## 파일

| 파일 | 내용 |
|---|---|
| [spec.md](spec.md) | U01~U06 정보 구조·상태·동선·문구, DS 추가 요청, 데이터 가정(확인 필요), 잔여 선행 |
| [verification.md](verification.md) | 실행 명령·출력 요약, 검사 항목, **미검증 목록** |
| [prototype/index.html](prototype/index.html) | 시안. 서버 없이 `file://`로 열린다(디자인 시스템 CSS는 `prototype/ds/`) |
| `prototype/tools/verify.mjs` | 상태 매트릭스 캡처 + 넘침·접근성·흐름 검사 |
| `prototype/shots/` | 캡처 134장(저장소 제외, 결과함 `creative-design/shots/`) |

## 시안 보는 법

`prototype/index.html`을 브라우저로 연다. 헤더의 설정(⚙) 버튼에서 화면·상태·테마를 고른다. 상태는 주소 해시에 담겨 새로고침·뒤로가기로 복원된다.

- 작업실 `#work?s=compare`(기본) / `new` `notools` `attach` `running` `cancelled` `v1` `approved` `error` `lost` `unsupported`
- 결과물 `#library?item=l1..l8`, 실행 이력 `#history?run=r1..r11`, 도구 `#tools`(실제 지금) / `#tools?s=sample&tool=photo`(상태 예시)

## 기존 구조 재사용

새로 만들지 않은 것: 편집기, Run, File Broker, Artifact, ArtifactViewer, FollowupComposer, 작업 캔버스, 보관함, 디자인 시스템. 이 시안은 이것들 위의 화면·상태만 정한다(spec §1.3). front 제품 코드와 공통 셸은 수정하지 않았다.

## 결정이 필요한 것

1. 결과물 화면을 별도 탭으로 둘지 보관함 안 보기 전환으로 둘지(spec §1.3).
2. 버전·역할을 Artifact 컬럼으로 둘지 별도 테이블로 둘지(spec §4.1).
3. 검증 "오래됨" 기준 일수(spec §2.1).
