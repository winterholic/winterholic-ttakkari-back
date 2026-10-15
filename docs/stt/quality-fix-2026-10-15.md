# 한국어 실회의 반복 환각 교정 (2026-10-15)

운영 회의·job·구간은 읽기 전용으로 조사했다. 원음과 개인 전사는 이 저장소에 넣지 않는다. 원회의와 원래 분석은 유지하며, 로컬 재전사 비교와 배포 후 재전사/분석 요청 경로를 제공한다.

## 근본 원인

운영 저장 메타데이터로 확인한 실행 경로:

`회의 생성 → local_whisper provider.submit → local_stt_jobs → execute_job → runner → faster-whisper 1.2.1 / CTranslate2 4.8.3 / large-v3 int8 CPU → job.transcript → meeting.transcript → 기존 분석 Run`

- 실제 profile=baseline, 한국어, beam=5, word_timestamps=true.
- condition_on_previous_text=true, temperature=0 **한 개**. 초기 prompt 없음.
- VAD=false, VAD 파라미터 없음. 원음 2,549.0973125초와 duration_after_vad가 동일했다.
- 설치된 엔진의 생략 인자 기본값은 compression_ratio_threshold=2.4, log_prob_threshold=-1.0, no_speech_threshold=0.6, prompt_reset_on_temperature=0.5다. 과거 job 메타데이터에는 생략한 임계값이 기록되지 않아 설치 버전의 함수 시그니처와 비교해 확인했다.
- 기존 엔진은 30초 특성 창을 순차 처리하며 이전 토큰을 다음 창에 전달했다. VAD 기반 독립 창이나 청크별 문맥 초기화는 없었다. 이것이 반복을 이후 창에 확산시켰다는 인과 해석은 코드 경로에 근거한 추론이다.
- 압축비가 기준을 넘어도 온도 후보가 0 하나뿐이라 다른 온도 재시도가 없었다. 엔진은 이 결과를 최종 반환할 수 있다.
- 문제 반복 이후 구간의 logprob은 -0.32973~-0.01092, no_speech는 0.00582~0.28568이었다. 엔진의 무음 제거 결합 조건(no_speech>.6 및 logprob<-1)에 해당하지 않았다. 모델의 높은 점수는 실제 발언의 정답 보증이 아니었다.

핵심은 **탐지 후 소비 경로의 결함**이다. 총 1,450개 원본 구간에 high_compression=1,431개, consecutive_duplicate=1,427개가 이미 저장돼 있었다. 그러나 플래그를 읽어 재디코딩하거나 표시용 텍스트를 제한하는 소비자가 없었다. 동일 문장은 1,429회 반복됐으며, 전사 전체 20,389자가 그대로 분석 입력이 됐다. 단일 구간의 토큰 빈도 검사는 짧은 한 문장씩 분리된 반복에 맞지 않지만, 구간 간 반복 탐지는 실제로 작동했다.

## 수정과 보존

[ADR 0028](../adr/0028-stt-quality-and-speaker-utterances.md), [발화 API 계약](utterances-api.md)을 따른다.

- robust 운영 경로: VAD 기본 활성화, 이전 문맥 차단, 독립 최대 25초 창. 2초 이내의 인접 음성을 최대 길이 안에서만 묶고, 내부 VAD의 시각 복원 후 원본 창 오프셋을 더한다.
- 온도 0/.2/.4/.6/.8/1과 2.4/-1/.6 임계값을 명시한다. 원음 경계와 실제 옵션을 결과에 기록한다.
- 경고 구간은 최대 8초 창·상향 온도·총 120초 예산으로 재시도한다. 빈 결과·재시도 오류는 해당 원문을 보존하고 실패 종류를 남긴다. 남는 반복은 표시용에서 접고 `needs_review`로 표시한다.
- DB 원본 구간/단어/raw는 보존한다. 표시용 구간·발화와 원문 전체·재시도 원시 출력은 별도 metadata다. 회의 본문·분석·색인 입력은 검증 표시가 있는 화자별 표시용 텍스트다.
- 과거 설정과 대기 중 baseline 작업도 robust로 승격한다. 과거 profile 재현은 standalone benchmark만 허용한다.

## 화자 분리

확인한 기본 런타임·테스트 캐시·사용자 모델 폴더에는 정식 Community-1 snapshot이 없었다. 공식 HF API의 gated=auto와 모델 카드의 조건 동의를 확인했다. [공식 페이지](https://huggingface.co/pyannote/speaker-diarization-community-1)에 로그인해 연락처 공유·모델 소식 수신 조건을 검토하고 동의한 뒤, [토큰 설정](https://huggingface.co/settings/tokens)에서 해당 저장소 읽기 권한을 준비해 별도 설치 단계로 전체 snapshot을 받는다. 모델은 CC-BY-4.0이다. 토큰 열람이나 게이트 우회는 하지 않았다.

기본 대안은 [SpeechBrain ECAPA](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb)이며 Apache-2.0·게이트 없음이다. revision `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286`의 공개 embedding_model.ckpt(83,316,686바이트)를 `.stt-runtime/ecapa-voxceleb`에 설치했다. Torch/torchaudio 2.8.0·SpeechBrain 1.0.3을 같은 런타임에 준비했다. 원격 YAML을 실행하지 않고 아키텍처와 tensor state만 로드한다.

최대 3초 VAD 창·300ms 패딩·최소 0.5초 임베딩, cosine 그래프 상위 30%·정규화 Laplacian 고유값 간격·고정 시드 군집으로 1~10개 화자를 추정한다. 초기 거리 임계값 방식은 짧은 발화에서 과도하게 분할돼 기각했다. 실제 인원 수·DER는 미측정이다. 이 대안에는 중첩 탐지 기능이 없으므로 overlap=null을 제공한다.

## 재현 명령과 산출물

이번 실행 결과함의 스크립트는 앱 Settings를 import하지 않으며 `.env`를 열지 않는다. 경로는 아래 결과함을 OUT으로 두어 재현한다.

```sh
OUT=/Users/winterholic/.ttakkari/data/outbox/57fd7f2e-823f-4555-ba4b-b681616a5b76
.venv/bin/python "$OUT/audit-meeting-readonly.py"
PYTHONPATH=src .stt-runtime/venv/bin/python -u "$OUT/retranscribe-local.py"
PYTHONPATH=src .venv/bin/python "$OUT/compare-quality.py"
```

운영 audit 출력: transaction_read_only=on, segments=1450, high_compression=1431, consecutive_duplicate=1427, loop_phrase_occurrences=1429.

읽기 전용 스냅샷 `before-private.json`, 수정 출력 `after-private.json`, 비교 집계 `quality-comparison.json`, 화자 발화 `utterances-private.json`, 새 전사 `retranscript-private.txt`를 같은 결과함에 둔다. 개인 데이터 파일은 커밋 대상에서 제외한다. 재시도 raw의 시각은 해당 재시도 창에 상대적이며, 함께 기록한 start/end로 원음 위치를 해석한다.

반복 구간 수는 문장 반복/연속 중복 규칙에 걸린 segment 수다. 반복 길이는 해당 구간의 겹치지 않는 시각 합계이며, 긴 침묵 간격까지 합친 전체 span과 구분한다. 표시는 반복 접기 전 원시 인식과 접은 뒤 결과를 따로 비교한다. 정답 주석이 없으므로 CER/WER/DER·환각률·누락률을 계산하지 않는다.

최종 전체 녹음 측정·검증 결과는 아래에 실행 후 기록한다.
