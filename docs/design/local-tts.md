# 로컬 TTS 설계: 별도 생성 페이지와 채팅 음성 재생 (개정 4판)

- 상태: 설계. 검증대기 (2026-10-20 4차 독립 검증 지적 2건 반영 개정). 이 단계에서는 기능 코드를 변경하지 않는다.
- 날짜: 2026-10-20.
- 관련: [ADR 0025](../adr/0025-on-demand-local-stt.md), [ADR 0026](../adr/0026-local-first-meetings.md), [ADR 0027](../adr/0027-retire-clovanote.md), [ADR 0028](../adr/0028-stt-quality-and-speaker-utterances.md), [STT 안내](../stt/README.md), [API 계약](../api-contract.md), ADR [0062](../adr/0062-local-tts.md).
- **엔진 상태 (2026-10-20 기준)**: Kokoro-82M은 한국어 음성이 없어 한국어 품질 미달로 확인되었고(사용자 전달 사항. 벤치 보고 원문은 이번 개정에서 읽지 않았다), 대체 모델은 사용자가 결정한다. 이 설계는 `TtsProvider` 추상화(§4.1)로 엔진 중립을 유지한다. 본문에서 Kokoro를 전제로 둔 수치(TTFA 목표, 스레드 수, 청크 길이, 모델 경로 등)는 모두 **[엔진 결정 후 재측정]** 으로 표기하며 확정값이 아니다. 엔진 결정 전에는 구현(WBS 5 이후)에 착수하지 않는다.
- 3차 검증 지적 반영 위치: 원문 수명 §8.2, 취소 경합·terminal 상태 확정 §8.3.2, drain/claim §8.3.3·§8.3.4, 소유자 생존 §8.3.5, GET 응답 §7.2.
- 4차 검증 지적 반영 위치: draining 정리와 세대 퇴역의 순서 보장 §8.3.3(퇴역 차단 guard), 사망 확인의 읽기 시점 규칙 §8.3.5, Bearer 근거 경로 정정 §7.2.

## 1. 사용자 결정 (확정)

1. **별도 생성 페이지**: TTS는 회의록(STT) 기능에 넣지 않고 별도 TTS 생성 페이지를 만든다. 텍스트 입력에서 음성을 생성하고 재생·다운로드한다. 음성 종류와 속도 설정이 있다.
2. **채팅 응답의 선택적 음성 재생**: 따까리 응답 메시지에 선택적 음성 재생 버튼을 둔다. 재생·일시정지·중지. 텍스트 응답이 기본이며 음성은 선택이다. 모바일(iOS Safari 자동재생 정책 및 제스처 제약) 환경을 고려한 복구 UX를 구비한다.
3. **엔진 추상화 및 채택 원칙**: 엔진은 Kokoro-82M 우선, 로컬·무료. 엔진 교체 가능한 인터페이스로 추상화한다. Kokoro 한국어 품질이 실사용 기준을 충족하면 구현으로 정상 진행한다. 단, 한국어 품질이 실사용 기준에 미달하여 타 모델로의 교체가 필요할 때에만 임의로 바꾸지 않고 벤치 결과와 대안을 사용자에게 보고하여 최종 결정을 받는다. **현재 상태: Kokoro는 한국어 음성 부재로 미달이 확인되어 대체 모델 결정이 사용자에게 넘어가 있다. 모델은 임의로 선택·설치하지 않는다.**
4. **자원 최소화 및 독립 실행**: 대형 모델 가중치는 평시에 메모리에 상주시키지 않는다. 기존 STT 직렬 큐와 E5 임베딩 락을 일체 수정하지 않고 독립 직렬 큐로 분리한다. 문장 청크 단위 캐싱 및 한국어 텍스트 정규화 단계를 둔다.

## 2. 기존 구조 분석 요약

### 2.1 백엔드 STT 및 모델 구조 (재사용 및 격리 대상)

- **일회용 모델 프로세스 감독기** `src/ttakkari/model_process.py::run_json_worker`: 별도 Python에 `모듈 + 출력경로`로 실행하고 요청 JSON을 stdin으로 전달, 프로세스 종료 후 결과 JSON 파일을 읽는 단일 배치 계약이다. 자식 환경은 PATH·HOME·TMPDIR·locale만 상속하고 HF 오프라인·암묵 토큰 금지·텔레메트리 금지를 강제한다. PYTHONPATH는 배포본 `src`로 고정한다. 출력 64MB 상한, 타임아웃 시 kill 후 반드시 회수한다. 결과에 `process_exited·worker_pid·wall_seconds`를 붙인다.
- **자식 프로세스 수명과 부모 감시**: 자식 프로세스는 `watch_parent(parent_pid, stop)` 스레드(`model_process.py:87`)를 통해 부모 PID를 주기적으로 감시하며, 부모 프로세스가 종료되거나 고아(PID 1)가 되면 `os._exit(74)`로 즉시 자살한다. 따라서 일회용 프로세스라 하더라도 실행 중에는 부모 프로세스의 수명에 엄격히 종속된다. 감시는 `run_json_worker`가 강제하지 않고 각 워커 모듈이 시작해야 하므로(`context/embedder_worker.py:48`, `meetings/local_stt/worker.py:269`) TTS 워커도 반드시 시작해야 한다. 또 감시 주기가 1초 폴링(`model_process.py:88`)이라 부모 사망 직후 최대 약 1초 동안 자식이 남을 수 있다(정확한 지연은 확인 필요). STT 큐가 running 행이 남아 있으면 다음 job을 시작하지 않는 이유가 이것이다(`meetings/local_stt/jobs.py:255-257`).
- **`run_json_worker`의 요청 참조 수명**: `request` 인자(`model_process.py:17`)는 호출이 끝날 때까지 호출 프레임이 쥐고, `json.dumps(request).encode()`로 만든 bytes(`model_process.py:65`)는 `process.communicate()` 코루틴이 자식이 종료·타임아웃·취소로 `wait_for`가 끝날 때까지 쥔다(`model_process.py:63-67`). 따라서 이 감독기를 그대로 쓰는 한 **부모는 stdin 전달 직후가 아니라 자식 종료까지 원문을 메모리에 보유한다.** 호출자가 자기 쪽 딕셔너리에서 지워도 위 두 참조는 남는다. 이 감독기의 호출자는 STT(`meetings/local_stt/runner.py:28`)와 E5(`context/embedder.py:64`) 두 곳이며, 두 호출 경로 테스트(`tests/test_embedder_lifetime.py`, `tests/test_local_stt_benchmark.py`)가 `run_json_worker` 이름을 monkeypatch한다. TTS 설계는 이 사실을 §8.2에서 계약으로 반영한다.
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
  jobs.py          # TTS 전용 직렬 큐: 세대별 claim(CAS)→워커 실행→terminal 확정(CAS), 인메모리 원문 수명, drain·사망 확인 정리 (§8.3)
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

- 부모 프로세스는 이 Protocol만 본다. 엔진별 차이는 워커 모듈(`worker_kokoro.py`는 첫 어댑터 이름일 뿐이며 대체 모델 결정 시 엔진 이름의 워커 모듈로 교체)과 설정 안에 가둔다. 큐·캐시·API·프론트는 엔진 이름을 알지 않는다(캐시 키에는 `engine`·`model_revision`이 들어가므로 엔진을 바꿔도 키가 섞이지 않는다).
- 어댑터의 플러그형 항목: 모델 경로·리비전, 음성 목록 파일, 권장 청크 길이, 출력 형식·샘플레이트, 디바이스(cpu 기본). 엔진이 결정되면 어댑터 내부와 설정 기본값만 바꾼다. 이 문서의 TTFA·스레드·청크 길이 수치는 Kokoro 전제 가설이므로 **[엔진 결정 후 재측정]** 이다.
- **모델 채택 원칙 및 조건부 사용자 승인 [GATE]**:
  - Kokoro 한국어 모델이 실사용 품질 기준을 충족하면 추가 게이트 승인 없이 정상적으로 구현 단계로 진행한다. (현재는 한국어 음성 부재로 미달이 확인되어 아래 조건 분기에 해당한다.)
  - **오직 Kokoro 한국어 모델이 실사용 기준에 미달하여 타 모델로의 전환이 필요할 때에만**, 임의로 변경하지 않고 벤치 결과와 대안을 사용자에게 보고하여 최종 채택 승인을 받는다. 대체 모델 후보는 이 문서가 정하지 않는다(이전 판의 Piper·ChatTTS 언급은 예시였으며 삭제했다). 결정 후 §5·§6·§9·§13·§14의 [엔진 결정 후 재측정] 수치를 새 엔진으로 다시 측정한다.

## 5. 프로세스 모델 (택일과 근거)

선택: Phase 1은 요청별 일회용 자식 프로세스다. `run_json_worker`를 재사용한다. 유휴 타임아웃 상주는 Phase 1에서 쓰지 않는다.

**근거** (수치는 [엔진 결정 후 재측정]):
1. 대형 모델 가중치(수백 MB)의 평시 메모리 점유를 방지한다. 단, 부모 프로세스의 라우터·큐 상태·캐시 메타데이터 관리를 위한 기본 메모리(수 MB)는 정상 소비된다(평시 0MB 과장 배제).
2. 검증된 감독기를 그대로 쓴다. 회수·타임아웃·무자격증명·오프라인 강제·PYTHONPATH 고정이 이미 안정화되어 있다. 부모 감시는 워커 모듈이 `watch_parent`를 시작해야 성립한다(§2.1). 감독기를 그대로 쓰면 요청 원문이 자식 종료까지 부모에 남는 점은 §8.2에서 계약으로 다룬다.
3. 자식 프로세스는 `watch_parent` 감시 스레드(1초 폴링)로 부모 사망 시 스스로 종료한다. 고아가 잠시 남을 수 있으므로 큐는 running 행이 있는 동안 새 claim을 하지 않는다(§8.3.4).
4. 캐시 히트 시에는 자식 프로세스를 아예 띄우지 않고 파일로 응답한다.

**전환 기준(측정 후 판단)**:
캐시 미스 단문(500자 이하) TTFA p50이 8초(Kokoro 전제 가설, [엔진 결정 후 재측정])를 넘으면 유휴 타임아웃 상주(타임아웃 5분) 도입을 재검토한다. 전환 시에도 일회용 프로세스는 폴백으로 유지한다.

## 6. 동시성: STT·E5와의 경합 제어

- **기존 STT/E5 보존**: STT의 단일 consumer 직렬 큐와 E5의 `asyncio.Lock`은 전혀 수정하지 않는다.
- **TTS 전용 직렬 큐**: TTS 비동기 작업은 TTS 전용 직렬 큐(`tts/jobs.py`)에서 처리되어 동시 다발적 프로세스 폭주를 방지한다.
- **직렬 실행 예산의 단위**: 프로세스 안의 `asyncio.Lock` 하나로는 세대가 겹치는 구간(구 세대 draining + 신 세대 active)에서 전역 1개를 보장하지 못한다. 두 세대는 서로 다른 프로세스이기 때문이다. 그래서 예산 단위를 둘로 나눈다.
  - **세대 모드**(`runtime_generation=true`): `generation.work("tts")`가 잡는 OS 파일 잠금 `<home>/runtime/work-tts.lock`(`generation.py:261-277`, `_lock`은 `LOCK_EX | LOCK_NB`)이 세대를 가로지르는 전역 1개 예산이다. 큐 consumer와 `/speak`가 모두 이 잠금 안에서만 자식을 띄운다. STT는 `consume("stt", ...)`으로 같은 방식의 `work-stt.lock`을 쓴다(`generation_service.py:100-114`). 이 잠금은 STT 큐 잠금과 이름이 달라 서로 막지 않는다.
  - **세대 미사용 모드**(`runtime_generation=false`, `config.py:107`, `main.py:144-148`): 단일 프로세스이므로 프로세스 내 `asyncio.Lock`(`_tts_execution_lock`)이 예산이다.
  - `work()`는 상태가 active가 아니면 잠금을 잡지 않고 False를 준다(`generation.py:264-266`). 따라서 draining 세대의 `/speak`는 새 합성을 시작하지 않고 503을 돌려준다(§8.3.3).
- **단문 동기(/speak)와 장문 비동기(/jobs)의 직렬 실행 예산 준수**: `/jobs` 워커가 실행 중일 때 `/speak`가 들어오면 같은 예산을 최대 15초 대기하고, 초과 시 503(busy)을 반환한다.
- **STT와 TTS의 동시 실행 및 간섭 [엔진 결정 후 재측정]**:
  - STT는 4스레드(`local_stt_cpu_threads`)를 쓴다. TTS 스레드 수는 엔진 결정 전이라 가설이다.
  - 동시 실행 시 상호 RTF 저하 및 CPU 캐시·메모리 대역폭 간섭은 **실측 전까지 미검증**이다. 이 설계는 STT와 TTS가 서로 다른 잠금을 쓰므로 동시 실행이 가능하다는 점만 확인하며, 간섭이 없다고 주장하지 않는다.
  - 벤치 단계에서 단독 vs 동시 실행의 상호 RTF 저하율을 실측하여 `LOCAL_TTS_CONCURRENCY_POLICY`(기본값 `allow_concurrent`, 옵션 `defer_to_stt`)의 최종 기본값을 확정한다.

## 7. API 설계

모두 Bearer 인증(owner API)이며 오류 본문은 `{"code","message"}` 계약을 따른다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/tts/status` | `{enabled, ready, reason, engine, model, voice_count}` |
| GET | `/api/tts/voices` | `{voices: [{id, name, lang, gender?, sample_url?}]}` |
| POST | `/api/tts/jobs` | 장문 비동기 합성. `{text(최대 5000자), voice?, speed?}` → 202 `{job_id, chunks, cached_chunks}`. 원문은 DB에 저장하지 않음(§8). 이 세대가 active가 아니면 503 `generation_not_active`(Retry-After, 접수하지 않음, §8.3.3) |
| GET | `/api/tts/jobs/{id}` | 상태 조회. 인증 실패 401, 형식 오류·미존재·타 소유자 404, **접근 가능한 job은 항상 200 + `status` 필드로 구분**(§7.2). `{status, error_code?, retryable?, progress:{done,total}, chunks:[{index, text_hash, audio_url?, duration?}], audio_full_url?, metrics?}` |
| POST | `/api/tts/speak` | 단문 동기 합성 (Fast-path). `{text(최대 500자), voice?, speed?}` → 200 `audio/wav`. 직렬 락 획득 후 단일 청크 합성 및 즉시 반환 |
| DELETE | `/api/tts/jobs/{id}` | 대기·실행 취소. DB를 조건부 UPDATE로 `cancelled` 확정(§8.3.2), 소유 세대가 원문을 지우고 자식을 회수한다. 이미 terminal이면 상태 변경 없이 현재 상태를 200으로 돌려준다(멱등). 인증·404 규칙은 GET과 같다 |
| GET | `/api/tts/cache/stats` | `{entries, bytes, hits, misses, max_bytes}` |
| POST | `/api/tts/cache/prune` | 상한 초과·TTL 경과 항목 정리 후 `{removed, freed_bytes}` |

### 7.1 워커 감독기 계약과 청크 응답 방식

- `src/ttakkari/model_process.py::run_json_worker`는 `process.communicate()`로 프로세스 종료 후 `result.json`을 단일 배치로 반환하는 계약이다.
- 따라서 단일 `run_json_worker` 실행 중에 첫 청크를 실시간으로 조기 스트리밍하는 것은 현재 감독기 계약상 불가능하다.
- 불필요한 프로토콜 확장(IPC stdout ndjson 스트리밍 등)을 피하고 검증된 감독기를 그대로 재사용하기 위해, Phase 1에서는 **워커 프로세스가 완료된 후 청크 목록을 일괄 반환**하는 계약을 유지한다.
- 첫 청크 조기 제공(빠른 TTFA)은 **문장 청크 단위 캐시 히트 패스**에서 실현되며, 캐시 히트된 청크는 워커 기동 없이 즉시 클라이언트에 전달된다.
- **사용자 요구와의 간극(투명 표기)**: 사용자 요구에는 청크가 만들어지는 즉시 재생하는 것이 포함되어 있다. 캐시 미스 job은 Phase 1에서 워커가 끝난 뒤에야 청크가 한꺼번에 보이므로 이 요구를 만족하지 못한다. 체감 지연은 캐시 히트와, 긴 글을 여러 job으로 나누는 클라이언트 분할로만 줄이며, 진짜 청크 스트리밍은 감독기 프로토콜 확장(Phase 2)이 필요하다. 이 간극은 구현 전 사용자에게 한 번 확인한다.

### 7.2 상태 조회 응답 계약 (GET /api/tts/jobs/{id}, DELETE 동일)

HTTP 오류 본문은 전역 핸들러가 `{code, message}`로 고정한다(`main.py:259-269`).

| 상황 | HTTP | 본문 | 근거 |
|---|---|---|---|
| Bearer 없음·무효·만료 | 401 | `{code:"unauthorized"}` | 핸들러가 `_: User`(`security.auth.User` = `Depends(require_user)`, 기존 사용례 `api/meetings.py:35, 109`)를 받으면 `security/auth.py:58-72`가 헤더 없음·Bearer 아님(`:62`), 만료(`:68`), 무효(`:70`)를 `WWW-Authenticate: Bearer`와 함께 401로 낸다. job 조회 전에 판정된다. (`api/auth.py`는 로그인·refresh 쿠키 엔드포인트이며 Bearer 검증 근거가 아니다) |
| `id`가 UUID 형식이 아님 | 404 | `{code:"not_found"}` | 경로 인자를 `str`로 받아 직접 파싱한다. `uuid.UUID` 타입 인자로 두면 전역 검증 핸들러가 422를 내므로(`main.py:279-284`) 쓰지 않는다 |
| 존재하지 않는 job | 404 | `{code:"not_found"}` | |
| 접근 권한이 없는(타 소유자) job | 404 | `{code:"not_found"}` | 존재 여부를 노출하지 않으려고 미존재와 같은 응답. 현재 단일 소유자 시스템(토큰 `sub`가 `owner`가 아니면 거부, `security/auth.py:32, 65`)이라 이 분기는 실제로 발생하지 않으며, 다중 소유자가 생기면 job에 소유 식별자 컬럼을 추가해 같은 404로 처리한다 |
| 접근 가능한 job | 200 | `{status: queued|running|succeeded|failed|cancelled, ...}` | `failed`·`cancelled`도 HTTP 200이다. 410은 쓰지 않는다 |
| DB·세대 전환 보류 | 503 | `{code:"deploying"}` 등 | `generation_service.py:252-258`의 기존 동작 |

- 200 본문 규칙: `status="failed"`면 `error_code`와 `retryable`(재요청으로 해결 가능한지)을 둔다. `status="cancelled"`는 `error_code` 없음. `succeeded`는 `chunks`와 `audio_full_url`을 둔다. 이전 판의 "인증·404까지 모두 200" 해석은 철회한다. 200으로 일원화되는 범위는 접근 가능한 job의 상태 구분뿐이다.
- `error_code` 목록(§8.3 전이표와 1:1): `input_ttl_expired`, `generation_draining`, `owner_shutdown`, `owner_gone`, `server_restarted`(세대 미사용 모드), `worker_failed`, `worker_timeout`, `input_missing`. `retryable`은 앞의 여섯 개와 `worker_timeout`이 true, `worker_failed`는 false다(입력 문제일 수 있어 같은 입력 재시도가 무의미할 수 있음).
- 이전 판의 `drained_without_completion`, `server_restarted_input_purged`는 폐기한다. 전자는 §8.3.3의 `generation_draining`이 대체하고, 후자는 사망 확인 규칙(§8.3.5) 하의 `owner_gone`가 대체한다.

## 8. 데이터·저장·원문 인메모리 수명 및 Generation 정책

### 8.1 원문 미저장 불변성 (설계상 데이터 최소화 방침)

개인정보 보호 및 데이터 최소화 설계 방침(Data Minimization by Design)에 따라, **DB `tts_jobs` 테이블에는 텍스트 원문을 영구 저장하지 않는다.**

- **DB 테이블 정의**:
  - `tts_jobs`: `id`, `owner_id`(접수 세대 UUID. 세대 모드는 `GenerationRuntime.id`(`generation.py:100`), 세대 미사용 모드는 프로세스별 임의 UUID. `runtime_generations`에 FK를 걸지 않는다), `status`(queued·running·succeeded·failed·cancelled), `params`(voice, speed, text_length, chunk_count, 원문 제외), `result_metadata`, `error_code`, `created_at`, `started_at`, `finished_at`. 인덱스: `(status, owner_id, created_at)`.
  - `tts_cache`: `chunk_key`(PK), `engine`, `model_revision`, `voice`, `speed`, `chunk_hash`, `audio_relpath`, `bytes`, `sample_rate`, `duration_seconds`, `hits`, `created_at`, `last_hit_at`.
- 워커가 돌려주는 `result.json`에도 원문을 싣지 않는다. 부모는 청크 키·길이·지표만 허용 목록으로 골라 `result_metadata`에 저장한다. 이 값은 DB에 남기 때문이다.

### 8.2 비동기 Job의 텍스트 원문 인메모리 수명 주기

Phase 1의 입력 원문 보관은 **서버 프로세스 인메모리 버퍼 하나로 확정**한다. 임시 디스크 staging 파일 대안은 배제하며, 초대용량 텍스트 대응이 필요한 경우 미래 검토(Phase 2)로 분리한다.

**계약(정직한 수명)**: Phase 1은 기존 `run_json_worker`를 변경 없이 그대로 쓴다. 이 감독기는 `request` 인자와 stdin용 bytes를 자식이 끝날 때까지 쥔다(§2.1, `model_process.py:17`, `:63-67`). 따라서 **부모의 원문 보유 상한은 "claim 후 자식 종료(정상·타임아웃·취소·kill/reap) 시점"이며 "stdin 전달 직후"가 아니다.** 이전 판의 "stdin 전달 직후 부모 참조 해제" 문구는 이 계약과 맞지 않아 철회한다.

1. **보관 위치**: 캐시 미스 job의 청크 텍스트는 접수한 프로세스의 `_pending_texts: dict[UUID, list[str]]`에만 둔다. 부모가 원문을 들고 있는 구간은 접수 시점부터 job이 terminal이 되는 시점까지다.
2. **참조 수명 및 소멸 단계**:
   - (a) 접수: `POST /api/tts/jobs`가 정규화·분할한 청크를 `_pending_texts[job_id]`에 넣고 DB에는 메타데이터만 커밋한다.
   - (b) claim: 소유 세대의 단일 consumer가 §8.3.4의 조건부 UPDATE로 `running`을 확정한다. 이 시점에 `_pending_texts[job_id]`가 없으면(`input_missing`) 그 job을 `failed`로 확정한다.
   - (c) 실행: `run_json_worker(request=...)`를 호출한다. **호출이 끝날 때까지 부모는 원문을 보유한다.** 호출자가 `_pending_texts`에서 미리 지워도 감독기 내부 참조는 남으므로, 의미 있는 해제는 호출 종료 뒤다. 호출이 끝나고 호출 프레임이 사라지면 CPython 참조 카운트로 곧 수거되나 순환 참조·할당기 보존 때문에 즉시 소멸을 보장하지 않는다.
   - (d) 자식: stdin에서 읽은 청크를 합성하고 종료하면 OS가 주소 공간을 회수한다.
   - (e) OS 한계: swap, 프로세스 덤프, 물리 RAM 잔존은 애플리케이션이 보장할 수 없다. `shred & unlink`나 물리적 영구 소멸 표현은 쓰지 않는다.
3. **수명 종료 규칙**(모든 terminal 전이는 §8.3.2의 조건부 UPDATE가 성공한 경우에만 확정하고, 확정 후 `_pending_texts.pop`):
   - **TTL**: queued 상태로 10분 초과 시 `failed(input_ttl_expired)`.
   - **취소(`DELETE`)**: DB가 `cancelled`로 바뀌면 소유 세대가 다음 틱(§8.3.2)에 원문을 지우고 실행 중이면 자식을 kill/reap한다.
   - **실패/완료**: 확정 직후 지운다.
   - **drain·종료·사망**: §8.3.3, §8.3.5.
   - 위 규칙과 별개로 consumer 틱마다 `_pending_texts`의 키 중 DB 상태가 terminal이거나 행이 없는 것을 정리한다(다른 세대·다른 경로에서 취소된 job의 잔존 방지).
4. **조기 해제가 필요해질 경우(Phase 2 옵션, 지금은 하지 않음)**: (i) TTS 전용 래퍼가 `asyncio.create_subprocess_exec`로 자식을 직접 띄워 stdin에 쓰고 닫은 뒤 자기 참조를 놓는 방식은 `run_json_worker`의 환경 구성·64MB 상한·회수 로직을 복제하게 되므로 어긋남 위험이 있다. (ii) 이를 피하려고 `model_process.py`에서 환경 구성부를 공용 함수로 추출하면 STT(`meetings/local_stt/runner.py:28`)와 E5(`context/embedder.py:64`)가 같은 코드 경로를 지나므로, 동작 불변 리팩터로 한정하고 `tests/test_embedder_lifetime.py`·`tests/test_local_stt_benchmark.py`와 `model_process` 신규 테스트로 동작 불변을 확인한 뒤에만 진행한다. 두 안 모두 이번 범위 밖이다.

### 8.3 Generation 수명 주기·소유권·terminal 확정·drain·사망 확인

세대 교체 환경에서 인메모리 원문은 프로세스 경계를 넘을 수 없다. 아래 규칙은 실제 코드에 맞춘 것이다.

#### 8.3.1 기존 코드의 사실 (근거)

- `activate()`는 이전 활성 세대를 `draining`, 대상을 `active`로 바꾼다(`generation.py:69-86`).
- `work(name)`은 상태가 active일 때만 True를 주고(`generation.py:264-266`, 잠금 후 재확인 `:274`), 파일 잠금 `work-<name>.lock`을 세대 간 전역 단일로 쓴다(`:267`). consumer는 `work()`가 True일 때만 `next_job()`을 부르므로(`generation_service.py:105-107`) **draining 세대는 새 job을 시작하지 않지만, 이미 `async with` 안에서 실행 중인 `execute`는 중단되지 않고 끝까지 간다.**
- `can_retire()`는 draining이며 `_work`·`http_inflight`·`owned_runs`가 비었을 때 True다(`generation.py:289-291`). **`_pending_texts`는 이 조건에 없다.** 즉 미claim queued job의 원문이 남아 있어도 구 세대는 퇴역할 수 있고, 그 순간 원문은 사라진다.
- STT 큐는 DB에서 `queued`를 고르되 `running` 행이 하나라도 있으면 None을 돌려준다. 부모가 죽어도 자식이 살아 있을 수 있다는 이유다(`meetings/local_stt/jobs.py:253-261`).
- 세대 등록은 먼저 `<home>/runtime/<generation_id>.lock`을 잡고(`generation.py:112-114`), 행을 넣은 뒤 다른 세대의 잠금을 확인해 잠금을 얻을 수 있으면(=보유 프로세스 없음) 그 행을 `offline`으로 바꾼다(`:125-136`). 정상 종료 시 `close()`도 `offline`으로 바꾼다(`:310-317`).
- 모듈 머리말은 "하트비트 만료로 lease를 훔치지 않는다. DB 연결 상실은 소유 프로세스나 자식이 외부 작업을 멈췄다는 증거가 아니다"라고 명시한다(`generation.py:1-4`). `heartbeat_at`은 `refresh()`가 갱신만 한다(`:159`).

#### 8.3.2 상태 전이와 취소 경합: 조건부 UPDATE(compare-and-set)

terminal 상태는 `succeeded`, `failed`, `cancelled`이며 **terminal에서 나가는 전이는 없다.** 모든 상태 변경은 SELECT 후 쓰기가 아니라 `UPDATE tts_jobs SET ... WHERE id=:id AND status=:expected [AND owner_id=:me]` 한 문장이고, **영향 행 수가 1일 때만 전이가 확정**된다. 0이면 전이에 졌다는 뜻이므로 부수 효과(결과 노출, 원문 보관 연장)를 모두 버리고 현재 행을 다시 읽어 따른다.

| 전이 | 주체 | WHERE 조건 | 성공 후 |
|---|---|---|---|
| queued→running | 소유 세대 consumer | `status='queued' AND owner_id=:me` + §8.3.4 조건 | 실행 시작 |
| running→succeeded | 소유 세대 | `status='running' AND owner_id=:me` | 결과 노출 |
| running→failed (워커 오류·타임아웃) | 소유 세대 | `status='running' AND owner_id=:me` | 원문 해제 |
| queued·running→cancelled | DELETE를 받은 임의 세대 | `status IN ('queued','running')` (owner 조건 없음) | 소유 세대가 감지 후 정리 |
| queued→failed (TTL·drain·종료) | 소유 세대 | `status='queued' AND owner_id=:me` | 원문 해제 |
| queued·running→failed (소유자 사망) | 사망을 확인한 세대 | `status IN ('queued','running') AND owner_id=:dead` + §8.3.5 증거 | 정리 |

- **DELETE 경합**: DELETE가 먼저 `cancelled`로 확정하면 이후 worker의 `running→succeeded`는 `status='running'`을 만족하지 못해 영향 행 0이 되고, worker는 결과를 노출하지 않는다. 반대로 worker가 먼저 `succeeded`를 확정했다면 DELETE의 UPDATE가 0행이 되고 DELETE는 현재 상태(`succeeded`)를 200으로 돌려준다. 어느 쪽이든 `cancelled`가 `succeeded`로 덮이는 일은 없다.
- **결과 노출 순서**: 청크 오디오는 내용 주소 캐시에 임시 파일 후 rename으로 먼저 쓴다. 그 뒤 `running→succeeded` UPDATE가 성공해야 job에 청크 목록을 연결한다. UPDATE가 0행이면 job에는 연결하지 않으며, 이미 쓴 캐시 파일은 job과 무관한 일반 캐시로 TTL·LRU로만 정리된다(취소가 캐시 오디오를 즉시 지우지는 않는다. 원문 텍스트가 아니라 오디오이며, 이 한계를 명시한다).
- **취소 감지**: DELETE를 받은 세대가 소유 세대와 다를 수 있다. 소유 세대는 (1) 실행 중 `runtime_poll_seconds`(기본 5초, `config.py:109`)보다 짧은 주기로 자기 running job의 `status`를 읽어 `cancelled`면 워커 태스크를 취소한다(취소 시 `run_json_worker`의 `finally`가 kill 후 `wait`한다, `model_process.py:81-84`), (2) claim 직전에는 claim UPDATE 자체가 0행이 되어 실행되지 않으며, (3) 성공 확정 직전에도 UPDATE 조건이 막는다. 감지 주기 값은 구현 시 정한다(확인 필요).
- 같은 세대가 DELETE를 받은 경우에는 같은 UPDATE 후 즉시 로컬 태스크 취소와 원문 해제를 수행한다.

#### 8.3.3 drain: 하나로 확정 (정리와 퇴역의 순서 보장 포함)

**결정: draining 세대는 이미 `running`인 job만 끝낸다. 아직 claim되지 않은 `queued` job은 구 세대가 계속 처리하지 않고 자기 소유 queued를 `failed(generation_draining, retryable)`로 확정하고 원문을 비운다. 이 정리가 끝나기 전에는 세대가 퇴역하지 못하도록 TTS가 기존 퇴역 차단 장치에 "queued guard 태스크"를 등록한다. 공유 세대 코드(`work`·`can_retire`·`ready`)는 변경하지 않는다.**

**4차 검증이 지적한 결함**: 이전 판은 TTS 루프가 draining을 감지해 queued를 실패시키되, 그 정리 완료를 세대 퇴역 판정이 알지 못했다. `can_retire()`는 `_pending_texts`를 보지 않으므로(`generation.py:289-291`) 정리 전에 퇴역하면 DB에는 `queued`가 남고 원문만 사라질 수 있었다.

**퇴역이 실제로 결정되는 경로(코드 확인)**:
- 외부 deployer가 `/api/ready`를 호출한다(`generation_deployment.py:239-249`, 라우트 `api/system.py:73-82`).
- `/api/ready`의 `drain_ready`는 `runtime.can_retire() and not (manager._handles or pending)`이다(`generation_service.py:158-168`, 즉 `:166`). `pending`은 미완료 태스크 수이며 `runs.manager._background`, assistant 인덱싱 태스크, `usage_store._tasks`, 벡터 `_pending`을 센다(`generation_service.py:159-165`). **`_background`는 모듈 수준 `set[asyncio.Task]`이며 이 목록에 태스크가 있는 동안 `drain_ready`는 False다**(`runs/manager.py:1681`, 등록·제거 방식 `:1705-1709`).
- deployer는 이전 세대의 `drain_ready`가 참일 때만 퇴역시키고(`generation_deployment.py:710-713`), `stop()`도 `drain_ready`가 아니면 거부한다(`generation_deployment.py:294-297`). 퇴역은 `launchctl bootout`이다(`:301`).
- `bootout`이 보내는 종료 신호 뒤에는 lifespan 정리가 돈다(`generation_service.py:229`의 `jobs.stop()` 등). 그러나 이 정리가 끝까지 실행된다는 보장(종료 유예 시간)은 저장소에서 설정을 찾지 못했다(확인 필요). 그래서 정상 경로의 순서 보장을 정리 루틴에 기대지 않는다.

**확정한 설계 (우선 검토 대안 중 (b)에 해당: 이미 있는 퇴역 차단 장치 사용)**:
1. **불변식 G**: "이 프로세스가 소유한 `queued` TTS job이 하나라도 있는 동안, `runs.manager._background`에 미완료 `tts-queued-guard` 태스크가 반드시 존재한다." 따라서 `drain_ready`는 소유 queued가 모두 claim되거나(`running`이 되면 `work("tts")`가 `_work`에 있어 `can_retire()`가 False, `generation.py:291`) terminal이 될 때까지 False다. 공유 코드는 건드리지 않고 `generation_service.ready()`가 이미 읽는 집합에 태스크를 넣을 뿐이다(`ready()`가 `_background`를 import하는 방식과 같음, `generation_service.py:159-163`).
2. **guard 생성 시점**: `POST /api/tts/jobs` 핸들러가 DB에 job을 커밋하기 **전에** `ensure_guard()`를 호출한다(이미 살아 있으면 재사용, `asyncio.Lock`으로 생성·종료 판정을 직렬화). 핸들러가 실행되는 동안은 `http_inflight`도 올라가 있어(`InflightMiddleware`, `generation_service.py:259-267`, `/api/ready`·`/api/health`·`/api/version`만 제외) `can_retire()`가 False이므로, "queued는 있는데 guard가 없는" 순간은 없다.
3. **guard 본문**: 주기(`runtime_poll_seconds`, 기본 5초, `config.py:109`)마다 `runtime.refresh()`로 상태를 읽는다. 상태가 active가 아니면 `UPDATE tts_jobs SET status='failed', error_code='generation_draining' WHERE status='queued' AND owner_id=:me`(§8.3.2의 CAS)를 실행하고 해당 `_pending_texts`를 비운다. 소유 queued가 DB에 0건이고 `_pending_texts`에 queued 키가 없을 때(락 안에서 재확인) 종료한다. 종료하면 `_background`에서 사라져(`runs/manager.py:1709`과 같은 done 콜백) `drain_ready`가 참이 될 수 있다. active 상태에서는 정리하지 않고 queued가 claim으로 소진될 때 종료한다.
4. **running job**: consumer는 `generation.work("tts")` 블록 안에서 claim부터 `execute` 종료까지 실행하므로 `_work`에 `tts`가 있고(`generation.py:261-277`) `can_retire()`가 False다(`:291`). 즉 running은 기존 장치만으로 퇴역을 막고, 이번 guard는 그 앞단(미claim queued)만 메운다.
5. **세대 미사용 모드**(`generation.current is None`): 퇴역이 없으므로 guard를 만들지 않는다.
6. **퇴역 신호 이후의 정상 정리**: `drain_ready` 참 → bootout → lifespan 종료 시 `tts` 정지가 남은 소유 job(이 시점에는 없어야 함)을 `failed(owner_shutdown)`으로 확정하고 guard 태스크를 취소한다. `_background`는 종료 시 취소되지 않으므로(`runs/manager.py`에 해당 처리가 없음) TTS가 자기 guard를 직접 취소한다.
7. **비정상 종료(SIGKILL·크래시·강제 종료)**: guard도 정리도 실행되지 않는다. 이 경우는 §8.3.5의 사망 증거 규칙(읽기 시점 규칙과 새 세대 정리)이 `failed(owner_gone, retryable)`로 수렴시킨다. 이 경로는 정상 드레인의 순서 보장이 아니라 비정상 종료의 수렴 보장이다.

**기각한 대안**:
- **(c) `can_retire()`/`ready()` 확장**: `generation.py:289-291`과 `generation_service.py:158-168`은 Run 세대 전환의 핵심이며 STT·벡터 등 모든 큐와 deployer 판정이 공유한다. TTS 전용 조건을 넣으면 모든 배포의 퇴역 조건이 바뀐다. 이미 있는 `_background` 집합으로 같은 효과를 낼 수 있어 채택하지 않는다. 필요해지면 STT 영향(`work("stt")` 경로는 `_work`로 이미 차단되어 변화 없음)과 `tests/`의 세대 관련 테스트 영향을 먼저 확인한다.
- **(a) 단독(읽기 시점 규칙 + 새 세대 정리만)**: 정상 퇴역에서도 job이 잠시 `queued`로 보이고 원문은 사라진 상태가 생기며, 소유자가 이미 종료된 뒤에야 사용자가 실패를 본다. 비정상 종료의 안전망으로는 필요하지만 정상 드레인의 순서 보장으로는 부족하다. 그래서 (b)를 주 경로로, (a)를 비정상 종료의 보조 규칙으로 둔다.

- 이유 1(계속 처리안 배제): "구 세대가 drain 동안 queued까지 계속 처리"하려면 draining에서도 `work()`가 True를 주도록 `generation.py`를 바꾸거나 `can_retire()`에 `_pending_texts`를 넣어야 한다. 모든 세대 큐가 공유하는 코드라 TTS 때문에 바꾸지 않는다.
- 이유 2(인수 불가): queued 원문은 구 세대 메모리에만 있어 새 세대가 인수할 수 없다. 그래서 퇴역 전에 명시적으로 실패를 확정해야 하고, 위 guard가 그 순서를 강제한다.
- 소비 루프: TTS consumer는 `generation_service.consume()`을 쓰지 않고 같은 `generation.work("tts")` 프리미티브로 자체 루프를 둔다. `consume`은 `work()`가 False이면 `next_job`을 부르지 않기 때문이다(`generation_service.py:105-107`). draining 정리는 consumer가 아니라 위 guard가 맡는다.
- `POST /api/tts/jobs`는 세대가 active가 아니면 접수하지 않고 503을 돌려준다(§7). 판정과 `activate()` 사이에 접수된 job은 2번의 순서(guard 먼저, 커밋 나중)와 `http_inflight` 때문에 guard가 살아 있는 상태로 queued가 되고 3번이 정리한다.
- 정상 종료 시 소유 job은 `failed(owner_shutdown)`(위 6번).
- 미측정 항목: drain 중 구 세대의 장시간 running job이 새 세대 TTS를 지연시키는 시간은 [엔진 결정 후 재측정]. guard 주기(5초)가 드레인 완료 시각에 더하는 지연은 최대 한 주기다.

#### 8.3.4 세대별 원자적 claim 조건

claim은 아래 조건을 **모두** 만족하는 한 문장의 조건부 UPDATE다. 하나라도 어기면 영향 행 0이고 job을 시작하지 않는다.

```sql
UPDATE tts_jobs SET status='running', started_at=now()
WHERE id = (SELECT id FROM tts_jobs
            WHERE status='queued' AND owner_id=:me
            ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
  AND status='queued' AND owner_id=:me
  AND NOT EXISTS (SELECT 1 FROM tts_jobs WHERE status='running')
RETURNING id
```

- `owner_id=:me`: **자기 세대가 접수한 job만** claim한다. 새 세대는 구 세대의 queued·running을 claim하거나 재실행하지 않는다. 원문이 없기 때문이다.
- 호출 전제: `generation.work("tts")`가 True(active이며 `work-tts.lock` 보유, `generation.py:261-277`)이고, `_pending_texts`에 해당 id가 있다.
- `NOT EXISTS running`: 소유자가 어느 세대든 running 행이 남아 있으면 새로 시작하지 않는다. STT와 같은 방식으로 고아 자식 가능성을 막고(`jobs.py:255-257`), 세대가 겹칠 때도 전역 1개 합성을 DB 수준에서 한 번 더 보장한다. 사망한 소유자의 running 행은 §8.3.5가 정리할 때까지 이 조건이 새 claim을 막는다.
- 새 세대 기동만으로 구 세대의 queued·running을 일괄 `failed`로 만들지 않는다. 구 세대가 살아 있으면 running은 끝까지 가고 queued는 §8.3.3에서 구 세대 스스로 정리한다.

#### 8.3.5 소유자 생존 판정: heartbeat 만료는 사망 증거가 아니다

- **사망 증거(허용 목록)**: (1) `<home>/runtime/<owner_id>.lock`의 `flock(LOCK_EX|LOCK_NB)`이 성공함. OS가 프로세스 종료 시 해제하며, 세대 등록이 같은 방식을 쓴다(`generation.py:112-136`). (2) `runtime_generations.state='offline'`(`close()`의 정상 종료 `:310-317`, 또는 등록 시 잠금 획득 성공으로 표시한 경우 `:128-136`).
- **사망 증거가 아닌 것**: `heartbeat_at` 경과, HTTP·DB 타임아웃, PID 단독 비교(재사용 가능). 코드 머리말이 이를 금지한다(`generation.py:1-4`). `_notebooklm_jobs`가 하트비트 경과로 정리하는 것(`generation_service.py:135-139`)은 다른 도메인 정책이며 TTS는 따르지 않는다.
- **규칙**: 위 증거를 확인하기 전에는 다른 세대의 job을 `failed`로 만들거나 인수하지 않는다. 증거가 없으면(잠금을 얻지 못함, `runtime_generations`에 행이 없음 포함) 아무것도 하지 않고 운영 지표에만 남긴다. 정리를 서두르다 살아 있는 구 세대의 job을 실패시키는 것이 더 나쁘다.
- **읽기 시점 규칙**: `GET`·`DELETE`가 non-terminal job을 읽고 `owner_id != :me`이면, 응답 전에 위 사망 증거를 확인한다. 증거가 확인되면 그 자리에서 CAS(`WHERE status IN ('queued','running') AND owner_id=:dead`)로 `failed(owner_gone, retryable)`을 확정하고 그 결과를 200으로 돌려준다. 증거가 없으면 실제 상태(`queued`·`running`)를 그대로 돌려준다. 이 규칙도 heartbeat를 보지 않는다. 쓰기 없이 "실패로 간주"만 하지 않고 CAS로 확정하는 이유는 같은 job이 이후 응답에서 다른 상태로 보이는 것을 막기 위해서다.
- **정리 절차**: active 세대의 TTS consumer가 기동 시 한 번, 그 뒤 틱마다 `owner_id != :me`인 non-terminal 행의 소유자를 검사한다. 잠금을 얻을 수 있어 사망이 확인되면 `UPDATE ... SET status='failed', error_code='owner_gone' WHERE status IN ('queued','running') AND owner_id=:dead`(§8.3.2의 CAS)를 실행한다. `running` 행은 부모 감시가 1초 폴링이므로(`model_process.py:88`) 확인 후 유예 시간(값은 구현 시 측정, 확인 필요)을 두고 정리하며, 그동안 §8.3.4의 `NOT EXISTS running`이 새 claim을 막는다.
- **세대 미사용 모드**: 단일 프로세스를 가정하고 시작 시 non-terminal 행을 `failed(server_restarted)`로 확정한다. STT의 `start()`가 running을 queued로 되돌리는 것과 같은 가정이다(`jobs.py:312-317`). 세대 모드로 전환 배포할 때 소유자가 `runtime_generations`에 없는 잔존 행은 자동 인수하지 않고 운영자가 확인한다.
- [미검증] 위 흐름의 경합(사망 확인 직후 같은 id의 세대가 재등록하는 경우 등)은 세대 id가 프로세스마다 새 `uuid4`(`generation.py:100`)이므로 같은 `owner_id`가 부활하지 않는다는 점에 기댄다. 구현 시 테스트로 확인한다.

#### 8.3.6 클라이언트 재시도

`status="failed"`이고 `retryable=true`이면 프론트는 브라우저에 남은 텍스트로 재요청 버튼을 제공한다. 서버는 원문을 가지고 있지 않으므로 서버 측 자동 재큐잉은 없다.

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
4. **문장 청크 분할**: 종결어미와 문장부호 기준으로 분할. 최대 청크 길이(기본 200자는 Kokoro 전제 가설, [엔진 결정 후 재측정]) 초과 시 절 경계(쉼표 등)에서 2차 분할.
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
- **API 및 큐 테스트 (`tests/`)**: `/api/tts/*` 라우트, 캐시 히트 시 워커 미호출, `/speak`와 `/jobs`의 예산 경합.
  - GET/DELETE 응답: 인증 없음 401, 형식 오류 UUID·미존재 404(422 아님), 접근 가능한 failed·cancelled가 HTTP 200 + status로 오는지(§7.2).
  - 상태 전이(§8.3.2): terminal에서 나가는 전이가 영향 행 0인지, **DELETE 후 worker의 succeeded UPDATE가 0행이어서 `cancelled`가 유지되는지**, 반대 순서(succeeded 후 DELETE는 현재 상태를 돌려줌), owner가 다른 세대의 UPDATE는 0행인지.
  - claim(§8.3.4): 다른 `owner_id`의 queued를 claim하지 않음, running 행이 있으면 claim 0행, `FOR UPDATE SKIP LOCKED`로 두 consumer가 같은 행을 잡지 않음.
- **원문 수명 검증**: 취소·실패·TTL·drain에서 `_pending_texts`가 비워지는지, 실행 중에는 감독기 계약상 부모가 원문을 보유함을 테스트 주석으로 명시(**"stdin 직후 해제"를 단정하는 테스트는 만들지 않는다**), `result_metadata`에 원문이 없는지.
- **퇴역 순서 검증(4차)**: 소유 queued가 있는 동안 `ready()`(`generation_service.py:158-168`)의 `drain_ready`가 False이고 `finalizers`에 guard가 잡히는지, draining 전환 뒤 guard가 queued를 `failed(generation_draining)`으로 확정하고 원문을 비운 **다음에만** `drain_ready`가 True가 되는지, running job이 있는 동안은 `_work` 때문에 False인지, POST가 처리 중이면 `http_inflight`로 False인지, guard 종료 판정과 새 POST의 경합(락)이 queued를 guard 없이 남기지 않는지, 세대 미사용 모드에서는 guard가 만들어지지 않는지.
- **세대·사망 확인 검증**: draining 전환 시 자기 소유 queued가 `failed(generation_draining)`이 되고 running은 끝까지 가는지, `heartbeat_at`만 오래된 소유자의 job은 건드리지 않는지, `<owner_id>.lock`을 얻을 수 있을 때만 `owner_gone`로 정리하는지, 사망한 소유자의 running 행이 정리될 때까지 새 claim이 막히는지.
- **동시성 및 간섭 검증 [엔진 결정 후 재측정]**: STT(4스레드)와 TTS(엔진 결정 후 확정) 동시 실행 시 상호 RTF 저하율 실측.
- **모바일 실기기 검증**: iOS 17+ Safari/PWA 실기기 체크리스트 검증.

## 13. 측정 항목 (모두 [엔진 결정 후 재측정])

- **TTFA (Time To First Audio)**: 요청 시작부터 첫 소리가 나기까지의 시간. 캐시 히트 경로와 캐시 미스(워커 종료 후 일괄 제공, §7.1) 경로를 구분해 정의한다. p50 < 8초는 Kokoro 전제 가설이며 [엔진 결정 후 재측정].
- **RTF (Real-Time Factor)**: 워커 합성 소요 시간 / 생성된 오디오 길이.
- **메모리 점유**: 워커 프로세스 peak RSS 및 부모 프로세스 메타데이터 메모리 소비량.
- **STT 동시 실행 저하율**: 단독 실행 대비 STT+TTS 동시 실행 시의 상호 RTF 저하율.
- **캐시 지표**: 청크 단위 캐시 히트율, 디스크 사용량, LRU 정리량.

## 14. 설정 목록 (초안, `TTAKKARI_` 접두)

엔진 관련 기본값(`ENGINE`, `PYTHON`, `MODEL_PATH`, `DEFAULT_VOICE/SPEED`, `CHUNK_MAX_CHARS`, `CPU_THREADS`)은 Kokoro 전제 초안이며 **[엔진 결정 후 재측정·재확정]** 대상이다.

| 키 | 기본값 | 설명 |
|---|---|---|
| `LOCAL_TTS_ENABLED` | true | TTS 기능 전체 토글 |
| `LOCAL_TTS_ENGINE` | (엔진 결정 후 확정) | TTS 엔진 ID. 초안 값 `kokoro`는 한국어 음성 부재로 기본값으로 쓰지 않는다 |
| `LOCAL_TTS_PYTHON` | `.tts-runtime/venv/bin/python` | TTS 전용 Python (STT venv와 분리) |
| `LOCAL_TTS_MODEL_PATH` | (엔진 결정 후 확정) | 가중치 디렉터리 경로 |
| `LOCAL_TTS_DEFAULT_VOICE/SPEED` | (벤치 확정)/1.0 | 기본 음성 및 속도 |
| `LOCAL_TTS_MAX_TEXT_CHARS` | 5000 | jobs 입력 글자 수 상한 |
| `LOCAL_TTS_SPEAK_MAX_CHARS` | 500 | speak 동기 호출 상한 |
| `LOCAL_TTS_CHUNK_MAX_CHARS` | 200 | 문장 청크 상한 (벤치 결과로 확정) |
| `LOCAL_TTS_TIMEOUT_SECONDS` | 600 | 워커 프로세스 타임아웃 |
| `LOCAL_TTS_CPU_THREADS` | 2 | 워커 CPU 스레드 예산 (Kokoro 전제 가설, [엔진 결정 후 재측정]) |
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
4. [GATE] 한국어 음성 품질 평가 및 모델 채택 (Kokoro는 한국어 음성 부재로 미달 확인. 대체 모델은 사용자 결정 대기. 이 결정 전에는 WBS 3 이후 구현 착수 안 함)
5. 영속 큐·API + 원문 인메모리 수명 관리 + terminal CAS·세대별 claim·drain·사망 확인(§8.3) + 오디오 토큰 서빙
6. DB migration (최신 Alembic head 기준) + 캐시 정리 정책 + 운영 문서
7. TTS 독립 직렬 큐 + STT 동시 실행 부하/RTF 측정 및 간섭 검증
8. TTS 페이지 프론트 (생성·재생·다운로드)
9. 채팅 재생 버튼 + 단일 Audio 소스 체이닝 + 재터치 복구 UX + iOS 실기기 검증
10. E2E·회귀·측정 리포트 + 배포

## 17. 미확인 및 추론 항목

1. 엔진 결정 후 재측정: 권장 청크 길이·출력 형식·샘플레이트·로드 시간·메모리·TTFA·RTF·스레드 수·STT 간섭. (Kokoro 한국어 품질은 미달 확인.)
2. 대체 모델 채택은 사용자 결정 대기. 이 설계는 후보를 정하지 않는다.
3. TTFA 목표 8초 및 TTS 2스레드 예산의 타당성 (벤치 실측 전 가설).
4. [추론] drain 중 구 세대 running job이 새 세대 TTS를 지연시키는 시간, 세대 간 CPU 경합 영향. 사망 확인 후 running 행 정리 유예 시간, 취소 감지 주기(§8.3.2, §8.3.5)는 구현 시 정한다(확인 필요).
5. [미검증] iOS Safari 및 PWA 실기기에서의 청크 순차 재생 제스처 유지율 및 WebAudio 갭리스 안정성.
6. 오디오 토큰 서빙: 기존 download-link 인프라 재사용 vs 전용 토큰 테이블.
7. 프론트엔드 작업 전 front 저장소 분기(`ahead 3, behind 73`) 및 작업 트리 정리.
8. Alembic 다음 번호 (구현 시점 최신 head 기준 확정).
9. 캐시 미스 job의 청크 조기 재생 미지원과 사용자 요구의 간극(§7.1) 확인.
10. 기존 코드 변경이 필요한 최소 지점: `generation_service.py` lifespan에 TTS start/stop 등록(STT의 `jobs.start/stop` 옆, 줄 `:218`, `:229`), 필요하면 `generation.py`에 사망 확인 공개 헬퍼 추가(기존 `_lock` 재사용, 동작 불변). `model_process.py`·`consume`·`work`·`can_retire`·`ready()`는 변경하지 않는다. 퇴역 순서 보장은 `runs.manager._background` 집합을 TTS 모듈이 직접 쓰는 방식이라, 이 집합을 `ready()`가 읽는 현재 구조(`generation_service.py:159-165`)에 의존한다. 그 구조가 바뀌면 §12의 퇴역 순서 테스트가 먼저 깨지도록 한다.
11. guard 주기(5초)와 사망 확인 유예·취소 감지 주기의 실제 값은 구현 시 측정(확인 필요). launchd의 종료 유예 설정은 저장소에서 찾지 못했다.

## 부록 A. 작업 로그

- 2026-10-20: 기존 STT·프론트 구조 분석 후 본 설계 초안 작성.
- 2026-10-20: 1차 독립 검증 반영 개정 (역사적 기록: 당시 ADR 0061→0062 이동 및 미추적 0053/0054 감안 0055 임시 언급 논의).
- 2026-10-20: 2차 독립 재검증 지적 반영 전면 정정 (원문 보관 인메모리 확정 및 DB 원문 부재를 데이터 최소화 방침으로 명시, 과장된 물리 소멸 표현 삭제, Generation 소유권 및 구 세대 job 복구 금지/fencing 확정, HTTP 200 failed 계약 일원화, /speak와 /jobs 직렬 락 명시, run_json_worker 배치 계약 정합, Alembic 0055 예약 전면 제거, iOS play Promise rejection 및 재터치 복구 필수화, CPU/스레드 미측정 결론 제거 및 STT 설정 출처 확인, 부모 감시-프로세스 수명 모순 수정, 한자어 날짜 시월 발음 정정, Kokoro 게이트 범위 정정, PMS/WBS 1 검증대기 전환).
- 2026-10-20: 3차 독립 검증 지적 5건 반영 (개정 3판). (1) 원문 수명: `run_json_worker`가 자식 종료까지 `request` 참조를 유지함을 코드로 확인하고 "stdin 직후 해제" 철회, 기존 감독기 무변경(§2.1, §8.2). (2) 취소 경합: 모든 전이를 owner·이전 상태 조건부 UPDATE로 확정(§8.3.2). (3) drain/claim: draining 세대는 running만 완료하고 자기 소유 queued는 `generation_draining`으로 실패 확정, 세대별 원자적 claim 조건 명시(§8.3.3, §8.3.4). (4) 소유자 생존: heartbeat 만료는 사망 증거가 아니며 flock·offline 상태 확인 전에는 구 세대 job을 실패·인수하지 않음(§8.3.5). (5) GET: 401/404/200+status 구분(§7.2). Kokoro 한국어 미달과 대체 모델 사용자 결정을 반영해 Kokoro 전제 수치를 [엔진 결정 후 재측정]으로 표기, `/speak`·`/jobs` 예산 단위를 세대 모드 파일 잠금으로 정정(§6).
- 2026-10-20: 4차 독립 검증 지적 2건 반영 (개정 4판). (1) draining 정리와 세대 퇴역의 순서: `drain_ready`는 `_background` 등 미완료 태스크가 있으면 False임을 코드로 확인(`generation_service.py:158-168`, `runs/manager.py:1681, 1705-1709`)하고, TTS가 소유 queued가 있는 동안 `_background`에 queued guard 태스크를 두도록 확정. `can_retire`/`ready` 변경은 기각, 읽기 시점 규칙·새 세대 정리는 비정상 종료 보조(§8.3.3, §8.3.5). 사망 코드명 `owner_process_terminated`를 `owner_gone`으로 통일. (2) Bearer 근거를 `api/auth.py`에서 `security/auth.py:58-72`로 정정(§7.2).
