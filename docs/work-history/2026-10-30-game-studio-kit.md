# 게임 스튜디오 제작 키트 (2026-10-30)

## 문제 (최근 실제 피드백에서 확인한 것)

근거: ceopark 저장소 `docs/FEEDBACK_R2.md`, `docs/REBUILD_STATUS.md`, 커밋 c7f0cf6·b58e69a·45c9547·1adee93·1ccacb1, `~/work-history/2026-10-29.md` 0900(session 0a371c2a). 사용자 직접 지시와 문서 '원문 요지'는 구분해 읽었다.

| 반복된 실패 | 공통 원인 |
|---|---|
| 이동 불가(스폰이 소품 안·1px), 적 화면 밖 softlock, 대화창 뒤 입력 유실 | 실제로 움직여 본 증거 없이 '테스트 16~17개 통과'로 닫음 |
| 결과 표시 없음, 게임 안 React 잔해, 걷기 40fps | 렌더된 화면을 눈으로 본 증거 없음 |
| 대기업 배경 되살아남, 스토리 왜곡 | 사용자 정전이 기계 검사 없이 문서에만 있음 |
| 같은 검사가 다시 필요 | 좋은 검사(`tools/check_mansion_reach.mjs`)가 저장소에 없는 `lib.mjs` 를 import 해서 깨끗한 체크아웃에서 실행 불가, Mansion 내부 필드에 하드코딩 |

플랫폼 쪽 갭(원본 소스 확인): worker prompt(`games/worker.py compose_prompt`)는 잠금 결정·빌드·일반 규칙만 전달하고 제작 절차·완료 기준이 없다. `PhaserAdapter.run_automated_test` 는 미지원이다. 일반 smoke(`tests/game_smoke/smoke_generic.mjs`)는 테스트 전용이라 worker 에게 전달되지 않고, 부팅·입력 반응·콘솔만 보며 도달성·정전·에셋은 보지 않는다.

## 선택한 증분 번들

새 DB·큐·프레임워크 없이, 의존성 없는 node 도구 한 벌과 스킬 한 개를 기존 worker 요청 경로에 연결했다.

- 정본: `src/ttakkari/games/studio_kit/ttakkari-game-studio/` (SKILL.md, `bin/studio.mjs`, `lib/*`, `references/*`, `templates/*`)
- 도구: 피드백 원장(`ledger`: 장면·원문·기대·재현·증거, 범주별 증거 요건, 증거 sha256 고정), 도달성(`reach`: 레벨 명세 JSON 입력의 일반화), 정전(`canon`), 에셋(`assets`: 규격·중복·빈 이미지·manifest·contact sheet), 린트(`lint`: 실제 피드백 유형 5종), 플레이 재생(`play`: Playwright 재생·스크린샷·콘솔·단언, 환경 미지원이면 exit 3), 완료 게이트(`gate`: FAIL / NEEDS_HUMAN / COMPLETE)
- 전달: `games/studio.py install_kit` 이 요청 시작 때 게임 저장소의 `.agents/skills/`·`.claude/skills/` 로 복사(스탬프 멱등, 낯선 폴더·심볼릭 거부, `.git/info/exclude` 로 커밋 제외)하고, `compose_prompt` 가 `[스튜디오 제작 계약]` 절을 `[사용자 요청]` 앞에 싣는다. 경로는 `start_request`(사용자 API)와 `orchestrator._game_worker_context`(MCP spawn_worker) 두 곳이다. verify 요청은 독립 검증 변형 문구를 받는다.
- 완료 계약: 테스트 통과(`machine-test`)는 bug 범주의 요건이 아니다. 주관 범주(art/ux/narrative/feel/audio/balance)는 사용자 확인(`user-confirmed`: 원문 quote + source) 없이는 COMPLETE 가 나오지 않는다.

## 실행 증거 (이 Run, 샌드박스 안)

```
node --test tests/studio_kit/        → 12 tests, pass 12 (png/원장/게이트/도달성/정전/에셋/린트/플레이 미지원/실제 브라우저 재생/정적 핸들러)
pytest --noconftest tests/test_game_studio_kit.py → 9 passed (설치 멱등·충돌·심볼릭, 공유 설치, prompt 배치, 설치 사본 실행, node 12개 래핑)
ruff check (변경 파일)               → All checks passed!
check-skill.py --self-test / 대상    → PASS / FAIL 0 (description 420자)
```

실제 브라우저 재생: 헤드리스 Chromium 이 `file://` 페이지에서 '오른쪽 이동' 재현을 눌러 버그 페이지는 `expect window.px > 10` 실패(exit 1, repro-before 증거), 고친 페이지는 통과(play-replay), 두 스크린샷은 비어 있지 않음, 게이트가 `verified` 로 판정했다.

실제 게임 읽기 전용 실행(저장소 파일 무변경, canon 은 scratch 파일): ceopark HEAD 1ccacb1 에 대해
- `canon`: 위반 2건 `docs/FEEDBACK_R2.md:25`(사용자 피드백이 '대기업'을 인용한 줄, 거짓 양성), `docs/STAGE5_DESIGN.md:49`("대기업", 설계 문서에 남은 문구 → 확인 필요). 필수 요소 '동업' 1/3 부족. `src/` 에는 금지어 없음.
- `lint`: 오류 0, 경고 1 (`src/game/systems/audio.ts:147` setInterval 3회·clearInterval 1회, 결함 여부는 미확인).
- `assets`: 384개, 오류 0·경고 0.
즉 이미 고쳐진 과거 결함(걷기 fps 등)은 현재 코드에서 재현되지 않았고, 과거 실패를 막는 것은 신규 작업 때 작동한다. 과거 결함에 대한 소급 탐지력은 이번에 측정하지 못했다.

## 미검증·제약 (정확한 경계)

- **API 통합 테스트 미실행**: `tests/test_game_studio_kit_flow.py`(실제 `/api/games/{id}/requests` → Run prompt, 설치된 사본 실행, git 청결)는 이 Run 의 샌드박스에서 Postgres loopback 연결이 `PermissionError: [Errno 1]` 로 막혀 실행하지 못했다. 기존 `tests/test_game_*`(prompt·빌드 스냅샷) 회귀도 같은 이유로 미실행이다. 이 환경만의 제약이며 호스트 전체 장애가 아니다.
- **`--serve`(loopback 정적 서버) 브라우저 경로 미검증**: 샌드박스가 Chromium 의 127.0.0.1 접속(`ERR_ACCESS_DENIED`)과 node 의 loopback 접속(`EPERM`)을 막았다. 핸들러 로직(경로 탈출 403·SPA 폴백)은 소켓 없이 단위 테스트했다. 재생은 `file://` 로만 실행했다.
- **실제 worker 가 키트를 열고 도구를 호출하는지 미검증**: prompt 에 경로와 명령을 직접 적어 엔진별 스킬 자동 발견에 의존하지 않게 했지만, 실제 엔진(Claude/Codex/Antigravity) 세션에서 SKILL.md 를 열고 `studio` 를 호출했는지는 배포 뒤 새 Run 으로 확인해야 한다. 엔진 라우팅 영수증도 확인하지 못했다.
- **push 미완**: 토픽 브랜치 `feat/game-studio-kit-20261030` 은 로컬 커밋만 있다. 이 Run 의 환경에는 git 자격증명이 없어 `git push` 가 비밀번호를 물었다(샌드박스·우회 모두). 자격증명을 찾으려 하지 않았다.
- **로컬 공유 설치 미완**: `~/.claude/skills/ttakkari-game-studio` 설치와 `skill-list.md` 등록은 쉘이 `~/.claude` 쓰기를 거부(`cp: Operation not permitted`)해 하지 않았다. 따라서 로컬 Claude Code·Codex 세션의 자동 발견은 아직 없고, 등록되지 않은 스킬을 등록됐다고 말하지 않는다. 설치 명령(호스트에서): `PYTHONPATH=src python -m ttakkari.games.studio install-skill --dest ~/.claude/skills` 후 `skill-list.md` 의 '상시 로드' 개수(현재 37, 실제 디렉터리 38 + 1)와 행 추가, `~/.agents/skills` 심볼릭 연결.
- **fresh 독립 검증 미실행**: 아래 발화 목록으로 root 가 fresh 검증자를 연결한다. 이 문서 작성자가 자기 스킬을 시험한 결과는 독립 검증이 아니다.
- 재미·시각·서사 품질은 이 도구가 보증하지 않는다. 비용·시간 우위는 측정하지 않았다(미측정).
- 이동·애니메이션·스토리 개선을 ceopark 에 직접 하지 않았다(개별 게임 재창작은 범위 밖, 사용자 정전·잠금·최근 Claude Code 수정 보존).

## fresh 검증자용 발화 (현재 피드백의 실제 발화에서 추출)

발동(양성):
1. "1장에서 영역 문제로 이동이 안 돼서 진행을 못 하겠어. 발 움직임도 이상해" (FEEDBACK_R2 1장)
2. "적이 화면에 안 나오는데 오른쪽으로 못 가는 진행불가 버그야" (2장)
3. "클리어했는데 끝났는지 알 수 없고 점수도 안 나와" (3장)
4. "게임 화면 안 UI에 React 컴포넌트 잔해가 어색하게 남아 있어" (공통)
5. "6장 시나리오 디테일이 부족하고 대기업 배경이 어색해. 정전 세계관 지켜줘" (6장·정전)

비발동(음성): "로그인 페이지 버튼 색 바꿔줘"(일반 웹앱 UI), "이 함수에 단위 테스트 추가해줘", "따까리 배포 과정을 그림으로", "새 게임 아이디어 브레인스토밍만 해줘".

관찰 항목: SKILL.md 를 열었는지, `ledger add --quote` 로 원문을 넣었는지, 고치기 전 재현(`play --phase before`)을 시도했는지, `gate` 판정을 최종 보고에 그대로 옮겼는지, 판정이 NEEDS_HUMAN 일 때 '완성'이라 쓰지 않았는지.

## 증거 gate 최소 교정 (Codex, 2026-10-30)

입력 근거: 후속 독립 리뷰(df1/e2c, Codex Luna, 44% FAIL)가 ①`tool`·`ok` 두 필드만 있는 수기 JSON ②`../` 외부 참조 ③임의 quote/source `user-confirmed` ④reach 명세 결과를 bug COMPLETE로 승격하는 경계를 지적했다. 이 단계에서는 위 네 경계를 로컬 CLI와 원장·gate에서 교정했다.

- `resolveInRoot` 를 공용 경계 검사로 두고 CLI 입력·출력, 원장·증거 참조, canon·asset manifest·lint 경로에서 root 밖과 symlink 경로를 거부한다. gate 는 원장 파일 경로를 다시 확인하고 증거 참조를 realpath 기준으로 다시 검사한다.
- `play-replay`·`repro-before`·`reach-check`·`canon-check` 는 임의 `--ref` JSON을 실행 결과로 받지 않는다. 해당 CLI가 직접 만든 결과를 ledger에 붙이고, 선언 입력 파일 해시를 gate 에서 재검사한다. `machine-test` 는 새 `ledger test ID --program ... --input ...` 경로에서 직접 명령을 실행하고, 명시한 입력이 바뀌면 기존 결과도 stale 로 처리한다. JSON/로그 수기 등록은 실행 증거가 아니다.
- 버그 항목의 `reach-check` 단독 통과는 실제 게임 충돌체·플레이를 검증하지 않으므로 `NEEDS_HUMAN` 과 play 재생 대기로 남긴다. 이미지 파일 통과는 실플레이 증거가 아니다.
- 로컬 CLI에는 인증된 앱 사용자 출처 검증이 없다. quote/source 문자열은 기록될 수 있지만 gate는 이를 user-confirmed 로 인정하지 않는다. 따라서 주관 범주의 COMPLETE 는 인증 연결 전까지 도달하지 않고 미검증 사용자 확인 대기로 남는다. 별도 DB·승인 흐름·서명 키는 만들지 않았다.
- 스킬 지침, production-contract, `studio_section` worker prompt 를 새 CLI 계약과 맞췄다. 기존 범주·명령은 두고 machine-test 와 producer 결과 등록은 직접 실행 경로를 사용한다.

검증(현재 Run에서 실행):

```
node --test --test-name-pattern='^(png:|ledger:|gate:|evidence:|paths:|reach:|canon:|assets:|lint:|serve:)' tests/studio_kit/
→ tests 14, pass 14, fail 0, skipped 0. positive name selector라 `play:` 테스트는 선택하지 않았다.

PYTHONPATH=src pytest --noconftest -q tests/test_game_studio_kit.py -k 'not test_node_studio_tools_pass'
→ 8 passed, 1 deselected

python .../check-skill.py --self-test && python .../check-skill.py src/ttakkari/games/studio_kit/ttakkari-game-studio
→ self-test PASS; 키트 검사 FAIL 0
```

합성 tmp CLI negative/positive: outside·symlink ref 등록 거부, 등록된 screenshot 경로를 outside symlink 로 바꾼 뒤 gate FAIL, 가짜 `tool/ok` 와 세션 문자열은 COMPLETE 불가, 실제 `reach --attach` 입력 변경 뒤 gate FAIL, 직접 실행 `ledger test` 선언 입력은 pass 후 입력 변경 시 stale FAIL. `reach` 의 before FAIL → after PASS 는 gate 에서 NEEDS_HUMAN 으로 유지된다. 이는 tool/ledger 상태의 로컬 합성 검증이며 게임 worker·실게임 품질 독립 검증이 아니다.

미실행·미완료: 첫 시도에서 사용한 negative name selector는 Chromium 케이스를 제외하지 못해 launch 시도가 발생했다. 현재 로컬 nested worker sandbox에서 관측된 실패는 `Executable doesn't exist at ... chromium_headless_shell ... | Target page, context or browser has been closed`; 알려진 이전 IPC 사전점검 denied 이후 브라우저 재시도는 하지 않았다. 이후에는 positive selector로 브라우저 테스트를 제외해 14개를 실행했다. 첫 실패는 해당 test command 경로에 한정된 관측이며 host 전체 상태가 아니다. Postgres loopback API flow, 실제 게임/API·DB/worker 호출, CeoPark 재수정, UI 렌더 검증, 독립 검증자 실행은 하지 않았다. 기계 gate에 사용자 인증 출처 연결도 없다. 로컬 공유 스킬 등록·게시, branch push·배포 예약, fcbsole integration/publish owner 작업은 이 stage에서 하지 않았다. 기존 OpenCodec 68 완료분 및 CeoPark 원본은 변경하지 않았다.

## 다음 owner

1. root/INFRA(fcb55b7d): 토픽 브랜치를 push 하고 누적 배포에 연결(직접 main push·중복 예약 없음). 배포 뒤 DB 있는 환경에서 `tests/test_game_studio_kit_flow.py` 와 기존 `tests/test_game_*` 를 실행.
2. root: 로컬 공유 설치·skill-list 등록(위 명령), fresh 검증자 연결.
3. 배포 뒤 실제 게임 worker 새 Run 한 번으로 SKILL.md 열기·`studio` 호출·`gate` 보고를 확인.
4. 후속(미구현): 게임별 `window.__studio.level()` 명세 추출기(ceopark 의 Mansion 용 어댑터), `.agents/.claude` 를 격리 빌드 복사에서 제외할지 결정(현재는 복사됨, 무해하나 미검증).

## 반복 옵션 파서 최소 후속 수정

- 재개 당시 HEAD `376d2ae75e241493f8091ffef90cdc2dbd7133cf`, 부모 `481292cb622d58cba0549878c984cbc1839a5716`, 작업 트리 clean 확인. 사용자 지시로 fetch·push는 하지 않았다.
- root cause의 `parseArgs` 중복 덮어쓰기를 재현하는 회귀를 먼저 작성했다. RED: `node --test --test-name-pattern='^(assets: 반복 manifest|ledger: 반복 input)' tests/studio_kit/studio_kit.test.mjs` → 2 fail. assets는 첫 manifest 누락을 놓쳤고 ledger receipt에는 마지막 `c.txt`만 기록됐다.
- `parseArgs`에 호출부가 선언한 repeatable 키만 배열 누적하는 선택 인자를 추가하고 CLI에서 `input`, `manifest`만 지정했다. 공백형·등호형의 입력 순서를 보존한다. 그 외 singleton은 기존 문자열 타입과 중복 시 마지막 값 우선 계약을 유지한다.
- 실제 합성 CLI 회귀: `ledger test`에 `a.txt`, `b.txt`, `c.txt`를 공백형·등호형·혼합으로 전달해 세 파일 receipt 순서를 확인했고, `a.txt`만 변경한 뒤 gate가 stale 입력으로 FAIL함을 확인했다. `assets`와 `gate`의 반복 manifest에서 첫 파일 누락은 FAIL, 파일 복구 후 assets는 정상·gate는 빈 원장에 대한 기존 NEEDS_HUMAN을 반환했다.
- GREEN: `node --test --test-name-pattern='^(png:|ledger:|gate:|evidence:|paths:|reach:|canon:|assets:|lint:|serve:)' tests/studio_kit/` → 16 pass, 0 fail, 0 skipped. `node --test --test-name-pattern='^args: 지정하지 않은 singleton' tests/studio_kit/studio_kit.test.mjs` → 1 pass. `git diff --check` 통과.
- 독립 검증자는 이번 후속 revision에 대해 실행하지 않았다. play/browser, Python wrapper, DB/API/worker/운영 검증, push·배포는 미실행. 기존 인증되지 않은 사용자 quote/reach의 실플레이 승격 제한을 수정하지 않았다.

## 반복 옵션 값 누락 사용법 교정

- 시작점은 `5049727699b0bb825b7c027b61c38cfb457515f4` (부모 `376d2ae75e241493f8091ffef90cdc2dbd7133cf`), clean 확인. 저장소·상위 경로 `AGENTS.md` 없음. README의 backend 일반 테스트 지침은 이번 로컬 게임 키트 범위에 적용하지 않았다.
- 새 positive-selector 테스트 RED: `node --test --test-name-pattern='^(args: 값 필요한 반복 manifest|ledger: 값 필요한 반복 input)' tests/studio_kit/studio_kit.test.mjs` → 2 fail. 실제 CLI에서 bare `--input`/`--manifest`가 경로 `TypeError`로 exit 1 함을 관측했다.
- CLI 시작부에 `input`, `manifest` 두 반복 옵션 값만 검사해 bare(`true`), 빈 값, 반복 중 하나라도 비어 있거나 문자열이 아닌 경우 사용법과 함께 exit 2를 반환하게 했다. 명령별 파일 처리·child 실행·evidence 작성보다 앞에서 검사한다. 다른 옵션 파싱 정책은 바꾸지 않았다.
- GREEN: 같은 positive-selector 명령 → 2 pass, 0 fail. 회귀에서 bare input(다음 옵션 뒤·끝), `--input=`, 정상+빈 반복 input을 확인하고 모두 exit 2, child marker 없음, evidence 변화 없음을 검사했다. 정상 단일 input control은 child 실행 및 evidence 1건을 확인했다. assets/gate 각각 bare·빈 manifest 및 정상+빈 반복값은 모두 exit 2.
- 이번 범위는 위 새 selector 테스트만 실행. 기존 반복값/receipt/stale 및 성숙 경계 테스트는 재실행하지 않고 앞선 검증을 재사용한다. 전체 suite, play/browser, Python wrapper, DB/API/운영/network/auth, 독립 검증, push/fetch/deploy 및 공용 skill 등록 미실행.
