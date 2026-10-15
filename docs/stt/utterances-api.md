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
    "speaker_name": "민수",
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
  "speaker_names": {"SPEAKER_00": "민수"},
  "diarization_status": "succeeded",
  "overlap_detection_status": "unsupported",
  "audio_artifact_id": "UUID"
}
```

- `id`: job 내 전체 목록의 안정된 순번 문자열. 페이지마다 0으로 돌아가지 않는다. job을 바꾸면 별도 ID 공간이다.
- `speaker_id`: 음향 클러스터 ID 또는 null. 작업 간 동일 인물 연결 금지.
- `speaker_label`: 첫 출현 순 `화자 1`, `화자 2` … 또는 null. null은 화면에서 `화자 미상`으로 표시.
- `speaker_name`: 사용자가 해당 job의 화자 ID에 직접 저장한 표시 이름 또는 null. 음향 라벨·자동 인물 식별·검수 상태와 별도 계층이다. `speaker_names`는 페이지 범위와 관계없이 그 job에 저장된 전체 이름 매핑이다. 이름이 없으면 `speaker_label`로 표시한다.
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

## 화자 표시 이름 저장 (2026-10-15 후속)

`PUT /api/meetings/{mid}/stt/{jid}/speakers`

```json
{"names": {"SPEAKER_00": "민수", "SPEAKER_01": "지영"}}
```

200 응답:

```json
{"job_id": "UUID", "names": {"SPEAKER_00": "민수", "SPEAKER_01": "지영"}}
```

- **전체 매핑 교체**다. 생략한 키는 삭제되고 `{"names":{}}`는 이름을 모두 지운다. 한 이름만 수정할 때도 유지할 매핑을 함께 보낸다. 조회는 utterances 응답의 `speaker_names`를 사용한다.
- 키는 해당 job 발화의 실제 `speaker_id`다. `화자 1` 같은 번역된 화면 문구를 키로 보내지 않는다. 값은 앞뒤 공백을 제거한 1~40자 문자열, 최대100개 매핑이다. 빈 이름·제어 문자·알 수 없는 ID·미지 본문 필드·문자열 아닌 값은422다. 이름을 지울 때는 빈 문자열 대신 키를 생략한다.
- 인증401, 없는 job/다른 회의의 job404, 전사 미완료409. 저장은 행 잠금과 단일 트랜잭션으로 직렬화하며 마지막으로 저장한 전체 매핑이 적용된다.
- `result_metadata.speaker_names`에 영속 저장한다. 원본 segment/raw/words·기존 생성 발화·`speaker_id`·`speaker_label`·`identified_person`·`speaker_identities`와 전사·분석은 바꾸지 않는다. 모델·LLM·벡터 재색인을 실행하지 않는다.
- 이름은 **job 범위의 사용자 표시명**이다. 새 재전사/후보 job이나 다른 회의로 자동 복사하지 않으며, 같은 문자열 ID가 나와도 동일 인물로 간주하지 않는다. 이름 저장을 화자 인식 정확도나 검수 완료로 해석하지 않는다.
- 기존 프론트의 브라우저 로컬 이름은 이 API로 자동 이전되지 않는다. 프론트 연결 시 GET utterances의 매핑으로 로드하고 PUT으로 저장한다. 이 후속 작업은 프론트 코드를 수정하지 않았다.

## 현재 채택된 전사와 분석 근거 시각

`GET /api/meetings/{mid}`에 `stt_job_id: string | null`을 추가한다.

- 본문 출처가 `provider:local_whisper`이고, 같은 회의의 성공한 전체 job 중 본문과 전사 텍스트가 일치하는 작업만 채택 대상으로 본다. 우선은 현재 provider 포인터, 그 포인터가 새 대기 작업을 가리키면 본문에 남아 있는 이전 성공 작업을 사용한다. 일치 작업이 여럿이면 현재 포인터 우선, 그다음 생성 시각·ID의 내림차순이다.
- 구간 재처리 child는 성공했어도 현재 채택 작업이 아니다. 붙여넣기·외부 전사·본문과 일치하는 성공 작업 없음은 null이다. 최신 job을 무조건 고르지 않는다.
- 프론트는 이 ID로 utterances를 조회한다. 기존 job 목록과 `candidate_only`도 유지한다.

회의 상세의 결정·액션·논의·미해결 항목과 `PATCH /api/meetings/{mid}/items/{item_id}` 응답에 다음 nullable 필드를 추가한다.

```json
{"evidence": "배포 일정은 목요일로 확정합니다.", "evidence_start": 12.5, "evidence_utterance_id": "7"}
```

`evidence_start`는 원본 음성 기준 초이며 **매칭된 첫 발화의 시작 시각**이다. 인용문 내부 단어의 정확한 시작 시각을 추정하지 않는다. `evidence_utterance_id`는 그 상세 응답의 `stt_job_id` 범위에서만 유효하다.

기존 분석의 `evidence` 인용문과 현재 채택된 표시 발화를 **조회 시 규칙으로 매칭**한다. 기존 분석도 재분석 없이 적용되며 DB 항목·분석 내용·승인 기억을 고치지 않는다. 추가 LLM 호출과 시간 필드용 DB migration은 없다.

매칭 규칙:

- NFC·대소문자·공백·일반 문장부호를 정규화하고, 전사 렌더러의 시각/익명 화자 접두부를 제거한다. 숫자 사이 소수점·쉼표와 부호·비율·퍼센트는 유지한다. 최소6개의 글자/숫자가 필요하다.
- 정규화한 인용문이 그대로 들어 있는 경우만 허용한다. 의역·유사도 점수·요약 내용으로 시각을 추측하지 않는다.
- 최대3개 인접 발화, 발화 사이 공백 최대2초, 전체 범위 최대30초까지 이어 붙일 수 있다. 반복 접기 표시가 있는 발화는 근거로 삼지 않는다.
- 서로 다른 발화 시각에 같은 인용이 나오는 경우, 미매칭·너무 짧은 인용·발화 없음·현재 채택 job 없음은 두 필드 모두 null이다. null을0초로 바꾸지 않는다.
- 링크는 규칙 기반 위치 안내이며 인용의 진실성·전사 정확도·화자 신원을 검증했다는 뜻이 아니다.

## 구간 API 경량 조회

기존 `/api/meetings/{mid}/stt/{jid}/segments`의 기본 응답은 그대로 유지한다. 새 선택 옵션:

- `?fields=light`: segment의 `raw`·`words`를 제외한 기존 필드.
- `?fields=start,end,original_text,speaker_id,verification_status`: 쉼표로 구분한 segment 필드(식별자 세 필드 제외)를 선택한다. 중복은 제거한다. 선택과 무관하게 `id`, `job_id`, `audio_artifact_id`는 항상 포함한다. `raw`·`words`도 명시 선택할 수 있다.
- `fields`를 지정한 응답은 job metadata도 `duration`, `start`, `end`, `speaker_count`, `diarization_status`, `overlap_detection_status`만 남긴다. 전체 전사·화자 turn·발화·재시도 토큰이 metadata로 다시 내려오는 것을 막는다. 값이 원래 없으면 만들지 않는다.
- offset/limit/인증/job 격리는 기존과 같고, 빈 fields·알 수 없는 필드는422다. 필드 선택은 SQL 컬럼 선택에도 반영해 선택하지 않은 원시 구간/단어를 로드하지 않는다.
