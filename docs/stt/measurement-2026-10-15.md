# 로컬 STT 1차 측정 보고서 · 2026-10-15

아래 구현·미구현 표는 1차 시점 기록이다. 후속 현황은 [2차 보고](phase2-2026-10-15.md)를 기준으로 확인한다.

선택 로컬 전사 경로를 구현했으며 클로바노트 기본값은 유지한다. 공개 낭독의 CER와 WER가 서로 다른 방향으로 움직여 실험 경로로 자동 전환하지 않는다.
모델: 앱 배정값 gpt-6.1-sol (세션 내부에서 자체 확인은 안 됨)

## 환경·재현 조건

- 관측: macOS 27.0 arm64. M5 Max·64GB는 사용자 제공 정보이며 샌드박스가 하드웨어 sysctl 조회를 제한했다.
- 실제 실행: faster-whisper 1.2.1 / CTranslate2 4.8.3 / PyAV 15.1.0 / ONNX Runtime 1.31.0. CPU int8, 4 threads, 1 worker.
- 가중치: `Systran/faster-whisper-large-v3`, HF revision `edaa852ec7e145841d8ffdb056a99866b5f0a478`.
- model.bin SHA-256: `69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1`.
- `baseline`: 한국어·transcribe·beam 5·temperature 0·word timestamps, VAD off·이전 문맥 on·prompt 없음.
- `improved`: 같은 모델/정밀도 + Silero VAD(무음 500ms·padding 300ms)·이전 문맥 off·선택 prompt. 공개 비교에서는 prompt 없음.
- MLX 0.32.4 / mlx-whisper 0.4.3을 실제 설치했으나 import 시 `No Metal device available`(exit 1). whisper.cpp/Core ML은 미실행. GPU 우열을 추정하지 않는다.

실측 원시 요약은 [runtime.txt](evidence/runtime.txt), [fleurs-benchmark.json](evidence/fleurs-benchmark.json), [private-full-summary.json](evidence/private-full-summary.json)이다.
개인 회의의 원음·전사는 저장소 밖에 보존했으며 이 보고서에는 내용·파일 경로를 넣지 않았다.

## 같은 입력 기준선·실험 경로 비교

[Google FLEURS](https://huggingface.co/datasets/google/fleurs), CC BY 4.0, Korean `ko_kr/test` 첫 3개(row index 0/1/2), 총 32.22초.
원시 참조 `raw_transcription`을 사용했다. 회의·중첩 표본이 아닌 짧은 낭독이다. 모델별로 같은 파일·범위·SHA-256을 사용했다.
빈 정답이 있는 8초 디지털 무음은 CER/WER 분모에 넣지 않았다.
CER는 NFC 후 공백 제외, WER는 공백 토큰, 둘 다 구두점을 보존한다. 전체 편집수/참조 길이의 마이크로 평균이다.

| 지표 | baseline | improved | 판단 |
|---|---:|---:|---|
| CER | 8/132 = 6.06% | 7/132 = 5.30% | 문자 편집 1개 감소 |
| WER | 12/40 = 30% | 14/40 = 35% | 단어 편집 2개 증가 |
| 8초 무음 출력 | 11자 | 0자 | 이 무음 표본에서 VAD 효과 확인 |

전체 정확도 개선으로 판정하지 않는다. 로컬 기본 profile은 baseline이고 실험 profile은 명시 설정·구간 재처리 후보로만 제공한다.
이 표본으로 한국어 회의 정확도나 전체 환각률을 추정하지 않는다.

| profile / 표본 | 원음 초 | 로드 초 | 추론 초 | 해제 요청 초 | 최대 RSS GB | 자식 포함 벽시계 초 |
|---|---:|---:|---:|---:|---:|---:|
| baseline / fleurs-ko-test-0 | 12.48 | 1.258 | 7.142 | 0.0183 | 3.609 | 8.552 |
| baseline / fleurs-ko-test-1 | 10.80 | 1.261 | 6.877 | 0.0177 | 3.609 | 8.288 |
| baseline / fleurs-ko-test-2 | 8.94 | 1.255 | 6.543 | 0.0177 | 3.632 | 7.949 |
| baseline / silence-8s | 8.00 | 1.260 | 5.350 | 0.0179 | 3.609 | 6.764 |
| improved / fleurs-ko-test-0 | 12.48 | 1.258 | 7.311 | 0.0186 | 3.608 | 8.726 |
| improved / fleurs-ko-test-1 | 10.80 | 1.293 | 6.999 | 0.0182 | 3.609 | 8.454 |
| improved / fleurs-ko-test-2 | 8.94 | 1.280 | 6.679 | 0.0203 | 3.609 | 8.124 |
| improved / silence-8s | 8.00 | 1.275 | 0.043 | 0.0195 | 3.609 | 1.474 |

공개 비교 8회 모두 부모가 자식 프로세스 종료를 기다리고 `process_exited: true`를 기록했다.

## 실제 회의 녹음

첫 60초의 초기 smoke는 기준선 추론 19.143초, 실험 경로 8.843초, 최대 RSS 각각 3.806/3.807GB였다.
기준선은 첫 import 11.240초, 실험 경로 import 0.084초였고 실험 경로에 용어 힌트도 제공했다.
따라서 cold/warm import와 prompt까지 통제한 전체 시간 비교가 아니다. 전사 길이는 106/98자로 달라 정답 없이 품질 개선을 판정할 수 없다.
정확도는 두 경로 모두 **미측정: 사람이 확인한 참조 전사 없음**이다.

42분 29.097초 파일 전체는 실험 경로 1회로 처리했다(같은 long-form baseline 실행은 하지 않음).

| 전체 파일 관측 | 값 |
|---|---:|
| 원음 길이 | 2549.097초 |
| VAD가 남긴 길이 | 1911.952초 |
| 모델 로드 | 1.312초 |
| 추론 | 859.294초 = 14분 19.294초 |
| 추론 RTF(추론/원음) | 0.3371 |
| 최대 RSS | 11,336,941,568 bytes = 11.337GB (십진) |
| 객체 해제 요청 | 0.0293초 |
| 출력 | 495개 구간 · 13,386자(공백 포함) |
| 자식 프로세스 | exit 0, 종료 후 동일 PID 부재 확인 |

최대 RSS가 짧은 표본보다 크므로 긴 회의의 메모리 사용을 짧은 표본에서 추정하지 않는다.
모델 해제는 객체 삭제 시간만으로 판정하지 않고 프로세스 종료를 확인했다. OS 파일 캐시는 별도다.
구간·단어 타임스탬프는 모두 원음 길이 안에 있었으나 발화에 맞는 정렬 정확도는 미측정이다.
규칙 플래그는 0개였다. 정답이 없으므로 실제 누락·환각이 없었다는 뜻은 아니다.
VAD가 남긴 길이도 전부 실제 음성이며 제거한 부분은 전부 무음이라고 단정하지 않는다.
사용자 전사·음성은 외부 STT로 보내지 않았다. 분석 LLM은 이번 실측에서 호출하지 않았다.

## 실행 명령과 출력 근거

작업 디렉터리는 backend, STT 전용 Python은 `testing/ttakkari-stt/venv/bin/python`이다.
측정 manifest·원음·원전사 JSON은 저장소 밖 `testing/ttakkari-stt`에 둔다. manifest는 개인 파일 경로를 포함하므로 Git에 넣지 않았다.

```sh
.venv/bin/python -m ttakkari.meetings.local_stt.benchmark \
  --manifest /Users/winterholic/testing/ttakkari-stt/manifest.json \
  --model /Users/winterholic/testing/ttakkari-stt/hf/hub/models--Systran--faster-whisper-large-v3/snapshots/edaa852ec7e145841d8ffdb056a99866b5f0a478 \
  --python /Users/winterholic/testing/ttakkari-stt/venv/bin/python \
  --output docs/stt/evidence/fleurs-benchmark.json
# baseline fleurs-ko-test-0 seconds 8.552
# improved fleurs-ko-test-0 seconds 8.726
# improved silence-8s seconds 1.474

/Users/winterholic/testing/ttakkari-stt/venv/bin/python \
  -m ttakkari.meetings.local_stt.worker \
  /Users/winterholic/testing/ttakkari-stt/full-improved.json \
  < /Users/winterholic/testing/ttakkari-stt/full-request.json
# exit 0; metadata inference_seconds=859.2941369158216
# peak_rss_bytes=11336941568; segments=495
```

[전체 pytest 로그](evidence/pytest-full-release.txt), [로컬 35개 검사](evidence/local-tests-final.txt),
[프론트 검사](evidence/front-full.txt), [빌드](evidence/front-build.txt), [린트](evidence/ruff-final.txt),
[마이그레이션 로그](evidence/migrations.txt), [새 테이블 정합성 검사](evidence/schema-check.txt).

```text
TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_stt_test_20261015 .venv/bin/pytest -q --tb=short
565 passed, 2 xfailed in 126.58s (0:02:06)
TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_stt_extra_20261015 .venv/bin/pytest tests/test_local_stt*.py -q --tb=short
35 passed in 8.04s
pnpm exec vitest run
353 passed | 1 skipped (354)
pnpm lint / pnpm build
exit 0 / exit 0
.venv/bin/ruff check <변경한 Python 파일>
All checks passed!
```

가짜 worker로 DB/API·동일 작업 멱등성·구간 후보 불변·자동 분석·PMS·벡터·재시작 복구를 테스트했다.
실제 subprocess로 정상 종료·실패·시간 초과·취소 회수를 확인했다. 부모 사라짐 감시는 단위 테스트했으며 운영 강제 종료 지연은 미측정이다.
새 스키마를 독립 DB에 처음부터 설치하고 `0025 → 0024 → 0025`를 실행했다. 새 두 테이블은 메타데이터와 일치하며, 의도적으로 누락한 column도 탐지했다.
전체 `alembic check`는 기존 수동 검색 인덱스 12개가 ORM 메타데이터에 없다는 차이를 보고했다. 새 STT 테이블 차이는 없으며 기존 인덱스를 삭제·변경하지 않았다.

조회가 먼저 전사를 반영한 뒤 자동 분석 전에 재시작된 경우도 추가로 재현하여, 재추론 없이 기존 분석을 이어가는 회귀 검사로 고정했다.

최초 전체 검사에서 기존 Clova fake fixture의 실제 Chrome PID 조회가 sysctl 권한으로 실패했다.
fake fixture에서 OS 조회도 격리했다. 두 번째 전체 검사에서는 모델 정책 테스트가 만든 두 fake worker 중 하나를 기다리지 않아 teardown의 killpg가 권한 오류를 냈다.
두 worker의 완료를 모두 검증하도록 보강했다. 최종 전체 검사는 위와 같이 통과했다.
새 검사에서 누락 기능을 먼저 확인한 근거와 초기 격리 오류 요약은 [red-and-isolation.txt](evidence/red-and-isolation.txt)에 남겼다.

## 미측정·미구현

| 항목 | 이유와 상태 |
|---|---|
| 실제 한국어 다중 화자 회의 CER/WER | 사람이 확인한 정답 없음. 클로바 전사는 참조일 뿐 정답 아님. |
| DER·화자 수·동일 화자 연결·실제 인물 식별 | Community-1 가중치 접근 조건 동의/로컬 준비 전. 정렬 알고리즘·분리 스키마만 구현. |
| 중첩 탐지·분리 후 성능 | 분리 엔진과 주석 표본 미구현. 중첩 여부는 unknown/null. |
| 누락률·환각률 | 시간별 참조·사람 판정 없음. 생성 무음의 출력 문자 수는 별도 관측. |
| 1시간 파일의 실제 처리 시간 | 42분 29초까지 실행했으며 1시간 입력은 없음. 단순 비례 추정하지 않음. |
| 강제 정렬 정확도 | Whisper word timestamps만 사용, forced alignment 모델 미실행. |
| 원본 reference 구현/FP16·FP32 기준선 | CT2 int8로 두 profile을 비교했으며 원본 OpenAI 구현·다른 정밀도 결과는 미측정. |
| GPU 최대 메모리·로드·해제·엔진 비교 | 이 세션의 MLX Metal 접근 불가, whisper.cpp/Core ML 미실행. |
| 물리 메모리 해제 지연 | 객체 삭제 시간과 종료는 확인했으나 OS 전체 메모리 변동은 미측정. |
| 브라우저 실제 화면 | Chrome launch SIGABRT/권한 제한. DOM 상호작용 테스트·lint·build는 통과했으나 실화면은 미검증. HTML 보고서 검사기도 bundled Chromium 미설치로 미실행. |

## 전환 판정

로컬 API·선택 UI는 추가했지만 운영 재시작·배포·기본 provider 전환을 하지 않았다.
기획 세션에서 선택 의존성·로컬 모델 설정·migration 0025 배포를 처리해야 실제 앱에서 켤 수 있다.
화자·중첩·GPU·사람 검수 표본의 결과가 갖춰지기 전에는 클로바노트를 제거하지 않는다.
