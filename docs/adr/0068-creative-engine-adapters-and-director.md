# ADR 0068: 창작 스튜디오 엔진 어댑터와 창작 감독 연결 (3·4단계)

## 상태
구현됨(2026-10-22, 기능 브랜치 `feat/creative-engines-director`). main 병합과 배포는 독립 검수 뒤 기획이 정한다.
설계 정본은 lab의 `design/ai-director-architecture-adr-draft.md`, 실행 순서는 `design/execution-order.md`의 3·4단계다.
선행: G0(`creative/director.py`, 계약·CAS·receipt), G1(`host_gpu.py`·`managed_comfy.py`, 공용 lease·managed Comfy). 두 계약의 안전장치는 약화하지 않았다.

## 결정

### 1. 어댑터 계약과 레지스트리 (`creative/engines/`)
- 공통 인터페이스 `EngineAdapter`: `probe`(실측, 렌더 없음) → `validate`(프로필 매개변수 스키마) → `execute`(submit·observe·collect를 합침) → `cancel` → `reconcile` → `release`. 실행은 등록된 프로필의 검증된 매개변수만 받고 shell 문자열은 받지 않는다(argv 리스트).
- 엔진: `ffmpeg`(단색·테스트 패턴·컨폼·최종 조립), `remotion`(4.0.537 고정, title-card), `three`(독립 Three.js, 소프트웨어 WebGL), `tts`(기존 Supertonic 공급자), `comfy`(G1 `ComfyManagedController` 위, 고정 profile `wan22-5b-draft`만).
- **가용성은 세 축**(ADR 초안 §7): installation(`absent|present|probe_failed|available`+환경·경로·revision·측정 시각), verification(`untested|lab_executed|contract_passed|quality_pilot_passed`), policy(`experimental|auto_eligible|held`). 자동 선택 후보는 (a) real 어댑터, (b) 신선한 `available` 측정, (c) 실제 smoke 증거, (d) 정책 `auto_eligible`, (e) 권리 범위, (f) gpu 프로필이면 공용 lease가 비어 있음을 모두 만족해야 한다. mock 계약 시험은 `mock_contract`에 따로 적고 어떤 축도 올리지 않는다. 재측정이 `available`이 아니면 정책을 `experimental`로 내린다(올리는 일은 사람이 `set_policy`로만).
- Comfy `probe`는 파일 존재만 읽고 runtime을 시작하지 않으므로 `present`까지만 가능하다. 그래서 Comfy는 자동 선택 후보가 될 수 없고, 사용자가 엔진을 명시한 lab 실행(`selection="explicit-lab"`)만 가능하다.
- Three는 소프트웨어(SwiftShader) WebGL만 등록했다. Metal 경로는 GPU lease가 필요하므로 이번에 등록하지 않았다. 렌더 코드는 이 저장소 `engines/assets/`이고 `node_modules`만 lab 프로젝트의 것을 링크한다.
- 실행 동시성은 파이프라인 슬롯(기본 1)으로 제한한다(Remotion은 Chrome까지 약 2GB). 슬롯 대기 중에도 취소가 통한다.

### 2. G0 attempt/receipt 연결 (`creative/pipeline.py`)
- 샷 revision → **render spec**(엔진·프로필·매개변수·바이블/타임라인 조각·cue·seed·입력 자산·idempotency key 전문)을 CAS에 저장 → `submit_attempt(input_sha256=spec sha, resolved_graph=spec)` → `execute_attempt` → 어댑터 실행.
- **engine receipt는 attempt의 마지막 출력**이다. G0가 출력들을 한 원자 단계로 모으므로 crash 뒤에 영상만 있고 receipt가 없는 상태가 생기지 않는다. receipt에는 엔진·프로필·`execution_mode`(real|mock)·`real_execution`·selection·spec sha·시간·RSS 표본·provenance·출력별 sha/미디어 요약이 있다. mock 또는 대역 컨트롤러 실행은 `real_execution=false`로 남는다.
- 샷이 아닌 실행(오디오 선행 TTS, 최종 조립)은 G0 attempt가 샷에 묶여 있어 같은 흐름을 쓸 수 없다. 같은 어댑터·receipt 규칙으로 `run_unattached`가 실행하고 sidecar(`engine_runs`, `compositions`)에 멱등 키로 기록한다. **이 두 실행에는 G0의 execution claim이 없다.** crash 뒤에는 같은 키로 다시 부르면 되고 결과는 CAS라 안전하지만 중복 실행을 막는 잠금은 없다.
- 재개: `resume`는 `queued|running|collecting`인 attempt만 어댑터별 규칙으로 이어 실행한다. Comfy는 제출 전에 `idempotency_key → task_id`를 영속 저장하므로 재개 때 새로 제출하지 않고 저장된 task를 관측·reconcile한다. 제출 재시도 없음.
- 취소: G0 `cancel_attempt`가 제작을 취소 상태로 만들면 어댑터가 `ctx.cancelled()`로 협조 중단한다(프로세스 그룹 종료, Comfy는 소유 task만 취소). 취소된 시도는 결함 샷이 아니다.

### 3. G0에 더한 것과 이유 (기존 규칙은 그대로)
| 추가 | 이유 |
|---|---|
| `submit_attempt(resolved_graph=…)` 선택 인자 | 공유 workflow revision을 샷마다 바꾸면 병렬 제출이 서로의 graph를 덮는다. 제출 시점의 spec만 receipt에 고정한다. 없으면 기존 동작. |
| `create_review(evidence_uris, reviewer, composition_key, attempt_id)` | 키프레임 증거를 output hash에 묶고, 최종 합성물 검수(`shot_revision=None`)와 같은 바이트를 낸 다른 샷의 검수를 구분한다. 증거 URI는 이 제작의 `artifact:<sha256>`만 허용, 승인은 여전히 null만. |
| `record_sidecar(section, key, value)` | 감독 기록·피드백·검수 보고·합성 기록을 추가 전용으로 남긴다. 구역은 고정 목록, 같은 키에 다른 내용은 거부(`ConflictError`). |
| `set_shot_plan(expected_timeline_revision, shots)` | G0에는 샷 목록을 만드는 공개 경로가 없고 `create_production`이 가짜 2샷을 둔다. 타임라인과 정확히 같은 ID·구간이어야 하고, 바뀐 샷만 새 revision이 되며, 실행 기록이 있는 샷은 뺄 수 없다. |
| `resolve_shot_stale`, `resolve_final_stale` | G0 stale 표시는 쌓이기만 했다. 현재 샷·바이블·타임라인 revision으로 만든 검수 대기 출력이 있을 때만 샷 stale을 지우고, 합성이 현재 출력 해시를 그대로 쓰고 그 합성의 검수가 있고 bible_assets까지 비었을 때만 이웃·전체 표시를 지운다. `bible` 검수 표시는 기계가 판단할 수 없어 일부러 남긴다. |

### 4. 창작 감독 (`creative/direction.py`, `director_llm.py`)
- 순서: 브리프 → **스타일 바이블**(색·타이포·카메라 언어·LUT 의도) → **오디오 선행 타임라인**(분석 또는 TTS 발화 시점을 정수 샘플로 받아 `sample_to_frame`으로만 프레임 변환, 박자·구간·발화 cue, 컷 양자화 오차 기록) → **샷 의도와 엔진 선택**(선택 이유와 대안 기록).
- LLM은 제안만 한다. 저장 전에 코드가 검증하고(프레임 정수·반열림 연속·샷 최소 길이·ID 형식·어댑터 스키마·자동 선택 후보), 오류는 한 번만 알려 다시 묻고, 계속 실패하면 아무것도 쓰지 않는다. 후보 엔진이 하나도 없으면 LLM을 부르기 전에 멈춘다.
- 호출 경로는 기존 worker 세션·Run 경로 재사용이다(`SessionRunDirectorLlm`: `ChatSession(worker)` + `queue_run` + `Run.result_text`, `request_key` 멱등). **이 경로는 이 환경에서 실행하지 않았다.** 시험은 `FixedResponseLlm` fake만 쓴다.
- 같은 (단계, 프롬프트 해시)는 sidecar에서 재사용해 LLM을 다시 부르지 않는다(재개). 감독의 제안은 승인이 아니다(`approval=null`).
- LUT는 의도만 기록하고 `applied=false`다. 적용 도구가 없다.
- 오디오 분석(`audio_analysis.py`)은 스펙트럼 플럭스 온셋·자기상관 템포·RMS 구간이다. 합성 클릭 트랙으로 프레임 정렬만 확인했고 실제 음악의 비트 정확도는 미검증이다.

### 5. 검수 (`creative/review.py`)
- 키프레임은 샷 경계·검출 컷·모션 극값·오디오 이벤트·균등 지점에서 상한 16장까지 뽑고, 버린 후보를 기록한다. PNG와 매니페스트는 CAS 자산이고 ReviewReceipt의 `evidence_uris`가 output hash에 묶는다.
- 기계 검사: 디코드, 프레임 수, 해상도, fps, 길이, 검은 프레임, 정지, 오디오 피크(클리핑), 무음, 길이 기반 싱크. hard fail 하나면 `fail`, 핵심 항목이 `unknown`이면 `review_pending`. 의도된 정지·암전·무음은 `acceptance_criteria`의 `allow_<종류>:<시작>-<끝>:<이유>`로 선언해야 하며 선언 범위 안에서만 예외다.
- 이 검수는 장면 의미·미감을 판정하지 않고 `independent=false`, 승인 null이다.

### 6. 결함 샷 재제작과 피드백 (`feedback.py`)
- 결함 판정은 현재 revision attempt의 `failed` 또는 검수 `fail`이다. 재제작은 그 샷의 새 revision 하나와 새 attempt 하나만 만들며 다른 샷의 revision·output hash·타임라인·바이블은 바뀌지 않는다(시험: 주변 샷 hash 비교).
- 사용자 피드백 API는 엔진·프로필(모델)·매개변수·seed 덮어쓰기(`explicit-lab`) 또는 감독 제안(`auto`)으로 샷을 바꾼다. 요청은 `idempotency_key`로 멱등이고 낡은 revision은 409다. 문서: `docs/design/creative-production-api.md`.

## 알려진 한계
- 실제 Comfy 생성, 실제 TTS 합성, 실제 감독 LLM, GPU 경로는 실행하지 않았다. Comfy·TTS는 대역 컨트롤러/공급자로 계약만 시험했다.
- 최종 조립은 재인코딩(concat 필터)이다. 코덱·timebase가 같을 때의 무손실 concat과 색 변환(LUT 적용)은 없다.
- 서버 시작 시 미완료 attempt를 자동 재개하지 않는다. `POST …/resume`이 필요하다.
- 레지스트리 파일은 한 호스트 전용이다(flock). 다중 호스트 공유는 고려하지 않았다.
- Remotion 라이선스: 개인·3인 이하 영리 조직 등 Free License 자격 조건과 파생 판매 제한이 있다. 회사·다중 사용자 상용 사용 전에 별도 분석이 필요하며 `rights.use_scope=personal`로만 후보가 된다.
