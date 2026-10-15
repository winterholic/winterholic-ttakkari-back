# ADR 0028 — 로컬 전사 반복 회복과 화자별 발화

- 날짜: 2026-10-15
- 상태: 채택. 운영 배포는 별도.
- 선행: ADR 0025·0026·0027

## 문제와 근거

운영 baseline 작업에서 VAD=false, condition_on_previous_text=true, temperature=0이 저장돼 있었다.
반복 구간에 압축비·연속 중복 플래그가 있었지만, 플래그는 검토 힌트였고 원문을 그대로 회의 전사·분석 입력으로 전달했다.
온도 후보가 한 개여서 엔진의 압축비 임계값을 넘겨도 다른 온도로 재시도하지 않았다. 이전 30초 창의 반복 문맥이 다음 창으로 이어졌다.
실제 원인 근거와 전후 측정은 [품질 기록](../stt/quality-fix-2026-10-15.md)에 둔다. 개인 원음·전사는 저장소 밖에 보존한다.

## 결정

1. 운영 job은 robust로 실행한다. 이전 설정·대기 중 baseline job도 안전한 경로로 승격한다. standalone benchmark에서만 과거 profile을 허용한다.
2. Silero VAD 기본 활성화, 음성 최대 25초·최소 침묵 500ms·패딩 300ms. 인접 음성은 간격 2초 이하이면서 전체 25초 이하일 때만 묶는다. 각 창은 독립 디코딩하고 문맥을 넘기지 않는다. 엔진 내부 VAD가 짧은 침묵을 처리하며 반환 시각에 창의 원본 오프셋을 더한다. 의도적 중복 패딩은 추가하지 않는다.
3. 온도 0→.2→.4→.6→.8→1, compression_ratio 2.4, log_prob -1, no_speech .6을 명시한다. 임계값은 확률적 정확도 보증이 아니다.
4. 압축비·반복 경고는 최대 8초 창과 상향 온도로 재디코딩한다. 작업당 재시도 원음 120초 예산. 예산 초과·빈 재시도·실패는 조용히 원문을 제거하지 않으며 상태와 원본을 남긴다. 남는 반복은 표시용에서 접고 검증 필요로 표시한다. 정답 없는 자동 재인식을 검수 완료로 간주하지 않는다.
5. DB 원본 segment와 raw는 같은 순서·개수로 보존한다. 별도 post_retry_segments(접기 전), presentation_segments, quality_retries, original_text, utterances를 job JSON metadata에 둔다. 최초·재시도 뒤·접기 뒤 지표를 분리한다. 접기 전 반복 탐지 구간의 시각 합계 15초 이상 또는 문자 합계 120자 이상이면 검증 필요로 판정하고 job/회의의 전사 단계를 실패 처리한다. 원시 증거·후보 본문을 보존하되 회의 본문으로 채택하거나 자동분석에 전달하지 않는다. 두 번의 짧은 정상 반복은 유지한다. 회의·분석·벡터는 이 차단 판정을 통과한 텍스트만 받는다. 이 품질 차단에 새 스키마 migration은 없다.
6. 명시한 정식 로컬 Community-1 snapshot을 우선 사용한다. 준비되지 않은 기본 환경에서는 공개 SpeechBrain ECAPA + VAD + 스펙트럴 군집을 사용한다. 원격 YAML 실행·요청 중 다운로드·상주 모델·참석자명 기반 인물 식별을 하지 않는다. 화자 수는 추정 클러스터 수다.
7. ECAPA 대안에는 중첩 탐지 기능이 없다. overlap은 null, overlap_detection_status는 unsupported다. Community-1의 일반 구간으로만 실제 중첩을 계산한다.
8. [화자별 발화 API 계약](../stt/utterances-api.md)을 제공한다. 단어 시각으로 화자를 정렬하고 모호한 음성을 여러 화자에게 복제하지 않는다.
9. 재전사는 같은 원음을 참조하는 새 회의로 만든다. 원회의 전사·분석·승인된 기억을 유지한다. 자동 재분석은 명시한 요청에서만 실행한다.

## 대안과 한계

- 반복 플래그만 늘리는 방법은 이미 플래그가 있던 이번 결함을 해결하지 못한다.
- 자동 접기는 실제 반복 발언에도 적용될 수 있다. 원본·재시도를 남기고 검증 필요 배지를 제공한다.
- Community-1은 CC-BY-4.0 공개 모델이나 HF에서 연락처 공유 조건 동의가 필요하다. 게이트 우회 복제본을 사용하지 않는다. [공식 모델 카드](https://huggingface.co/pyannote/speaker-diarization-community-1), [토큰 설정](https://huggingface.co/settings/tokens).
- ECAPA는 Apache-2.0이며 게이트가 없다. [모델 카드](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb), [공식 AMI diarization recipe](https://github.com/speechbrain/speechbrain/blob/develop/recipes/AMI/Diarization/hparams/ecapa_tdnn.yaml). 장시간 회의·원거리·짧은 발언·중첩은 사람 검수가 필요하다. 숫자 정답 주석 없이는 WER/CER/DER나 화자 수 정확도를 보고하지 않는다.
- CPU 비용과 모델 시작 비용은 남는다. 요청별 자식 프로세스 종료로 평시 메모리 수명을 제한한다.
