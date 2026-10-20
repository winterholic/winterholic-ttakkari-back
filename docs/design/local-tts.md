# 로컬 TTS 설계: 별도 생성 페이지와 채팅 음성 재생

- 상태: 설계. 구현 미착수. 이 단계에서는 기능 코드를 변경하지 않는다.
- 날짜: 2026-10-20.
- 관련: [ADR 0025](../adr/0025-on-demand-local-stt.md), [ADR 0026](../adr/0026-local-first-meetings.md), [ADR 0027](../adr/0027-retire-clovanote.md), [ADR 0028](../adr/0028-stt-quality-and-speaker-utterances.md), [STT 안내](../stt/README.md), [API 계약](../api-contract.md), ADR 초안 [0061](../adr/0061-local-tts.md).
- 엔진 벤치(별도 세션 병행): Kokoro 한국어 품질과 실행 방식 측정 결과는 이 문서의 플러그형 항목(§5, §7, §9, §17)에 반영한다.

## 1. 사용자 결정 (확정)

1. TTS는 회의록(STT) 기능에 넣지 않고 별도 TTS 생성 페이지를 만든다. 텍스트 입력에서 음성을 생성하고 재생·다운로드한다. 음성 종류와 속도 설정이 있다.
2. 따까리 응답(채팅) 메시지에 선택적 음성 재생 버튼을 둔다. 재생·일시정지·중지. 텍스트 응답이 기본이며 음성은 선택이다. 모바일(iOS Safari 자동재생 제약 포함)에서 자연스럽게 동작해야 한다.
3. 엔진은 Kokoro-82M 우선, 로컬·무료. 엔진 교체 가능한 인터페이스로 추상화한다. 다른 작업 세션이 Kokoro 한국어 품질·실행 방식 벤치를 병행 중이므로 엔진 세부는 플러그형으로 둔다.
4. 사용하지 않을 때 자원 점유를 최소화한다. STT·LLM 오케스트레이터와 느슨하게 결합한다. 청크(문장) 단위 생성·스트리밍, 반복 음성 캐싱(키: 엔진·모델·음성·속도·정규화 텍스트 해시), 한국어 텍스트 정규화 단계를 둔다.

## 2. 기존 구조 분석 요약

### 2.1 백엔드 STT (재사용 대상)

- 일회용 모델 프로세스 감독기 `src/ttakkari/model_process.py::run_json_worker`. 별도 Python에 `모듈 + 출력경로`로 실행하고 요청 JSON을 stdin으로 전달, 결과 JSON 파일을 읽는다. 자식 환경은 PATH·HOME·TMPDIR·locale만 상속하고 HF 오프라인·암묵 토큰 금지·텔레메트리 금지를 강제한다. PYTHONPATH는 배포본 `src`로 고정해 선택 런타임에 오래된 앱 코드가 섞이지 않게 한다. 출력 64MB 상한, 타임아웃 시 kill 후 반드시 회수한다. 결과에 `process_exited·worker_pid·wall_seconds`를 붙인다. 자식은 부모 PID 감시 스레드로 고아 프로세스를 자살 처리한다.
- 로컬 STT 패키지 `src/ttakkari/meetings/local_stt/`. `jobs.py`는 모델 없는 부모의 직렬 큐다. 추론 전에 claim하고 DB 커넥션·행 잠금을 해제한 뒤 자식을 띄운다. 취소는 queued로 되돌리고, 시작 시 running을 queued로 복구한다. `pipeline.reconcile`로 회의 전사 상태에 반영한다. generation 배포 모드에서는 `generation_service.consume` 경로를 탄다(`_generation_next`). `worker.py`는 요청당 1프로세스다. 앱 Settings·DB·토큰·네트워크 다운로드를 쓰지 않는다. `runner.py`는 설정·파라미터를 워커 요청으로 옮기는 얇은 어댑터다. `core·quality·utterances`는 순수 로직이다.
- 설정 `src/ttakkari/config.py`. `TTAKKARI_LOCAL_STT_*`(enabled·python·model_path·timeout·max_audio_seconds·cpu_threads·profile·diarization·separation). 기본 루트는 저장소 안 `.stt-runtime/`이며 Git에서 제외한다. `extra="ignore"`라 삭제된 키가 남아도 시작이 실패하지 않는다.
- DB `local_stt_jobs·local_stt_segments`(Alembic 0025~0027). 원문·원시 출력 보존, 후보는 child job으로만 생성, 자동 채택 금지 원칙.
- API `src/ttakkari/api/meetings.py`. `APIRouter(prefix="/api/meetings")`. 생성·재처리는 202 후 폴링, 구간은 offset·limit 페이지네이션(최대 500), 교정·비교는 후보 판정만 반환한다.
- 인증·라우팅 관례. owner API는 `Authorization: Bearer`(`ttakkari.security.auth.User`), DB는 `ttakkari.api.deps.Db`. 오류 본문은 `{"code","message"}`이며 422만 `errors`가 추가된다. 프론트는 401 시 refresh 후 1회 재요청한다.
- 오디오·파일 서빙 선례. `GET /api/artifacts/{id}/content`는 Bearer + `FileResponse` + Range 지원이다. `<audio>` 태그처럼 헤더를 못 붙이는 클라이언트는 `POST .../download-link`의 단기 토큰 URL(`/api/dl/{token}`)을 쓴다. TTS 오디오 서빙도 이 패턴을 따른다(§7).
- 추상화 선례. E5 임베딩(`context/embedder.py`)은 `Embedder` Protocol + `run_json_worker` 재사용의 두 번째 사례다. 요청별 자식 프로세스 + 직렬 락 구조다. TTS Provider도 같은 모양으로 둔다.

### 2.2 프론트 (삽입 지점)

- 라우트 `front/src/App.tsx`. 새 페이지는 Route 1개와 `shell/features.ts`의 FEATURES 항목 1개(라우트·아이콘·라벨·설명)로 데스크톱 부가 기능·모바일 더보기에 함께 노출된다. TTS는 탭이 아니라 더보기 진입이 기본이다.
- 채팅 메시지 `front/src/features/chat/Messages.tsx::AgentMessage`. 본문 아래 `tk-message__actions`에 복사 버튼이 있다. 음성 재생 버튼은 이 자리 옆에 둔다. `live`(스트리밍 중)일 때는 액션이 숨으므로 재생 버튼도 완료된 응답에만 노출된다.
- 오디오 기존 코드 없음. `AudioContext·speechSynthesis·new Audio` 사용처가 없다. 회의록은 녹음 원본(`m.audio` → artifact content) 재생만 한다. TTS가 첫 합성 오디오 재생 코드다.
- API 클라이언트 `front/src/api/client.ts`. `json·raw` 헬퍼, access token 메모리 보관, `ApiError{status,code}`. 새 tts 함수는 여기에 두고 `src/api/types.ts`를 백엔드 스키마와 함께 바꾼다(api-contract.md 규칙).

## 3. 목표와 비목표

- 목표. (a) 별도 TTS 페이지에서 텍스트→음성 생성·재생·다운로드. (b) 채팅 응답의 선택적 음성 재생. (c) 로컬·무료 엔진, 유휴 자원 0, STT와 안전한 동시 실행. (d) 문장 청크 스트리밍급 체감(첫 소리까지 짧게), 반복 문장 캐시.
- 비목표. 실시간 대화형 음성(저지연 풀듀플렉스), 음성 클로닝·학습, 회의록 STT 파이프라인 변경, 외부 유료 TTS 연동, 자동재생·백그라운드 연속 재생 고도화(Phase 2 후보).

## 4. 아키텍처

독립 패키지 `src/ttakkari/tts/`를 둔다. `meetings/` 안에 넣지 않는다. 느슨한 결합 요구와 재사용 방향이 STT→TTS 단방향이기 때문이다.

```text
src/ttakkari/tts/
  __init__.py
  providers.py     # TtsProvider Protocol + capabilities + 음성 목록 타입
  normalize.py     # 한국어 정규화·문장 분리 (순수 함수, ML 의존 없음)
  cache.py         # 캐시 키·조회·저장·LRU 정리 (부모 프로세스에서 실행)
  jobs.py          # 영속 큐: claim→워커→청크 확정→정리 (local_stt/jobs.py 대응)
  runner.py        # 설정·파라미터→워커 요청 어댑터 (local_stt/runner.py 대응)
  worker_kokoro.py # Kokoro 워커: 요청당 1프로세스 (local_stt/worker.py 대응)
  read_model.py    # 조회용 조립 (음성·job·청크 출력)
src/ttakkari/api/tts.py  # APIRouter(prefix="/api/tts")
```

### 4.1 엔진 추상화 (교체 가능 인터페이스)

```python
class TtsCapabilities(NamedTuple):
    chunked: bool            # 문장 청크 분할 합성 지원
    speed_range: tuple[float, float]
    sample_rate: int         # 출력 샘플레이트 (확인 필요: 벤치 세션)
    output_format: str       # "wav" 기본 (확인 필요: 벤치 세션)

class TtsProvider(Protocol):
    id: str                  # "kokoro"
    label: str
    capabilities: TtsCapabilities
    def availability(self) -> ProviderAvailability: ...
    async def voices(self) -> list[TtsVoice]: ...
    async def synthesize(self, chunks: list[str], *, voice: str, speed: float) -> TtsResult: ...
```

- 부모 프로세스는 이 Protocol만 본다. 엔진별 차이는 워커 모듈(`worker_kokoro.py`)과 설정 안에 가둔다.
- Kokoro 어댑터의 플러그형 항목: 모델 경로·리비전, 음성 목록 파일, 권장 청크 길이, 출력 형식·샘플레이트, 디바이스(cpu 기본, mps 후보). 벤치 세션 결과가 나오면 어댑터 내부와 설정 기본값만 바꾼다.
- `availability()`는 `meetings/providers.py`의 `ProviderAvailability·ProviderState`를 재사용한다. 미준비 사유(런타임 없음·가중치 없음)를 그대로 노출해 STT와 같은 안내 UX를 쓴다.

## 5. 프로세스 모델 (택일과 근거)

선택: Phase 1은 요청별 일회용 자식 프로세스다. STT와 같은 `run_json_worker`를 재사용한다. 유휴 타임아웃 상주는 Phase 1에서 쓰지 않는다.

근거.

1. 유휴 자원 0 요구를 직접 충족한다. 프로세스가 없으면 메모리·파일 핸들 점유도 없다. 상주 방식은 타임아웃 창 동안 가중치를 들고 있어 요구를 약하게 충족한다.
2. 검증된 감독기를 그대로 쓴다. 회수·타임아웃·부모 감시·무자격증명·오프라인 강제·PYTHONPATH 고정이 이미 테스트돼 있다.
3. 배포와 세대가 안전하다. 상주 프로세스는 세대 교체 시 드레인·프리로드·헬스체크가 필요하지만, 요청별 프로세스는 기존 generation 공존 로직(`consume`)만 따르면 된다.
4. 캐시가 상주의 이점을 줄인다. 반복 문장은 프로세스 없이 파일로 응답한다. 상주가 빨라지는 구간은 캐시 미스 첫 합성뿐이다.
5. 시기상조 최적화를 피한다. Kokoro-82M 로드 비용이 미확인인 상태에서 상주 복잡도를 먼저 떠안을 이유가 없다.

호출 단위: 워커 1회 호출이 캐시 미스 청크 전체를 처리한다. 부모가 정규화·분할·캐시 조회를 먼저 하고 미스분만 워커에 넘긴다. 모델 로드 1회로 청크 N개를 합성해 로드 비용을 분할한다. 워커는 청크별 wav 조각 파일 경로와 지표를 반환한다.

전환 기준(측정 후 판단). 캐시 미스 단문(§7의 speak, 500자 이하) TTFA p50이 8초를 넘으면 유휴 타임아웃 상주(타임아웃 5분, 설정값)를 도입한다. 8초는 가설이며(확인 필요), 벤치 세션의 로드 시간·메모리·RTF 결과를 입력으로 확정한다. 전환해도 요청별 경로를 기본 폴백으로 유지한다.

## 6. 동시성: STT·E5와의 경합 제어

Mac Studio는 멀티코어이므로 STT 실행 중 TTS를 무조건 대기시키는 설계는 채팅 재생 UX를 해친다. 반대로 무제한 동시는 CPU 경합으로 양쪽 RTF를 무너뜨린다.

- 전역 모델 실행 슬롯을 둔다. `asyncio.Semaphore` 용량 2(설정 `TTAKKARI_MODEL_MAX_CONCURRENT`, 기본 2). STT job 1 + TTS job 1의 동시 실행을 허용한다. E5 임베딩도 같은 슬롯을 쓴다(짧은 작업이라 대기 영향이 작다).
- CPU 스레드 예산을 나눈다. STT 4(현행 유지) + TTS 2(신규 기본). 총 6을 상한으로 두고 측정 후 조정한다. 워커에 `cpu_threads`로 전달하며, STT 워커와 같은 방식으로 `num_workers=1`을 유지한다.
- 기아 방지. STT 큐와 TTS 큐는 독립 DB 큐이며 슬롯이 2개라 STT 장시간 작업이 TTS를 막지 않는다. 슬롯 고갈 시 TTS 요청은 큐에서 대기하고(202), speak 단문 동기 호출은 30초 대기 후 503 `tts_busy` + `Retry-After`로 돌려준다.
- 디바이스. STT 현행과 같이 CPU 고정. Kokoro MPS는 벤치 세션 결과 후 결정한다. GPU 경합이 생기면 슬롯 용량을 1로 낮추는 설정으로 대응한다.

## 7. API 설계

모두 Bearer 인증(owner API)이며 오류 본문은 `{"code","message"}` 계약을 따른다. 새 코드는 `tts_not_ready·tts_text_too_long·tts_voice_unknown·tts_busy·tts_unsupported_speed`다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/tts/status` | `{enabled, ready, reason, engine, model, voice_count}`. 미준비면 사유와 설치 안내 링크를 준다 |
| GET | `/api/tts/voices` | `{voices: [{id, name, lang, gender?, sample_url?}]}`. 미준비면 503 `tts_not_ready` |
| POST | `/api/tts/jobs` | `{text(최대 5000자), voice?, speed?}` → 202 `{job_id, chunks, cached_chunks}`. 정규화·분할·캐시 조회까지 하고 큐에 넣는다 |
| GET | `/api/tts/jobs/{id}` | `{status, progress:{done,total}, chunks:[{index, text, audio_url?, duration?}], audio_full_url?, metrics?}`. `audio_url`은 단기 토큰 URL이다 |
| POST | `/api/tts/speak` | 단문 동기 합성. `{text(최대 500자), voice?, speed?}` → 200 `audio/wav` 직접 반환. 채팅 재생 버튼용 fast-path. 캐시 히트면 워커를 띄우지 않는다. 30초를 넘기면 503 `tts_busy` |
| DELETE | `/api/tts/jobs/{id}` | 대기·실행 취소. 실행 중이면 자식 회수 후 queued로 되돌리지 않고 cancelled로 둔다 |
| GET | `/api/tts/cache/stats` | `{entries, bytes, hits, misses, max_bytes}` |
| POST | `/api/tts/cache/prune` | 상한 초과·TTL 경과 항목 정리 후 `{removed, freed_bytes}` |

스트리밍 방식: 진짜 바이트 스트리밍(SSE·chunked)이 아니라 청크 단위 완성 + 순차 재생이다. 이유 세 가지. 첫째, iOS Safari에서 `<audio>` 순차 큐가 가장 호환성이 높다. 둘째, 서버 구현이 단순하고 캐시와 자연스럽게 결합한다(히트 청크는 즉시 URL 발급). 셋째, STT의 구간·페이지네이션 패턴과 일관된다. 바이트 스트리밍은 Phase 2 최적화 후보로 둔다.

오디오 서빙: `<audio>`·모바일 재생기는 Authorization 헤더를 못 붙인다. artifacts의 download-link 패턴을 따른다. TTS 캐시 파일은 Artifact가 아니므로(재생성 가능한 파생물) tts 전용 단기 토큰을 둔다. `GET /api/tts/jobs/{id}`의 `audio_url`은 `/api/dl/tts/{token}` 형태이며 TTL 10분(설정)이다. 기존 download-link 인프라를 재사용할지 tts 전용 토큰 테이블을 둘지는 구현 단계에서 결정한다(§17 미확인 항목).

오디오 형식: 기본 wav(엔진 원시 출력). Kokoro 출력 형식·샘플레이트는 벤치 세션에서 확정한다(확인 필요). mp3 트랜스코딩은 Phase 1 범위 밖이다. iOS Safari는 wav를 재생할 수 있어 호환성 차단이 아니며, 트랜스코딩은 ffmpeg 의존 추가와 CPU 비용이 들기 때문이다. 용량 측정 후 필요하면 Phase 2에서 다룬다.

전체 합본: job 완료 시 청크 wav를 이어 붙인 전체 파일을 캐시에 함께 둔다(`audio_full_url`). TTS 페이지의 다운로드 버튼과 긴 채팅 응답의 연속 재생이 쓴다. 이어 붙이기는 무손실 PCM 연결이며 재합성이 아니다.

## 8. 데이터·저장·캐시 정리 정책

DB 테이블(신규 migration 0053, 다음 번호는 작성 시점 최신 확인. 확인 필요).

- `tts_jobs`: id, status(queued·running·succeeded·failed·cancelled), params(voice·speed·text_length·chunk_count, 원문은 저장하지 않음), result_metadata(지표·엔진·모델 리비전), error, created·started·finished_at.
- `tts_cache`: cache_key(PK), engine, model_revision, voice, speed, text_hash, audio_relpath, bytes, sample_rate, duration_seconds, hits, created_at, last_hit_at. 정규화 텍스트 원문은 저장하지 않고 해시만 둔다.

원문을 저장하지 않는 근거: TTS 입력은 회의록 전사처럼 민감할 수 있으나, 캐시 적중에는 해시면 충분하고 디버깅은 앞 100자도 원본 노출이기 때문이다. 개인정보 최소화가 재생성 가능성보다 우선한다. 품질 이슈 추적은 지표·리비전·길이로 한다.

캐시 키: `blake2b("tts1" ‖ engine ‖ model_revision ‖ voice ‖ speed ‖ normalize(text))`. 요구의 키 항목(엔진·모델·음성·속도·정규화 텍스트 해시)을 그대로 쓴다. 버전 접두 `tts1`은 정규화 규칙 변경 시 무효화를 위한 것이다.

파일 위치: `<home>/tts-cache/<shard2>/<key>.wav`. `home`은 설정 `home`(기본 `~/.ttakkari`)이며 테스트의 임시 HOME을 따라간다. STT 런타임(저장소 안 `.stt-runtime/`)과 달리 저장소 밖에 두는 근거: 캐시는 재생성 가능한 파생물이라 백업·Git 대상이 아니며, 용량 상한 관리가 저장소 밖에서 쉽기 때문이다. 샤딩 2단계로 디렉터리 비대화를 막는다.

정리 정책.

- 상한 2GB(설정 `TTAKKARI_LOCAL_TTS_CACHE_MAX_GB`), 초과 시 `last_hit_at` 오래된 순 삭제.
- TTL 30일(설정), 경과 항목은 조회 시 miss 처리 후 지연 삭제.
- job 완료 시점과 서버 기동 시점에 정리를 실행한다. 별도 상주 스케줄러는 두지 않는다.
- TTS 페이지 다운로드는 캐시 파일을 Artifact(copy 모드)로 등록해 보관함에 영속화한다. 캐시 삭제와 무관해진다.

## 9. 한국어 텍스트 정규화

`tts/normalize.py` 순수 함수 파이프라인이다. ML 의존 없이 단위 테스트한다.

1. 평문화. 채팅 응답용: 코드 블록·인라인 코드·링크·표·이미지를 걷어내고 읽을 텍스트만 남긴다. TTS 페이지 직접 입력은 평문을 가정하되 같은 처리를 적용한다.
2. 이모지·특수문자. 이모지는 제거(읽지 않음). 연속 공백·개행을 정리한다. NFC 정규화.
3. 숫자·기호 읽기 변환. 날짜·시간·금액·단위·서수·백분율을 한국어 읽기로 바꾼다. 예: `2026-10-20` → `이천이십육년 십월 이십일`, `3개` → `세 개`, `50%` → `오십 퍼센트`. 규칙 테이블로 두고 단위 테스트로 고정한다.
4. 문장 분리. 한국어 종결어미(·다·요·까·라 등)와 문장 부호 기준으로 나누고, 청크 최대 길이를 넘기면 절 경계(쉼표·접속어미)에서 2차 분할한다. 청크 최대 길이는 Kokoro 권장 입력에 맞춘다(벤치 세션 확정 전 기본 200자, 확인 필요).
5. 청크별 캐시 키 생성(§8). 빈 청크·기호만 남은 청크는 합성에서 제외하고 무음으로 둔다.

정규화 규칙 버전(`tts1`…)은 캐시 키 접두와 함께 올린다. 규칙 변경 시 이전 캐시는 자연 무효화된다.

## 10. 프론트 설계

### 10.1 TTS 페이지 (`/tts`)

- 라우트 1개(`App.tsx`) + FEATURES 항목 1개(`features.ts`): `{root: "tts", to: "/tts", icon: "volume"(신규 등록), label: "음성 만들기", description: "텍스트를 로컬 음성으로 만들어 듣기"}`. 현재 Icon 목록에 speaker·volume·play·pause가 없으므로 lucide-react에서 추가 등록이 필요하다(구현 단계).
- 화면 구성. 텍스트 입력(textarea, 글자 수·최대 5000자 표시) → 음성 선택(select, `/voices`) → 속도 슬라이더(0.5~2.0, 0.1 단위) → 생성 버튼 → 진행률(청크 done/total 폴링) → `<audio controls>` + 다운로드 버튼 + 다시 듣기. 미준비(`ready=false`)면 사유와 설치 안내를 표시하고 입력을 막는다(STT 안내 UX와 동일).
- 상태 관리. 컴포넌트 지역 상태로 충분하다. job 폴링은 1초 간격, 완료·실패 시 중단. 페이지를 떠나면 폴링만 중단하고 job은 서버에서 계속 돈다(재진입 시 job id로 이어보기. job id는 세션 스토리지에 최근 1개 보관).

### 10.2 채팅 재생 버튼

- 위치. `AgentMessage`의 `tk-message__actions`(복사 버튼 옆). 완료된 응답(`!live && view.text`)에만 노출한다. aria-label `음성으로 듣기`.
- 동작. `useTtsPlayback` 훅(신규, 전역 단일 Audio 인스턴스). 상태 idle·loading·playing·paused. 버튼 1개가 재생·일시정지를 토글하고, 재생 중 다시 누르면 일시정지, 외부(다른 메시지·TTS 페이지) 재생 시작 시 기존 재생은 중지한다. 중지는 일시정지 후 재생 위치를 처음으로 돌린다.
- 단문·장문 분기. 평문화 후 500자 이하는 `/api/tts/speak` 동기 호출 후 blob URL 재생. 초과분은 `/api/tts/jobs` 생성 후 청크 URL 순차 재생(ended → 다음 청크). 분기 기준은 서버 `speak_max_chars`와 같은 상수를 프론트에 둔다.
- 텍스트가 기본. 자동재생은 하지 않는다. 재생 실패(미준비·네트워크)는 버튼에 오류 상태와 재시도를 표시하고 토스트로 사유를 알린다.

## 11. 모바일 재생 전략 (iOS Safari)

- 사용자 제스처 안에서 시작한다. `onClick` 핸들러에서 `Audio` 생성·`play()`까지 호출해 자동재생 차단을 피한다. speak 호출이 비동기라 제스처가 끊길 수 있으므로, 클릭 즉시 무음 unlock 후 URL 확정 시 `src`를 교체하는 방식을 쓴다(구현 단계 실기기 검증).
- `<audio>` 순차 큐를 쓴다. MediaSource·WebAudio 직접 버퍼링은 하지 않는다. iOS 호환성과 구현 단순함이 이유다.
- 백그라운드·잠금 화면. 네이티브 `<audio>`에 위임한다. MediaSession 메타데이터·이어듣기는 Phase 2 후보이며 Phase 1은 포그라운드 재생을 보장 범위로 한다.
- 실패 처리. 청크 로드 실패 시 1회 재시도 후 남은 청크부터 이어듣기를 제공한다. 네트워크 끊김은 `audio_url` 토큰 재발급(job 재조회)으로 복구한다.
- PWA. 오디오 URL은 서비스 워커 캐시 대상에서 제외한다(부분 콘텐츠·토큰 URL 캐싱 깨짐 방지). SW 설정을 구현 단계에서 확인한다(§17 미확인 항목).

## 12. 테스트·E2E 계획

- 단위(`unit_tests/`). 정규화(날짜·숫자·단위·이모지·마크다운 평문화), 문장 분할 경계(긴 문장·종결 없는 입력), 캐시 키 결정성(필드 변경 시 키 변경, 무관 필드 변경 시 유지).
- API·DB(`tests/`). voices·status(준비·미준비), jobs 생성(202)·조회·취소, speak 동기 성공·긴 텍스트 422·모르는 음성 404·미준비 503, 캐시 히트 시 워커 미호출, 재시작 시 running → queued 복구, 토큰 만료·위조 거부.
- 프로세스. 타임아웃 kill·회수, 부모 사망 시 자식 자살, 출력 상한 초과 거부. STT 쪽 기존 테스트 패턴을 따른다.
- 동시성. 슬롯 용량 2에서 STT 장시간 job 실행 중 TTS job이 진행됨을 확인. 슬롯 고갈 시 speak 503 + Retry-After.
- 프론트. `useTtsPlayback` 목 단위 테스트(단문·장문 분기·토글·중복 재생 중지), TtsPage 렌더·상태·미준비 표시 테스트.
- E2E. (a) TTS 페이지: 생성→진행률→재생→다운로드→보관함 등록 확인. (b) 채팅: 단문 재생·일시정지·중지, 장문 청크 연속 재생, 동시 재생 1개 보장. (c) iOS 실기기: Safari + PWA 설치 상태에서 제스처 재생·잠금 후 복귀. (d) STT 전사 중 TTS 동시 실행.
- 벤치 하네스. `local_stt/benchmark.py` 패턴의 `tts/benchmark.py`(manifest → JSON 리포트)를 둔다. 병행 벤치 세션과 측정 항목(§13)을 공유한다.

## 13. 측정 항목

- TTFA. 요청 시작 → 첫 청크 오디오 재생 시작(프론트 계측)과 첫 청크 바이트 도착(서버 계측)을 분리 기록한다.
- RTF. 워커 합성 시간 / 생성 오디오 길이. 캐시 미스 청크만 집계한다.
- 메모리. 워커 peak RSS(`resource.getrusage`, STT와 동일), 평시 상주(부모 프로세스 RSS 증가 0 + tts 자식 프로세스 0개 확인).
- STT 동시 실행 저하율. 단독 실행 RTF 대비 동시 실행 RTF. 슬롯 2·스레드 4+2 기본값의 타당성 근거로 쓴다.
- 캐시. 히트율(청크 단위), 용량·항목 수, 정리 삭제량.
- 품질. 한국어 자연스러움은 벤치 세션의 정성 평가에 위임하고, 이 설계의 자동 측정은 수치 지표만 다룬다.

측정 기록은 `back/docs/tts/measurement-*.md`에 둔다(STT의 `docs/stt/` 대응).

## 14. 설정 목록 (초안, `TTAKKARI_` 접두)

| 키 | 기본값 | 설명 |
|---|---|---|
| `LOCAL_TTS_ENABLED` | true | 기능 전체 토글 |
| `LOCAL_TTS_ENGINE` | kokoro | 엔진 id. 교체 시 어댑터 선택 키 |
| `LOCAL_TTS_PYTHON` | `.stt-runtime`와 별도의 `.tts-runtime/venv/bin/python` | TTS 전용 Python. STT venv와 분리해 의존 충돌을 막는다 |
| `LOCAL_TTS_MODEL_PATH` | `.tts-runtime/kokoro-82m` | 가중치 경로 (리비전은 벤치 확정 후 고정) |
| `LOCAL_TTS_VOICES_PATH` | 모델 경로 안 음성 목록 | 음성 정의 파일 (형식은 벤치 확정 후) |
| `LOCAL_TTS_DEFAULT_VOICE/SPEED` | (벤치 확정)/1.0 | 기본 음성·속도 |
| `LOCAL_TTS_MAX_TEXT_CHARS` | 5000 | jobs 입력 상한 |
| `LOCAL_TTS_SPEAK_MAX_CHARS` | 500 | speak 동기 상한 |
| `LOCAL_TTS_CHUNK_MAX_CHARS` | 200 | 문장 청크 상한 (벤치 확정 후 조정) |
| `LOCAL_TTS_TIMEOUT_SECONDS` | 600 | 워커 호출 타임아웃 |
| `LOCAL_TTS_CPU_THREADS` | 2 | 워커 스레드 (§6 예산) |
| `LOCAL_TTS_CACHE_MAX_GB/TTL_DAYS` | 2/30 | 캐시 상한·TTL |
| `LOCAL_TTS_TOKEN_TTL_SECONDS` | 600 | 오디오 URL 토큰 TTL |
| `MODEL_MAX_CONCURRENT` | 2 | STT·TTS·E5 공용 실행 슬롯 수 |

`LOCAL_TTS_PYTHON`을 STT venv와 분리하는 근거: Kokoro 의존(torch 계열, 확인 필요)이 faster-whisper·pyannote 고정 버전과 충돌할 수 있어, STT 현행(`pyproject.toml`의 stt extras) 버전을 건드리지 않기 위해서다. 설치 안내는 구현 단계에서 `back/docs/tts/README.md`로 작성한다.

## 15. 마이그레이션·배포·운영

- Alembic 0053(가설, §17 확인 필요): `tts_jobs·tts_cache·tts_audio_tokens` 생성, downgrade 포함. 운영 DB 적용은 배포 감독이 실행한다.
- 배포: 통상 백엔드 배포 절차를 따른다. 이 설계 단계는 문서 커밋만 하며 배포 요청을 하지 않는다.
- 운영: 캐시 용량 모니터링(§7 stats), 미준비 안내 문구, 가중치 설치 가이드. 평시 부하는 STT와 같은 원칙이다. 큐는 이벤트 대기로 빈 폴링 없음, 모델은 요청 시에만 존재.

## 16. WBS 개요

PMS 「따까리 · 로컬 TTS」 프로젝트에 등록한다(상세는 PMS 참조).

1. 설계·경계 확정(본 문서, ADR 0061 초안) — 완료
2. 한국어 정규화·분할·캐시 키 (순수 모듈 + 단위 테스트)
3. 엔진 추상화 + Kokoro 워커 (벤치 결과 반영, 플러그형 항목 확정)
4. 영속 큐·API (jobs·speak·voices·status·cache) + 오디오 토큰 서빙
5. DB migration 0053 + 정리 정책 + 운영 문서
6. 동시성 슬롯 + STT 동시 실행 측정·튜닝
7. TTS 페이지 프론트 (생성·재생·다운로드·미준비 안내)
8. 채팅 재생 버튼 + 재생 훅 + 모바일(iOS) 검증
9. E2E·회귀·측정 리포트 + 배포

## 17. 미확인 항목

1. Kokoro 한국어 품질·권장 청크 길이·출력 형식·샘플레이트·로드 시간·메모리·의존 목록. 병행 벤치 세션 결과 대기. 확정 전 기본값은 가설이다.
2. TTFA 목표 8초(§5 전환 기준)의 타당성. 벤치 로드 시간으로 확정한다.
3. 오디오 토큰: 기존 artifacts download-link 인프라 재사용 vs tts 전용 토큰 테이블. 구현 단계 결정.
4. mp3 트랜스코딩 필요 여부. wav 용량 측정 후 Phase 2 판단.
5. 프론트 Icon 신규 등록(play·pause·volume) 및 PWA 서비스 워커의 오디오 URL 제외 설정.
6. Alembic 다음 번호(0053 가설). 구현 착수 시 최신 확인.
7. `speak` 동기 타임아웃·슬롯 대기 상한(30초 가설)의 실측 타당성.

## 부록 A. 작업 로그

- 2026-10-20: 기존 STT·프론트 구조 분석 후 본 설계 초안 작성. 기능 코드 변경 없음. PMS 「따까리 · 로컬 TTS」에 WBS 등록.

