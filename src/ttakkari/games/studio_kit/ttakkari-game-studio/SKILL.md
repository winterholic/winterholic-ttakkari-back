---
name: ttakkari-game-studio
description: 따까리 게임 스튜디오에서 게임(Phaser 등)을 만들거나 사용자 플레이 피드백을 고칠 때, 피드백을 장면·기대·재현 입력·완료 증거로 묶고 실제 도구(원장·도달성·정전·에셋·린트·플레이 재생·완료 게이트)로 검증한다. "1장에서 이동이 안 돼서 진행을 못 하겠어", "대화창 닫으면 키가 안 먹혀", "적이 화면 밖인데 오른쪽으로 못 가", "클리어했는데 끝났는지 모르겠어", "게임 안 UI가 웹 같아서 어색해", "타격감이 없고 재미없어", "세계관이 왜곡됐어", "게임 피드백 반영해줘"에 쓴다. 테스트 통과만으로 완료를 말하지 않게 한다. 게임과 무관한 웹앱 UI 수정, 일반 단위 테스트 작성, 게임 아이디어 브레인스토밍만 할 때는 쓰지 않는다. 다이어그램은 ttakkari-diagram, 일반 브라우저 검증은 webapp-testing.
---

# 따까리 게임 스튜디오 제작 계약

게임 피드백이 반복해서 되돌아오는 이유는 코드 실력이 아니라 **제작 흐름**이다. 같은 실패가 장마다 다시 나왔다: 이동 불가(스폰이 소품 안), 대화창 뒤 입력 유실, 화면 밖 적 softlock, 클리어 표시 없음, 게임 안 웹 UI 잔해, 세계관 이탈, 걷기 애니메이션 40fps. 원인은 모두 같다: 피드백이 작업 단위로 쪼개지지 않았고, '테스트 17개 통과'가 완료 신호였고, 사람이 눈으로 본 증거가 없었다.

## 도구 위치

이 폴더가 키트다. 게임 저장소에서는 `.agents/skills/ttakkari-game-studio/`(또는 `.claude/skills/...`) 에 플랫폼이 복사해 둔다. 아래에서 `STUDIO` 는 `node <이 폴더>/bin/studio.mjs` 를 뜻한다. 게임 저장소 루트에서 실행하고, 의존성은 없다(node 20+). 이 폴더 파일은 플랫폼이 관리하니 고치지 않는다.

```bash
STUDIO="node .agents/skills/ttakkari-game-studio/bin/studio.mjs"
$STUDIO help
```

## 절차 (순서대로)

1. **피드백을 원장에 넣는다.** 사용자 원문을 그대로 `ledger add --quote` 또는 `ledger import <feedback.md>`. 한 줄에 요구가 여럿이면 쪼갠다. 범주(`bug|logic|balance|art|ux|narrative|feel|audio`)는 키워드 추정이므로 맞는지 확인한다. 범주가 곧 필요한 증거를 정한다 → `references/production-contract.md`
2. **정전과 아트 기준을 먼저 고정한다.** `init` 으로 `docs/studio/` 를 만들고 사용자가 못 박은 세계관·금지 사항을 `canon.json`, 화풍·규격을 `art-bible.md`·`asset-rules.json` 에 옮긴다. 사용자 원문을 `why` 에 인용한다. 정전이 없으면 없다고 보고하고 지어내지 않는다.
3. **버그는 먼저 재현한다(red).** 재현 입력을 `play` 스크립트로 쓰고 고치기 전에 `play --attach ID --phase before` 로 실패를 기록한다. 실패하지 않으면 스크립트가 틀렸다. 스크립트 형식 → `references/play-and-level-spec.md`
4. **고친다.** 변경은 피드백 범위로 한정한다. 새 장면은 한 장면을 끝까지 만들어 사용자에게 보인 뒤 확장한다(`docs/studio/vertical-slice.md`).
5. **같은 재현을 다시 돌린다(green).** `play --attach ID --phase after`, 이동·문·상호작용은 `reach --spec level.json --attach ID` 도 돌린다. `reach` 단독 통과는 실플레이 증거가 아니므로 gate 의 실제 재생 확인을 따른다. 화면 변경은 실제 렌더 화면을 캡처해 `ledger evidence --type screenshot`. 기계 테스트는 수기 로그를 등록하지 말고 `ledger test ID --program node --input tests/foo.mjs --args-json '["--test","tests/foo.mjs"]'` 처럼 선언한 입력을 묶어 직접 실행한다. 에셋은 `assets --sheet` 로 contact sheet 를 만들고 브라우저로 열어 눈으로 본다.
6. **`ledger status ID fixed` → `gate`.** 판정이 `COMPLETE` 가 아니면 완료·해결이라고 쓰지 않는다. `NEEDS_HUMAN` 이면 "기계 검증 통과, 재미·시각·서사는 사용자 확인 대기"라고 쓰고 캡처 경로를 준다.

## 하지 않는 것

| 하지 않는다 | 이유 |
|---|---|
| 테스트 통과·빌드 성공을 '해결'로 보고 | 17개 통과한 뒤에도 1장은 못 움직였다 |
| 도구가 못 돌았는데 통과로 기록 | `play` 가 exit 3(unsupported)이면 `ledger evidence --type not-run --reason` 으로 남기고 미검증으로 보고 |
| 사용자 확인을 대신 기록 | `user-confirmed` 는 사용자 발화 원문과 출처가 있을 때만. 작업자가 쓰면 위조다 |
| 단색·체커 이미지를 완성 아트로 납품 | `assets` 가 BLANK 로 막는다. 필요한 에셋만 실제 생성한다 |
| 게임 UI 를 웹 컴포넌트로 | 게임 안 UI 는 엔진 안에서 그린다(`feedback: React 잔해`) |
| CeoPark 같은 개별 게임 내용을 스튜디오 능력이라 부름 | `reach` 의 명세·`canon` 규칙은 게임마다 새로 쓴다 |

로컬 CLI 는 사용자 발화 출처를 인증할 수 없다. `user-confirmed` quote/source 문자열은 보존해도 gate 에서 미검증으로 남기며 주관 범주의 `COMPLETE` 근거가 아니다. 인증된 앱 연결 전에는 사용자 확인 대기라고 보고한다.

## 거부·불가 시

- 브라우저를 못 띄운다 → `play` 가 `status: unsupported` 와 이유를 낸다. 도구는 같은 실행 환경에서 macOS IPC 사전 검사를 하고 blocked·unknown·검사 실패면 실행하지 않는다. 실행 오류 후 재시도하지 않고 not-run 으로 기록한다. 승인된 브라우저 도구를 사용할 수 있으면 그 경로로 확인한다. 셸로 다른 Chrome 을 띄우는 우회는 하지 않는다.
- 로컬 서버를 브라우저가 못 연다(loopback 차단) → `--url file://.../index.html` 로 열 수 있는 정적 산출물인지 확인하고, 안 되면 not-run.
- 빌드는 플랫폼의 격리 러너가 한다. 호스트에서 npm install·빌드를 실행하지 않는다.

## 보고 형식

원장 항목별로 `ID · 범주 · 상태 · 증거 파일`, 그다음 `gate` 의 [막는 문제]·[검증되지 않은 것] 을 그대로 옮긴다. 사용자는 폰으로 읽으니 한 항목 한 줄이다.

## Gotchas

- 2026-10-30 최초 작성. ceopark 의 `tools/check_mansion_reach.mjs` 는 저장소에 없는 `lib.mjs` 를 import 해서 깨끗한 체크아웃에서 실행되지 않았다. 도구는 저장소 안에서 자체 완결이어야 한다.
- 정전 검사는 사용자 피드백 문서 자체가 금지어를 인용하면 걸린다(ceopark `docs/FEEDBACK_R2.md`). 그 문서는 canon `include` 에서 빼거나 `allowIfLineMatches` 로 예외 처리한다.
- 범주 추정은 틀릴 수 있다. 틀린 범주로 닫으면 증거 요건이 낮아진다.
