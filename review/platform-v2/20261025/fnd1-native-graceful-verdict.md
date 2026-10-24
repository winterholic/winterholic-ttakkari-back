# FND1 native graceful 종료 경로 판정

2026-10-25 · root Codex 명령 샌드박스 · 제품 부모 `2fde9018fde4ea84af96331433ba789a85875433`

**현재 요구를 만족하는 자동 전환 장벽으로 채택하지 않는다.** 무제한 graceful 종료는 이번에 보류한 HTTP 요청을 기다렸지만 그동안 새 접속이 불가능했고, 유한 grace는 응답이 미완료여도 정상 종료 코드 0을 반환했다. 기존 stock Slots의 명시적 거절을 유지한다. FND1과 전체 V2 목표는 미완료다.

## 실행 범위와 결과

명령:

```sh
PYTHONPATH=src ../back/.venv/bin/python review/platform-v2/20261025/fnd1_native_graceful_probe.py
```

최종 출력: `4 cases passed`. [실험 코드](fnd1_native_graceful_probe.py), [Caddy 로그와 타임라인 원문](fnd1-native-graceful-result.json).

격리 Caddy **2.11.7**, 자체 생성한 임시 upstream/admin/public 포트만 사용했다. 운영 포트·운영 프록시·launchctl·DB·GPU는 사용하지 않았다. 기존 `proxy_config`를 가져와 upstream/포트와 `request_buffers=16`, metrics를 변경했다. 이는 운영 설정의 재현이 아니라 pre-counter 본문 대기를 제어하기 위한 fixture다. upstream과 job은 메모리 모델이다.

각 사례에서 SSE 시작 이벤트를 클라이언트까지 보낸 뒤 보류했다. POST는 헤더와 `Expect: 100-continue`만 보내고 Caddy의 `100 Continue`를 받아 upstream 진입 전 대기임을 확인했다. 종료를 요청한 뒤 실제 새 TCP 연결 실패를 관측했다. 무제한 grace의 0.15초 보류 관측은 해당 시간창의 사실이며 임의 기간의 안전 증명으로 쓰지 않는다. 이후 본문/SSE를 명시적으로 풀고 최종 바이트와 자체 child의 `wait()`를 확인했다.

| 사례 | 관측 |
|---|---|
| 현재 설정 + SIGTERM, 무제한 grace | 새 연결 거절. 지연 POST의 202 수신, SSE end 수신 후 exit 0. 모델 job은 queued로 남음 |
| 접수 차단 `/load` 후 SIGTERM, 무제한 grace | 이전 설정에서 시작한 POST와 SSE도 위 순서로 마침. 새 연결은 거절 |
| 현재 설정 + admin `/stop`, 무제한 grace | 보류 중 `/stop` 응답도 미완료. POST/SSE 완료 및 process wait 후 응답 200 확인. 이 사례에서 200이 먼저 온다는 주장은 하지 않음 |
| 현재 설정 + SIGTERM, app grace=0.2초 | POST upstream 미도달, 클라이언트 202 없음, SSE end 없음, **exit 0**. 원문 로그에 shutdown timeout |

네 사례 모두 검증 종료 시 자체 child가 이미 끝나 있었으며 `cleanup_started.forced_child_kill=false`다. 유한 grace 사례의 `upstream_sse_finished`는 `cleanup_started` 뒤 fixture 해제에서 발생한 이벤트로, 프록시를 통한 SSE 전달 성공이 아니다.

초기 실행은 검사기 문제로 두 번 중단됐다. asyncio transport 생성 중 TCP_NODELAY의 EINVAL, raw socket connect의 ECONNRESET을 관측했다. 서비스 장애/권한 거부가 아니다. 연결 가용성 관측을 raw socket으로 바꾸고 ECONNREFUSED/ECONNRESET만 명시 수용했다. 그 밖의 OSError는 실패를 유지한다. 타임라인의 `seconds`를 관측창 길이로 덮던 필드도 `observation_window_seconds`로 교정하여 최종 네 사례를 다시 측정했다. 이전 출력은 정본으로 사용하지 않는다.

## 채택하지 않는 이유와 bootstrap 경계

1. **전송 종료와 접속 가용성을 동시에 충족하지 못한다.** listener가 종료된 뒤 SSE/지연 요청을 기다리므로 해당 구간에 새 연결이 거절된다. 이번은 짧게 해제한 실험이며 장기 SSE를 실제 장시간 유지한 측정은 아니다. SSE가 끝나지 않는 동안 무제한 대기가 가능하다는 결론은 아래 공식 소스에서의 추론이다.
2. **정상 process exit만으로 무손실을 판정할 수 없다.** 유한 grace의 반례에서 응답 미완료와 exit 0이 동시에 관측됐다. 프로세스 종료가 완료 경계가 되려면 해당 설정의 종료 의미와 timeout/강제 종료 여부까지 보장해야 한다.
3. **HTTP 완료는 accepted job 완료가 아니다.** 세 무제한 사례는 전송을 마쳤어도 모델 job이 queued였다. 기존 API와 DB에서 queued/running을 별도로 확인해야 한다. 실제 consumer 생존은 이번 실험 범위 밖이다.
4. **최초 legacy bootstrap의 유휴 예외를 혼합 세대 전환으로 확대하지 않는다.** `generation_bootstrap.Bootstrap.process`의 독점 admission/holding/유휴 전환은 최초 legacy 서비스 교체 경로다. `GenerationController.drain_previous_tts`는 이미 generation이 존재하는 local→remote 전환에도 호출된다. 단지 첫 TTS 전환이라는 이유로 진행 Run을 멈추거나 proxy를 재시작할 수 없다.
5. **실제 Caddy는 launchd KeepAlive 소유다.** `Slots.ensure_proxy`가 외부 서비스로 만들며, 이 Run은 그 child를 소유하지 않는다. 실험에서 기다린 own-child `wait()`를 운영 PID 종료 제어로 치환하지 않는다. 외부 controller의 restart/recovery와 운영 rollback은 미검증이다.

이 결과는 새 lease/fence/MSA/번들 플랫폼을 추가할 근거가 아니다. 최소 adapter 구현을 붙이면 보장이 생긴다는 가정도 하지 않는다. FND1의 안전 거절을 유지하고 독립 P0/P1 미완료를 진행한다. 자동 전환 해소·운영 배포를 선언하지 않는다.

## 공식 소스와 해석 범위

- [Caddy 2.11.7 HTTP app stop](https://github.com/caddyserver/caddy/blob/v2.11.7/modules/caddyhttp/app.go#L670-L831): 무제한 grace와 shutdown 대기, exiting일 때 이전 설정의 pending shutdown도 대기하는 경로를 확인했다. timeout 오류는 로그에 기록된다.
- [Caddy 2.11.7 admin stop](https://github.com/caddyserver/caddy/blob/v2.11.7/admin.go#L1072-L1082): `/stop`은 exitProcess를 호출한다. 실험의 응답 시점 외 다른 실패 조건에서의 응답/종료 순서는 미검증이다.

실제 장기 Run/HTTP2/HTTP3/WebSocket·운영 인증·외부 tunnel 재접속·프로세스 crash/timeout rollback·DB 보존은 이번 네 사례로 검증하지 않았다. 기존 pre-counter/slow-header/111개 회귀는 제품 실행 코드가 바뀌지 않아 재실행하지 않았다.
