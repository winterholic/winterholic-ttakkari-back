# ADR 0060: 플레이 녹화 구간 피드백 — 프레임 시퀀스 녹화와 구간 프레임 전송 (Phase 3, TASK-G30·G31)

- **상태**: 설계 결정(2026-10-20). 게임 쪽 녹화 bridge(`record.ts`, `bridge.ts` 의 record-start/stop)는 구현·실제 브라우저 검증을 마쳤다(tests/test_game_record.py 8개: node 순수 로직 6, 실제 Phaser 게임·Chromium 데스크톱/390x844 2). 관제탑 녹화 패널(녹화·구간 슬라이더·대표 화면 미리보기)은 vitest 로 확인했고 실브라우저 390x844 UI 확인은 아직이다. back 의 `FeedbackIn.clip` 검증·배치·prompt(G31)는 구현과 거부 테스트를 마쳤다(tests/test_game_feedback_clip.py 4개). 미구현·미검증: front 의 "이 구간을 수정 요청에 담기"(확인 후 업로드), 실제 worker 가 프레임을 읽는지 확인.
- **관련**: 설계서 §5.1 "3) 플레이 영상 녹화 및 타임라인 구간 피드백", ADR 0049·0052(bridge·피드백 계약), ADR 0059(선택 결과 연결 방식)

## 설계서와 다르게 정한 것과 이유

설계서는 `MediaRecorder` 로 캔버스 스트림을 WebM 으로 녹화하고 구간을 자르는 것으로 적었다. 이 Mac 에서 실측한 제약 때문에 **WebM 클립 대신 프레임 시퀀스**로 정한다.

1. **ffmpeg 가 없다**(설계서 §2.7, ADR 0046: PATH·`/opt/homebrew/bin` 모두 없음). 서버에서 WebM 을 구간 단위로 자를 수단이 없다.
2. **브라우저에서 WebM 을 자를 수 없다.** `MediaRecorder` 산출물은 시킹 정보(Cues)와 길이가 없고, 키프레임 경계가 아닌 곳에서 자르면 깨진다. 구간 재인코딩은 선택 구간만큼 실시간으로 다시 재생해 녹화해야 해서 느리고 Safari 에서 `video.captureStream` 지원이 불확실하다.
3. **worker 는 영상을 읽지 못한다.** worker(Claude CLI)가 열어 볼 수 있는 것은 이미지 파일이다. 어느 방식이든 worker 에게 닿는 것은 구간의 프레임이다.
4. **"전체 녹화를 올리지 않는다"는 설계 원칙**은 프레임 단위로 고르면 구조적으로 지켜진다. 올리는 것은 사용자가 확인한 구간의 프레임뿐이다.

## 결정

### 녹화(게임 쪽 bridge, G30)

- 부모→게임 요청 `record-start`(+ `fps`, `maxMs`)와 `record-stop`. 게임 템플릿 `bridge.ts` 가 캔버스를 낮은 해상도 JPEG 로 주기적으로 모아 둔다(`preserveDrawingBuffer` 가 이미 켜져 있어 WebGL 에서도 읽힌다). 메모리에만 두고 stop 때 한 번에 부모 origin 으로 돌려준다.
- **게임이 스스로 상한을 지킨다**: 길이 30초, 초당 6프레임 이하, 가로 480px 이하, 전체 6MB 이하. 넘으면 오래된 프레임을 버리지 않고 **녹화를 멈추고** 사유(`reason: "limit"`)를 응답에 싣는다(조용히 잘리지 않는다).
- 응답 `record:result`: `{frames: [{t_ms, dataUrl}], width, height, fps, duration_ms, reason}`. 부모는 `acceptParentMessage` 와 같은 source·origin·id 검증을 하고 프레임 수·바이트·JPEG 서명을 다시 검사한다.
- 미연결 게임(이전 템플릿)은 `supported: false` 로 답한다(pick 과 같은 규칙).

### 구간 선택(front, G30)

- 녹화 중 표시, 정지 후 플립북 미리보기와 구간 슬라이더(시작·끝). 대표 프레임은 구간을 균등하게 나눠 최대 8장을 미리 보여 주며, 사용자가 장당 제외할 수 있다.
- 업로드 전에는 아무것도 서버로 가지 않는다.

### 전송과 제한(back, G31)

- 사용자가 미리보기에서 "이 구간을 수정 요청에 담기"를 확인했을 때만 선택한 프레임을 기존 `/api/uploads` 로 올려 Artifact 로 만든다(새 업로드 시스템을 만들지 않는다).
- `FeedbackIn.clip`: `{start_ms, end_ms, frames: [{artifact_id, t_ms}], note}`. 검증: 프레임 1~8장, `0 <= start_ms < end_ms`, 구간 15초 이하, 각 `t_ms` 가 구간 안, 프레임당 1MB 이하, 합계 6MB 이하, JPEG 서명, Artifact 의 해시·작업 공간 일치, 알 수 없는 필드 거부. 어긴 요청은 아무 파일도 쓰지 않고 422/400 으로 거부한다.
- worker 에게는 기존 스크린샷과 같은 방식으로 저장소 안 `.ttakkari-feedback/<id>/clip-NN.jpg` 로 복사하고 prompt 에 경로와 시각(ms)을 싣는다. prompt 문구는 "플레이어가 문제라고 표시한 구간의 프레임이다. 프레임 사이는 보이지 않는다. 한 장씩 열어 보고 순서대로 비교하라"로 한다.

### 개인정보

- 녹화되는 것은 게임 캔버스뿐이다(기기 화면·카메라·마이크는 쓰지 않는다). `getDisplayMedia` 는 쓰지 않는다.
- 사용자가 전송 전에 올라갈 프레임을 모두 본다. 녹화 전체는 메모리에서만 존재하고 전송하지 않은 프레임은 페이지를 닫으면 사라진다.

## 한계(예상)

- 프레임 사이의 움직임은 전달되지 않는다(초당 6프레임). 짧은 순간의 현상은 놓칠 수 있다.
- 게임의 콘솔 로그와 `publishState` 값은 구간에 싣지 않는다(게임이 낸 문자열이라 prompt 주입 표면이 커진다). 필요하면 별도 단위로 다룬다.
- WebM 클립 파일은 만들지 않는다. 사람이 영상으로 돌려 보는 용도는 이 결정에서 제외한다.
- iOS Safari 는 확인하지 않는다.
