# ADR 0059: 게임 오브젝트 픽킹 bridge — 화면 좌표로 오브젝트 관측 정보 질의 (Phase 3, TASK-G27)

- **상태**: 게임 쪽 bridge·Phaser 구현과 실제 브라우저 검증 완료(2026-10-20). 관제탑 선택 모드 UI(G28)와 피드백·worker 연결(G29)은 아직 없다.
- **관련**: ADR 0049(postMessage 계약), ADR 0052(피드백 계약), 설계서 §5.1 "2) 게임 오브젝트 선택"
- **구현**: 템플릿 `templates/web-phaser/src/bridge.ts`(요청·응답 스키마), `src/pick.ts`(`phaserPickAt`), `src/main.ts`(연결). 검증 `tests/test_game_pick.py`, 픽스처 `tests/game_smoke/smoke_game_pick.ts`, 시나리오 `pick_scenario.mjs`

## 결정

기존 bridge 에 요청 종류 하나를 더한다. 새 채널·새 권한은 없고 sandbox·origin 경계는 그대로다.

- **요청**: 부모→게임 `{type: 'ttakkari:pick', id, x, y}`. `x`, `y` 는 캔버스 왼쪽 위가 (0,0)인 **0~1 비율**이다. 유한한 숫자가 아니거나 범위 밖이면 `acceptParentMessage` 가 거부하고 게임은 응답하지 않는다(다른 요청의 검증 규칙과 같다). 비율을 쓰는 이유는 부모가 iframe 크기와 DPR 을 알 필요가 없어서다.
- **응답**: `{type: 'ttakkari:pick:result', id, supported, point, canvas: {width, height}, scenes, hits[], truncated}`. `hits[]` 는 위에 그려진 것부터이고 최대 10개다. 각 항목은 `scene`, `name`(게임이 `setName()` 으로 붙인 값, 없으면 빈 문자열), `type`(Phaser 오브젝트 종류), `texture`·`frame`(이미지·스프라이트), `text`(텍스트는 최대 60자), `container`(컨테이너 안이면 그 이름), `depth`, `bounds`(캔버스 대비 0~1 비율)다. 응답 전체는 16KB 를 넘으면 빈 결과로 줄인다.
- **연결하지 않은 게임**: `supported: false` 와 사유(`pickAt` 미연결)를 돌려준다. 조용히 멈추지 않는다. `installBridge` 의 `controls.pickAt` 이 엔진별 구현이 꽂히는 자리이고, web-phaser 는 `phaserPickAt(game, x, y)` 를 쓴다.
- **판정 규칙(Phaser)**: 활성 씬마다 보이는 오브젝트(`visible`·`alpha` 가 0 이 아닌 것)의 `getBounds()` 가 그 지점을 덮으면 hit 이다. 카메라 이동·줌을 반영하고, `scrollFactor` 가 0 인 HUD 는 화면 좌표로 본다. 컨테이너는 자식을 함께 훑어 자식을 컨테이너보다 위에 둔다(자식의 depth 는 컨테이너 안에서만 뜻이 있으므로 화면 순서는 컨테이너 깊이를 따른다). 정렬은 나중 씬, 깊이, 나중에 방문한 순서다.
- **담지 않는 것**: `source_hint`(소스 위치)는 만들지 않는다. 설계서 §5.1 대로 이 값은 Phaser 가 알 수 없는 가상 정보이고, 소스 후보 조사는 이 관측 정보를 받은 worker 몫이다. 이름·텍스처 키가 소스의 변수·에셋 이름과 같을수록 찾기 쉬우므로, 새 게임 첫 개발 요청(`develop_instruction`)이 "주요 오브젝트에 setName() 으로 소스 변수와 같은 이름을 붙이라"고 안내한다.
- Text 오브젝트의 `texture` 는 비운다. Phaser 가 글자를 그리려고 만든 내부 캔버스 키(UUID)라 소스와 무관한 잡음이다(실측으로 발견).

## 검증

- 요청 검증은 node 로 순수 함수를 직접 실행한다(비율 범위·NaN·무한대·문자열·누락·다른 요청에 좌표가 섞이지 않음).
- 실제 Phaser 3 게임을 격리 컨테이너에서 빌드(tsc 단계 통과 포함)해 별도 origin 의 sandbox iframe 으로 열고 Chromium 으로 데스크톱(800x600)·모바일(390x844)에서 확인했다. 겹친 사각형(`player` 위, `shadow` 아래), 가장자리(아래 것만), 원(`Arc`), 텍스트(`Text`, 글자 포함), 텍스처 이미지(`crate-tex`), 컨테이너 자식(`heart` 이 `hud` 안, 컨테이너보다 앞), 숨긴 오브젝트와 빈 곳(빈 `hits`), 규약 밖 요청(무응답), pick 미연결 게임(`supported: false`).

## 한계와 미검증

- **관제탑 UI 는 없다.** 사용자가 화면에서 오브젝트를 고르는 모드와 하이라이트·팝오버(G28), 선택 결과를 피드백 payload 와 worker prompt 로 보내는 일(G29)이 남았다. 이 ADR 만으로는 사용자에게 보이는 기능이 아니다.
- **소스 위치 정확도는 측정하지 않았다.** 설계서 완료 기준의 "pick payload 와 소스 매핑 증거"는 G29 에서 실제 worker 로 재야 한다.
- 이미 만들어진 게임(점프 고양이 등)과 이전 템플릿 게임은 `pickAt` 이 없어 미지원으로 답한다. worker 가 새로 쓴 `main.ts` 가 `installBridge` 에 `pickAt` 을 넣지 않으면 마찬가지다.
- Phaser 의 `getBounds()` 는 회전·스케일을 포함한 바운딩 박스라 회전한 오브젝트의 모서리 빈 곳도 hit 이 될 수 있다. 픽셀 단위 투명도 검사는 하지 않는다. 카메라 회전은 화면 경계 계산에서 무시한다.
- 서브카메라·여러 카메라, 물리 바디·타일맵 레이어의 개별 타일 정보는 다루지 않는다. 오브젝트가 많은 게임에서의 성능은 재지 않았다(요청당 1회 순회).
- iOS Safari·실기기는 확인하지 않았다.
