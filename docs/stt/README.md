# 로컬 STT 실행·통합 안내

현재는 선택 경로다. `clovanote` 기본값과 기존 회의록 분석·PMS·벡터를 유지한다.
먼저 [ADR 0025](../adr/0025-on-demand-local-stt.md)와 [측정·한계](measurement-2026-10-15.md)를 읽는다.

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
미설정 시 선택지는 나타나지 않는다. 화자·중첩 처리는 미지원으로 안내한다. 구간 조회·재처리는 현재 API로 제공한다.
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
