# 화자별 발화 API 계약 (2026-10-15)

프론트 동시 구현용 계약. 새 백엔드 배포 뒤 사용할 수 있다. 단위는 모두 **원본 녹음 시작 기준 초**다. 구간 재처리도 0부터 다시 시작하지 않는다.

## 읽기

기존 `GET /api/meetings/{mid}/stt`의 jobs(최신 순)에서 작업 ID를 고른다.
`GET /api/meetings/{mid}/stt/{jid}/utterances?offset=0&limit=100`

사용자 인증 필요(미인증 401). 다른 회의 job/없는 job은 404. 전사 미완료는 409.
`offset >= 0`, `1 <= limit <= 500` 위반은 422. 범위를 넘은 offset은 빈 배열.

```json
{
  "job_id": "UUID",
  "utterances": [{
    "id": "0",
    "start": 12.5,
    "end": 15.9,
    "speaker_id": "SPEAKER_00",
    "speaker_label": "화자 1",
    "text": "발언 내용입니다.",
    "verification_status": "unverified",
    "overlap": null,
    "flags": [],
    "segment_positions": [0]
  }],
  "offset": 0,
  "limit": 100,
  "total": 1,
  "has_more": false,
  "speaker_count": 2,
  "diarization_status": "succeeded",
  "overlap_detection_status": "unsupported",
  "audio_artifact_id": "UUID"
}
```

- `id`: job 내 전체 목록의 안정된 순번 문자열. 페이지마다 0으로 돌아가지 않는다. job을 바꾸면 별도 ID 공간이다.
- `speaker_id`: 음향 클러스터 ID 또는 null. 작업 간 동일 인물 연결 금지.
- `speaker_label`: 첫 출현 순 `화자 1`, `화자 2` … 또는 null. null은 화면에서 `화자 미상`으로 표시.
- `speaker_count`: 추정 클러스터 수. 미실행/실패의 경우 null, 성공한 무음 입력은 0. 실제 참석 인원이나 검증된 정답이 아니다.
- `verification_status`: `unverified`(자동 인식, 미검수), `needs_review`(검증 필요). 기계 재인식 성공만으로 검수 완료로 바꾸지 않는다. UI는 알 수 없는 향후 값을 검증 필요로 취급한다.
- `overlap`: true=겹침 관측, false=지원 모델이 해당 범위에서 겹침을 관측하지 않음, null=겹침 탐지 불가/미실행. ECAPA 대안에서는 **항상 null**. false로 바꾸지 않는다.
- `diarization_status`: `succeeded`, `not_run`, `unavailable`, `failed`.
- `overlap_detection_status`: `succeeded`, `unsupported`, `not_run`.
- `flags`: 품질·화자 정렬 경고. `quality_retry`, `repetition_folded`, `speaker_ambiguous` 등. 알려지지 않은 플래그가 있어도 텍스트 표시는 유지한다.
- `segment_positions`: **표시용 구간**의 위치. 원본 DB `/segments` position과 일치한다고 가정하지 않는다. 품질 재처리로 표시용 구간이 바뀔 수 있다.
- 단어 시각이 있으면 화자 변경에서 발화를 나누고, 없으면 구간 전체를 보수적으로 정렬한다. 모호한 음성을 여러 화자에게 복제하지 않는다.
- 일반 발화는 최대 25초 묶음. 반복 접기는 긴 문제 범위를 한 항목으로 표시할 수 있다. 접힌 문장은 해당 전체 구간의 확정 발언이 아니므로 `검증 필요` 배지를 필수로 표시한다.
- `utterances[].text`는 반복을 접은 **표시·분석용** 텍스트. 원본 증거는 기존 `/segments`의 `original_text`, `raw`, job metadata의 `original_text`·`quality_retries`에 별도 보존한다.
- 이전 job도 조회 가능하지만 소급 재인식하지 않는다. 기존 전사를 표시용으로 정렬할 뿐 품질 교정/화자 추론은 새 job이 필요하다.

녹음 재생은 기존 audio artifact 다운로드 API를 사용하고 start로 seek한다. 원음 바이트를 이 응답에 포함하지 않는다.

## 원본 보존 재전사·재분석

`POST /api/meetings/{mid}/retranscribe {"auto_analyze": false}` → 202, 기존 회의 상세와 같은 응답 형식.

- **새 회의 ID**를 반환한다. 같은 원음을 참조하고 제목에 `(재처리)`를 붙인다.
- 원회의의 전사·분석·항목·제안·승인한 기억을 유지한다. 재처리 회의에서 `robust` job을 새로 만든다.
- `auto_analyze:false`가 기본이다. 새 회의가 `transcribed`가 된 뒤 `POST /api/meetings/{new_mid}/analyze`로 기존 분석을 실행한다.
- 처음부터 재전사 후 자동 분석을 원하면 `auto_analyze:true`. 분석은 기존 앱의 모델 선택 정책을 따른다.
- 분석·색인 입력은 반복을 접고 `[검증 필요]`를 표시한 화자별 텍스트다. 새로운 결정이나 기억의 승인은 기존 절차를 따른다.
- 원음 없음/런타임 미준비 409. 본문 미지 필드 422. 자동으로 과거 분석을 덮어쓰는 API가 아니다.
- 기존 120초 구간 후보 API `/stt/{jid}/reprocess`도 유지한다.

## 기본 로컬 화자 엔진

명시한 Community-1 로컬 snapshot이 있으면 이를 사용한다. 없으면 `.stt-runtime/ecapa-voxceleb/embedding_model.ckpt`의 공개 SpeechBrain ECAPA로 VAD 구간 임베딩+클러스터링한다. 두 엔진 모두 요청별 자식 프로세스에서 실행 후 종료한다. 파일이 없거나 실행 실패 시 상태와 사유를 남기며 화자 미상 목록을 제공한다. 요청 중 모델을 다운로드하지 않는다.

Community-1 사용 동의: https://huggingface.co/pyannote/speaker-diarization-community-1 에 로그인해 연락처 공유·모델 소식 수신 조건을 검토하고 동의한다. 이후 https://huggingface.co/settings/tokens 에서 해당 저장소 읽기 권한 토큰을 만들고, 사용자가 인증한 별도 설치 절차로 전체 snapshot을 다운로드한다. 토큰은 채팅/문서에 붙이지 않는다. 모델은 CC-BY-4.0이며 배포 시 저작자 표시를 유지한다.

ECAPA: https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb (Apache-2.0, gate 없음), revision `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286`. 최대 3초 VAD 창, 최소 0.5초, cosine 유사도 상위 30% 그래프·고유값 간격으로 1~10개 클러스터 추정·고정 시드 스펙트럴 군집. 짧은 발언·같은 음색·원거리·중첩의 화자 혼동 가능성이 있으며, 정답 주석 없이는 DER/화자수 정확도를 제시하지 않는다.
