# 로컬 TTS 설계: 별도 생성 페이지와 채팅 음성 재생 (개정 2판)

- 상태: 설계. 검증대기 (2026-10-20 독립 재검증 지적 반영 개정). 이 단계에서는 기능 코드를 변경하지 않는다.
- 날짜: 2026-10-20.
- 관련: [ADR 0025](../adr/0025-on-demand-local-stt.md), [ADR 0026](../adr/0026-local-first-meetings.md), [ADR 0027](../adr/0027-retire-clovanote.md), [ADR 0028](../adr/0028-stt-quality-and-speaker-utterances.md), [STT 안내](../stt/README.md), [API 계약](../api-contract.md), ADR [0062](../adr/0062-local-tts.md).
- 엔진 벤치(별도 세션 병행): Kokoro 한국어 품질과 실행 방식 측정 결과는 이 문서의 플러그형 항목(§4.1, §5, §7, §17)에 반영한다.

## 1. 사용자 결정 (확정)

1. **별도 생성 페이지**: TTS는 회의록(STT) 기능에 넣지 않고 별도 TTS 생성 페이지를 만든다. 텍스트 입력에서 음성을 생성하고 재생·다운로드한다. 음성 종류와 속도 설정이 있다.
2. **채팅 응답의 선택적 음성 재생**: 따까리 응답 메시지에 선택적 음성 재생 버튼을 둔다. 재생·일시정지·중지. 텍스트 응답이 기본이며 음성은 선택이다. 모바일(iOS Safari 자동재생 정책 및 제스처 제약) 환경을 고려한 복구 UX를 구비한다.
3. **엔진 추상화 및 채택 원칙**: 엔진은 Kokoro-82M 우선, 로컬·무료. 엔진 교체 가능한 인터페이스로 추상화한다. Kokoro 한국어 품질이 실사용 기준을 충족하면 구현으로 정상 진행한다. 단, 한국어 품질이 실사용 기준에 미달하여 타 모델로의 교체가 필요할 때에만 임의로 바꾸지 않고 벤치 결과와 대안을 사용자에게 보고하여 최종 결정을 받는다.
4. **자원 최소화 및 독립 실행**: 대형 모델 가중치는 평시에 메모리에 상주시키지 않는다. 기존 STT 직렬 큐와 E5 임베딩 락을 일체 수정하지 않고 독립 직렬 큐로 분리한다. 문장 청크 단위 캐싱 및 한국어 텍스트 정규화 단계를 둔다.

## 2. 기존 구조 분석 요약

### 2.1 백엔드 STT 및 모델 구조 (재사용 및 격리 대상)

- **일회용 모델 프로세스 감독기** `src/ttakkari/model_process.py::run_json_worker`: 별도 Python에 `모듈 + 출력경로`로 실행하고 요청 JSON을 stdin으로 전달, 프로세스 종료 후 결과 JSON 파일을 읽는 단일 배치 계약이다. 자식 환경은 PATH·HOME·TMPDIR·locale만 상속하고 HF 오프라인·암묵 토큰 금지·텔레메트리 금지를 강제한다. PYTHONPATH는 배포본 `src`로 고정한다. 출력 64MB 상한, 타임아웃 시 kill 후 반드시 회수한다. 결과에 `process_exited·worker_pid·wall_seconds`를 붙인다.
- **자식 프로세스 수명과 부모 감시**: 자식 프로세스는 `watch_parent(parent_pid, stop)` 스레드를 통해 부모 PID를 주기적으로 감시하며, 부모 프로세스가 종료되거나 고아(PID 1)가 되면 `os._exit(74)`로 즉시 자살한다. 따라서 일회용 프로세스라 하더라도 실행 중에는 부모 프로세스의 수명에 엄격히 종속된다.
- **로컬 STT 패키지** `src/ttakkari/meetings/local_stt/`: `jobs.py`는 단일 컨슈머 직렬 큐다. 추론 전에 claim하고 DB 커넥션·행 잠금을 해제한 뒤 자식을 띄운다. 취소는 queued로 되돌리고, 시작 시 running을 queued로 복구한다. generation 배포 모드에서는 `generation_service.consume("stt", ...)` 경로를 탄다. `worker.py`는 요청당 1프로세스다.
- **STT 스레드 설정 확인**: STT의 CPU 스레드는 `config.py`의 `local_stt_cpu_threads`(기본값 4)로 설정되며, `runner.py` 및 `worker.py`에서 `torch.set_num_threads(4)`를 통해 4스레드로 구동된다. TTS의 2스레드 설정은 아직 실측되지 않은 가설이다(§6).
- **오디오 서빙 선례**: `GET /api/artifacts/{id}/content`는 Bearer + `FileResponse` + Range 지원이다. `<audio>` 태그처럼 헤더를 못 붙이는 클라이언트는 `POST .../download-link`의 단기 토큰 URL(`/api/dl/{token}`)을 쓴다. TTS 오디오 서빙도 이 패턴을 따른다(§7).
- **기존 모델 동시성 구조 확인**: E5 임베딩(`context/embedder.py`)은 자체 `asyncio.Lock`을 사용하며, STT는 단일 consumer 직렬 큐(`jobs.py`)를 쓴다. 시스템 전역에 STT·E5를 묶는 공용 슬롯은 존재하지 않는다. 따라서 TTS 도입 시에도 기존 STT 큐와 E5 락을 일체 수정하지 않고 독립 직렬 큐를 유지한다(§6).

### 2.2 프론트 (삽입 지점)

- **라우트** `front/src/App.tsx`: 새 페이지는 Route 1개와 `shell/features.ts`의 FEATURES 항목 1개(라우트·아이콘·라벨·설명)로 데스크톱 부가 기능·모바일 더보기에 함께 노출된다. TTS는 탭이 아니라 더보기 진입이 기본이다.
- **채팅 메시지** `front/src/features/chat/Messages.tsx::AgentMessage`: 본문 아래 `tk-message__actions`에 복사 버튼이 있다. 음성 재생 버튼은 이 자리 옆에 둔다. `live`(스트리밍 중)일 때는 액션이 숨으므로 재생 버튼도 완료된 응답에만 노출된다.
- **오디오 기존 코드**: 현재 프론트 코드베이스에 `<audio>`, `AudioContext`, `speechSynthesis`, `new Audio` 사용처가 없다. TTS가 첫 합성 오디오 재생 코드다.
- **저장소 주의사항**: 현재 프론트 저장소(`front`)는 `main...origin/main [ahead 3, behind 73]` 및 dirty 상태이므로, 프론트 작업 착수 전 기준 브랜치 정리 및 로컬 커밋 보존 절차가 선행되어야 한다.

## 3. 목표와 비목표

- **목표**: (a) 별도 TTS 페이지에서 텍스트→음성 생성·재생·다운로드. (b) 채팅 응답의 선택적 음성 재생. (c) 로컬·무료 엔진, 대형 모델 미상주, STT와 안전한 독립 병행 실행. (d) 문장 청크 캐시 히트를 통한 체감 단축, 반복 문장 캐시. (e) iOS Safari에서의 제스처 기반 재생 및 차단 시 재터치 복구 UX.
- **비목표**: 실시간 대화형 음성(저지연 풀듀플렉스), 음성 클로닝·학습, 회의록 STT 파이프라인 변경, 외부 유료 TTS 연동, 자동재생 무조건 보장(iOS WebKit 정책상 불가능), 세대 간 원문 프로세스 마이그레이션.

## 4. 아키텍처

독립 패키지 `src/ttakkari/tts/`를 둔다. `meetings/` 안에 넣지 않는다.

```text
src/ttakkari/tts/
  __init__.py
  providers.py     # TtsProvider Protocol + capabilities + 음성 목록 타입
  normalize.py     # 한국어 정규화·문장 분리 (순수 함수, ML 의존 없음)
  cache.py         # 청크 단위 캐시 키·조회·저장·LRU 정리 (부모 프로세스)
  jobs.py          # TTS 전용 직렬 큐: claim→워커 실행→청크 확정 (인메모리 원문 수명 관리)
  runner.py        # 설정·파라미터→워커 요청 어댑터 (local_stt/runner.py 대응)
  worker_kokoro.py # Kokoro 워커: 요청당 1프로세스 (run_json_worker 재사용)
  read_model.py    # 조회용 조립 (음성·job·청크 출력)
src/ttakkari/api/tts.py  # APIRouter(prefix="/api/tts")
```

### 4.1 엔진 추상화 및 모델 채택 원칙 [GATE]

```python
class TtsCapabilities(NamedTuple):
    chunked: bool            # 문장 청크 분할 합성 지원
    speed_range: tuple[float, float]
    sample_rate: int         # 출력 샘플레이트 (벤치 확정)
    output_format: str       # "wav" 기본 (벤치 확정)

class TtsProvider(Protocol):
    id: str                  # "kokoro"
    label: str
    capabilities: TtsCapabilities
    def availability(self) -> ProviderAvailability: ...
    async def voices(self) -> list[TtsVoice]: ...
    async def synthesize(self, chunks: list[str], *, voice: str, speed: float) -> TtsResult: ...
```

- 부모 프로세스는 이 Protocol만 본다. 엔진별 차이는 워커 모듈(`worker_kokoro.py`)과 설정 안에 가둔다.
- Kokoro 어댑터의 플러그형 항목: 모델 경로·리비전, 음성 목록 파일, 권장 청크 길이, 출력 형식·샘플레이트, 디바이스(cpu 기본). 벤치 세션 결과가 나오면 어댑터 내부와 설정 기본값만 바꾼다.
- **모델 채택 원칙 및 조건부 사용자 승인 [GATE]**:
  - Kokoro 한국어 모델이 실사용 품질 기준을 충족하면 추가 게이트 승인 없이 정상적으로 구현 단계로 진행한다.
  - **오직 Kokoro 한국어 모델이 실사용 기준에 미달하여 타 모델(Piper, ChatTTS 등)로의 전환이 필요할 때에만**, 임의로 변경하지 않고 벤치 결과와 대안을 사용자에게 보고하여 최종 채택 승인을 받는다.

## 5. 프로세스 모델 (택일과 근거)

선택: Phase 1은 요청별 일회용 자식 프로세스다. `run_json_worker`를 재사용한다. 유휴 타임아웃 상주는 Phase 1에서 쓰지 않는다.

**근거**:
1. 대형 모델 가중치(수백 MB)의 평시 메모리 점유를 방지한다. 단, 부모 프로세스의 라우터·큐 상태·캐시 메타데이터 관리를 위한 기본 메모리(수 MB)는 정상 소비된다(평시 0MB 과장 배제).
2. 검증된 감독기를 그대로 쓴다. 회수·타임아웃·부모 감시·무자격증명·오프라인 강제·PYTHONPATH 고정이 이미 안정화되어 있다.
3. 자식 프로세스는 `watch_parent` 감시 스레드를 통해 부모 프로세스 수명에 종속되어 부모 비정상 종료 시 즉시 자살하므로 고아 프로세스가 발생하지 않는다.
4. 캐시 히트 시에는 자식 프로세스를 아예 띄우지 않고 파일로 응답한다.

**전환 기준(측정 후 판단)**:
캐시 미스 단문(500자 이하) TTFA p50이 8초(실측 전 가설)를 넘으면 유휴 타임아웃 상주(타임아웃 5분) 도입을 재검토한다. 전환 시에도 일회용 프로세스는 폴백으로 유지한다.

## 6. 동시성: STT·E5와의 경합 제어

- **기존 STT/E5 보존**: STT의 단일 consumer 직렬 큐와 E5의 `asyncio.Lock`은 전혀 수정하지 않는다.
- **TTS 전용 직렬 큐**: TTS 비동기 작업은 TTS 전용 직렬 큐(`tts/jobs.py`)에서 처리되어 동시 다발적 프로세스 폭주를 방지한다.
- **단문 동기(/speak)와 장문 비동기(/jobs)의 직렬 실행 예산 준수**:
  - `/speak`와 `/jobs`가 동시에 자식 프로세스를 띄우면 TTS 추론 프로세스가 중복 실행되어 자원 예산을 초과할 수 있다.
  - 따라서 부모 프로세스 내에 **단일 TTS 실행 락(`_tts_execution_lock = asyncio.Lock()`)**을 두어 `/speak`와 `/jobs`가 동일한 락을 획득하도록 직렬화 경로를 명시한다.
  - `/jobs` 워커가 실행 중일 때 `/speak` 요청이 들어오면 최대 15초간 락 획득을 대기하며, 대기 시간 초과 시 503(busy)을 반환한다.
- **STT와 TTS의 동시 실행 및 간섭 실측 가설**:
  - STT는 4스레드(`local_stt_cpu_threads`), TTS는 2스레드(가설)를 사용한다.
  - 하드웨어 코어 수가 많더라도 동시 실행 시 상호 RTF 저하 및 CPU 캐시/메모리 대역폭 간섭 여부는 **실측 전까지 미검증 상태**이다.
  - 벤치마크 단계에서 단독 vs 동시 실행 시의 상호 RTF 저하율을 실측하여 `LOCAL_TTS_CONCURRENCY_POLICY`(기본값 `allow_concurrent`, 옵션 `defer_to_stt`)의 최종 기본값을 확정한다.

## 7. API 설계

모두 Bearer 인증(owner API)이며 오류 본문은 `{"code","message"}` 계약을 따른다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/tts/status` | `{enabled, ready, reason, engine, model, voice_count}` |
| GET | `/api/tts/voices` | `{voices: [{id, name, lang, gender?, sample_url?}]}` |
| POST | `/api/tts/jobs` | 장문 비동기 합성. `{text(최대 5000자), voice?, speed?}` → 202 `{job_id, chunks, cached_chunks}`. 원문은 DB에 저장하지 않음(§8) |
| GET | `/api/tts/jobs/{id}` | 상태 조회. **항상 200 OK 반환**. `{status, error_code?, progress:{done,total}, chunks:[{index, text_hash, audio_url?, duration?}], audio_full_url?, metrics?}`. 실패 시 `status: "failed"` 및 `error_code` 제공 |
| POST | `/api/tts/speak` | 단문 동기 합성 (Fast-path). `{text(최대 500자), voice?, speed?}` → 200 `audio/wav`. 직렬 락 획득 후 단일 청크 합성 및 즉시 반환 |
| DELETE | `/api/tts/jobs/{id}` | 대기·실행 취소. 인메모리 원문 즉시 삭제, 실행 중이면 자식 회수 후 `cancelled` 처리 |
| GET | `/api/tts/cache/stats` | `{entries, bytes, hits, misses, max_bytes}` |
| POST | `/api/tts/cache/prune` | 상한 초과·TTL 경과 항목 정리 후 `{removed, freed_bytes}` |

### 7.1 워커 감독기 계약과 청크 응답 방식

- `src/ttakkari/model_process.py::run_json_worker`는 `process.communicate()`로 프로세스 종료 후 `result.json`을 단일 배치로 반환하는 계약이다.
- 따라서 단일 `run_json_worker` 실행 중에 첫 청크를 실시간으로 조기 스트리밍하는 것은 현재 감독기 계약상 불가능하다.
- 불필요한 프로토콜 확장(IPC stdout ndjson 스트리밍 등)을 피하고 검증된 감독기를 그대로 재사용하기 위해, Phase 1에서는 **워커 프로세스가 완료된 후 청크 목록을 일괄 반환**하는 계약을 유지한다.
- 첫 청크 조기 제공(빠른 TTFA)은 **문장 청크 단위 캐시 히트 패스**에서 실현되며, 캐시 히트된 청크는 워커 기동 없이 즉시 클라이언트에 전달된다.

## 8. 데이터·저장·원문 인메모리 수명 및 Generation 정책

### 8.1 원문 미저장 불변성 (설계상 데이터 최소화 방침)

개인정보 보호 및 데이터 최소화 설계 방침(Data Minimization by Design)에 따라, **DB `tts_jobs` 테이블에는 텍스트 원문을 영구 저장하지 않는다.**

- **DB 테이블 정의**:
  - `tts_jobs`: `id`, `generation_id`(소유 세대 식별자), `status`(queued·running·succeeded·failed·cancelled), `params`(voice, speed, text_length, chunk_count, 원문 제외), `result_metadata`, `error`, `error_code`, `created_at`, `started_at`, `finished_at`.
  - `tts_cache`: `chunk_key`(PK), `engine`, `model_revision`, `voice`, `speed`, `chunk_hash`, `audio_relpath`, `bytes`, `sample_rate`, `duration_seconds`, `hits`, `created_at`, `last_hit_at`.

### 8.2 비동기 Job의 텍스트 원문 인메모리 수명 주기

Phase 1의 입력 원문 보관은 **서버 프로세스 인메모리 버퍼 하나로 확정**한다. 임시 디스크 staging 파일 대안은 배제하며, 초대용량 텍스트 대응이 필요한 경우 미래 검토(Phase 2)로 분리한다.

1. **보관 위치**:
   - 캐시 미스가 발생한 비동기 job의 텍스트는 해당 요청을 수신한 서버 프로세스의 인메모리 딕셔너리(`_pending_texts: dict[UUID, list[str]]`)에 일시 보관한다.
2. **참조 수명 및 소멸 단계**:
   - (a) 요청 접수: `POST /api/tts/jobs` 시 정규화 및 분할된 청크 텍스트 리스트를 `_pending_texts[job_id]`에 보관. DB에는 메타데이터만 커밋.
   - (b) 워커 기동 및 stdin 전달: 단일 컨슈머가 job을 claim하고 자식 프로세스를 기동하여 stdin 파이프로 JSON을 전달.
   - (c) 부모 메모리 해제: stdin 쓰기 완료(`drain()` 및 파이프 닫힘) 즉시 부모 프로세스에서 `_pending_texts.pop(job_id, None)`을 호출하여 메모리 참조를 해제한다(Python GC 수거 대상).
   - (d) 자식 프로세스 메모리 수명: 자식 프로세스는 stdin에서 읽은 청크를 순차 합성하고, 각 청크의 wav 캐시 저장이 끝나면 청크 참조를 해제한다. 전체 합성이 끝나거나 실패 시 자식 프로세스가 `exit`하면서 OS 가상 메모리 공간 전체가 회수된다.
   - (e) OS 레벨 한계: OS swap 페이징, 프로세스 덤프, 물리 RAM 잔존 등 OS 커널 레벨의 물리적 소멸은 애플리케이션 레벨에서 보장할 수 없으므로 `shred & unlink`나 물리적 영구 소멸과 같은 과장된 표현은 배제한다.
3. **수명 종료 규칙**:
   - **TTL 만료**: 큐 대기 시간이 10분을 초과하면 `_pending_texts`에서 제거하고 job을 `failed(error_code="input_ttl_expired")`로 종결.
   - **취소 (`DELETE`)**: 인메모리 텍스트 즉시 삭제. 실행 중인 자식은 kill/reap 후 `cancelled`.
   - **실패 (워커 에러/타임아웃)**: 즉시 인메모리 텍스트 삭제 후 `failed(error_code=...)`.
   - **정상 완료**: 모든 청크 완료 시 `succeeded`.

### 8.3 Generation 수명 주기·소유권 및 Fencing 정합성

Ttakkari의 무중단 세대 교체(generation deployment) 환경에서 인메모리 원문은 프로세스 경계를 넘어 타 세대로 복사/이전될 수 없다. 이에 따라 다음 정합성 규칙을 강제한다:

1. **소유권 (Origin Generation Owner)**:
   - 각 TTS job은 DB 레코드에 접수 세대의 `generation_id`를 기록한다.
2. **새 세대의 구 세대 job 복구 금지 조건**:
   - 새 세대는 DB에 구 세대의 `queued` 또는 `running` job이 존재하더라도, 해당 job의 인메모리 원문을 가지고 있지 않으므로 **자신이 그 job을 claim하거나 재실행(복구)해서는 안 된다.**
3. **새 세대 기동 시 일괄 실패 처리 금지**:
   - 새 세대가 기동되었다는 사실만으로 기존 구 세대의 queued/running job을 일괄 `failed` 처리해서는 안 된다. 구 세대가 아직 `draining` 상태로 살아있다면 자신이 보유한 원문으로 작업을 정상 완료할 수 있기 때문이다.
4. **Drain 중 처리**:
   - `draining` 상태에 진입한 구 세대는 새 HTTP 요청 접수는 중단되지만, 이미 접수한 인메모리 큐의 job을 계속 실행하여 완료할 수 있다.
   - 구 세대 프로세스 종료 시점(graceful shutdown)까지 미완료된 job은 소멸 전 `failed(error_code="drained_without_completion")`로 종결 처리하고 메모리를 비운다.
5. **Owner 생존 여부 판단과 Fencing**:
   - 새 세대는 구 세대의 프로세스 생존 여부(generation runtime 등록 해제 또는 하트비트 만료)를 확인한다.
   - 구 세대 프로세스가 완전히 종료(사망)되었음이 확인된 경우에 한해, 남겨진 고아 job들을 새 세대가 `failed(error_code="server_restarted_input_purged")`로 안전하게 fencing 종결한다.
   - [추론: drain 타임아웃 내 미완료 job의 안전한 중단 여부 및 단일 호스트 내 세대 간 CPU 경합 가능성은 실측/구현 전 가설적 위험임].
6. **상태 Polling 및 취소 라우팅**:
   - `GET /api/tts/jobs/{id}`는 DB를 조회하므로 어느 세대로 라우팅되어도 200 OK와 최신 상태를 응답한다.
   - `DELETE /api/tts/jobs/{id}`는 DB 상태를 `cancelled`로 변경한다. 구 세대 워커는 청크 루프 및 실행 전 DB 상태를 확인하여 취소를 감지하고 자식을 회수한다.
   - 클라이언트는 상태 응답에서 `status === "failed"` 및 `error_code === "server_restarted_input_purged"`를 수신하면 브라우저에 남아 있는 텍스트로 사용자에게 재요청(Retry) 버튼을 제공한다.

### 8.4 청크 단위 캐시 키 통일

```text
chunk_cache_key = blake2b("tts1" ‖ engine ‖ model_revision ‖ voice ‖ speed ‖ normalize(chunk_text))
```

- 파일 위치: `<home>/tts-cache/<shard2>/<chunk_cache_key>.wav` (2단계 샤딩).
- 정리 정책: 상한 2GB, 초과 시 LRU 삭제, TTL 30일. job 완료 및 서버 기동 시 정리.

## 9. 한국어 텍스트 정규화 (`tts/normalize.py`)

순수 함수 파이프라인으로 ML 의존 없이 단위 테스트한다.

1. **평문화**: 마크다운 코드 블록·인라인 코드·URL 링크·표·HTML 태그 제거.
2. **이모지 및 특수문자**: 이모지는 발음하지 않고 제거, 기호·특수문자 정리, NFC 정규화.
3. **숫자·날짜 한글화**:
   - 날짜: `2026-10-20` → `이천이십육년 시월 이십일` (국어맞춤법 제52항 한자어 음운 탈락 규칙 반영: '십월'이 아닌 '시월', '육월'이 아닌 '유월').
   - 시간, 금액, 단위, 백분율을 한국어 표준 발음 텍스트로 치환.
4. **문장 청크 분할**: 종결어미와 문장부호 기준으로 분할. 최대 청크 길이(벤치 확정 전 기본 200자 가설) 초과 시 절 경계(쉼표 등)에서 2차 분할.
5. **청크별 캐시 키 생성**: 각 분할된 청크 단위로 캐시 키를 계산.

## 10. 프론트엔드 설계

### 10.1 TTS 생성 페이지 (`/tts`)
- 라우트 및 메뉴: `App.tsx` 라우트 등록 + `features.ts` 항목(더보기 메뉴 노출).
- 화면 UI: 텍스트 입력창(textarea, 최대 5000자) → 음성/속도 설정 → 생성 버튼 → 진행률(폴링) → 오디오 플레이어 + 다운로드 버튼.
- 다운로드 연동: 사용자가 다운로드 클릭 시 캐시 파일을 Artifact 보관함(copy 모드)으로 영속 등록.

### 10.2 채팅 재생 버튼
- 위치: `AgentMessage` 컴포넌트의 복사 버튼 옆(`tk-message__actions`). 완료된 메시지(`!live`)에만 노출.
- UI 상태: `idle`(재생 버튼) → `loading`(스피너) → `playing`(일시정지 버튼) → `paused`(재생 버튼).
- 단문/장문 분기: 500자 이하는 `/api/tts/speak` 동기 호출(Fast-path), 초과분은 `/api/tts/jobs` 비동기 큐 호출 후 청크 순차 재생.

## 11. 모바일(iOS Safari) 재생 전략 및 실기기 검증 계획

iOS Safari의 WebKit 자동재생 정책(https://webkit.org/blog/6784/new-video-policies-for-ios/) 및 제스처 만료 버그(https://bugs.webkit.org/show_bug.cgi?id=134925)에 대응하기 위한 방어적 설계를 적용한다. 모바일 연속 재생은 검증 전 무조건 보장하지 않으며 **[미검증 가설]**로 다룬다.

1. **단일 HTMLAudioElement 재사용 및 언락 시도**:
   - 사용자가 재생 버튼을 터치하는 동기 이벤트 핸들러 내에서 전역 단일 `Audio` 인스턴스의 `play()`를 호출(0.01초 무음 음원)하여 브라우저 오디오 언락을 시도한다.
2. **단일 엘리먼트 소스 체이닝 (Source Chaining)**:
   - 장문 청크 연속 재생 시 새 Audio 객체를 생성하지 않고, 활성화된 단일 `Audio` 인스턴스의 `onended` 이벤트에서 `audio.src = nextChunkUrl; audio.play()`를 호출한다.
3. **Play Promise Rejection 처리 및 재터치 복구 UX (필수)**:
   - iOS WebKit은 비동기 시간 지연이나 정책에 따라 `play()` 호출 시 `NotAllowedError` Promise rejection을 발생시킬 수 있다.
   - 따라서 모든 `audio.play()` 호출은 반드시 rejection을 `catch`하도록 구현한다.
   - 청크 전환 시 자동 재생이 차단된 경우, 재생 UI 상태를 차단 모드로 전이시키고 "다음 문장 듣기 (터치)" 안내 버튼을 노출하여 사용자의 1회 터치로 즉시 재생을 이어갈 수 있는 **재터치 복구 UX**를 필수로 구비한다.
4. **WebAudio(AudioContext) 대안 [미검증]**:
   - `AudioContext`의 `decodeAudioData` 및 버퍼 타임라인 스케줄링을 통한 갭리스(gapless) 재생 방식은 실기기 동작 확인 전까지 미검증 대안으로 둔다.
5. **백그라운드 제약**:
   - `navigator.mediaSession` 메타데이터 및 액션을 등록하되, Phase 1의 공식 목표는 화면 켜짐(포그라운드) 상태의 정상 재생이며 백그라운드 재생은 보장하지 않는다.
6. **실기기 검증 계획 [미검증 상태]**:
   - 실제 iPhone(iOS 17+ Safari 및 PWA 모드)에서 다음 체크리스트를 필수 검증한다:
     - [ ] 단문 즉시 재생 동작
     - [ ] 3문장 이상 청크 연속 재생 시 제스처 유지 여부
     - [ ] 재생 차단 시 재터치 복구 버튼 정상 동작 여부
     - [ ] 무음 모드 하드웨어 스위치 상태에서의 오디오 출력
     - [ ] 화면 잠금 후 복귀 시 플레이어 상태 정합성

## 12. 테스트·검증 계획

- **단위 테스트 (`unit_tests/`)**: 한국어 정규화(날짜 시월/유월 음운 탈락, 숫자, 마크다운 평문화), 문장 분할 경계, 청크 캐시 키 결정성.
- **API 및 큐 테스트 (`tests/`)**: `/api/tts/*` 라우트, 캐시 히트 시 워커 미호출, 비동기 job 상태 전이(200 OK + failed 계약 검증), /speak와 /jobs 간 직렬 실행 락 경합 검증.
- **원문 인메모리 수명 검증**: stdin 전달 후 부모 메모리 참조 해제 확인, TTL 만료 시 실패 처리, 서버 재시작 시 구 세대 job 복구 금지 및 고아 job의 `server_restarted_input_purged` 실패 처리 검증.
- **동시성 및 간섭 검증**: STT(4스레드)와 TTS(2스레드 가설) 동시 실행 시 상호 RTF 저하율 실측.
- **모바일 실기기 검증**: iOS 17+ Safari/PWA 실기기 체크리스트 검증.

## 13. 측정 항목

- **TTFA (Time To First Audio)**: 요청 시작부터 첫 소리가 나기까지의 시간(p50 < 8초 가설 검증).
- **RTF (Real-Time Factor)**: 워커 합성 소요 시간 / 생성된 오디오 길이.
- **메모리 점유**: 워커 프로세스 peak RSS 및 부모 프로세스 메타데이터 메모리 소비량.
- **STT 동시 실행 저하율**: 단독 실행 대비 STT+TTS 동시 실행 시의 상호 RTF 저하율.
- **캐시 지표**: 청크 단위 캐시 히트율, 디스크 사용량, LRU 정리량.

## 14. 설정 목록 (초안, `TTAKKARI_` 접두)

| 키 | 기본값 | 설명 |
|---|---|---|
| `LOCAL_TTS_ENABLED` | true | TTS 기능 전체 토글 |
| `LOCAL_TTS_ENGINE` | kokoro | TTS 엔진 ID |
| `LOCAL_TTS_PYTHON` | `.tts-runtime/venv/bin/python` | TTS 전용 Python (STT venv와 분리) |
| `LOCAL_TTS_MODEL_PATH` | `.tts-runtime/kokoro-82m` | 가중치 디렉터리 경로 |
| `LOCAL_TTS_DEFAULT_VOICE/SPEED` | (벤치 확정)/1.0 | 기본 음성 및 속도 |
| `LOCAL_TTS_MAX_TEXT_CHARS` | 5000 | jobs 입력 글자 수 상한 |
| `LOCAL_TTS_SPEAK_MAX_CHARS` | 500 | speak 동기 호출 상한 |
| `LOCAL_TTS_CHUNK_MAX_CHARS` | 200 | 문장 청크 상한 (벤치 결과로 확정) |
| `LOCAL_TTS_TIMEOUT_SECONDS` | 600 | 워커 프로세스 타임아웃 |
| `LOCAL_TTS_CPU_THREADS` | 2 | 워커 CPU 스레드 예산 (실측 전 가설) |
| `LOCAL_TTS_CACHE_MAX_GB/TTL_DAYS` | 2/30 | 캐시 용량 상한 및 TTL |
| `LOCAL_TTS_TOKEN_TTL_SECONDS` | 600 | 오디오 재생 단기 토큰 TTL |
| `LOCAL_TTS_CONCURRENCY_POLICY` | allow_concurrent | STT 동시 실행 정책 (allow_concurrent / defer_to_stt) |

## 15. 마이그레이션·배포·운영

- **Alembic Migration**: 미래의 특정 마이그레이션 번호(0055 등)를 미리 예약하지 않는다. 구현 시점에 통합된 최신 Alembic head 및 down_revision을 기준으로 다음 리비전 번호를 결정하여 생성한다. `tts_jobs`, `tts_cache` 테이블 생성 및 downgrade 지원.
- **배포 및 운영**: 기능 코드 변경이 없는 본 설계 단계에서는 백엔드 배포를 요청하지 않는다. 운영 시에는 캐시 용량 모니터링과 미준비 상태(`ready=false`)에 대한 사용자 가이드를 제공한다.

## 16. WBS 개요 (PMS 「따까리 · 로컬 TTS」 프로젝트)

1. 설계·경계 확정 (분석 + local-tts.md + ADR 0062 개정) — **검증대기**
2. 한국어 정규화·문장 분할·청크 캐시 키 (순수 모듈 + 단위 테스트)
3. 엔진 추상화 + Kokoro 워커 (.tts-runtime 격리)
4. [GATE] 한국어 음성 품질 평가 및 모델 채택 (품질 충족 시 진행, 미달로 타 모델 전환 필요 시 사용자 승인)
5. 영속 큐·API + 원문 인메모리 수명 관리 + 오디오 토큰 서빙
6. DB migration (최신 Alembic head 기준) + 캐시 정리 정책 + 운영 문서
7. TTS 독립 직렬 큐 + STT 동시 실행 부하/RTF 측정 및 간섭 검증
8. TTS 페이지 프론트 (생성·재생·다운로드)
9. 채팅 재생 버튼 + 단일 Audio 소스 체이닝 + 재터치 복구 UX + iOS 실기기 검증
10. E2E·회귀·측정 리포트 + 배포

## 17. 미확인 및 추론 항목

1. Kokoro 한국어 품질·권장 청크 길이·출력 형식·샘플레이트·로드 시간·메모리 (병행 벤치 세션 측정 대기).
2. 타 모델 전환 필요 시 사용자 채택 결정 (품질 미달 시에만 발생).
3. TTFA 목표 8초 및 TTS 2스레드 예산의 타당성 (벤치 실측 전 가설).
4. [추론] 무중단 배포 drain 타임아웃 내 미완료 TTS job의 취소 안정성 및 세대 간 CPU 경합 영향.
5. [미검증] iOS Safari 및 PWA 실기기에서의 청크 순차 재생 제스처 유지율 및 WebAudio 갭리스 안정성.
6. 오디오 토큰 서빙: 기존 download-link 인프라 재사용 vs 전용 토큰 테이블.
7. 프론트엔드 작업 전 front 저장소 분기(`ahead 3, behind 73`) 및 작업 트리 정리.
8. Alembic 다음 번호 (구현 시점 최신 head 기준 확정).

## 부록 A. 작업 로그

- 2026-10-20: 기존 STT·프론트 구조 분석 후 본 설계 초안 작성.
- 2026-10-20: 1차 독립 검증 반영 개정 (역사적 기록: 당시 ADR 0061→0062 이동 및 미추적 0053/0054 감안 0055 임시 언급 논의).
- 2026-10-20: 2차 독립 재검증 지적 반영 전면 정정 (원문 보관 인메모리 확정 및 DB 원문 부재를 데이터 최소화 방침으로 명시, 과장된 물리 소멸 표현 삭제, Generation 소유권 및 구 세대 job 복구 금지/fencing 확정, HTTP 200 failed 계약 일원화, /speak와 /jobs 직렬 락 명시, run_json_worker 배치 계약 정합, Alembic 0055 예약 전면 제거, iOS play Promise rejection 및 재터치 복구 필수화, CPU/스레드 미측정 결론 제거 및 STT 설정 출처 확인, 부모 감시-프로세스 수명 모순 수정, 한자어 날짜 시월 발음 정정, Kokoro 게이트 범위 정정, PMS/WBS 1 검증대기 전환).
