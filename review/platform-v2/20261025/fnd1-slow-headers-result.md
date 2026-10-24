# FND1 격리 Caddy: 미완성 헤더 연결 probe

2026-10-25 root GPT Sol / Codex command sandbox. Caddy 실행 버전 `v2.11.7`. 운영 서비스와 무관한 임시 upstream/public/admin 포트 3개를 사용했고 8787/8791/8792/8793/20191 제외를 assert했다. PostgreSQL·GPU·모델·운영 proxy 파일은 사용하지 않았다.

원본 설계 프로토타입 `e522187`의 실제 `GenerationController.drain_previous_tts()`와 `Slots.proxy_inflight()`를 사용했다. 입구 닫기/복구 요청만 격리 Caddy의 admin으로 보냈고, 구 API와 job/DB는 이벤트 모델로 대체했다. 프로토타입 소스는 수정하지 않았다.

```sh
../back/.venv/bin/python review/platform-v2/20261025/fnd1_slow_headers_probe.py
```

실제 출력(exit 0):

```json
{"caddy": "2.11.7", "prototype": "e522187", "zero_snapshot": {"proxy": 0, "http": 0, "jobs": 0}, "new_post_status": 503, "old_connection_status": "", "old_api_admitted": false, "modeled_job_status": null}
```

## 해석과 한계

HTTP 헤더가 덜 도착한 구 연결을 Caddy reload 뒤까지 붙잡았다. 장벽은 세 값 모두 0으로 반환했고 새 POST는 실제 503을 받았다. 이후 구 연결의 헤더를 완성했지만 그 연결은 EOF로 종료됐고 구 API에 전달되지 않았다. **이 입력에서는 유실이 재현되지 않았다.**

이를 라우팅을 시작한 요청 전체의 안전성 증명으로 쓰지 않는다. 이 probe는 완성된 헤더 뒤의 기존 라우트 처리 goroutine이 업스트림 카운터 전에 지연되는 경우를 제어하지 않는다. 기존 `3643a1f`의 결정적 모델 반례와 같은 실제 Caddy 실행 순서를 확보한 것도 아니다. 프로토타입의 고정 0.5초 스케줄링 가정은 여전히 증명되지 않았다. prototype은 미채택·FND1은 미완료를 유지한다.

초기 probe 명령은 test 전용 PORTS tuple을 요소 대입하려다가 TypeError로 Caddy 기동 전에 중단됐다. tuple 전체를 이 격리 인터프리터에서만 치환하도록 고친 뒤 위 결과를 얻었다. 허가 거부를 우회하거나 운영 주소로 재시도한 것이 아니다. 기동한 Caddy 자식은 소유한 process handle로 terminate·wait 했고 서버·클라이언트 소켓과 임시 디렉터리를 정리했다.

다음 root 작업은 실제 라우트 내부의 집계 전 구간을 포함하는 관측·거절 경계를 찾는 것이다. 고정 sleep이나 같은 0값을 여러 번 관측하는 것으로 이를 대체하지 않는다. 이 환경에서 이번 TCP bind와 자체 Caddy 실행은 가능했지만, 기존 PostgreSQL initdb shmget 권한 거부는 별개의 미검증 경계이며 다시 시도하지 않았다.
