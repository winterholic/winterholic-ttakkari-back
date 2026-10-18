# Muse 사용량 조회 실패 재진단과 표시 개선 — 2026-10-18

## 진단 (06:53 KST 이후 `/usage … unavailable`)

- 수집기 자체는 정상이다. 활성 Muse 계정(390567eb)의 XDG 설정으로 CLI 1.4.10-R6427.1을 수집기와 같은 PTY 경로로 띄우면 composer 확인, `/usage` 3회 전송, 카드 파싱까지 동작하고 CLI 화면이 `Subscription · Muse Code Power Usage` 아래 `Currently unavailable`을 직접 출력했다. 실제 `usage._fetch_muse`도 login=ok, windows=[], error `/usage 구독 사용량 미관측: unavailable`로 같은 결과였다. 모델 호출은 없다.
- 이전 기록(2026-10-17-muse-usage-refresh, 2026-10-18-muse-usage-unavailable)도 공식 터미널의 수동 `/usage`가 같은 문구였음을 남겼다.
- 계정은 10-18 03:46 KST에 429로 소진됐고 응답이 알려 준 초기화는 06:58 KST였다. 마지막 관측 06:53 이후 5시간 창이 초기화됐고 새 관측이 없다. 유휴 창에서 공급자가 값을 생략한다는 외부 보고와 일치하나, 공급자 응답 본문은 확인하지 못했다(추정).
- 수집기 플래그 없는 CLI(기본 preset, MUSE_LOGIN 미설정) 비교 실행은 보호 구역 가드에 막혀 못 했다(미실행).

## 표시 변경

- b427e31 이후 초기화가 지난 5시간 창이 응답에서 사라지고 낡은 주간 값만 남았다.
- Muse 응답은 5시간·주간 창을 항상 내보내고 창마다 `state`를 둔다. `current`(최신 조회 성공), `stale`(마지막 관측값, 현재 잔여량 아님), `unavailable`(자리표시자, 퍼센트 무의미).
- 초기화가 지난 창과 누락된 창은 `unavailable`이며 낡은 퍼센트를 버린다. 잔여량·라우팅 판정(`muse_quota_current`, headroom)은 변경하지 않았다.
- 프론트는 `확인 불가`, `오래된 값 · N시간 전 관측` 문구로 표시한다.
