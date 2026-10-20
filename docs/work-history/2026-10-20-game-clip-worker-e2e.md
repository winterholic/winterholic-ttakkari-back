# G31 남은 종단 검증 시도: 실제 worker 프레임 E2E (미실행, 도구 추가)

목표는 실제 worker 가 `.ttakkari-feedback/<id>/clip-NN.jpg` 프레임을 열어 보고 수정하고 자동 빌드까지 성공하는지 확인하는 것이었다.
이 실행(muse 엔진 샌드박스)에서는 소켓이 전부 막혀 격리 스택을 띄울 수 없어 **미실행**으로 남긴다. 대신 E2E 도구 4종을 추가하고 오프라인으로 검증했다.
G31 전체 완료로 표시하지 않는다. 서버 코드 변경은 없어 배포 예약 대상이 아니다.

## 차단 근거(실행 환경 한정, 제품 결함 아님)

- 프로브 측정(muse, 2026-10-20 13:19 UTC): `network_loopback`·`network_outbound`·`process_list`·`gpu_metal` blocked.
  직접 확인: loopback bind·connect, `/tmp/.s.PGSQL.5432`·OrbStack docker.sock 연결이 전부 `PermissionError [Errno 1] Operation not permitted`.
- 격리 스택(API bind·postgres·docker 컨테이너 빌드·Chromium·worker 네트워크)이 모두 소켓을 필요로 해서 이 샌드박스에서는 실행 불가.
- `require_escalated` 1회 시도 → 즉시 거부: `tool denied: unsandboxed execution requires human approval, but approval prompts are disabled`.
  우회가 아니라 정해진 승인 경로가 없는 것이므로 재시도하지 않았다.
- outbound https 는 프록시 경유로 되어 git fetch·push 는 된다. 이 결론을 호스트·운영 상태로 일반화하지 않는다.

## 추가한 도구(커밋 포함, `tests/game_smoke/`)

- `clip_bench.py`: 수동 E2E 드라이버(pytest 가 수집하지 않음). G29b `pick_bench.py` 와 같은 격리 스택(API 18790·게임 origin·분리 DB/HOME)을 전제로 한다.
  사례마다 게임 생성 → 픽스처 기록·커밋 → 격리 빌드 → 실제 bridge record 녹화(Chromium) → JPEG 전체 픽셀 스캔 → 결함 2장+정상 2장 선별 →
  `/api/uploads` 업로드 → `FeedbackIn.clip` 수정 요청(`auto_build=true`) → Run 대기 → events 에서 clip 파일 열람 근거 수집 →
  git diff·마커 확인 → 자동 빌드 대기 → 수정 빌드 재녹화 픽셀 판정. 비용·시간을 결과에 남긴다. 결함이 녹화에 안 보이면 worker 를 부르지 않는다.
- `clip_bench_collect.mjs`: record-start(2fps·6초)/stop 후 프레임 전체를 JPG 로 저장하는 수집 스크립트(`record_scenario.mjs` 변형).
- `clip_bench_color.ts`: 빨간 공이 10초에 걸쳐 화면을 가로지른다. `bx > 0.6` 부터 파란색으로 바뀌는 결함. 지시문은 바뀌는 사실과 "처음 색 그대로"만 말한다.
- `clip_bench_missing.ts`: 빨강·초록·파랑 공 세 개가 함께 이동한다. `bx > 0.5` 부터 초록이 사라지는 결함. 지시문은 어느 공인지 말하지 않아 프레임을 봐야 안다.
- 판정: `pass` = clip 파일 1장 이상 열람 흔적(tool.call 입력에 `clip-NN.jpg`) + 재녹화 전 프레임 정상 픽셀 + 자동 빌드 성공(소스 커밋 일치) + Run 성공.

## 오프라인 검증(소켓 없이 한 것)

- `py_compile clip_bench.py` → PY-OK, `node --check clip_bench_collect.mjs` → MJS-OK, import → `IMPORT-OK ['color' 'missing']`.
- 픽스처 2종을 스텁(phaser·bridge) + front tsc `--strict` 로 검사 → TSC-OK. Phaser API 이름은 통과한 in-repo 픽스처와 대조했다.
  실제 tsc·vite 는 격리 컨테이너 빌드가 유일한 확인 수단이라 E2E 실행 시 판정된다.
- 스캔 임계값(빨강 R≥150·G,B≤130, 파랑 B≥150·R≤130·G≤150, 초록 G≥150·R,B≤150, 존재 기준 200px)을 bridge 와 같은 품질 60 JPEG 합성 이미지로 확인:
  각 색 468~483px, 교차 검출 0.
- 서버 `FeedbackIn.clip` → 저장소 배치(`clip-NN.jpg`) → worker prompt(시각순 열람 지시) 경로는 코드·기존 테스트로만 재확인했다.

## 재실행 절차(다음 작업자용, 소켓 되는 환경에서)

1. `/private/tmp/gs-g31/env.sh` 를 읽어 격리 env 를 쓴다(DB `ttakkari_gs_g31_20261020`, HOME·게임 루트 분리, 운영과 무관).
   `TTAKKARI_PLAYWRIGHT_DIR` 은 playwright node 가 설치된 쪽으로 지정한다(당시 canonical back `.browser-runtime`).
2. `/private/tmp/gs-g31/run.sh [color|missing|all]`: createdb → init-config → migrate → API 18790·serve-games 18795 기동 → clip_bench.py → 서버 종료.
   결과는 `/private/tmp/gs-g31/out/<사례>/{pre,clip,post,request,run,events,prompt_clip,result}.json` 과 `results.json`.
3. 브라우저·컨테이너 이미지는 호스트 캐시를 쓴다. worker 비용은 G29b 기준 건당 약 0.07~0.10달러였다.
4. 성공하면 ADR 0060 상태·game-studio.md G31 행을 갱신하고, 서버 코드 변경이 있을 때만 관련 pytest 통과 후 clean SHA 로 배포 예약한다.

## 불변 조건 준수

운영·사용자 게임 저장소를 건드리지 않았다(격리 DB·HOME·게임 루트만 설계하고 실행도 못 했다).
canonical back 의 다른 세션 미커밋 변경과 back-games-p3 의 다른 작업자 미커밋 변경(G32 추정: `M api/games.py`, `versions.py` 등)을 건드리지 않고,
origin/main 기준 clean worktree 에서 내 파일만 커밋했다. launchctl·백엔드 재시작은 실행하지 않았다. 실기기 iOS Safari 는 범위 밖.
