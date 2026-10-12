# ADR 0010. 회의록: 전사 → 구조화 → 승인 후 맥락 반영

- 상태: 채택 (2026-10-13)
- 결정자: 사용자. 회의 녹음·전사에서 결정과 후속 작업을 뽑아 에이전트가 이후 작업에 참고하게 한다.
- 범위: 클로바노트 연동을 뺀 나머지. 자체 STT 서버·GPU·별도 API 키 경로·별도 서비스는 두지 않는다.

## 재사용하는 것

| 필요 | 쓰는 것 |
|---|---|
| 녹음 파일 보관 | 기존 `POST /api/uploads` → Artifact(`source=upload`, 보관함 files). 회의는 `audio_artifact_id` 만 가진다. |
| LLM 분석 | 기존 Run·엔진 체계. 분석 전용 작업 세션(`kind=worker`, 부모 없음, 보관 처리)을 만들고 `queue_run` 으로 돌린다. |
| 계정 범위 | 회의 `scope` 가 세션 `pool` 이 된다. 회사 회의는 회사 계정만 쓴다. |
| 맥락 반영 | `ctx_memories`(kind=decision), 프로젝트 `ctx_entities`. 새 엔터티·출처·색인 경로는 만들지 않는다. |
| 알림 | `push.notify`. 분석이 끝나면 한 번 보낸다. |
| 에이전트 읽기 | 공통 MCP 서버에 읽기 도구 3개(`list_meetings`, `get_meeting`, `search_meeting_items`). |

## 데이터 모델 (0016)

- `meetings`: 제목, `scope`(personal/work), `status`, `entity_id`(프로젝트, SET NULL), `held_at`, `participants`,
  `source`(audio/text), `audio_artifact_id`(SET NULL), `provider`/`provider_job_id`(전사 요청 기록),
  `transcript`+`transcript_source`(paste/file/provider:<id>), `summary`, `analysis_run_id`·`analysis_session_id`,
  `failed_stage`·`error`, 시각.
- `meeting_items`: `kind`(discussion/decision/action/open_issue), `position`, `text`, `detail`, `owner`, `due`,
  `evidence`(전사에서 근거가 된 짧은 인용), `status`(open/done/cancelled. 액션·미해결 쟁점에만 의미).
- `meeting_proposals`: `kind`, `status`(pending/approved/rejected), `item_id`(SET NULL), `payload`(JSONB), `reason`, `result`, 시각.

회의 정본은 이 세 표다. 에이전트는 승인 전에도 읽을 수 있다(사용자가 직접 넣은 자료의 구조화일 뿐 맥락 도서관을 바꾸지 않는다).
회의를 지워도 이미 승인되어 만들어진 기억은 남는다. `source_ref=meeting:<id>` 로 출처를 추적한다.

## 상태 머신

```
uploaded ──request_transcription──▶ transcribing ──provider 완료──▶ transcribed
   │ (전사 텍스트 직접 입력)                                           ▲
   └──────────────────────────────────────────────────────────────────┘
text 로 만들면 처음부터 transcribed
transcribed|analyzed|review|failed(analysis) ──analyze──▶ analyzing ──Run 성공·검증 통과──▶ review(대기 제안 있음) | analyzed
analyzing ──Run 실패·출력 검증 실패──▶ failed(failed_stage=analysis)
transcribing ──provider 실패──▶ failed(failed_stage=transcription)
review ──마지막 대기 제안 처리──▶ analyzed
```

전이는 `meetings/pipeline.py` 의 `transition()` 한 곳에서만 일어나고 허용되지 않는 전이는 `PipelineError` 다.
`analyzing` 은 서버가 재시작돼도 Run 상태를 보고 이어진다. 완료 처리는 멱등이며(`reconcile`) 백그라운드 대기 태스크와
목록·상세 조회 양쪽에서 호출한다. 그래서 `main.py` 에 별도 루프를 달지 않는다.

## 전사 Provider 인터페이스

`meetings/providers.py`. 분석 로직은 전사 텍스트만 받으므로 Provider 를 바꿔도 분석을 고치지 않는다.

```python
class TranscriptionProvider(Protocol):
    id: str; label: str
    def availability(self) -> ProviderAvailability      # ready | unconfigured | not_implemented, 사용자용 문구
    async def submit(self, audio: AudioInput) -> str    # provider_job_id
    async def poll(self, job_id: str) -> TranscriptionPoll  # pending | done(text) | failed(reason)
```

- 등록된 것은 `clovanote` 하나이고 `not_implemented` 다. `submit`/`poll` 은 `ProviderNotAvailable` 을 던진다.
  네이버 UI·자동화 가능 범위·업로드 제한은 가정하지 않는다.
- 설정은 `TTAKKARI_CLOVANOTE_PROFILE_DIR`(추후 Playwright 브라우저 프로필 경로) 하나뿐이다. 인증 정보는 코드·DB·API 응답에 두지 않는다.
  API 는 경로를 내보내지 않고 "설정됨/안 됨"만 알린다. 로그인은 사람이 프로필에서 한 번 하고, 따까리는 쿠키·비밀번호를 다루지 않는다.
- 추후 구현은 기존 브라우저 제어 기능과 합쳐 `providers.py` 에 어댑터 하나를 채우는 일이다. 파이프라인은 `sync_transcription()` 이 `poll` 결과를
  `ingest_transcript()` 로 넘기는 것까지 이미 구현·테스트한다(가짜 Provider 로).

## 분석 (Run)

1. 서버가 후보 맥락을 모은다: 연결된 프로젝트의 기억 중 결정·지침·사실, 제목·프로젝트명으로 한 `store.recall`(기억만),
   같은 프로젝트의 이전 회의에서 아직 열린 액션 아이템. 각각 `M1..`, `A1..` 짧은 참조를 붙여 최대 40개로 자른다.
2. 프롬프트에 지침·JSON 스키마·후보·전사를 담아 Run 을 만든다. 전사는 `meeting_transcript_max_chars`(기본 12만 자)까지다.
   넘으면 접수 단계에서 거절한다(인자 길이·컨텍스트 한도, 나눠서 올리기).
3. 결과(`run.result_text`)에서 JSON 을 꺼내 pydantic 으로 검증한다. 스키마: `summary`, `participants`, `discussions[]`,
   `decisions[]`(+`conflicts[]`), `action_items[]`(+`conflicts[]`), `open_issues[]`. 충돌 항목은 `{ref, reason, change}` 이고
   `ref` 는 후보에 있는 것만 인정한다(없는 참조는 버리고 `meeting.error` 가 아니라 경고 로그만).
   검증에 실패하면 `failed(analysis)` 이고 저장은 하지 않는다. 다시 분석할 수 있다.
4. 검증된 결과를 `meeting_items` 로 저장하고 제안을 만든다. 같은 회의를 다시 분석하면 항목과 **대기 중** 제안만 교체한다.
   이미 승인·거절된 제안은 남긴다.

LLM 은 상충 "후보"를 지목할 뿐이고 어떤 기억·액션도 직접 바꾸지 못한다. 분석 Run 은 읽기 도구(recall 등)를 쓸 수 있으나
프롬프트가 파일 쓰기를 금지하며, 반영은 서버가 승인 API 에서만 한다.

## 맥락 도서관 연동과 승인

| 회의 산출 | 맥락 도서관 | 근거 |
|---|---|---|
| 결정 사항 | 제안 `add_memory`(kind=decision). 기존 기억과 충돌하면 `supersede_memory` | 결정은 시간이 지나도 유효한 사실이라 기억이 맞다 |
| 액션 아이템 | 기억으로 복사하지 않는다. 회의 DB 가 정본이고 MCP 로 `open` 만 찾는다 | 상태가 변하는 값이라 기억에 박으면 낡는다 |
| 기존 액션과 충돌(취소·변경) | 제안 `update_action_item`(status=cancelled 또는 text 변경) | "기존 태스크와 충돌" 은 이전 회의의 열린 액션이다 |
| 미해결 쟁점·논의·요약 | 회의 DB 에만 | 사실이 아니라 진행 상태다 |

- 새 기억의 `scope` 는 회의 `scope`, `entity_ids` 는 연결된 프로젝트, `source="meeting"`, `source_ref="meeting:<id>#<item>"`,
  본문은 "[회의 2026-10-13 '제목'] …" 로 혼자 읽어도 되는 문장이다. 같은 본문(정규화)이 이미 있으면 제안을 만들지 않는다.
- `supersede_memory` 승인: 새 결정 기억 추가 + 기존 기억 보관(`archived_at`). 사용자가 직접 입력한 기억도 사용자 승인으로는 보관할 수 있다.
- 제안의 승인·거절은 사용자 UI 전용 API(`/api/meetings/{id}/proposals/{pid}/approve|reject`)만 한다. MCP 에는 쓰기 도구가 없다.
  에이전트의 `forget` 은 `source=meeting` 기억을 지우지 못한다(`manual` 과 같은 취급, 사용자가 승인한 결정이라서).
- 승인 시점에 대상(기억·액션)이 이미 사라졌거나 바뀌었으면 `409` 로 알리고 반영하지 않는다. 제안은 `pending` 으로 남는다.
- 프로젝트 연결의 범위 규칙: 회사 회의는 work/shared 엔터티만, 개인 회의는 personal/shared 엔터티만 연결한다.
- 대기 제안이 있으면 `review`, 모두 처리하면 `analyzed`.

## 한계·남긴 것

- 한 회의 전사는 12만 자까지. 더 길면 나눠서 올린다(분할 분석은 하지 않는다).
- 충돌 판단은 LLM 의 의미 판단이다. 후보는 어휘 검색(ADR 0009)이라 표현이 다르면 후보에 못 들 수 있다. 제안은 사용자가 최종 판단한다.
- 화자 구분·타임스탬프 구조는 저장하지 않는다. 전사는 텍스트 하나다.
- 클로바노트 어댑터의 실제 자동화(로그인 유지, 업로드·변환 요청, 완료 감지, 전사 수집)는 별도 작업이다.

## 검증 기준

가짜 엔진으로 전사 → 분석 Run → 구조화 저장 → 충돌 제안 → 승인 반영을 테스트한다. 잘못된 JSON·모르는 참조·범위 불일치(회사 회의에
개인 프로젝트)·중복 기억·승인 전 맥락 미변경·승인 시 대상 소실·가짜 Provider 의 전사 수집·MCP 읽기 범위(회사 세션은 회사/공통만)·MCP
쓰기 도구 부재·알림을 확인한다.
