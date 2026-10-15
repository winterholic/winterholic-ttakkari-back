# 외부 전사 연동 제거·로컬 기본 환경 준비 · 2026-10-15

코드·마이그레이션·로컬 런타임을 준비했다. 백엔드 운영 재시작·배포 요청·운영 마이그레이션은 이 작업에서 실행하지 않는다.
기존 운영 기준 069c88b의 배포 성공(b8262bda)은 사용자 제공 상태다. 이번 변경의 적용은 기획 세션이 최종 SHA로 별도 요청한다.
현행 결정은 [ADR 0027](../adr/0027-retire-clovanote.md)이다.

## 제거·보존

- backend: Playwright 브라우저 어댑터, 업로드/가져오기/동기화 큐·자동 시작, REST/agent API, 네 MCP 도구, 도구 권한/소개, provider·설정·로그인 코드 제거.
- frontend: 연결·로그인·노트 탭·가져오기 대화상자·외부 전사 버튼·실패 작업 위젯·API 타입/클라이언트·전용 CSS 제거. 기존 탭 URL은 일반 목록을 연다. 공용 도서관 저장 상태는 VectorLine으로 분리해 유지했다.
- Playwright 런타임 사용처는 제거한 어댑터 한 곳뿐이었다. lockfile에서 playwright/pyee만 제거됐고 남은 패키지 버전은 모두 동일하다. 별도 브라우저 검사 도구를 앱 의존성으로 남기지 않는다.
- 기존 meetings/items/proposals/chunks·녹음 Artifact·분석/PMS/검색 경로와 과거 provider 출처 문자열은 보존한다.
- ~/.ttakkari/clovanote-chrome 폴더의 존재만 확인했으며 내용 조회·삭제는 하지 않았다.

## 데이터 마이그레이션 0026

외래키는 구 두 테이블→meetings 방향이다. meetings의 provider_job_id는 문자열이고 구 테이블을 참조하는 외래키가 아니다.
구 노트/작업의 모든 행을 retired_integration_records에 전체 JSONB로 보관한 후 활성 clovanote_notes/jobs를 삭제한다.
보관본에는 원전사·화자/시각 구간·메타·작업 이력이 그대로 남는다. 새 API나 외부 연동에서는 사용하지 않는다.
연결된 회의 전사가 비었을 때만 최신 노트의 전사를 구제한다. 사람이 교정한 기존 전사, 요약·항목·벡터는 덮어쓰지 않는다.
미가져오기 노트는 보관만 하며 새 회의록을 자동 생성하지 않는다.

downgrade는 원래 타입·테이블·인덱스를 만들고 JSONB에서 모든 행을 복원한다. 그 사이 삭제된 회의를 가리키면 해당 링크만 null로 복원해 원전사·작업을 살린다.
구제된 회의 전사는 downgrade에서도 지우지 않는다. 기존에 진행 중이던 구 provider 전사는 조회 시 기존 오류 전이로 실패하고 로컬 재전사를 할 수 있다.
새 운영 마이그레이션은 배포 감독만 실행한다. 실제 운영 원문을 이 작업에서 조회하거나 이동하지 않았다.

독립 임시 DB에서 0025→0026→0025와 재왕복, 모든 원본 JSONB 일치·교정 원문 유지·빈 전사 구제·삭제된 회의 참조 복원을 확인했다.
이 과정의 JSON `:null` SQLAlchemy 바인드 오인식은 실제 SQL 로그로 확인하고 jsonb_build_object로 고쳐 왕복 검사로 검증했다.

## .env 편집 없는 STT 기본 경로

| 구성 | 코드 기본값 |
|---|---|
| 지속 런타임 루트 | ~/development/projects/winterholic-ttakkari/back/.stt-runtime/ |
| Python | 루트의 venv/bin/python |
| Whisper 가중치 | 루트의 faster-whisper-large-v3/ |
| profile / CPU | baseline / int8 / 4스레드 |

이 Mac에 별도 venv와 모델 실파일을 준비했다. Git에서 제외하며 테스트 자료 캐시를 가리키는 임시 심볼릭 링크가 아니다.
런타임은 faster-whisper 1.2.1과 PyAV 15.x, 공개 가중치는 Systran/faster-whisper-large-v3 revision edaa852ec7e145841d8ffdb056a99866b5f0a478이다.
model.bin SHA-256은 69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1로 기존 검증 가중치와 일치한다.
자식 PYTHONPATH는 현재 배포본 src로 지정한다. 앱 wheel을 venv에 중복 설치할 필요가 없고 과거 런타임 코드가 배포 코드를 가리지 않는다.
명시한 LOCAL_STT_PYTHON/MODEL_PATH는 계속 우선한다. 구 외부 연동·MEETING_DEFAULT_PROVIDER 설정 키는 제거되어 extra=ignore로 무시된다.
비밀 설정 파일은 직접 열거나 수정하지 않았다. 합성 비밀 없는 설정 파일에서 구 키 무시와 현재 CPU 스레드 키 적용을 함께 검증했다.

다른 Mac을 준비할 때 backend 루트에서:

```sh
uv venv .stt-runtime/venv --python 3.13
uv pip install --python .stt-runtime/venv/bin/python 'faster-whisper==1.2.1' 'av>=15,<16'
# 인증 토큰을 읽지 않는 별도 공개 가중치 설치. 요청 처리 중 다운로드는 금지한다.
HF_HOME="$PWD/.stt-runtime/public-cache" HF_HUB_DISABLE_IMPLICIT_TOKEN=1 .stt-runtime/venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download('Systran/faster-whisper-large-v3', token=False,
    revision='edaa852ec7e145841d8ffdb056a99866b5f0a478',
    local_dir='.stt-runtime/faster-whisper-large-v3',
    allow_patterns=['config.json','model.bin','preprocessor_config.json','tokenizer.json','vocabulary.json'])
PY
```

## 실제 기본 경로 스모크

운영 계정·DB·환경파일 없이 Settings(_env_file=None)의 기본 경로와 실제 run_worker를 사용했다.
공개 Google FLEURS ko_kr/test 첫 표본(12.48초, CC BY4.0)을 baseline으로 전사했다.
모델 로드 1.361초, 추론 7.189초, 첫 import 포함 벽시계 21.166초, 최대 RSS 3,617,521,664 bytes(십진3.618GB).
출력 66자·1구간, process_exited=true·동일 PID 부재·부모에 ML 모듈이 import되지 않음을 확인했다.
이 1회 실측은 한국어 회의 정확도·장시간 속도 개선이나 OS 물리 메모리 반환 지연의 측정이 아니다.
원시 요약은 [runtime-smoke.json](evidence/remove-clovanote/runtime-smoke.json)이다.

## 검사

backend 전체: **596 passed, 2 xfailed in 128.04s**. 실제 모델을 반복 로드하는 검사가 아니라 단위·DB/API·마이그레이션·프로세스 회수 검사다.
frontend: **351 passed, 1 skipped**, lint/build exit0. 신규/변경 화면 4개 design-lint 검출 없음.
실제 브라우저는 Chrome headless launch가 TargetClosedError로 종료되어 미검증이다. [probe](evidence/remove-clovanote/browser-probe.txt).
초기 Vite 검사는 기본 환경파일 자동 로드를 차단하지 못했다. 내용을 직접 열거나 출력하지 않았으며, 이후 envDir=false 플래그를 추가하고 아래 검사로 다시 확인했다.
환경파일 값이 없는 빌드는 검증용이며 Vercel은 기존 배포 환경을 주입해 빌드한다.

```sh
TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_remove_clova_test_20261015 .venv/bin/pytest -q --tb=short
TTAKKARI_NO_ENV_FILE=1 pnpm lint
TTAKKARI_NO_ENV_FILE=1 pnpm exec vitest run
TTAKKARI_NO_ENV_FILE=1 pnpm build
```

[전체 로그·초기 실패·왕복 검사·설치·스모크](evidence/remove-clovanote/). 모델/원음·사용자 전사·운영 설정은 Git에 넣지 않는다.

## 기획 세션: 배포 후 운영 API 스모크

1. 최종 보고의 backend 전체 SHA를 독립 배포 감독으로 예약한다. 이 작업은 예약·재시작을 호출하지 않았다.
2. succeeded 후 인증된 기존 클라이언트에서 GET /api/meetings/providers: local_whisper 하나, ready, is_default=true인지 확인한다. 구 /api/clovanote/status는 404다.
3. 짧은 분명한 한국어 음성(약5~15초)을 준비한다. 결과함에 공개 평가용 stt-smoke-ko.wav(12.48초)도 제공한다. 개인 원음은 추가로 외부 서비스에 보내지 않는다.
4. POST /api/uploads에 workspace_id와 multipart file을 올려 Artifact ID를 받는다.
5. POST /api/meetings에 title, audio_artifact_id, auto_process:false를 보내 회의를 만든다.
6. POST /api/meetings/{id}/transcribe에 {"provider_id":"local_whisper","auto_analyze":false}를 보낸다. 202를 확인한다. 외부 분석 요청 없이 STT만 검증한다.
7. GET /api/meetings/{id}/stt에서 succeeded와 metadata.metrics.process_exited=true를 확인한다. GET /api/meetings/{id}에서 transcript, /stt/{job}/segments에서 원문·원음 참조를 확인한다.
8. 예전 가져온 회의 1건의 전사·항목·검색·분석 시작 동작이 유지되는지 확인한다. 운영 분석은 기존 정책상 외부 AI를 사용할 수 있으므로 사용자 목적에 맞는 회의만 검증한다.
9. DB 관리 권한이 있는 배포 감독에서 구 두 활성 테이블 부재·retired_integration_records 보관 행 수를 확인한다. 원문을 로그에 출력할 필요는 없다.

로그인 토큰·쿠키·.env 값을 이 보고서에 옮기지 않는다. 평시 모델 프로세스가 남지 않는지는 종료 metadata와 필요 시 해당 worker PID 부재로 확인한다.
