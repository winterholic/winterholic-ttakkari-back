# ADR 0062. 로컬 TTS: 별도 페이지·엔진 추상화·요청별 프로세스 (개정 4판)

- 상태: 검증대기 (2026-10-20, 4차 독립 검증 지적 2건 반영 개정). **엔진 상태: Kokoro-82M은 한국어 음성이 없어 품질 미달로 확인되었고, 대체 모델은 사용자가 결정한다(임의 선택·설치 금지). 이 ADR은 `TtsProvider` 추상화로 엔진 중립이며, Kokoro 전제 수치는 [엔진 결정 후 재측정]이다.**
- 선행: ADR 0025·0026·0027·0028(로컬 STT·요청 수명), ADR 0010(회의록), [TTS 설계](../design/local-tts.md).
- 사용자 결정: TTS는 회의록 STT에 통합하지 않는다. 별도 생성 페이지 + 채팅 응답의 선택적 재생 버튼. 엔진은 Kokoro-82M 우선·로컬·무료·교체 가능 추상화. 대형 모델 미상주로 평시 자원 점유 최소화.

## 배경과 개정 사유

초안(0061) 및 1차 개정안에 대한 독립 검증에서 다음 사항이 지적되었다:
1. 입력 원문 보관 대안의 혼선(인메모리 vs 스테이징 파일) 및 DB 원문 부재를 사용자 추가 지시처럼 표현한 점, 과장된 영구 소멸 표현(shred & unlink 등).
2. generation 전환/drain 시 인메모리 원문의 프로세스 격리 한계와 구 세대 job 복구 불가 조건 미반영.
3. HTTP job 상태 조회 계약(410 vs 200 failed)의 모순, /speak와 /jobs 간 직렬 실행 예산 미명시, `run_json_worker`의 배치 완료 계약과 청크 조기 제공 간 불일치.
4. 미래 Alembic 마이그레이션 번호(0055) 사전 예약.
5. iOS Safari 모바일 재생 안정성의 무조건적 보장 과장 및 play Promise rejection 대비 부재.
6. CPU/스레드 경합에 대한 미측정 충분성 단정, 유휴 자원 0/부모 RSS 0 과장, 부모 PID 감시와 프로세스 수명 간 모순, 날짜 발음(십월 vs 시월) 오류.
7. Kokoro 품질 통과 시에도 불필요하게 사용자를 차단하는 게이트 과대 해석.

3차 독립 검증(2026-10-20)은 위 대부분을 해소로 보았으나 다음 계약 결함을 추가로 지적했다.
8. `run_json_worker`가 `request` 참조를 자식 종료까지 유지하므로 "stdin 전달 직후 부모 참조 해제"가 실제 계약과 맞지 않는다.
9. DELETE로 `cancelled`가 된 job을 worker가 `succeeded`로 덮을 수 있다.
10. draining 세대가 미claim queued job을 어떻게 하는지, 세대별 원자적 claim 조건이 정해지지 않았다.
11. heartbeat 만료를 사망으로 보는 문구가 코드 원칙(`generation.py:1-4`)과 충돌한다.
12. GET의 "항상 200"이 인증 실패·미존재까지 포함하는 것처럼 읽힌다.

본 3차 개정안은 8~12를 코드 근거와 함께 반영했다. 상세는 [설계서](../design/local-tts.md) §2.1, §7.2, §8.2, §8.3.

4차 독립 검증은 3차 5건을 반영으로 확인하고 다음 2건을 추가로 지적했다.
13. draining 중 queued 실패 처리가 세대 퇴역과 동기화되지 않는다. `can_retire()`·`/api/ready`가 TTS 정리 완료를 보지 않아, 정리 전에 퇴역하면 job은 `queued`로 남고 원문만 사라질 수 있다.
14. Bearer 인증 근거 경로가 `api/auth.py`로 잘못 인용되었다(실제 Bearer 검증은 `security/auth.py`).

본 4차 개정안은 13을 결정 6의 queued guard로, 14를 설계서 §7.2 인용 정정으로 반영한다.

## 결정

1. **별도 TTS 경로와 기존 격리**: `src/ttakkari/tts/` 독립 패키지와 `/api/tts` 라우터, 프론트 `/tts` 페이지를 신설한다. 회의록 파이프라인, 기존 STT 직렬 큐(`meetings/local_stt/jobs.py`), STT 스키마, E5 임베더(`context/embedder.py`의 자체 락)는 일체 변경하지 않는다.
2. **엔진 추상화와 모델 채택 원칙**: 부모 프로세스는 `TtsProvider` Protocol(voices·synthesize·capabilities·availability)만 본다. 엔진 어댑터는 플러그형으로 둔다. Kokoro 한국어 품질이 실사용 기준을 충족하면 별도 승인 게이트 없이 다음 단계(WBS 5 이후)로 진행한다. **단, 한국어 품질 미달로 타 모델 교체가 필요할 때에만 임의로 교체하지 않고 벤치 결과와 대안을 보고하여 사용자 승인을 받는다. 현재 Kokoro는 한국어 음성 부재로 미달이 확인되어 이 분기에 있으며, 대체 모델은 사용자가 결정한다. 이 ADR은 후보를 정하지 않고, 엔진 결정 전에는 구현에 착수하지 않는다.**
3. **요청별 일회용 프로세스**: Phase 1 프로세스 모델은 요청별 일회용 자식 프로세스다. `model_process.run_json_worker`를 재사용하며 STT와 동일한 감독(회수·타임아웃·부모 감시·무자격증명·오프라인 강제)을 따른다. 부모 감시는 `run_json_worker`가 강제하지 않고 워커 모듈이 `watch_parent`(`model_process.py:87-91`, 1초 폴링)를 시작해야 성립하므로 TTS 워커도 시작해야 한다. 부모 사망 시 자식은 `os._exit(74)`로 종료하지만 최대 약 1초 지연될 수 있다. **`run_json_worker`는 변경하지 않으며**(호출자 STT `meetings/local_stt/runner.py:28`, E5 `context/embedder.py:64`에 영향이 없도록) 부모는 요청 원문을 자식 종료까지 보유한다(결정 5). 대형 모델 가중치(수백 MB)는 메모리에 상주하지 않으나 부모 프로세스의 큐/메타데이터 관리 메모리는 정상 소비된다.
4. **청크 단위 합성 및 캐시 키 통일**: 문장 청크 단위로 합성하며, 캐시 키의 원자 단위를 청크 키로 통일한다. 키는 `blake2b("tts1" ‖ engine ‖ model_revision ‖ voice ‖ speed ‖ normalize(chunk_text))`이며 파일은 `<home>/tts-cache/`에 보관한다.
5. **원문 미저장 불변성(설계상 데이터 최소화) 및 인메모리 수명 관리**:
   - DB `tts_jobs`에는 텍스트 원문을 저장하지 않는다(파라미터·해시·길이만). 워커 결과에도 원문을 싣지 않고 부모가 허용 목록 지표만 `result_metadata`에 저장한다.
   - 비동기 job의 원문은 접수한 프로세스의 인메모리 버퍼(`_pending_texts[job_id]`) 하나로만 관리한다(임시 디스크 파일 없음).
   - **수명(정직한 계약)**: 기존 감독기를 그대로 쓰므로 부모는 `request` 인자(`model_process.py:17`)와 stdin용 bytes(`:65`)를 자식이 끝날 때까지 쥔다(`:63-67`). 부모 보유 상한은 "claim 후 자식 종료(정상·타임아웃·취소 kill/reap)"이며 "stdin 전달 직후"가 아니다. 이전 판의 해제 문구는 철회한다. OS swap·덤프·RAM 잔존은 보장하지 않는다.
   - 종료 규칙: TTL 만료(queued 10분) `failed(input_ttl_expired)`, 취소·실패·완료 시 확정 직후 해제, drain·종료·사망은 결정 6. 모든 terminal 전이는 결정 6의 조건부 UPDATE가 성공한 뒤에만 확정한다. consumer 틱마다 terminal·행 없음인 `_pending_texts` 키를 정리한다.
   - 조기 해제(TTS 전용 래퍼 또는 `model_process.py`의 환경 구성부 추출)는 Phase 2 옵션으로 미룬다. 하게 되면 STT·E5가 같은 경로를 지나므로 동작 불변 리팩터로 한정하고 `tests/test_embedder_lifetime.py`, `tests/test_local_stt_benchmark.py`와 신규 테스트로 확인한다.
6. **Generation 소유권·terminal 확정·drain·사망 확인** (근거: `generation.py:69-86, 261-277, 289-291, 1-4`, `generation_service.py:100-114, 158-168`, `generation_deployment.py:239-249, 294-297, 710-713`, `meetings/local_stt/jobs.py:253-261`):
   - **소유권과 claim**: job은 접수 세대의 `owner_id`를 기록한다. claim은 `status='queued' AND owner_id=:me AND NOT EXISTS(running 행)`을 만족하는 한 문장의 조건부 UPDATE(`FOR UPDATE SKIP LOCKED`)이며, `generation.work("tts")`가 True(active + `work-tts.lock` 보유)일 때만 시도한다. 새 세대는 구 세대의 job을 claim하거나 재실행하지 않는다(원문이 없다).
   - **terminal 확정(취소 경합)**: terminal(`succeeded`·`failed`·`cancelled`)에서 나가는 전이는 없다. 모든 전이는 `UPDATE ... WHERE id=:id AND status=:expected [AND owner_id=:me]`이고 영향 행 1일 때만 확정한다. DELETE가 먼저 `cancelled`를 확정하면 worker의 `running→succeeded`는 0행이 되어 결과를 노출하지 않는다. `cancelled`가 `succeeded`로 덮이지 않는다.
   - **drain(하나로 확정)**: draining 세대는 이미 running인 job만 끝낸다(`work()`는 active에서만 True, 실행 중 블록은 중단되지 않음). 미claim queued job은 구 세대가 계속 처리하지 않고 자기 소유 queued를 `failed(generation_draining, retryable)`로 확정하고 원문을 비운다. 계속 처리하려면 `generation.py`의 `work()`/`can_retire()`를 바꿔야 하고 모든 세대 큐에 영향이 가며, 원문은 구 세대 메모리에만 있어 인수도 불가능하다.
   - **정리와 퇴역의 순서 보장(4차)**: 퇴역은 deployer가 `/api/ready`의 `drain_ready`를 보고 결정하며(`generation_deployment.py:239-249, 294-297, 710-713`), `drain_ready`는 `can_retire() and not (manager._handles or pending)`이고 `pending`은 `runs.manager._background` 등의 미완료 태스크 수다(`generation_service.py:158-168`, `runs/manager.py:1681, 1705-1709`). `can_retire()`는 `_pending_texts`를 보지 않는다(`generation.py:289-291`). 따라서 TTS는 **소유 queued job이 있는 동안 `_background`에 `tts-queued-guard` 태스크를 반드시 두고**(`POST /jobs`가 job 커밋 전에 생성, 이미 있으면 재사용), guard가 draining을 감지해 queued를 CAS로 `failed(generation_draining)` 처리하고 원문을 비운 뒤 종료하도록 한다. 그 전에는 `drain_ready`가 False다. running은 기존 `_work`가, 처리 중인 POST는 `http_inflight`가 막는다. **공유 세대 코드(`work`·`can_retire`·`ready`)는 변경하지 않는다.** `can_retire` 확장안은 모든 큐와 배포 판정에 영향이 가서 기각했다.
   - `generation_service.consume()`은 draining 분기를 넣을 수 없으므로(`generation_service.py:105-107`) TTS는 같은 `generation.work("tts")` 프리미티브로 자체 소비 루프를 둔다. active가 아닌 세대의 `POST /api/tts/jobs`는 503으로 접수하지 않는다. 정상 종료 시 소유 job은 `failed(owner_shutdown)`. 종료 유예 설정은 저장소에서 찾지 못해(확인 필요) 정상 경로 순서 보장을 종료 루틴에 기대지 않는다.
   - **소유자 생존**: **heartbeat 만료는 사망 증거가 아니다**(`generation.py:1-4`는 하트비트 만료로 lease를 훔치지 않는다고 명시). 허용 증거는 `<home>/runtime/<owner_id>.lock`의 flock 획득 성공(OS가 프로세스 종료 시 해제), 또는 `runtime_generations.state='offline'`(`generation.py:125-136, 310-317`)뿐이다. 증거를 확인하기 전에는 다른 세대의 job을 실패 처리하거나 인수하지 않는다. 증거가 없거나 소유자 행이 없으면 아무것도 하지 않는다. 사망 확인 후에만 CAS로 `failed(owner_gone)`를 확정하고, running 행은 부모 감시 지연을 고려한 유예 후 정리한다(그동안 claim의 `NOT EXISTS running`이 새 시작을 막는다). **읽기 시점 규칙**: GET·DELETE가 다른 소유자의 non-terminal job을 읽을 때 같은 증거로 사망이 확인되면 CAS로 `failed(owner_gone, retryable)`을 확정해 돌려주고, 증거가 없으면 실제 상태를 돌려준다. 이 규칙과 새 세대의 정리는 SIGKILL·크래시 같은 비정상 종료의 수렴 보장이며 정상 드레인의 순서 보장은 위 guard가 맡는다. 세대 미사용 모드는 단일 프로세스를 가정해 시작 시 non-terminal을 `failed(server_restarted)`로 확정한다.
7. **HTTP 응답 계약 및 직렬 실행 예산**:
   - `GET /api/tts/jobs/{id}`: 인증 실패 **401**, UUID 형식 오류·미존재·타 소유자 job **404**(존재 비노출, 경로 인자를 `str`로 받아 직접 파싱해 전역 422 핸들러를 피함, `main.py:259-269, 279-284`). 401의 근거는 `security/auth.py:58-72`(`require_user`, 핸들러는 `_: User`로 받음, 사용례 `api/meetings.py:35, 109`)이며 `api/auth.py`가 아니다. 접근 가능한 job은 **항상 200 + `status` 필드**(`queued|running|succeeded|failed|cancelled`)이며 failed에는 `error_code`·`retryable`을 둔다. 410은 쓰지 않는다. 이전 판의 "인증·404까지 200" 해석은 철회한다. DELETE도 같은 인증·404 규칙이며 이미 terminal이면 현재 상태를 200으로 돌려준다.
   - 직렬 실행 예산: 세대 모드에서는 `work-tts.lock` 파일 잠금(세대를 가로지르는 전역 1개)을, 세대 미사용 모드에서는 프로세스 내 `asyncio.Lock`을 단위로 하며 `/speak`와 큐 consumer가 같은 예산을 쓴다. 프로세스 내 락만으로는 세대 중첩 시 전역 1개가 보장되지 않는다. `/speak`는 최대 15초 대기 후 503.
   - `run_json_worker`는 종료 시 `result.json`을 일괄 반환하므로 Phase 1에서 캐시 미스 job의 첫 청크 조기 재생은 지원하지 않는다(캐시 히트로만 단축). 청크 즉시 재생이라는 사용자 요구와의 간극은 구현 전 사용자에게 확인한다. 프로토콜 확장은 Phase 2.
8. **iOS Safari 모바일 재생 전략**:
   - 모바일 재생 보장을 단정하지 않고 [미검증 가설]로 다룬다.
   - 사용자 제스처 동기 시점에 단일 `HTMLAudioElement` 언락을 시도하고 동일 엘리먼트의 `src` 체이닝을 적용하되, WebKit 자동재생 정책(https://webkit.org/blog/6784/new-video-policies-for-ios/) 및 제스처 만료 버그(https://bugs.webkit.org/show_bug.cgi?id=134925)에 따른 `play()` Promise rejection을 반드시 catch한다.
   - 재생 차단 시 사용자에게 "다음 문장 터치" 안내를 표시하는 재터치 복구 UX를 필수로 구비하고, iOS 실기기 체크리스트 검증을 통과해야 한다.

## 상주를 택하지 않은 근거

대형 모델 가중치 미상주 요구를 직접 충족하며, 배포 세대 교체 시 상주 워커 풀 드레인·프리로드의 복잡도를 피하고, Kokoro 로드 비용 미확인 상태의 시기상조 최적화를 방지하기 위함이다. 캐시 미스 단문 TTFA p50이 8초(Kokoro 전제 가설, [엔진 결정 후 재측정])를 넘으면 상주 도입을 재검토한다.

## 대안과 한계

- **공용 슬롯 2개안**: 기존 STT 직렬 큐 및 E5 락과 불일치하여 폐기했다. 독립 직렬 큐로 분리한다. STT 4스레드(실제 설정 확인)와 TTS 스레드 수(엔진 결정 후 확정)의 동시 실행 간섭은 벤치로 실측한다.
- **구 세대가 drain 동안 queued까지 계속 처리안**: `work()`·`can_retire()` 변경이 필요하고 모든 세대 큐에 영향이 가서 채택하지 않았다(결정 6).
- **`can_retire()`/`ready()` 확장안**: TTS 전용 조건이 모든 배포의 퇴역 판정에 들어가므로 기각하고, 이미 `ready()`가 읽는 `_background` 집합에 guard를 두는 방식을 택했다(결정 6).
- **읽기 시점 규칙·새 세대 정리 단독안**: 정상 퇴역에서 job이 `queued`로 남고 원문만 없어지는 구간을 막지 못해 주 경로로는 기각, 비정상 종료 보조로 유지(결정 6).
- **heartbeat 만료로 구 세대 job 인수·실패안**: `generation.py:1-4` 원칙과 충돌하고 살아 있는 소유자의 작업을 실패시킬 수 있어 폐기했다(결정 6).
- **임시 파일 스테이징안**: 인메모리 관리로 단순화하며, 디스크 I/O 및 shred 복잡도는 Phase 1에서 제외하고 대용량 대비 미래 검토로 분리한다.
- **마이그레이션 번호**: 0055 등의 번호를 사전에 예약하지 않으며, 구현 시점의 Alembic 최신 head 및 down_revision을 기준으로 확정한다.

## 검증 범위

채택 전 필수 확인: 대체 엔진 결정(사용자) 후 한국어 품질 실측, iOS 실기기 연속 재생 및 재터치 복구 검증, STT 동시 실행 시 상호 RTF 저하율 실측. 구현 단계 확인: 취소·claim·drain·사망 확인 경합 테스트와 퇴역 순서 테스트(queued guard가 있는 동안 `drain_ready` False, 정리 후에만 True, 설계서 §12). 검증 기록은 `back/docs/tts/`에 둔다.
