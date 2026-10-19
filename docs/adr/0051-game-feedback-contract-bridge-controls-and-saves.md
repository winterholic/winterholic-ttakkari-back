# ADR 0051: 시각 피드백 요청 계약, bridge 일시정지·계측, 게임 내 세이브 (Phase 2 연결 공백)

- **상태**: 구현됨 (2026-10-19). 세이브는 템플릿 수준 최소 구현이며 관제탑 UI는 없다.
- **관련**: ADR 0047(데이터 계약), 0049(게임 origin, postMessage), 0050(worker 연결·롤백)

## 1. 요청 계약에 시각 피드백을 구조화한다

이전에는 `RequestIn`이 스크린샷 Artifact 참조를 받지 않아 프론트가 문장에 Artifact ID만 적었고, worker가 그 ID로 파일을 열 수 없었다(Artifact는 `data_dir`의 관리 영역에 있고 worker의 작업 디렉터리 밖이다).

`POST /api/games/{id}/requests`의 `feedback`(선택):

| 필드 | 규칙 |
|---|---|
| `build_id` | 캡처한 빌드. 요청의 `build_id`가 있으면 같아야 하고, 없으면 이 값이 대상 빌드가 된다. |
| `artifact_id`, `annotated_artifact_id` | 원본·주석본 스크린샷. 이 게임 workspace의 `image/png` Artifact만 허용하고 PNG 서명·크기(16MB)·기록된 sha256을 다시 확인한다. |
| `width`, `height` | 이미지 크기(1~8192). |
| `annotations` | 최대 20개. `box`(x,y,w,h)·`arrow`(x1,y1,x2,y2)·`freehand`(points 2~200개). 모든 좌표는 이미지 왼쪽 위 (0,0) 기준 0~1이고 box는 이미지 밖으로 나가면 거부한다. |

- 하나도 안 붙었으면(스크린샷·주석 모두 없음) 422다. 알 수 없는 필드·주석 type은 422다(`extra=forbid`, discriminated union).
- **worker가 열 수 있는 경로**: 검증을 모두 통과한 뒤에만 PNG를 게임 저장소의 `.ttakkari-feedback/<12자 id>/`로 복사하고(`screenshot.png`, `screenshot-annotated.png`, `feedback.json`) prompt에 **절대 경로**와 사람이 읽는 영역 설명을 싣는다. 검증 실패 시 Run도 파일도 만들지 않는다.
- 이 폴더는 `.git/info/exclude`로 추적에서 빼서 저장소 파일(`.gitignore` 포함)을 바꾸지 않고, 작업 트리를 더럽히지 않으며(롤백의 `dirty_worktree` 검사에 걸리지 않는다), 격리 빌드 복사(`SKIP_DIRS`)에서도 제외한다. 최근 20개만 남긴다.
- Run의 `usage.game.feedback`에 폴더 이름·Artifact ID·주석 수를 남긴다(서버 경로는 API로 내보내지 않는다).
- `GameOut.genre`: 저장소 매니페스트의 장르. 매니페스트를 읽을 수 없으면 빈 문자열. 엔진은 기존 `engine_id`다.

## 2. 템플릿 bridge: pause/resume·state·save

부모→게임 요청은 모두 `{type, id}`이고 기존과 같은 검사(`event.source === window.parent`, `event.origin === #ttk-parent` 힌트, 알려진 type, 64자 이하 id)를 거친다. 응답은 부모 origin으로만 보낸다(와일드카드 없음).

| 요청 | 응답 |
|---|---|
| `ttakkari:capture` | `ttakkari:capture:result {width,height,dataUrl}` (기존) |
| `ttakkari:pause` / `ttakkari:resume` | `ttakkari:paused {paused, supported}` (Phaser `game.pause()/resume()`) |
| `ttakkari:state` | `ttakkari:state:result {state}`: 게임이 `publishState(obj)`로 게시한 직렬화 값(16KB 이하). 점수·위치 등 계측용 |
| `ttakkari:save-export` / `ttakkari:save-import {data}` | `…:result` (아래 세이브) |

`publishState`는 G13 브라우저 smoke와 G14 E2E가 게임 규칙 작동(이동·점수·체력·게임오버)을 화면 픽셀이 아니라 값으로 확인하는 계측 창구이기도 하다. 이전 템플릿으로 만든 게임은 pause 메시지를 모르므로 응답이 없고, 프론트는 시간 초과를 "이 빌드는 일시정지를 지원하지 않아요"로 안내한다.

## 3. 게임 내 세이브: 메모리 + export/import (결정)

sandbox iframe은 `allow-same-origin`이 없는 opaque origin이라 `localStorage`·IndexedDB·쿠키를 쓸 수 없다(ADR 0049). 선택지:

1. 게임 안 메모리 저장 + bridge export/import (채택)
2. 부모 PWA가 `localStorage`에 슬롯별로 보관하는 bridge 경유 영구 저장
3. 서버 API에 저장(게임 origin은 자격증명이 없으므로 부모 대리 호출 필요)

**채택 이유**: 1은 sandbox·origin 경계를 전혀 약화하지 않고, 게임 코드는 `save.get/set`만 쓰면 된다. 영구 저장(2·3)은 부모 쪽 UI·용량 한도·게임별 격리 키·삭제 정책이 필요해 Phase 3 범위로 넘긴다. 현재 템플릿은 `save`(메모리, 새로고침 시 사라짐)와 `ttakkari:save-export/import`(JSON 문자열 64KB 이하, 객체만 허용)를 제공한다. 부모 UI는 아직 없으므로 **새로고침 후에도 유지되는 세이브는 지원하지 않는다**(미구현). 한 판짜리 게임은 영향이 없고, 진행 저장이 필요한 게임은 export/import UI가 생기기 전까지 불가능하다.
