# 로컬 STT 실행·통합 안내

현재는 선택 경로다. `clovanote` 기본값과 기존 회의록 분석·PMS·벡터를 유지한다.
먼저 [ADR 0025](../adr/0025-on-demand-local-stt.md), [1차 측정](measurement-2026-10-15.md), [2차 구현·실측과 잔여 항목](phase2-2026-10-15.md)을 읽는다.

## 설치 (운영 재시작은 기획 세션 담당)

기존 backend Python에 선택 의존성을 설치할 때는 `uv sync --extra stt`를 쓴다.
별도 venv를 쓰면 그 Python에 저장소 패키지와 `[stt]` 의존성을 설치하고 `TTAKKARI_LOCAL_STT_PYTHON`으로 지정한다.
런타임은 faster-whisper 1.2.1, PyAV 15.x다. PyAV 19는 `metadata_errors` 인자가 없어 이 엔진과 실제로 호환되지 않았다.
ffmpeg CLI, Torch, MLX, LLM 교정 API는 이 경로에 필요하지 않다.

다운로드는 STT 요청과 분리한다. 새 빈 HF_HOME으로 기존 토큰을 탐색하지 않고 공개 가중치만 받는 예:

```sh
HF_HOME=/absolute/stt-model-cache HF_HUB_DISABLE_IMPLICIT_TOKEN=1 python - <<'PY'
from huggingface_hub import snapshot_download
print(snapshot_download("Systran/faster-whisper-large-v3", token=False,
    revision="edaa852ec7e145841d8ffdb056a99866b5f0a478",
    allow_patterns=["config.json", "model.bin", "preprocessor_config.json", "tokenizer.json", "vocabulary.json"]))
PY
```

반환된 로컬 snapshot 디렉터리를 모델 경로로 쓴다. 요청 처리 중 다운로드는 금지한다.
설정 예(이번 세션은 운영 환경 파일을 읽거나 수정하지 않았다):

```text
TTAKKARI_LOCAL_STT_ENABLED=true
TTAKKARI_LOCAL_STT_PYTHON=/absolute/stt-venv/bin/python
TTAKKARI_LOCAL_STT_MODEL_PATH=/absolute/stt-model-cache/hub/models--Systran--faster-whisper-large-v3/snapshots/edaa852ec7e145841d8ffdb056a99866b5f0a478
TTAKKARI_LOCAL_STT_PROFILE=baseline
TTAKKARI_LOCAL_STT_CPU_THREADS=4
TTAKKARI_LOCAL_STT_TIMEOUT_SECONDS=7200
TTAKKARI_LOCAL_STT_MAX_AUDIO_SECONDS=7200
TTAKKARI_LOCAL_STT_MAX_REPROCESS_SECONDS=120
```

`baseline`이 기본이다. `improved`는 VAD·이전 문맥 차단·용어 힌트 실험 설정이며 모든 평가 지표가 개선된 것은 아니다.
이번 Mac에서 설치·측정한 별도 Python은 `/Users/winterholic/testing/ttakkari-stt/venv/bin/python`이다.
모델도 같은 `testing/ttakkari-stt/hf` 아래에 있다. 캐시에는 파일만 남고 실행 모델은 남지 않는다.

## 사용자 API

- 회의 생성 시 `auto_process: true, provider_id: "local_whisper"`로 전사→기존 분석을 요청한다.
- 기존 녹음은 `POST /api/meetings/{id}/transcribe {provider_id: "local_whisper", auto_analyze: false}`로 전사만 할 수도 있다.
- `GET /api/meetings/{id}/stt`: 시도 목록·queued/running/succeeded/failed·실제 계측.
- `GET /api/meetings/{id}/stt/{job}/segments?offset=0&limit=100`: 원문·지표·음성 참조·절대 오프셋. 최대 500개씩.
- `POST /api/meetings/{id}/stt/{job}/reprocess {start: 10, end: 30, terms: ["프로젝트명"]}`: 후보만 생성. 원전사와 분석은 유지한다.

프론트의 녹음 추가 폼에서 준비된 로컬 엔진을 고르거나, 기존 녹음 상세의 '로컬로 전사'를 쓴다.
미설정 시 선택지는 나타나지 않는다. 기존 프론트에는 화자·중첩 후보 편집 UI가 없으며, 2차 선택 처리 결과는 STT job metadata·구간 API로 조회한다. 구간 조회·재처리는 현재 API로 제공한다.
로컬 전사 후 분석 Run은 기존 정책에 따라 외부 모델을 사용할 수 있다.

## 재현 측정

manifest는 `[{"id":"clip-1","audio_path":"/absolute/clip.wav","reference":"사람이 확인한 전사"}]` 형태다.
정답이 없으면 reference를 null로 둔다. 실제 사용자 파일 경로·전사는 저장소 밖에 둔다.

```sh
python -m ttakkari.meetings.local_stt.benchmark \
  --manifest /absolute/manifest.json --model /absolute/local-model-snapshot \
  --python /absolute/stt-venv/bin/python --output /absolute/benchmark.json
```

두 profile에 같은 입력·SHA-256·범위를 사용한다. 표본마다 새 프로세스를 띄우고 종료를 확인한다.
CER는 NFC 정규화 후 공백을 제외한 문자 편집 거리, WER는 공백 토큰 편집 거리다. 구두점은 유지한다.
마이크로 평균(전체 편집 수/전체 참조 길이)을 사용한다. 빈 정답은 분모가 없어 null이며 환각률 0으로 치환하지 않는다.
최대 RSS는 프로세스 `getrusage` high-water mark다. `release_request_seconds`는 객체 해제 요청 시간이며 물리 메모리 반납 지연의 실측값이 아니다.
실제 메모리 수명 보장은 부모가 확인한 자식 종료다. OS 캐시/메모리 압박 변화와 GPU 메모리는 별도 측정이 필요하다.

## 2차 선택 기능 (기본 꺼짐)

기존 `stt`만 설치하면 화자·분리용 Torch/SpeechBrain을 import하지 않는다. 필요할 때 같은 STT 전용 Python에 추가한다.

```sh
uv sync --extra stt --extra stt-diarization --extra stt-separation
```

`stt-diarization`은 pyannote.audio 4.0.3 및 해당 버전이 고정한 Torch 2.8.0·torchaudio 2.8.0·TorchCodec 0.7.0을 사용한다.
`stt-separation`은 SpeechBrain 1.0.3·Torch 2.8.0·torchaudio 2.8.0이다. backend 기본 의존성·기존 lockfile 버전은 변경하지 않았다.
설치와 모델 다운로드는 요청 처리와 분리하며 운영 설치·재시작은 기획 세션이 담당한다.

```text
TTAKKARI_LOCAL_STT_DIARIZATION_MODEL_PATH=/absolute/complete-community-1-snapshot
TTAKKARI_LOCAL_STT_DIARIZATION_DEVICE=cpu
# 합성 한국어 표본에서는 악화되어 운영 활성화를 권하지 않음. 실험할 때만 지정.
TTAKKARI_LOCAL_STT_SEPARATION_MODEL_PATH=/absolute/sepformer-snapshot
```

화자 모델 경로는 전체 Community-1 snapshot(`config.yaml` 및 하위 모델)을 가리켜야 한다.
HF 계정으로 [모델 카드](https://huggingface.co/pyannote/speaker-diarization-community-1)의 조건에 동의한 뒤,
사용자가 직접 인증한 별도 설치 절차에서 snapshot을 준비한다. 토큰을 채팅·설정 예시에 붙이지 않는다.
이 워커는 token=False·offline·일회용 HOME/HF_HOME·telemetry 비활성화로 실행한다. 부분 모델이나 실패는 metadata의 unavailable/failed로 남기고 원전사를 보존한다.
PyAV로 디코딩한 16kHz 파형을 pyannote에 전달하여 TorchCodec 파일 디코딩을 사용하지 않는다.

분리 모델은 `speechbrain/sepformer-whamr` revision `9950fd8055b38679d2af5bef2366b5dba600ec66`의 `encoder.ckpt`, `decoder.ckpt`, `masknet.ckpt` 세 파일이다.
공식 아키텍처로 구성한 뒤 로컬 tensor state만 weights_only=True로 로드한다. YAML 실행·허브 자동 조회·런타임 다운로드는 없다.
2인·8kHz 모델이므로 16→8→16kHz 변환을 거친다. 3인 이상 중첩, 15초를 넘는 단일 중첩, 총 120초 예산 초과는 상태를 남겨 건너뛴다.
분리 트랙 번호는 구간 내부 번호이며 Community-1 화자 ID나 인물 이름과 연결하지 않는다.

추가 API:

- `POST /api/meetings/{mid}/stt/{job}/corrections {replacements:{"오인식":"정정"}, offset:0, limit:100}`: 원문 기준 비연쇄 치환 미리보기. DB 원문·교정 필드·분석을 덮어쓰지 않는다.
- `POST /api/meetings/{mid}/stt/{job}/overlap/{candidate_index}/compare {references:["화자1 정답","화자2 정답"], human_verified:true}`: 해당 구간의 검수된 정답 2개로 비교. CER가 줄고 WER가 늘지 않을 때만 adopt=true. 이 응답도 후보 판정이며 회의 본문을 변경하지 않는다.

`metadata.diarization.turns`는 일반 화자 구간, `exclusive_turns`는 비중첩 구간이다. 중첩은 일반 구간으로만 계산한다.
`metadata.speaker_count`는 해당 작업의 음향 클러스터 수이며 실제 인원 수의 정답 검증값이 아니다.
인물 식별은 별도 계층(`identity_status=not_run`, `speaker_identities={}`)으로 두며 참석자명으로 추정하지 않는다.
`metadata.overlap_candidates`에는 같은 원음 구간의 재전사·분리 트랙 전사·비교 판정·건너뜀/실패 상태가 남는다.
앱 요청에는 정답이 없으므로 selected_tracks는 null이며 원문은 항상 보존한다. API는 명시 검수 참조로 재비교만 제공한다.
