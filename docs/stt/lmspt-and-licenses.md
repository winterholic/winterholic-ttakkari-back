# LM-SPT 적용 판단과 라이선스 (2026-10-15 확인)

## LM-SPT: 이번 STT 실행 경로에서는 제외

검토 원문은 [arXiv 2506.16738v1](https://arxiv.org/html/2506.16738v1),
[Kakao 공식 코드](https://github.com/kakao/lmspt), [공식 가중치 카드](https://huggingface.co/kakaocorp/lmspt)다.
코드는 실제로 clone하여 commit `965ffb815c580e62e4d36f20b797afcbabbc7259`의 다음 경로를 읽었다.

| 실제 읽은 코드 | 확인한 역할 |
|---|---|
| `lmspt/model_codec/lmspt_model.py:encode/decode/aux_semantic_decode` | 별도 semantic/acoustic encoder, 양자화, 음성 복원. 텍스트 전사 API가 아니다. |
| `lmspt/model_codec/teacher/whisper.py:WhisperASRTeacher.forward` | frozen Whisper encoder의 원음 특징과 semantic 복원음 특징 사이 MSE/L1 손실. |
| `lmspt/model_codec/trainer.py:SemanticResynthesisDistillationTrainer` | semantic 복원음에 teacher 손실을 적용하는 학습 단계. |
| `lmspt/infer/get_model.py`, `infer/conf/model/lmspt_12d5hz_16384_4096_8vq.yaml` | 공개 codec checkpoint 로드, 12.5Hz 토큰, 24kHz 입력. |

논문은 의미 토큰만으로 복원한 음성과 원음의 ASR 특징을 가깝게 학습한다. acoustic stream은 화색·운율 등 잔차를 담당한다.
따라서 압축된 토큰을 Whisper에 바로 넣을 수 없다. decoder로 음성을 다시 만든 뒤 STT를 하면 추가 추론과 신호 변형이 생긴다.
짧은 token sequence는 별도로 학습한 SLM의 입력 효율에 유리하지만, 기존 Whisper의 입력 길이·전사 비용을 그대로 줄여 주지는 않는다. 논문은 25/12.5/6.25Hz를 비교하며 프레임률을 낮출수록 토큰 수가 줄어드는 조건을 다룬다. 이는 waveform→Whisper 호출의 가속 배율이 아니다.
논문의 SLM 결과를 한국어 다중 화자 회의의 Whisper 개선 수치로 옮길 수 없다. 초기 논문 실험과 현재 공개 checkpoint의 학습 데이터·구성을 구분한다.

이번 판단은 **재합성 전처리를 넣지 않는다**이다. 한국어 회의의 의미·고유명사·겹친 발언을 더 보존한다는 실측이 없고,
Whisper teacher 특징 유사도만으로 문구가 보존됐다고 보증할 수도 없다. 비용 최소화·원음 보존 원칙에도 맞지 않는다.
게다가 공개 가중치는 비상업 라이선스다. 코드 Apache-2.0과 가중치 CC BY-NC 4.0을 혼동하지 않는다.

응용할 만한 원칙은 의미와 음향 지표를 분리하고 압축·재처리 전후 같은 원음 기준으로 검증하는 것이다.
구현에는 이를 '원전사와 후보 분리 + 참조 기반 CER/WER 비교'로 반영했다. codec 모듈·학습·가중치는 의존성에 추가하지 않았다.
LM-SPT 추론 속도·재합성 STT 정확도는 **미측정: 채택 대상에서 제외하여 가중치 로드·추론하지 않음**이다.

## 화자·중첩 검토

[Community-1 공식 카드](https://huggingface.co/pyannote/speaker-diarization-community-1)는 일반 화자 구간과 exclusive diarization을 제공한다.
겹침을 지운 exclusive 결과만으로 중첩 여부를 판단하면 안 된다. 원래 화자 구간의 동시 교집합을 보존하고,
STT 시간축과 별도로 정렬해야 한다. 이번에는 이 정렬·모호성 알고리즘과 null 스키마만 검증했다.

Community-1은 게이트 조건 동의·접근 허가가 필요한 모델이다. 현재 세션은 토큰·HF 로그인 상태를 읽지 않았고 모델을 받지 않았다.
실제 화자 수 추정·동일 화자 연결·DER·Mac MPS/CPU 성능은 미측정이다. 화자 이름은 클러스터에 자동 붙이지 않는다.
후속 실행을 위해서는 사용자가 모델 카드 조건에 동의하고, 기획 세션이 정식 권한으로 모델을 로컬에 준비해야 한다.
토큰 값을 이 세션이나 보고서에 전달할 필요는 없다. 아직 pyannote용 설정·운영 어댑터를 추가하지 않았다.

중첩 발화 음원 분리는 미구현이다. 실측 전에는 전체 녹음에 분리 모델을 적용하지 않는다.
후속 기준은 중첩 주석이 있는 구간만 분리·재전사하고 원결과와 비교하여 개선 시만 채택하는 것이다.

## 라이선스와 배포 상태

상용 허용 여부는 모델 카드/라이선스의 조건부 허용을 뜻한다. 고지·배포 조건까지 면제된다는 뜻은 아니다.

| 구성 | 확인한 라이선스·상용 조건 | 이번 사용 |
|---|---|---|
| [OpenAI Whisper](https://github.com/openai/whisper/blob/main/LICENSE), [Systran large-v3 변환 가중치](https://huggingface.co/Systran/faster-whisper-large-v3) | MIT, 저작권·허가 고지 보존 조건으로 상용 사용 가능 | large-v3 snapshot `edaa852e…` 실제 사용 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper/blob/master/LICENSE) 1.2.1 | MIT, 고지 유지 | 선택 extra `stt` |
| [CTranslate2](https://github.com/OpenNMT/CTranslate2/blob/master/LICENSE) 4.8.3 | MIT, 고지 유지 | CPU int8 실제 실행 |
| [Silero VAD](https://github.com/snakers4/silero-vad/blob/master/LICENSE) | MIT, 고지 유지 | 엔진에 포함된 ONNX 모델 사용 |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime/blob/main/LICENSE) 1.31.0 | MIT, 고지 유지 | VAD 런타임 |
| [PyAV](https://github.com/PyAV-Org/PyAV/blob/v15.1.0/LICENSE.txt) 15.1.0 | BSD-3-Clause. 링크된 FFmpeg는 별도 조건 | 오디오 디코딩. 19.x 비호환 확인 |
| [FFmpeg](https://ffmpeg.org/legal.html) (PyAV wheel 포함) | 기본 LGPL 2.1+, 빌드에 따라 GPL. 설치 바이너리는 LGPL v3+로 보고하나 libx264/x265 구성 플래그도 존재. wheel 재배포 시 실제 링크·소스·고지 검토 필요 | 개인 Mac에서 설치된 wheel 사용, 바이너리 재배포하지 않음 |
| [MLX/MLX Whisper](https://github.com/ml-explore/mlx-examples) 0.32.4/0.4.3 | MIT | 별도 조사 venv 설치. Metal 불가로 import 실패; 운영 의존성 제외 |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) | MIT | 미설치·미측정, 운영 의존성 제외 |
| [pyannote.audio](https://github.com/pyannote/pyannote-audio/blob/develop/LICENSE) / [Community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) | 코드 MIT / 가중치 CC BY 4.0, 모델 게이트 조건 동의 필요. 출처 표시 조건 | 실제 추론 미실행 |
| [LM-SPT 코드](https://github.com/kakao/lmspt) / [가중치](https://huggingface.co/kakaocorp/lmspt) | Apache-2.0 / **CC BY-NC 4.0**, 공개 가중치의 상업 사용 허용 안 됨 | 소스 검토만, 의존성·가중치 제외 |
| [Google FLEURS](https://huggingface.co/datasets/google/fleurs) | CC BY 4.0. Google FLEURS 및 [논문](https://arxiv.org/abs/2205.12446) 출처 표시 | 한국어 test 앞 3개, 공개 낭독. 회의 데이터가 아님 |

FFmpeg 라이선스 표기는 설치된 `libavcodec`의 `avcodec_license()`/`avcodec_configuration()`도 직접 호출해 확인했다.
런타임 문자열만으로 빌드 전체의 재배포 적합성을 단정하지 않는다.
AI Hub 한국어 회의·대화 데이터는 [공식 포털](https://www.aihub.or.kr/)의 데이터별 이용 조건·접근 절차를 먼저 확인해야 하며 이번에는 받지 않았다.
사용자 클로바 전사는 참조 비교에 쓸 수 있지만 사람이 확인한 정답과 같은 열에 합치지 않는다. 이번 수치에는 쓰지 않았다.
