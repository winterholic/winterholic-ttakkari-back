# G31 실제 worker 종단 검증: 구간 프레임 열람·수정·자동 빌드 (실행 완료, 작은 표본)

2026-10-20 호스트 엔진(claude)에서 격리 스택을 띄워 실제 worker 로 E2E 를 실행했다. 앞선 muse 작업자의 도구를 독립 검토해 거짓 통과 구멍 5개를 고친 뒤 두 픽스처를 다시 돌렸다.
서버 코드는 바꾸지 않았다(배포 예약 대상 아님). 전체 G31 완료가 아니라 "worker 가 프레임을 열어 수정하고 자동 빌드까지 성공하는 경로가 작동한다"는 표본 확인이다.

## 실행 환경과 사전 확인

- 호스트 엔진 Run 에서 `socket.bind(("127.0.0.1",0))` → `bind ok ('127.0.0.1', 56670)`, `docker ps` 응답, `pg_isready -h 127.0.0.1` → `접속을 받아드리는 중`. 앞선 muse 샌드박스의 차단은 그 환경에 한정된 것이었다.
- `/private/tmp/gs-g31/run.sh` 는 있었으나 `env.sh` 가 없었고 ROOT 가 다른 작업자(G32)의 worktree 였다. `env.sh` 를 다시 만들고(DB `ttakkari_gs_g31_20261020`, HOME·게임 루트 `/private/tmp/gs-g31`, API 18790, 게임 origin 18795), run.sh 의 ROOT 를 최신 origin/main 의 clean worktree(`back-g31-e2e`)로 바꿨다.
- 운영 DB·.env·운영 게임은 쓰지 않았다. worker 는 실제 Claude(engine claude, model sonnet), 빌드는 실제 OrbStack 컨테이너, 녹화는 실제 Chromium bridge 다. 대체 경로·mock 없음.

## 독립 검토에서 고친 구멍(clip_bench.py)

1. 녹화 6초는 `color` 결함 시작(약 6.2초)을 거의 못 덮었고, 수정 뒤 재녹화도 결함 구간을 겨우 스쳐서 임계값을 뒤로 밀기만 해도 "수정됨"으로 통과했다. 녹화를 11초로 늘리고 공 중심 x 로 전 구간(첫 프레임 ≤0.25, 마지막 ≥0.8)을 덮었는지를 판정 조건에 넣었다. 사전 녹화도 같은 조건이다.
2. 열람 판정이 tool.call 입력에 `clip-` 문자열만 있으면 인정했다(`ls`·`cat | wc` 도 열람). 이미지를 보는 도구(Read 등)가 `clip-NN.jpg` 를 경로로 불렀고 성공 tool.result 가 있는 것만 센다.
3. 열람이 첫 편집보다 먼저인지, 결함 프레임을 하나 이상 열었는지를 추가했다. worker 가 Bash(python)로 소스를 고치므로 Bash 편집(`sed -i`·`.replace(`·`git commit` 등)도 편집으로 본다. 정규식 휴리스틱이라 완전하지 않다.
4. 격리 가드: `TTAKKARI_NO_ENV_FILE=1`, loopback API, 임시 경로 게임 루트, DB 이름 표식(gs)이 아니면 아무것도 만들지 않고 멈춘다.
5. `missing` 공 반경 22 는 스캔 면적(약 243px)이 임계 200 에 겨우 닿아 JPEG 가장자리에서 흔들릴 수 있어 30(약 440px)으로 바꿨다. 비용·시간에 요청→자동 빌드 구간을 추가했다.

판정 함수는 소켓 없이 `tests/test_game_clip_bench.py`(8개)로 고정했다. 관련 pytest 17개 통과.
첫 실행에서 새 coverage 검사가 `color` 에서 오탐했다(공이 파랗게 변한 뒤 빨간 픽셀 중심이 없음). 색 무관 중심으로 고쳤다. 이 오탐 때문에 worker 를 부르지 않은 실행은 비용 0이다.

## 결과(최종 도구, 사례당 1회)

| | color(공 색이 중간에 파랑으로) | missing(세 공 중 초록이 중간에 사라짐) |
|---|---|---|
| Run | 80c98ad3 (claude/sonnet, succeeded) | da1817e1 (claude/sonnet, succeeded) |
| 올린 프레임 | 4장(정상 2·결함 2), 480x360 JPEG | 4장(정상 2·결함 2) |
| clip 열람 근거 | Read 4회 seq 9·11·13·15, 모두 성공 result, 결함 프레임(clip-03·04) 포함 | Read 4회 seq 7·9·11·13, 결함 프레임 포함 |
| 열람 vs 첫 편집 | 편집 seq 20, 열람이 먼저 | 편집 seq 23, 열람이 먼저 |
| 수정 | `tint` 계산·`setFillStyle` 삭제, 색 상수 고정(3+, 4-줄) | `green.setVisible(bx <= 0.5)` 삭제(1+, 2-줄) |
| 자동 빌드 | v3 succeeded, source_commit 이 worker 커밋과 일치 | v4 succeeded, 일치 |
| 수정 빌드 재녹화(11초·23프레임) | 전 프레임 빨강 공만, 공 x 0.16→0.92 로 전 구간 | 전 프레임 세 공, x 0.15→0.92 |
| 비용 | 0.1018달러 | 0.0974달러 |
| 시간 | Run 25초, 요청→자동 빌드 35초 | Run 21초, 요청→자동 빌드 31초 |
| 판정 | pass | pass |

도구 호출 8개씩, guard.blocked 0. 결과는 `/private/tmp/gs-g31/out/<사례>/`(임시 경로, 증거는 이 문서에 요약).
검토 도중 이전 판정 코드로 돌린 `missing` 1회(Run 34e1b4dd, 0.0981달러, 17초/37초)도 같은 방식으로 성공했고 새 기준으로 재판정해도 열람·순서·전 구간 픽셀이 성립했다. worker 비용 합계 3건 0.2973달러.
비용은 Claude CLI 가 보고한 `cost_usd` 이며 구독 청구액과 같다고 확인하지 않았다(확인 필요).

## 이 결과가 말하지 않는 것

- 표본은 사례 2종(missing 은 2회)이다. 일반 성공률이 아니다.
- 두 결함 모두 소스가 짧아 프레임 없이 소스만 읽어도 고칠 수 있다. 이 E2E 는 "worker 가 프레임을 실제로 열고, 그 뒤 고치고, 빌드가 성공한다"를 증명하지 "프레임이 있어야만 고칠 수 있다"는 증명하지 못한다. 후자는 소스에 단서가 없는 결함(예: 에셋·타이밍 결함)과 프레임 없는 대조군이 필요한 다음 단위다.
- Read 도구 결과 본문은 이미지라 이벤트에 비어 있다. 열람 증거는 호출·성공 result·순서이고, worker 의 결과 글이 클립 메타의 시각(0.00·5.01·5.51·11.01초)과 공 위치·색을 정확히 인용한 것은 프레임을 봤다는 정황 근거(추론)다.
- worker 는 빌드·실행을 하지 않았고(격리 정책) 자동 빌드와 재녹화가 유일한 검증이다. Chromium 헤드리스만이며 iOS Safari·실기기는 범위 밖이다.

## 재실행 절차(다음 작업자용)

1. `/private/tmp/gs-g31/env.sh`(위 환경)를 두고 `/private/tmp/gs-g31/run.sh [color|missing|all]` 을 실행한다. ROOT 는 clean worktree 여야 한다. 격리 가드가 환경을 확인한다.
2. createdb → init-config → migrate → API 18790·serve-games 18795 → clip_bench.py → 서버 종료. 결과는 `out/<사례>/{pre,clip,post,request,run,events,prompt_clip,result}` 와 `results.json`.
3. `TTAKKARI_PLAYWRIGHT_DIR` 은 canonical back 의 `.browser-runtime`. 컨테이너 이미지는 호스트 캐시(node:20-alpine).
4. 실패 시 `result.json` 의 `clip_evidence`(열람·순서), `pre_covers_traverse`, `fixed_pixels`, `auto_build` 부터 본다.

## 불변 조건 준수

운영·사용자 게임 저장소를 건드리지 않았다(격리 DB·HOME·게임 루트만). canonical back 의 다른 세션 미커밋 변경과 back-games-p3(G32)를 건드리지 않고 origin/main 기준 clean worktree 에서 작업했다. launchctl·백엔드 재시작·배포 예약은 하지 않았다(서버 코드 변경 없음).

## 앞선 muse 시도 기록(보존)

앞선 실행(muse 엔진 샌드박스)은 소켓 차단(`network_loopback`·`network_outbound` blocked, loopback·pg·docker.sock 이 `PermissionError [Errno 1]`)과 승인 비활성으로 E2E 를 못 돌려 도구 4종만 추가했다. 이 차단은 그 환경 한정이며 호스트 엔진에서는 위 사전 확인대로 막히지 않았다.
