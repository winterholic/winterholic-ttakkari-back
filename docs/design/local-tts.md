# 로컬 TTS 설계: 별도 생성 페이지와 채팅 음성 재생 (개정판)

- 상태: 설계. 구현 미착수. 이 단계에서는 기능 코드를 변경하지 않는다(독립 검증 지적 반영 전면 개정).
- 날짜: 2026-10-20.
- 관련: [ADR 0025](../adr/0025-on-demand-local-stt.md), [ADR 0026](../adr/0026-local-first-meetings.md), [ADR 0027](../adr/0027-retire-clovanote.md), [ADR 0028](../adr/0028-stt-quality-and-speaker-utterances.md), [STT 안내](../stt/README.md), [API 계약](../api-contract.md), ADR 초안 [0062](../adr/0062-local-tts.md).
- 엔진 벤치(별도 세션 병행): Kokoro 한국어 품질과 실행 방식 측정 결과는 이 문서의 플러그형 항목(§4.1, §5, §7, §17)에 반영한다.

## 1. 사용자 결정 (확정)

1. TTS는 회의록(STT) 기능에 넣지 않고 별도 TTS 생성 페이지를 만든다. 텍스트 입력에서 음성을 생성하고 재생·다운로드한다. 음성 종류와 속도 설정이 있다.
2. 따까리 응답(채팅) 메시지에 선택적 음성 재생 버튼을 둔다. 재생·일시정지·중지. 텍스트 응답이 기본이며 음성은 선택이다. 모바일(iOS Safari 자동재생 제약 포함)에서 자연스럽게 동작해야 한다.
3. 엔진은 Kokoro-82M 우선, 로컬·무료. 엔진 교체 가능한 인터페이스로 추상화한다. 다른 작업 세션이 Kokoro 한국어 품질·실행 방식 벤치를 병행 중이므로 엔진 세부는 플러그형으로 둔다.
4. 사용하지 않을 때 자원 점유를 최소화한다. STT·LLM 오케스트레이터와 느슨하게 결합한다. 청크(문장) 단위 생성·스트리밍, 반복 음성 캐싱(키: 엔진·모델·음성·속도·정규화 텍스트 해시), 한국어 텍스트 정규화 단계를 둔다.

## 2. 기존 구조 분석 요약

### 2.1 백엔드 STT 및 모델 구조 (재사용 및 격리 대상)

- **일회용 모델 프로세스 감독기** `src/ttakkari/model_process.py::run_json_worker`: 별도 Python에 `모듈 + 출력경로`로 실행하고 요청 JSON을 stdin으로 전달, 결과 JSON 파일을 읽는다. 자식 환경은 PATH·HOME·TMPDIR·locale만 상속하고 HF 오프라인·암묵 토큰 금지·텔레메트리 금지를 강제한다. PYTHONPATH는 배포본 `src`로 고정해 선택 런타임에 오래된 앱 코드가 섞이지 않게 한다. 출력 64MB 상한, 타임아웃 시 kill 후 반드시 회수한다. 결과에 `process_exited·worker_pid·wall_seconds`를 붙인다. 자식은 부모 PID 감시 스레드로 고아 프로세스를 자살 처리한다.
- **로컬 STT 패키지** `src/ttakkari/meetings/local_stt/`: `jobs.py`는 모델 없는 부모의 단일 컨슈머 직렬 큐다. 추론 전에 claim하고 DB 커넥션·행 잠금을 해제한 뒤 자식을 띄운다. 취소는 queued로 되돌리고, 시작 시 running을 queued로 복구한다. generation 배포 모드에서는 `generation_service.consume("stt", ...)` 경로를 탄다. `worker.py`는 요청당 1프로세스다.
- **설정** `src/ttakkari/config.py`: `TTAKKARI_LOCAL_STT_*`(`local_stt_*` 필드, enabled·python·model_path·timeout 등). 기본 루트는 저장소 안 `.stt-runtime/`이며 Git에서 제외한다.
- **오디오 서빙 선례**: `GET /api/artifacts/{id}/content`는 Bearer + `FileResponse` + Range 지원이다. `<audio>` 태그처럼 헤더를 못 붙이는 클라이언트는 `POST .../download-link`의 단기 토큰 URL(`/api/dl/{token}`)을 쓴다. TTS 오디오 서빙도 이 패턴을 따른다(§7).
- **기존 모델 동시성 구조 확인**: E5 임베딩(`context/embedder.py`)은 자체 `asyncio.Lock`을 사용하며, STT는 단일 consumer 직렬 큐(`jobs.py`)를 쓴다. **시스템 전역에 STT·E5를 묶는 공용 슬롯은 존재하지 않는다.** 따라서 TTS 도입 시에도 기존 STT 큐와 E5 락을 일체 수정하지 않고 독립 직렬 큐를 유지한다(§6).

### 2.2 프론트 (삽입 지점)

- **라우트** `front/src/App.tsx`: 새 페이지는 Route 1개와 `shell/features.ts`의 FEATURES 항목 1개(라우트·아이콘·라벨·설명)로 데스크톱 부가 기능·모바일 더보기에 함께 노출된다. TTS는 탭이 아니라 더보기 진입이 기본이다.
- **채팅 메시지** `front/src/features/chat/Messages.tsx::AgentMessage`: 본문 아래 `tk-message__actions`에 복사 버튼이 있다. 음성 재생 버튼은 이 자리 옆에 둔다. `live`(스트리밍 중)일 때는 액션이 숨으므로 재생 버튼도 완료된 응답에만 노출된다.
- **오디오 기존 코드**: 현재 프론트 코드베이스에 `<audio>`, `AudioContext`, `speechSynthesis`, `new Audio` 사용처가 없다. TTS가 첫 합성 오디오 재생 코드다.
- **저장소 주의사항**: 현재 프론트 저장소(`front`)는 `main...origin/main [ahead 3, behind 73]` 및 dirty 상태이므로, 프론트 작업 착수 전 기준 브랜치 정리 및 로컬 커밋 보존 절차가 선행되어야 한다.

## 3. 목표와 비목표

- **목표**: (a) 별도 TTS 페이지에서 텍스트→음성 생성·재생·다운로드. (b) 채팅 응답의 선택적 음성 재생. (c) 로컬·무료 엔진, 유휴 자원 0, STT와 안전한 독립 병행 실행. (d) 문장 청크 스트리밍급 체감(첫 소리까지 짧게), 반복 문장 캐시. (e) iOS Safari에서의 제스처 기반 안정적 연속 재생.
- **비목표**: 실시간 대화형 음성(저지연 풀듀플렉스), 음성 클로닝·학습, 회의록 STT 파이프라인 변경, 외부 유료 TTS 연동, 자동재생·백그라운드 연속 재생 고도화(Phase 2 후보).

## 4. 아키텍처

독립 패키지 `src/ttakkari/tts/`를 둔다. `meetings/` 안에 넣지 않는다. 느슨한 결합 요구와 재사용 방향이 STT→TTS 단방향이기 때문이다.

```text
src/ttakkari/tts/
  __init__.py
  providers.py     # TtsProvider Protocol + capabilities + 음성 목록 타입
  normalize.py     # 한국어 정규화·문장 분리 (순수 함수, ML 의존 없음)
  cache.py         # 청크 단위 캐시 키·조회·저장·LRU 정리 (부모 프로세스)
  staging.py       # 비동기 job 입력 텍스트 단기 버퍼/수명 관리
  jobs.py          # TTS 전용 직렬 영속 큐: claim→워커→청크 확정→원문 파기
  runner.py        # 설정·파라미터→워커 요청 어댑터 (local_stt/runner.py 대응)
  worker_kokoro.py # Kokoro 워커: 요청당 1프로세스 (run_json_worker 재사용)
  read_model.py    # 조회용 조립 (음성·job·청크 출력)
src/ttakkari/api/tts.py  # APIRouter(prefix="/api/tts")
```

### 4.1 엔진 추상화 및 한국어 품질 게이트 [GATE]

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
- **한국어 품질 평가 및 채택 게이트 [GATE]**:
  - Kokoro 한국어 모델의 발음 정확도, 부자연스러운 억양, 숫자/외래어 발음이 실사용 기준에 미달할 경우:
  - **임의로 다른 모델(Piper, ChatTTS, VITS 등)로 독단 교체하는 것을 엄격히 금지**한다.
  - 벤치마크 결과(지표 및 실제 음성 샘플)와 대안(한국어 정규화 강화, 파인튜닝 가중치, 타 모델 후보 비교)을 사용자에게 정식 보고하고, **사용자의 승인 및 모델 채택 결정을 받아야만 다음 단계(WBS 4 이후)를 진행**한다.

## 5. 프로세스 모델 (택일과 근거)

선택: Phase 1은 요청별 일회용 자식 프로세스다. STT와 같은 `run_json_worker`를 재사용한다. 유휴 타임아웃 상주는 Phase 1에서 쓰지 않는다.

**근거**:
1. 유휴 자원 0 요구를 직접 충족한다. 평시 메모리 점유가 0MB다.
2. 검증된 감독기를 그대로 쓴다. 회수·타임아웃·부모 감시·무자격증명·오프라인 강제·PYTHONPATH 고정이 이미 안정화되어 있다.
3. 배포와 세대가 안전하다. 상주 프로세스는 세대 교체 시 드레인·프리로드·헬스체크가 필요하지만, 일회용 프로세스는 부모 수명에 묶이지 않는다.
4. 캐시가 상주의 이점을 줄인다. 청크 캐시 히트 시 프로세스를 아예 띄우지 않고 파일로 응답한다.

**전환 기준(측정 후 판단)**:
캐시 미스 단문(500자 이하) TTFA p50이 8초(가설, 벤치 결과로 확정)를 넘으면 유휴 타임아웃 상주(타임아웃 5분) 도입을 재검토한다. 전환 시에도 일회용 프로세스는 폴백으로 유지한다.

## 6. 동시성: STT·E5와의 경합 제어 (재설계)

초기 초안의 "공용 슬롯 2개" 가설은 기존 STT 큐(`meetings/local_stt/jobs.py` 단일 consumer) 및 E5(`context/embedder.py` 독립 락)의 실제 구조와 불일치하며 "STT 큐를 건드리지 않는다"는 불변성을 훼손하므로 폐기했다.

- **기존 STT/E5 보존**: STT의 단일 consumer 직렬 큐와 E5의 `asyncio.Lock`은 전혀 수정하지 않는다.
- **TTS 전용 직렬 큐**: TTS 작업은 TTS 전용 직렬 큐(`tts/jobs.py`)에서 처리되어, TTS 작업 상호 간의 CPU/GPU 폭주를 방지한다.
- **STT와 TTS의 동시 실행 허용**:
  - Mac Studio M5 Max는 18개 CPU 코어, 40개 GPU 코어, 64GB 통합 메모리를 보유하고 있어 단일 STT와 단일 TTS 자식 프로세스를 동시에 실행할 하드웨어 자원이 충분하다.
  - CPU 스레드 예산: STT 4스레드(기존 유지) + TTS 2스레드(신규 기본). 총 6스레드로 시스템 전체 코어 수의 33%에 불과하여 CPU 경합 위험이 낮다.
- **동시 실행 저하율 측정 및 안전장치**:
  - 벤치마크 단계에서 STT 단독, TTS 단독, STT+TTS 동시 실행 시의 상호 RTF 저하율 및 음성 생성 지연을 실측한다.
  - 설정 플래그 `LOCAL_TTS_CONCURRENCY_POLICY`(기본값 `allow_concurrent`, 옵션 `defer_to_stt`)를 제공하여, 실측 결과 간섭이 심할 경우 STT 실행 중 TTS를 안전하게 대기시킬 수 있는 여지를 확보한다.

## 7. API 설계

모두 Bearer 인증(owner API)이며 오류 본문은 `{"code","message"}` 계약을 따른다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/tts/status` | `{enabled, ready, reason, engine, model, voice_count}` |
| GET | `/api/tts/voices` | `{voices: [{id, name, lang, gender?, sample_url?}]}` |
| POST | `/api/tts/jobs` | 장문 비동기 합성. `{text(최대 5000자), voice?, speed?}` → 202 `{job_id, chunks, cached_chunks}`. 원문은 DB에 저장하지 않음(§8) |
| GET | `/api/tts/jobs/{id}` | `{status, progress:{done,total}, chunks:[{index, text_hash, audio_url?, duration?}], audio_full_url?, metrics?}`. `audio_url`은 단기 토큰 URL |
| POST | `/api/tts/speak` | 단문 동기 합성 (Fast-path). `{text(최대 500자), voice?, speed?}` → 200 `audio/wav`. 메모리 상에서 직접 처리 및 즉시 반환 |
| DELETE | `/api/tts/jobs/{id}` | 대기·실행 취소. 실행 중이면 자식 회수 후 cancelled 처리 |
| GET | `/api/tts/cache/stats` | `{entries, bytes, hits, misses, max_bytes}` |
| POST | `/api/tts/cache/prune` | 상한 초과·TTL 경과 항목 정리 후 `{removed, freed_bytes}` |

- **스트리밍 방식**: 바이트 스트리밍 대신 청크 단위 완성 + 순차 재생을 채택한다(iOS Safari `<audio>` 호환성 보장 및 청크 캐시 결합).
- **오디오 서빙**: `<audio>` 태그를 위한 단기 토큰 URL(`/api/dl/tts/{token}`, TTL 10분)을 발급하여 인증 헤더 제약을 해결한다.

## 8. 데이터·저장·원문 수명 및 캐시 정책

### 8.1 원문 미저장 불변성과 비동기 Job 수명 규칙

사용자 개인정보 보호 및 데이터 최소화 원칙에 따라, **DB `tts_jobs` 테이블에는 텍스트 원문을 영구 저장하지 않는다.**

- **DB 테이블 정의**:
  - `tts_jobs`: `id`, `status`(queued·running·succeeded·failed·cancelled), `params`(voice, speed, text_length, chunk_count, **원문 제외**), `result_metadata`(지표, 엔진, 모델 리비전), `error`, `created_at`, `started_at`, `finished_at`.
  - `tts_cache`: `chunk_key`(PK), `engine`, `model_revision`, `voice`, `speed`, `chunk_hash`, `audio_relpath`, `bytes`, `sample_rate`, `duration_seconds`, `hits`, `created_at`, `last_hit_at`.
- **비동기 Job의 텍스트 원문 보관 및 삭제 수명**:
  1. **보관 위치**: 캐시 미스가 발생한 비동기 job의 텍스트는 서버 프로세스의 **인메모리 단기 맵(`_pending_job_texts[job_id]`, TTL 10분)** 또는 **보안 임시 디렉터리(`~/.ttakkari/tts-staging/{job_id}.stage`, 0600 권한)**에 일시 보관한다.
  2. **삭제 시점**: 워커 프로세스가 기동되어 stdin을 통해 텍스트를 전달받아 합성에 착수한 직후 **즉시 인메모리 버퍼 또는 스테이징 파일을 영구 파기(shred & unlink)**한다. 작업의 성공/실패 여부와 무관하게 합성 진입 즉시 디스크/메모리에서 원문이 소멸된다.
  3. **서버 재시작(Restart/Crash) 시 복구 규칙**:
     - 서버가 재시작되면 메모리 버퍼나 임시 파일이 소멸되므로, DB에 `queued` 또는 `running` 상태로 남아 있는 미완료 job은 재시도하지 않는다.
     - 기동 시 복구 루틴은 해당 job들의 상태를 `failed`로 전이시키고 error 코드로 `server_restarted_input_purged`를 기록한다.
     - 클라이언트는 상태 폴링 중 이 오류를 수신하면 410 상태로 인지하고, 브라우저가 보관 중인 텍스트 원문으로 사용자에게 재요청(Retry)을 유도한다. 이를 통해 서버 측에 원문이 영구 잔존하지 않는 보안 불변성을 보장한다.

### 8.2 캐시 키 통일

캐시 키의 기본 원자 단위(Atomic Unit)는 **"문장 청크(Chunk)"**로 통일한다.

```text
chunk_cache_key = blake2b("tts1" ‖ engine ‖ model_revision ‖ voice ‖ speed ‖ normalize(chunk_text))
```

- 전체 텍스트 합본(`audio_full`)은 청크 캐시 조각들이 완성된 후 부모 프로세스에서 무손실 PCM 연결로 조립하며, 그 결과는 매니페스트 또는 전체 해시 키로 캐시한다.
- 파일 위치: `<home>/tts-cache/<shard2>/<chunk_cache_key>.wav` (2단계 샤딩).
- 정리 정책: 상한 2GB, 초과 시 LRU 삭제, TTL 30일. job 완료 및 서버 기동 시 정리.

## 9. 한국어 텍스트 정규화 (`tts/normalize.py`)

순수 함수 파이프라인으로 ML 의존 없이 단위 테스트한다.

1. **평문화**: 마크다운 코드 블록·인라인 코드·URL 링크·표·HTML 태그 제거.
2. **이모지 및 특수문자**: 이모지는 발음하지 않고 제거, 기호·특수문자 정리, NFC 정규화.
3. **숫자·기호 한글화**: 날짜(`2026-10-20` → `이천이십육년 십월 이십일`), 시간, 금액, 단위, 백분율을 한국어 발음 텍스트로 치환.
4. **문장 청크 분할**: 종결어미와 문장부호 기준으로 분할. 최대 청크 길이(벤치 확정 전 기본 200자) 초과 시 절 경계(쉼표 등)에서 2차 분할.
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

## 11. 모바일(iOS Safari) 재생 전략

iOS Safari의 엄격한 User Gesture Activation 정책과 백그라운드 제약을 해결하기 위한 구체적 기술 전략을 적용한다.

1. **단일 HTMLAudioElement 재사용 (Audio Priming)**:
   - 사용자가 재생 버튼을 터치하는 순간(동기 제스처 이벤트 내), 전역 단일 `Audio` 인스턴스의 `play()`를 호출(0.01초 무음 wav)하여 브라우저 오디오 언락(unlock)을 획득한다.
   - 비동기 API 응답이 도착할 때까지 새 `Audio` 객체를 생성하지 않고, 언락된 기존 단일 인스턴스를 유지한다.
2. **단일 엘리먼트 소스 체이닝 (Source Chaining)**:
   - 장문 청크 연속 재생 시, 새 Audio 객체를 만들면 제스처 상실로 재생이 차단된다.
   - 따라서 이미 언락된 동일한 `Audio` 인스턴스에서 첫 청크가 재생되는 동안 다음 청크의 오디오 Blob을 프리페치하고, `onended` 핸들러에서 `audio.src = nextChunkUrl; audio.play()`를 호출한다. iOS Safari는 활성화된 동일 인스턴스의 `src` 교체 연속 재생을 허용한다.
   - (보조 대안) `AudioContext`의 타임라인 버퍼 스케줄링(`decodeAudioData` + `AudioBufferSourceNode`)을 병행 검토하여 완전한 갭리스(gapless) 재생을 보장한다.
3. **재생 실패 복구 UX**:
   - 모바일 절전 모드 등으로 인해 청크 전환 시 재생이 차단될 경우, 재생 버튼에 "다음 문장 터치" 안내를 표시하여 원터치로 즉시 이어들을 수 있는 복구 경로를 제공한다.
4. **백그라운드 및 잠금 화면 연동**:
   - `navigator.mediaSession` 메타데이터(제목: "따까리 음성 응답") 및 재생/일시정지 액션 핸들러를 등록하여 잠금 화면 제어를 지원한다.
   - Phase 1의 공식 보장 범위는 화면 켜짐(포그라운드) 상태의 안정적 재생이며, 탭 전환 후 장시간 백그라운드 재생은 Phase 2 고도화로 둔다.
5. **실기기 검증 계획**:
   - 실제 iPhone(iOS 17+ Safari 및 PWA 설치 모드)에서 (1) 단문 즉시 재생, (2) 3문장 이상 청크 연속 재생, (3) 무음 모드 하드웨어 스위치 상태, (4) 화면 잠금 후 복귀 시 상태를 필수 체크리스트로 검증한다.

## 12. 테스트·검증 계획

- **단위 테스트 (`unit_tests/`)**: 한국어 정규화(날짜, 숫자, 마크다운 평문화), 문장 분할 경계, 청크 캐시 키 결정성.
- **API 및 큐 테스트 (`tests/`)**: `/api/tts/*` 라우트, 캐시 히트 시 워커 미호출, 비동기 job 상태 전이.
- **원문 수명 검증**: 워커 기동 즉시 staging 원문 파기 확인, 서버 재시작 시 미완료 job의 `server_restarted_input_purged` 실패 처리 및 410 상태 확인.
- **동시성 및 간섭 검증**: STT 직렬 큐와 TTS 직렬 큐의 동시 실행 시 상호 RTF 저하율 실측.
- **프론트 및 모바일 검증**: 단일 Audio 재사용 및 청크 체이닝 테스트, iOS 실기기 체크리스트 검증.
- **E2E 테스트**: TTS 생성 페이지 E2E, 채팅 메시지 음성 듣기/일시정지/중지 E2E.

## 13. 측정 항목

- **TTFA (Time To First Audio)**: 요청 시작부터 첫 소리가 나기까지의 시간(p50 < 8초 가설 검증).
- **RTF (Real-Time Factor)**: 워커 합성 소요 시간 / 생성된 오디오 길이.
- **메모리 점유**: 워커 프로세스 peak RSS 및 평시 상주 메모리(부모 RSS 증가 0 확인).
- **STT 동시 실행 저하율**: 단독 실행 대비 STT+TTS 동시 실행 시의 RTF 저하율.
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
| `LOCAL_TTS_CPU_THREADS` | 2 | 워커 CPU 스레드 예산 |
| `LOCAL_TTS_CACHE_MAX_GB/TTL_DAYS` | 2/30 | 캐시 용량 상한 및 TTL |
| `LOCAL_TTS_TOKEN_TTL_SECONDS` | 600 | 오디오 재생 단기 토큰 TTL |
| `LOCAL_TTS_CONCURRENCY_POLICY` | allow_concurrent | STT 동시 실행 정책 (allow_concurrent / defer_to_stt) |

## 15. 마이그레이션·배포·운영

- **Alembic Migration**: 번호는 구현 시점 최신 head 다음 revision(현재 작업 트리의 미추적 0053_kb_core, 0054_kb_evidence 감안 시 `0055_local_tts_jobs` 이후)으로 생성한다. `tts_jobs`, `tts_cache` 테이블 생성 및 downgrade 지원.
- **배포 및 운영**: 기능 코드 변경이 없는 본 설계 단계에서는 배포 요청을 하지 않는다. 운영 시에는 캐시 용량 모니터링과 미준비 상태(`ready=false`)에 대한 사용자 가이드를 제공한다.

## 16. WBS 개요 (PMS 「따까리 · 로컬 TTS」 프로젝트)

1. 설계·경계 확정 (분석 + local-tts.md + ADR 0062 개정) — 완료
2. 한국어 정규화·문장 분할·청크 캐시 키 (순수 모듈 + 단위 테스트)
3. 엔진 추상화 + Kokoro 워커 (.tts-runtime 격리)
4. [GATE] 한국어 음성 품질 평가 및 모델 채택 (사용자 승인)
5. 영속 큐·API + 원문 단기 수명 관리 + 오디오 토큰 서빙
6. DB migration (0055 이후) + 캐시 정리 정책 + 운영 문서
7. TTS 독립 직렬 큐 + STT 동시 실행 부하/RTF 측정 및 간섭 검증
8. TTS 페이지 프론트 (생성·재생·다운로드)
9. 채팅 재생 버튼 + 단일 Audio 소스 체이닝 + iOS 실기기 검증
10. E2E·회귀·측정 리포트 + 배포

## 17. 미확인 항목

1. Kokoro 한국어 품질·권장 청크 길이·출력 형식·샘플레이트·로드 시간·메모리 (병행 벤치 세션 대기).
2. 한국어 품질 게이트 [GATE] 사용자 채택 결정 (품질 미달 시 대안 승인).
3. TTFA 목표 8초의 타당성 (벤치 실측 결과로 확정).
4. 오디오 토큰 서빙: 기존 download-link 인프라 재사용 vs 전용 토큰 테이블.
5. 프론트엔드 작업 전 front 저장소 분기(`ahead 3, behind 73`) 및 작업 트리 정리.
6. Alembic 다음 번호 (구현 시점 최신 head 다음, 0055 이후 확인).

## 부록 A. 작업 로그

- 2026-10-20: 기존 STT·프론트 구조 분석 후 본 설계 초안 작성.
- 2026-10-20: 독립 검증(GPT-6 Luna) failed 판정 사항 반영 전면 개정 (ADR 0061→0062, Alembic 0055 이후 명시, 공용 슬롯 폐기 및 독립 동시성 재설계, 비동기 원문 인메모리/스테이징 수명 주기 및 재시작 실패 처리 확정, iOS 단일 Audio 소스 체이닝 전략 구체화, 한국어 품질 평가 사용자 승인 [GATE] 명시, 청크 캐시 키 통일).
