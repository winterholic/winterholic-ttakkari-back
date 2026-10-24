# FND1: 전송 완료 미확인 전환 거절 및 실제 Caddy 반례

2026-10-25 · root Codex command sandbox · 부모 `1cc48b6a3c750ada88d0edf8d54a581dc3c96a94`

이번 교정은 **첫 전환의 안전한 거절**이다. lossless 자동 전환 장벽을 완성한 것이 아니다. `Slots.wait_tts_transport_drained`의 실구현은 현재 stock Caddy에서 명시적으로 거절한다. 실제 첫 local→remote 배포는 계속 보류한다. 운영 프록시·서비스·DB·GPU는 변경하지 않았다.

## 관측과 원인

기존 `drain_previous_tts`는 새 접수를 닫고 앱 HTTP와 queued/running 0을 읽은 뒤 반환했다. 이전 `e522187`의 upstream counter 제안까지 포함해, route handler 진입부터 upstream countRequest 사이를 증명하지 못한다.

이번에는 헤더 미완성 연결을 반복하지 않았다. **실제 Caddy v2.11.7**의 기존 reverse_proxy에 `request_buffers=16`과 metrics를 켠 격리 fixture를 사용했다. 이는 운영 설정과 다르다. 운영에서 버퍼링이 켜졌거나 실제 job 유실이 발생했다는 측정이 아니다. 구 API/DB/guard/consumer는 메모리 모델이다.

완료된 헤더와 `Expect: 100-continue`, 미전송 본문으로 prepareRequest의 body reader를 대기시켰다. Caddy의 `100 Continue`를 받아 대기 구간 진입을 확인했다. 고정 sleep으로 안전을 판정하지 않았다.

| 관측 | 부모 controller | 교정 controller |
|---|---|---|
| reload 전 handler metrics | reverse_proxy in_flight=1 | 동일 |
| upstream / API HTTP / active jobs | 0 / 0 / 0 | 동일 |
| reload 후 handler metrics | 빈 목록 | 빈 목록 |
| controller 판정 | allowed | refused / transport completion unavailable |
| 대기 요청 재개 후 실제 HTTP 응답 | 202 | 202 |
| 구 API 모델의 job | failed/generation_draining | succeeded (구세대 active 유지) |

실행 원문: [baseline JSON](fnd1-pre-counter-baseline.json), [guarded JSON](fnd1-pre-counter-guarded.json). baseline JSON은 같은 턴의 stdout을 보존했다. baseline은 Git 객체에서 정확한 부모 generation_deployment.py를 로드하고 나머지 의존성은 현재 checkout을 사용한다. 구 운영 바이너리 E2E가 아니다.

공식 소스에서 확인한 범위:

- [Caddy v2.11.7 HTTP app stop](https://github.com/caddyserver/caddy/blob/v2.11.7/modules/caddyhttp/app.go): reload는 startedShutdown을 기다리고 exiting일 때만 finishedShutdown/pending shutdown을 동기 대기한다. 시작 ack와 완료 ack는 다르다.
- [reverseproxy.go](https://github.com/caddyserver/caddy/blob/v2.11.7/modules/caddyhttp/reverseproxy/reverseproxy.go): prepareRequest의 선택적 body buffering은 reverseProxy의 countRequest 증가보다 앞선다.
- [metrics.go](https://github.com/caddyserver/caddy/blob/v2.11.7/modules/caddyhttp/metrics.go): handler metrics가 upstream counter보다 넓어도, 이번 reload 후 구 handler gauge가 조회 결과에서 사라졌다. 빈 결과는 완료 ack가 아니다.

## 최소 안전 교정

1. 이전 generation identity를 먼저 확인한다. 확인 전 실패에는 proxy를 쓰지 않는다.
2. **확인된 remote + executor_protocol=2 API**는 TTS 원문/작업을 durable `QUEUE_OWNER`에 적재하므로 legacy TTS drain을 생략한다. 기존 executor queue 수명주기 계약을 재사용한다.
3. local/unknown이면 ingress close 뒤 전송 완료 ack를 반드시 기다린다. `True` 이외, timeout, 예외, 취소는 activation을 허용하지 않는다. stock Caddy adapter는 지원하는 완료 경계가 없어 즉시 `StageError(tts-drain)`를 낸다.
4. close를 시도한 뒤 실패하면 이전 route를 복원한다. 부분 close 실패도 복원하며, 복원 실패 로그와 원래 오류를 보존한다.
5. 테스트 FakeSlots의 `True`는 **시뮬레이션 입력**으로 명시했다. 이 값을 실제 Caddy 검증 증거로 사용하지 않는다.

## 검증

Python: `../back/.venv/bin/python`, PYTHONPATH `src unit_tests tests`, `--noconftest -o addopts=''`. 실제 PostgreSQL fixture를 생략한 DB-free 검사다.

- 신규 전송 검사의 첫 실행: **7 failed / 0.73s**, bool ack 미검사·wait 미호출·stock adapter 부재 재현. 최초 시험의 timeout case는 DB mock이 빠졌으므로 mock 보완 후 이 red 결과를 정본으로 삼는다.
- remote 분기: **1 failed, 1 passed / 0.24s** 후 교정. identity mismatch 복원 부작용: **1 failed, 1 passed / 0.24s** 후 교정.
- timeout·취소·명시 거절·관측 순서·remote/identity, 기존 partial close/running drain 포함: **46 passed / 4.88s**.
- 대표 10파일 검사 첫 실행: **110 passed, 1 failed / 8.25s**. 실패는 기존 migration SQL 검사에서 부모 두 행 중 어느 쪽을 INSERT하는지 고정한 assertion이었다. 제품 마이그레이션 변경이 아니다.
- `PYTHONHASHSEED=0,1,2,3`로 해당 기존 검사만 재현: 0/2 pass, 1/3 fail. 두 INSERT/UPDATE 순서가 같은 부모 집합을 만든다. SQL version DML을 메모리 SQLite에 적용하고 결과 집합을 검사하도록 바꾼 뒤 네 seed 모두 pass. **PostgreSQL schema/data rollback의 대체 검증이 아니다.**
- 최종 대표 10파일: **111 passed / 8.18s**, 원문 [fnd1-transport-regression.txt](fnd1-transport-regression.txt). 이전 110/1을 숨기거나 전체 gate 통과로 확대하지 않는다.
- 별도 proxy config 표본: **1 passed, 8 deselected / 0.21s**. 변경 Python 8파일 Ruff 및 `git diff --check` pass.

실제 Caddy 재현 명령:

```sh
PYTHONPATH=src ../back/.venv/bin/python review/platform-v2/20261025/fnd1_pre_counter_probe.py --baseline
PYTHONPATH=src ../back/.venv/bin/python review/platform-v2/20261025/fnd1_pre_counter_probe.py
```

자체 생성한 Caddy child·소켓·임시 디렉터리만 종료/정리했다. 운영 포트는 명시 거부한다. 환경에서 이 경로가 실행됐다는 관측이며 다른 엔진 또는 호스트의 포괄 권한 판정이 아니다.

## 남은 일

- FND1 자동 전환 **미해결**. native Caddy 프로세스 종료는 완료 ack를 제공하는 경로이지만, 이를 쓰려면 첫 bootstrap의 유휴 예외·SSE/Run 보존·접속 가용성·외부 controller 소유권을 함께 검증해야 한다. 이 문서만으로 운영 proxy stop/restart를 허용하지 않는다. 새 lease/fence/MSA/번들 플랫폼은 추가하지 않는다.
- 실제 DB 왕복/기존 DB 9건·library 통합·전체 gate·독립 residual 검증·P0/P1/P2/P4/P6 운영 실증은 남는다. initdb shmget 권한거부는 재시도하지 않았다.
- MCP 최신 배포 목록의 운영은 여전히 d56455c8 succeeded다. 이번 후보는 운영 배포가 아니다. PMS 9개 항목 중 8개는 done이 아니며 전체 목표를 완료로 판정할 수 없다.
