# Muse 사용량 미관측과 잘못된 잔여량 판정 수정 — 2026-10-18

## 현재 판정

따까리의 수집·배정 결함을 수정했다. **구독 사용량이 항상 조회되도록 복구한 것은 아니다.** 수정 후보의 실제 native `/usage`도 `Currently unavailable`을 반환했다. 공급자가 수치를 주지 않는 상황에서 0%/100%를 만들어 내지 않는다. 운영 적용은 아래 커밋의 배포 예약 후 별도 확인 대상이다.

## 재현과 근거 범위

- 조사 시작 시 운영 `GET http://127.0.0.1:8787/api/version`: `f90796c1e020f3449c5570a206e0040434e6a949`, PID 57177. proxy가 가리키는 runtime은 `127.0.0.1:8792`, release current 파일도 같은 SHA였다. 개발 체크아웃과 배포 아티팩트를 구분했다.
- `account_usage`에서 활성 Muse 계정은 마지막 관측 01:36 KST, 5시간 reset 01:43 KST, `/usage ... unavailable` 오류였다. 01:50 조회는 유효한 주간 0%만으로 `quota_known=true`, `remaining_to_limit_percent=100`을 반환했다. 15분을 넘자 stale로 null이 됐다. 실패·만료 상태에서도 약 15분 동안 확정 여유로 취급될 수 있는 결함이다.
- 운영 DB는 `BEGIN READ ONLY; SELECT id,name,engine,config_dir,enabled FROM engine_accounts WHERE engine='muse'; COMMIT;`만 실행했다. 활성 계정은 `390567eb-a61a-4d71-9c65-1e00093e759a`, 비활성 default 계정과 분리했다. 인증 파일·키체인·브라우저 쿠키는 직접 읽거나 복사하지 않았다.
- 중첩 Codex Run에서 공식 CLI 1.4.10-R6427.1, 활성 계정의 원래 config, 별도 허용 data/cache를 사용했다. 운영 원래 data/cache에 대한 과거 sandbox 거절을 같은 조건으로 재시도하지 않았다. 이 진단은 운영 backend subprocess의 증거가 아니다.
- SDK `usage/read`의 host-local 마지막 관측 계약은 설치 공식 schema/msp/msp.d.ts의 UsageReadResult/SubscriptionUsage에서 확인했다. native helper가 실제 `/usage`만 최대 3회 실행하도록 했고, 후보에서 18초를 기다려도 native 카드 2개가 모두 `Currently unavailable`이었다. 모델 호출은 0회, 자식 회수 true. 증거: outbox `183aa345-f32c-402d-9428-56fcb620761e/muse-native-usage-check.json`.
- 공식 공개 stable manifest는 `https://api.meta.ai/muse-code/channels/muse-stable`에서 같은 설치 버전을 반환했다. CLI 업데이트로 해결됐다는 주장은 하지 않는다.

## 원인과 수정

1. native helper가 새로고침 도중 보이는 첫 `unavailable` 카드에서 종료했다. 제한 시간 안에서 실제 갱신을 기다리며, 마지막 unavailable 상태는 그대로 남긴다. 반복 카드가 터미널에 함께 있으면 마지막 Subscription 카드만 파싱한다. 이전 카드의 오류나 수치가 새 카드와 섞이지 않는다.
2. Muse의 두 창 중 하나가 reset됐거나 새 조회가 실패했을 때 남은 한 창으로 측정 여유량을 계산했다. Muse의 확정 여유 판정에는 성공한 관측과 유효한 5시간·주간 창을 모두 요구한다. 내부 라우팅과 account_usage의 잔여량·최소 잔여량에 같은 판정을 적용한다. 과거에 관측된 소진은 reset 전까지 계속 차단한다.
3. 기존 관측 시각·수치·플랜 보존, 오류 표시, 실패 backoff, 실행 스트림의 usage/changed 수집을 유지한다. 조회를 위한 모델 호출, 종량제 API, 브라우저 인증 추출은 추가하지 않았다.

## 외부 근거와 남은 제약

CodexBar의 원 재현 보고와 유지관리자 확인은 5시간 창이 유휴 상태일 때 Meta key 응답에서 `subs_usage`가 생략되고 CLI 자체도 unavailable이 되는 경우를 설명한다. 이번 계정의 직접 관측은 **native unavailable**까지이며, 공급자의 실제 응답 body를 확인한 것은 아니므로 동일한 공급자 원인이라는 판단은 추론이다.

- https://github.com/steipete/CodexBar/issues/4002
- https://github.com/getpaseo/paseo/issues/6161

CodexBar는 별도 명시 선택한 웹 팀의 quota를 브라우저 세션으로 읽는 경로를 제공한다. 이는 CLI 계정과 동일하다고 자동 확정할 수 없으며 이번 변경에 넣지 않았다. 이 Mac에는 해당 앱/CLI도 없었다. 운영에서 새 수치 조회 성공·배포 후 실제 수집은 아직 미검증이다.

## 검증

TDD에서 새 카드/비동기 갱신 테스트 **2 failed**, 오류·reset 후 여유 판정 **2 failed, 1 passed**를 먼저 관찰했다. 수정 후 회귀:

```sh
TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_muse_usage_20261018_183a PYTHONPATH=src ../back/.venv/bin/pytest -q --tb=short --timeout=120 tests/test_muse_api.py tests/test_muse_auto_routing.py tests/test_usage_collector.py tests/test_muse_usage_pump.py tests/test_worker_routing.py unit_tests/test_muse_tui_usage.py unit_tests/test_muse_usage.py unit_tests/test_muse_usage_capacity.py
```

**122 passed in 25.50s**, 별도 테스트 DB 사용. provider 실제 호출은 위 native 진단과 구분한다. fake PTY 검사도 single-thread helper subprocess 안에서 실행한다.

변경 파일 ruff check 통과, git diff --check exit 0, Python compile 통과, Python pitfall scanner 0 findings. mypy는 venv에 없어 미실행. 기존 파일들은 변경 전에도 ruff format 기준과 달랐으므로 전체 재포맷하지 않았다. 새 unit test 파일은 포맷했다.
