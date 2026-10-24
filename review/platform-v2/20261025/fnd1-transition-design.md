# FND1 첫 혼합 세대 전환 TTS 유실 방지 설계 (`fnd1-transition-design.md`)

> **2026-10-25 root 검수: 미채택 / FAIL.** 아래 본문은 설계 작업자의 제안 원문이며, 구현 승인이나 유실 0건의 증명이 아니다. §5.1의 `t0→t1` 스케줄링 유예는 고정 0.5초로 상한을 보장할 수 없다. 별도 검증 브랜치 `review/platform-v2-fnd1-proof-20261025`의 `3643a1f`에서 업스트림 집계 전 요청에 대한 결정적 DB-free 반례와 부분 차단 복구 실패, 총 **2 failed in 0.25s**를 확인했다. 실제 Caddy를 이 검수에서 띄운 결과는 아니다. [검수·부분 교정 기록](fnd1-prototype-audit.md)을 최신 판정으로 본다.
>
> 운영 기준도 이동했다. MCP 배포 영수증 `596947fc-f72d-4d68-b0e9-966826673f21`은 `d56455c816cf286e3cf367d083f3affcb45b00fe`의 succeeded를 기록한다. `git diff 17212d9..d56455c8 -- src/ttakkari/tts/jobs.py src/ttakkari/generation_service.py`는 차이가 없어 구 TTS 문제 조건은 유지된다. 새 main의 `0068_library_intent`와 V2 마이그레이션 체인 재통합은 별도 미완료다. 아래 §1/§4의 "확정"·"유실 0"·§8의 "반영 후 배포" 문구는 이 검수로 기각된 과거 제안이다.

- 작성일: 2026-10-25
- 대상 통합 후보: `integration/platform-v2-execution-all-20261025` (`/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-execution-all-20261025`)
- 구세대(운영 활성) 기준 리비전: `17212d95932d90192912e4aaf235cf8017971576` (`917ea4b` 계열 프로토콜 1 로컬 세대)
- 프로토타입 브랜치 및 로컬 커밋: `prototype/platform-v2-fnd1-transition-20261025` @ `e522187b454c6bdbcf5cc8f4748facb0410560e1` (`/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025`)

---

## 1. 결론 및 선택안 요약

### 1.1 결정: 후보 방향 1 (변형 1-A) 채택
구세대 코드(`17212d95932d90192912e4aaf235cf8017971576`)를 수정하지 않는 제약 아래에서, **프록시(Caddy) 503 입구 차단 영속화 + Caddy Admin API(`GET /reverse_proxy/upstreams`) 기반 업스트림 In-Flight 장벽(`proxy_inflight == 0`) + 구세대 HTTP In-Flight(`http_inflight == 0`) + 구세대 활성 TTS Job(`status IN ('queued', 'running') == 0`)의 3단계 단조 인수인계(Hand-over-hand) 드레인 게이트**를 선택안으로 확정한다.

### 1.2 핵심 동작 요약
1. **입구 차단 및 영속화 (`Slots.close_tts_ingress`)**:
   - 전환 직전 `POST /api/tts/jobs`, `/api/tts/jobs/`를 `503 Service Unavailable`(`Retry-After: 5`)로 거절하는 구성을 `POST http://127.0.0.1:20191/load`로 적용하고, `proxy.json`에도 원자적으로 영속화(`persist_proxy(slot, tts_ingress_closed=True)`)한다 (`src/ttakkari/generation_deployment.py:518-526, 571-574`).
   - 드레인 실패·예외·롤백 시 `finally`/`except` 경로의 `slots.switch(previous_slot)`이 열린 구성을 다시 로드하고 `proxy.json`을 복원한다 (`src/ttakkari/generation_deployment.py:566-569, 1097-1100`).
2. **프록시 전송 계층 In-Flight 관측 (`Slots.proxy_inflight`)**:
   - `GET http://127.0.0.1:20191/config/`로 현재 프록시 업스트림이 구세대 슬롯(`127.0.0.1:{PORTS[slot]}`)을 유지하고 첫 라우트가 `503` 닫힘 상태인지 매 폴링마다 재검증한다.
   - `GET http://127.0.0.1:20191/reverse_proxy/upstreams`에서 구세대 슬롯 업스트림 주소(`127.0.0.1:{PORTS[slot]}`)의 `num_requests` 합계를 읽어 프록시가 이미 수락·포워딩 중이나 아직 응답이 끝나지 않은 요청 수를 확인한다 (`src/ttakkari/generation_deployment.py:576-601`).
3. **3단계 단조 인수인계 폴링 (`GenerationController.drain_previous_tts`)**:
   - 매 폴링마다 반드시 **(1) `proxy_inflight == 0` → (2) `http_inflight == 0` → (3) `_previous_tts_queued(previous_id) == 0` (`status IN ('queued', 'running')`)** 순서로 관측한다 (`src/ttakkari/generation_deployment.py:1086-1142`).
   - 세 값이 모두 `0`일 때만 `activate`로 진행하며, 시간 초과(기본 120초) 또는 어느 한 단계의 조회 실패 시 `StageError("tts-drain", ...)`를 발생시켜 **fail-closed**로 전환을 거부하고 입구를 다시 연다.

---

## 2. 문제(FND1)의 정확한 실행 경로와 기존 후보의 공백

### 2.1 구세대(`17212d9`) TTS 코드의 고정 동작
1. **원문 청크의 인메모리 전용 보관**:
   - 구세대 `accept(plan)`(`17212d9:src/ttakkari/tts/jobs.py:201-239`)은 `if await runtime.refresh() != "active": raise TtsError("generation_not_active", ..., 503)`(`204-205행`)을 한 번 통과한 뒤 캐시를 조회하고, 미스 청크가 있으면 프로세스 메모리 딕셔너리 `_pending_texts[job_id] = list(plan.chunks)`(`48, 230행`)에만 원문을 넣은 채 `TtsJob(status="queued")` 행을 DB에 커밋한다(`214-234행`).
   - 당시 데이터 최소화 설계(`docs/design/local-tts.md:153-168`, `17212d9:src/ttakkari/tts/jobs.py:3-4`)에 따라 DB `tts_jobs`에는 원문 청크가 저장되지 않으며, `params.chunk_keys`(`187-198행`)는 `src/ttakkari/tts/cache.py:61-64`의 `hashlib.blake2b(material, digest_size=16).hexdigest()` 단방향 해시만 담는다.
2. **`draining` 전이 시 `queued` Job 즉시 실패 처리**:
   - 구세대 `_guard_loop()`(`17212d9:src/ttakkari/tts/jobs.py:149-168`)는 주기적으로 `await runtime.refresh() != "active"`(`156행`)를 검사하여, 세대 상태가 `draining`으로 바뀌면 `_fail_queued_draining(runtime.id)`(`144-146행`)를 호출해 자신 소유의 모든 `queued` 행을 `status="failed", error_code="generation_draining"`으로 바꾸고 `_pending_texts.pop(job_id, None)`으로 원문을 삭제한다.
   - 동시에 구세대 소비자 `tick()`(`17212d9:src/ttakkari/tts/jobs.py:462-468`)도 `await runtime.refresh() != "active"`이면 새 `queued` 행 클레임을 중단한다.
3. **구세대 `can_retire()` 및 `tts_jobs.stop()`의 `running` Job 취약점 (추가 발견)**:
   - 구세대 `can_retire()`(`17212d9:src/ttakkari/generation.py:324-326`)는 `snap["state"] == "draining" and not (snap["owned_runs"] or self._work or self.http_inflight)`만 검사하고 `_has_pending_tts_jobs()`를 확인하지 않는다(새 세대 `src/ttakkari/generation.py:387`에서야 추가됨).
   - 반면 구세대 종료 시 호출되는 `tts_jobs.stop()`(`17212d9:src/ttakkari/generation_service.py:321`, `17212d9:src/ttakkari/tts/jobs.py:523-534`)은 실행 중인 합성을 기다리지 않고 취소한 뒤 `fail_owned(me, expected=("queued", "running"), error_code="owner_shutdown")`(`533행`)을 실행한다.

### 2.2 Caddy v2.11.7 `/load` 리로드의 비동기 종료 특성
1. **구 `http.Server`의 비동기 종료와 무제한 유예**:
   - Caddy `modules/caddyhttp/app.go:750-817`에서 `POST /load`에 의한 리로드 시 `App.Stop()`은 `app.stop(false)`(`exiting == false`)를 호출한다.
   - `app.stop(false)`는 각 구 `http.Server` 고루틴이 `server.server.Shutdown(ctx)`에 진입하기 직전에 호출하는 `startedShutdown.Wait()`만 동기 대기하고, 실제 연결·요청 완료를 뜻하는 `finishedShutdown.Wait()`는 백그라운드 고루틴(`783-786행`)으로 넘긴 채 `POST /load`에 `200 OK`를 즉시 반환한다.
   - 또한 구세대 `proxy_config`에는 `grace_period`가 없어 구 `http.Server`는 `"servers shutting down with eternal grace period"`(`app.go:790`)로 동작한다.
2. **프록시-앱 전송 간극(Pre-admission transport gap)**:
   - `close_tts_ingress`가 `POST /load`를 호출하기 직전에 구 `http.Server` 라우트를 통과한 `POST /api/tts/jobs` 요청은 구 라우트 체인의 `reverse_proxy` 핸들러를 타고 구세대 업스트림(`127.0.0.1:8791`)으로 포워딩된다.
   - 만약 이 요청이 업스트림 소켓 버퍼나 Uvicorn 요청 수신 단계에 머물러 아직 구세대 `InflightMiddleware`(`17212d9:src/ttakkari/generation_service.py:338-360`)의 `runtime.http_inflight += 1`에 도달하지 않았다면, 기존 `drain_previous_tts`(`src/ttakkari/generation_deployment.py:1063-1100`)는 `http_inflight == 0`과 DB `queued == 0`만 보고 즉시 성공 반환한다.
   - 직후 그 요청이 구세대 `accept()`에 도달해 `202 Accepted`(`status="queued"`)를 받고 컨트롤러가 `activate()`를 커밋하면, 구세대 `_guard_loop()`가 이를 `failed/generation_draining`으로 유실시킨다 (`review/platform-v2/20261025/root-fnd-verdict.md:30-55`, `tests/test_tts_first_transition_caddy.py:110-134`).

---

## 3. 후보 방향 1~4 및 세부 변형 비교·기각 사유

| 후보 방향 / 변형 | 채택 여부 | 핵심 평가 및 기각/채택 사유 (코드 근거) |
| :--- | :---: | :--- |
| **방향 1-A (선택안)**: 프록시 503 차단 영속화 + Caddy `/reverse_proxy/upstreams` (`num_requests == 0`) + `http_inflight == 0` + DB `queued/running == 0` 순차 장벽 | **채택** | 구세대 무수정 제약을 지키면서 프록시 포워딩 구간($[t_1, t_5]$), 앱 핸들러 구간($[t_2, t_4]$), DB 작업 구간($[t_3, t_6]$)을 중첩된 단조 인수인계 순서로 검증하여 기수락 `202` 유실을 0으로 닫음 (`src/ttakkari/generation_deployment.py:518-601, 1086-1142`). |
| **방향 1-B (기각)**: `proxy_config`에서 TTS 경로만 `localhost:{port}` 등 별도 upstream dial로 분리 | 기각 | ① 첫 전환 시작 시점에는 이미 구세대(`17212d9`)가 띄운 단일 업스트림(`127.0.0.1:8791`)이 활성 상태이므로 첫 전환의 기진입 요청에는 적용되지 않음.<br>② 두 번째 전환부터는 구세대도 V2 코드(`chunks_json` DB 저장, `src/ttakkari/tts/jobs.py:303`)여서 `FND1` 자체가 발생하지 않음.<br>③ Uvicorn은 `--host 127.0.0.1`(`src/ttakkari/generation_deployment.py:339`)로 바인딩되는데 macOS에서 `localhost`는 `::1`을 포함해 불필요한 연결 재시도를 유발할 수 있음. |
| **방향 1-C (기각)**: `close_tts_ingress` 시 `grace_period` 주입 또는 Caddy 프로세스 재기동 | 기각 | ① Caddy `modules/caddyhttp/app.go:776-817`에서 `graceDur := time.Duration(server.GracePeriod)`는 **종료되는 구 서버 인스턴스**(`app.servers`)의 필드를 읽으므로, 이미 `GracePeriod=0`으로 떠 있는 구세대 Caddy에 새 `/load`로 `grace_period`를 보내도 구 서버 종료에 적용되지 않음.<br>② Caddy 프로세스를 강제 재시작하면 포워딩 중이던 요청과 일반 HTTP 요청의 연결을 절단하여 무중단 전환 원칙에 위배됨. |
| **방향 2 (기각)**: 구세대가 접수한 TTS job을 새 세대가 인수/재큐잉 | 기각 | **원천 불가능**: 구세대 `accept()`(`17212d9:src/ttakkari/tts/jobs.py:214-234`)는 DB에 `chunks_json`을 저장하지 않고 `_pending_texts`(`48, 230행`) 메모리에만 보관함. DB `params.chunk_keys`(`187-198행`)는 `blake2b` 16바이트 단방향 해시(`src/ttakkari/tts/cache.py:61-64`)이므로 캐시 미스 청크의 원문 복구가 불가능하며, 구세대 `_guard_loop()`(`144-158행`)가 `draining` 전환 시 메모리 원문도 즉시 삭제함. |
| **방향 3 (기각)**: 프록시에서 503으로 닫고 클라이언트 자동 재시도에만 의존 | 기각 | ① **프런트엔드 자동 재시도 부재**: `front`(`04f29f4`)의 `src/api/client.ts:106-107, 173-184`는 `Retry-After`를 파싱만 하고 `503`을 즉시 throw하며, `src/features/tts/ttsJob.ts:41-49`도 자동 재요청 없이 에러를 반환함. `src/features/tts/useTtsGeneration.ts:246, 260-265`, `src/pages/TtsPage.tsx:180-190`, `src/features/tts/SpeakButton.tsx:64-67, 94-98` 모두 즉시 실패 UI를 띄우고 사용자의 수동 재시도 클릭을 요구함.<br>② **기진입 요청 미보호**: 503 입구 차단은 닫힌 뒤의 신규 요청만 거절할 뿐, 닫기 직전 프록시를 통과해 구세대에서 `202`를 받은 요청의 유실을 막지 못함. |
| **방향 4 (기각)**: 첫 전환만 수동 운영 절차로 분리하고 코드 변경 생략 | 기각 | 수동 확인 시점과 실제 전환 명령 실행 시점 사이의 경쟁 조건을 제거할 수 없으며, 증명 실패 시 자동 차단(fail-closed)하는 안전장치가 없음. (단, 방향 1-A 코드 장벽 위에 첫 전환 시 활성 Run SSE 스트림 유휴 시점을 택하는 운영 가이던스는 보조적으로 유효함.) |

---

## 4. 안전성 논증 (Safety Proof)

### 4.1 단일 요청의 생명주기 시각 정의
프록시를 통해 구세대로 진입하여 `queued` TTS job을 생성하는 임의의 요청 $R$에 대해 다음 시각을 정의한다:
- $t_0$: Caddy `http.Server`가 소켓에서 HTTP 요청 헤더를 읽기 시작한 시각.
- $t_1$: Caddy `reverseProxy.ServeHTTP`(`modules/caddyhttp/reverseproxy/reverseproxy.go`)가 업스트림 선택 직후 `di.Upstream.Host.countRequest(1)`을 호출하여 `num_requests`가 $+1$ 증가한 시각.
- $t_2$: 구세대 Uvicorn의 `InflightMiddleware.__call__`(`17212d9:src/ttakkari/generation_service.py:354-355`)이 `runtime.http_inflight += 1`을 실행한 시각.
- $t_3$: 구세대 `accept(plan)`(`17212d9:src/ttakkari/tts/jobs.py:232-234`)이 `db.add(job); await db.commit()`으로 `tts_jobs`에 `status='queued'` 행을 커밋한 시각.
- $t_4$: 구세대 `InflightMiddleware.__call__`(`17212d9:src/ttakkari/generation_service.py:359-360`)의 `finally` 블록이 `runtime.http_inflight -= 1`을 실행한 시각.
- $t_5$: Caddy `reverseProxy.ServeHTTP`가 업스트림으로부터 HTTP `202` 응답 수신 및 전달을 마치고 `defer di.Upstream.Host.countRequest(-1)`을 실행하여 `num_requests`가 $-1$ 감소한 시각.
- $t_6$: 구세대 TTS 소비자(`run_claimed`, `17212d9:src/ttakkari/tts/jobs.py:284-354`)가 해당 job을 `status='succeeded'`(또는 터미널 상태)로 커밋한 시각.

### 4.2 구간 중첩 불변식 (Overlapping Interval Invariant)
HTTP 요청-응답의 동기 감싸기 구조에 의해 모든 `202 Accepted`(`queued`) 요청 $R$은 반드시 다음 엄밀한 부등식을 만족한다:
$$t_0 < t_1 < t_2 < t_3 < t_4 < t_5 < t_6$$

따라서 세 관측 지표가 양수($\ge 1$)인 구간:
- $S_1 = [t_1, t_5]$ (`proxy_inflight >= 1`)
- $S_2 = [t_2, t_4]$ (`http_inflight >= 1`)
- $S_3 = [t_3, t_6]$ (`queued_or_running >= 1`)

사이에는 빈틈(gap)이 존재하지 않으며, 반드시 구간이 겹친다:
- $S_2 = [t_2, t_4] \subset [t_1, t_5] = S_1$
- $S_1 \cap S_3 = [t_3, t_5] \neq \emptyset$ ($t_3 < t_4 < t_5$이므로 DB 커밋 $t_3$ 시점에는 `http_inflight`와 `proxy_inflight`가 모두 아직 감소하기 전임)

### 4.3 단조 인수인계(Hand-over-hand) 관측을 통한 유실 0 증명
`drain_previous_tts`가 시각 $\tau_{\text{close}}$에 `close_tts_ingress`를 완료한 뒤, 어떤 폴링 반복에서 시각 $\tau_1 < \tau_2 < \tau_3$ 순서로:
1. $\tau_1$: `proxy_inflight(previous_slot) == 0`
2. $\tau_2$: `http_inflight == 0`
3. $\tau_3$: `_previous_tts_queued(previous_id) == 0` (`status IN ('queued', 'running')`)

을 모두 관측했다고 하자.
- $\tau_{\text{close}}$ 이후에 새로 들어오는 `POST /api/tts/jobs`는 새 Caddy `http.Server`의 0번 라우트(`status_code: 503`)에서 즉시 거절되므로 구세대 업스트림에 진입하지 않는다.
- $t_1 \le \tau_1$에 이미 Caddy `reverseProxy`에 진입했던 요청 $R$에 대해, $\tau_1$에서 `proxy_inflight == 0`이라는 것은 $\tau_1 \notin [t_1, t_5]$, 즉 $t_5 < \tau_1$임을 뜻한다.
- 부등식 $t_3 < t_4 < t_5 < \tau_1 < \tau_2 < \tau_3$에 의해, 요청 $R$이 `202 Accepted`를 반환했다면 그 DB 행은 $\tau_3$보다 앞선 $t_3$에 이미 커밋되어 있다.
- 그런데 $\tau_3$에서 `status IN ('queued', 'running')` 행 수가 `0`이므로 $t_6 < \tau_3$이다. 즉 요청 $R$은 컨트롤러가 `activate()`를 호출하기 전에 구세대(`state == 'active'`)에서 이미 합성 완료(`succeeded`)되었다.
- 반대로 만약 $\tau_1, \tau_2, \tau_3$ 중 하나라도 `> 0`이거나 조회 에러가 나거나 타임아웃(`120s`)에 도달하면, `drain_previous_tts`는 `StageError("tts-drain")`를 던져 **`activate()`를 호출하지 않고** `slots.switch(previous_slot)`으로 프록시 입구를 복원한다. 구세대가 `draining`으로 전이되지 않으므로 구세대 `_guard_loop()`의 `_fail_queued_draining`이나 `tts_jobs.stop()`의 `owner_shutdown`은 실행되지 않는다.

---

## 5. 남는 가정 및 경계 조건

1. **Caddy 내부 라우트 매칭($t_0 \to t_1$) 스케줄링 유예**:
   - `POST /load` 반환(`startedShutdown.Wait()`) 직전에 구 `http.Server`가 소켓에서 읽었으나 아직 `reverseProxy()` 첫머리의 `Host.countRequest(1)`($t_1$)에 도달하지 않은 순수 인메모리 라우트 매칭 고루틴은 `drain_previous_tts` 시작부의 `await asyncio.sleep(poll)`(운영 기본값 `0.5s`) 안에 $t_1$에 도달한다고 가정한다 (`modules/caddyhttp/app.go:776-817`).
2. **첫 전환 시 단일 업스트림과 활성 Run SSE(`/api/runs/{id}/stream`)의 공존**:
   - 현재 운영 중인 구세대(`17212d9`)의 Caddy는 `127.0.0.1:8791` 단일 업스트림으로 구성되어 있다.
   - `/api/runs/{id}/stream`(`src/ttakkari/api/sessions.py:282-324`)은 `InflightMiddleware`(`17212d9:src/ttakkari/generation_service.py:352-353`)에서 `http_inflight` 집계에서는 제외되지만, Caddy의 `127.0.0.1:8791` `num_requests`에는 포함된다.
   - 단, `stream_run`은 터미널 상태 Run에 대해 즉시 `end` 이벤트를 보내고 종료하므로(`src/ttakkari/api/sessions.py:313-315`), **실행 중인(`running`/`queued`) Run을 브라우저가 실시간 구독 중일 때만** SSE 연결이 열려 있다.
   - 만약 첫 전환 순간에 열린 Run SSE가 120초 이상 유지되면 `proxy_inflight >= 1`로 인해 `drain_previous_tts`가 **fail-closed**(활성화 거부 및 입구 복구)한다. 이는 데이터 유실 위험이 아니라 가용성 지연이며, 활성 Run 스트림이 닫힌 후 재시도하면 통과한다.
3. **드레인 창 동안의 신규 TTS POST 503 응답**:
   - 유휴 상태에서 드레인 창은 약 `0.5s` 동안만 열리며, 이 짧은 구간에 새로 도착한 `POST /api/tts/jobs`는 `503 Service Unavailable`(`Retry-After: 5`)을 받는다. 원문은 서버에 접수되지 않고 클라이언트 입력창에 그대로 남는다.

---

## 6. 구현 범위 (파일 및 줄 수)

프로토타입 커밋 `e522187b454c6bdbcf5cc8f4748facb0410560e1` 기준 실제 변경 범위는 다음과 같다:

| 파일 경로 | 변경 성격 | 변경 줄 수 | 주요 변경 내용 |
| :--- | :--- | :---: | :--- |
| `src/ttakkari/generation_deployment.py` | 프로덕션 코드 수정 | `+62 / -19` | `Slots.admin_url` 기본값 도입, `Slots.persist_proxy(tts_ingress_closed)` 확장, `Slots.close_tts_ingress` 영속화, `Slots.proxy_inflight(slot)` 추가, `_previous_tts_queued`를 `status IN ('queued', 'running')`로 확장, `drain_previous_tts`에 `proxy_inflight` 선행 검증 추가 |
| `tests/test_generation_deployment.py` | 기존 테스트 더블 보강 | `+3 / -0` | `FakeSlots.proxy_inflight` 추가 |
| `tests/test_tts_first_transition.py` | 단위·계약 테스트 확장 | `+96 / -9` | `_Slots.proxy_inflight` 추가, `proxy_inflight`/`proxy_error`/`running` 거절 케이스 추가, `proxy.json` 영속 및 `MockTransport` 기반 `Slots.proxy_inflight` + `drain_previous_tts` 장벽 계약 테스트 추가 |
| `tests/test_tts_first_transition_pending_delivery.py` | 경계 재현 테스트 갱신 | `+76` (신규) | 프록시 포워딩 후 미전달 요청(`proxy_inflight=1, inflight=0`)의 전달·완료 대기 후 `succeeded` 검증 및 전달 정체 시 fail-closed 검증 |
| `tests/test_tts_first_transition_caddy.py` | 실 Caddy 재현 테스트 갱신 | `+185` (신규) | 격리 실 Caddy 인스턴스(`/opt/homebrew/bin/caddy`) + `/reverse_proxy/upstreams` 연동 시 정체 요청 fail-closed 및 지연 전달 요청 완주 후 `succeeded` 검증 |

---

## 7. 검증 계획 및 프로토타입 실행 결과

### 7.1 기존 Caddy 및 미전달 재현 테스트를 Green으로 전환하는 방법
1. `tests/test_tts_first_transition_pending_delivery.py`:
   - 기존 반례 프로브는 `_Slots`에 프록시 계층 진행 요청 모델이 없어 `inflight=0`에서 즉시 게이트가 열리는 현상을 재현했다.
   - `_Slots(old_id, inflight=0, proxy_inflight=1)`로 포워딩 중 요청을 표현하고, (1) 요청이 API에 도달해 `queued` → `succeeded`로 끝날 때까지 `drain_previous_tts`가 대기한 뒤 `activate` 이후에도 `("succeeded", None)`이 되는지, (2) 끝내 API에 도달하지 못하면 `StageError("tts-drain")`로 fail-closed하여 `activate`를 막고 구세대(`active`)에서 유실되지 않는지를 검증한다.
2. `tests/test_tts_first_transition_caddy.py`:
   - 격리된 임시 포트로 띄운 실제 Caddy 서브프로세스에서 `Slots.proxy_inflight`가 `GET http://127.0.0.1:{admin_port}/config/` 및 `GET http://127.0.0.1:{admin_port}/reverse_proxy/upstreams`를 조회하도록 연결한다.
   - `forwarded.wait()` 직후(`admitted == 0`, Caddy `num_requests == 1` 상태):
     - (1) `deliver`가 풀리지 않은 채 `drain_previous_tts(timeout=0.15, poll=0.02)`를 호출하면 조기 통과하지 않고 `StageError("tts-drain", ... "proxy_inflight=1")`로 fail-closed하며 구세대가 `active`로 유지됨을 assert한다.
     - (2) 드레인 중 `deliver.set()` → `commit.set()` → DB `status="succeeded"` 전이가 일어나면 `drain_previous_tts`가 `proxy_inflight == 0` → `http_inflight == 0` → `queued == 0`까지 대기한 뒤 반환하고, 이어서 `activate(new_id, expected=old_id)` 및 `_old_guard_pass(old_id)`를 실행해도 최종 상태가 `("succeeded", None, ["forwarded original"])`로 green이 됨을 assert한다.

### 7.2 중첩 워커 샌드박스 내 실행 완료 항목
- 실행 환경: Antigravity 중첩 워커 샌드박스 (`Standard Sandbox Mode`)
- 대상 프로토타입 worktree: `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025` (`e522187b454c6bdbcf5cc8f4748facb0410560e1`)

1. **Ruff 정적 검증 + MockTransport 계약 단위 테스트 실행**:
   - 명령:
     ```bash
     /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/ruff check --no-cache --config /Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/pyproject.toml /Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/src/ttakkari/generation_deployment.py /Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/tests/test_tts_first_transition.py /Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/tests/test_tts_first_transition_pending_delivery.py /Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/tests/test_tts_first_transition_caddy.py && /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest --noconftest -o addopts="" -o asyncio_mode=auto -o pythonpath="/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/src /Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/tests" /Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/tests/test_tts_first_transition.py -k "test_proxy_config_closes_only_new_tts_job_posts_and_keeps_the_upstream_route_last or test_slots_proxy_inflight_and_drain_barrier_contract" -q
     ```
   - 출력:
     ```text
     ..                                                                       [100%]
     2 passed, 11 deselected in 0.41s
     ```
2. **프록시 전환/영속 회귀 테스트 실행**:
   - 명령:
     ```bash
     /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest --noconftest -o addopts="" -o asyncio_mode=auto -o pythonpath="/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/src /Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/tests" /Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/tests/test_generation_deployment.py -k "test_third_slot_proxy_config_is_persisted or test_proxy_switch_persists_its_own_restart_config" -q
     ```
   - 출력:
     ```text
     ..                                                                       [100%]
     2 passed, 47 deselected in 0.24s
     ```

### 7.3 샌드박스 제약으로 인한 미실행 항목 및 호스트 실행 명령
- **미실행**: 중첩 워커 샌드박스(`antigravity` worker sandbox)에서 루프백 TCP 소켓 연결(`conftest.py`의 `127.0.0.1:56750` 테스트 PostgreSQL 연결 및 `test_tts_first_transition_caddy.py`의 `127.0.0.1:0` 로컬 소켓 바인딩)이 `PermissionError: [Errno 1] Operation not permitted`로 차단되어 DB·격리 Caddy 직결 pytest는 샌드박스 내에서 실행하지 못함.
- **호스트(샌드박스 외부) 검증 실행 명령**:
  ```bash
  PYTHONPATH="/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/src:/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/tests" \
    /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python \
    /Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/scripts/pytest_slots.py -- \
    /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest \
    /Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/tests/test_tts_first_transition.py \
    /Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/tests/test_tts_first_transition_pending_delivery.py \
    /Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-fnd1-proto-20261025/tests/test_tts_first_transition_caddy.py -q
  ```

---

## 8. 운영 적용 시 필요한 사용자 결정 사항

1. **추가 아키텍처·범위 결정 불필요**:
   - 새 다중세대 lease/fence, MSA 분리, 프런트엔드 프로토콜 변경 없이 단일 컨트롤러(`src/ttakkari/generation_deployment.py`)와 기존 Caddy Admin API(`127.0.0.1:20191`) 안에서 완결된다.
2. **통합 브랜치 반영 및 첫 전환 시점**:
   - 프로토타입 커밋(`prototype/platform-v2-fnd1-transition-20261025` @ `e522187b454c6bdbcf5cc8f4748facb0410560e1`)을 통합 후보 브랜치(`integration/platform-v2-execution-all-20261025`)에 반영하고 호스트에서 위 3개 테스트를 통과시킨 뒤 배포를 진행하면 된다.
   - 첫 전환 시에는 구세대(`17212d9`)가 단일 업스트림(`127.0.0.1:8791`)으로 떠 있으므로, 브라우저에서 실행 중인 Run의 SSE 스트림(`/api/runs/{id}/stream`)이 열려 있지 않은 시점에 전환하면 120초 fail-closed 대기 없이 약 `0.5s` 만에 즉시 컷오버된다.
