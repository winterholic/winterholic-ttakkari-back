# ADR 0035: ArtCraft 관제

## 상태
채택(2026-10-18)

## 배경
Mac Studio에는 ArtCraft 앱 13개(PhotoCraft 등)가 `~/development/projects/artcraft`에 설치돼 있다. 앱마다 CLI, MCP(stdio), 루프백 TCP JSON 라인 UI 제어 포트가 있고 HTTP 엔드포인트는 없다. 폰에서 설치 상태, 스모크 결과, 버그 후보, 에이전트 연결 정보를 보고 몇 가지를 조작할 수 있어야 한다. 디자인시스템 도메인(ADR 0034)과는 무관하다.

## 결정
- 독립 모듈 `ttakkari/artcraft.py`와 `api/artcraft.py`(`/api/artcraft/*`). 디자인시스템 코드·데이터·라우트를 쓰지 않는다. 인증은 소유자 로그인(`User`)뿐이며 에이전트 전용 라우트는 만들지 않는다.
- 상태 조회는 고정 저장소(`TTAKKARI_ARTCRAFT_ROOT`)의 `bin/artcraft`만 호출한다. 명령은 고정 목록(`status --json --deep`, `status --json --remote`, `mcp-config`, `gui start|stop <app>`, `smoke [apps] [--no-gui]`)이고 앱 이름은 `registry.json`에 있는 값만 받는다. 셸을 쓰지 않고(`create_subprocess_exec`) 사용자 입력이 인자 위치에 그대로 들어가는 경로가 없다.
- 토큰·키·control 파일은 읽지 않는다. `registry.json`에서도 인증 방식(none/token/key/file)만 꺼내고 경로는 응답에 싣지 않는다. 자식 프로세스 환경에서는 `TTAKKARI_*`를 제거하고 registry의 앱별 보정 변수(`PHOTOCRAFT_NO_SYSTEM_FONTS=1` 등)를 더한다.
- 모든 호출에 제한시간과 프로세스 그룹 종료가 있다. 느린 일(GitHub 최신 릴리스 확인, 스모크, 앱 실행·종료)은 메모리 Job으로 202를 돌려주고 `GET /jobs/{id}`로 본다. 스모크끼리, GUI 포함 스모크와 앱 실행·종료는 동시에 돌리지 않는다(409). Job 기록은 서버 재시작 때 사라진다.
- 최신 릴리스는 GitHub 미인증 한도(시간당 60회, 1회 조회에 13회) 때문에 6시간 캐시하고 수동 재확인은 120초 간격으로 제한한다. 일부만 실패해도 이전 값을 지우지 않는다.
- `smoke`는 `smoke/results/latest.json` 전체를 덮어쓴다. 부분 실행이 13앱 기준 기록을 지우지 않도록 실행 후 새 결과는 따까리 상태 파일로 옮기고 원래 파일 내용을 되돌린다. 화면의 스모크 값은 앱별로 두 기록 중 더 새것이다.
- 확인 단계: 앱 종료(저장 안 한 문서가 사라질 수 있음)와 GUI 포함 스모크(창을 열고 닫음)는 요청에 `confirm=true`가 없으면 428이다. GUI 포함 스모크는 이미 실행 중인 앱이 하나라도 있으면 시작하지 않는다(스모크가 그 창을 닫아 버리므로). 앱 종료는 `artcraft gui start`로 켠 pid만 대상이다.
- 설치·업데이트는 실행하지 않는다. 최신 릴리스와 비교해 "업데이트 가능"만 표시하고 STATUS.md §6의 수동 절차를 보여 준다(자동 업데이터가 아직 없고 dist/ 교체는 설치 상태를 망가뜨릴 수 있다).
- 버그 후보는 `BUGS.md` 표와 `## BUG-nnn` 절을 읽기 전용으로 파싱한다. 미보고/보고됨/해결 상태와 메모는 `<data_dir>/artcraft/state.json`(0700 디렉터리, 파일 락 + 원자적 교체)에 둔다. DB 마이그레이션이 없다.

## 결과
- 새 테이블·마이그레이션 없음. generation protocol 변경 없음.
- 한계: 세대 배포 중 겹치는 두 프로세스는 Job을 서로 모른다. 서버가 스모크 도중 재시작되면 이미 띄운 앱 창이 남을 수 있다(`gui stop`으로 정리).
