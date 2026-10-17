# Muse auto 라우팅 편입 (게이트 방식) — 2026-10-17

요청: "뮤즈가 사용량이 넉넉한 플랜이니까 좀 더 적극적으로 쓸 수 있게 오케스트레이션 구성". 운영 리비전 `0609648`(`/api/version`, 2026-10-17 22:00 KST 관측)에서 시작했다.

## 결론

Muse를 `general`·`routine` 첫 단계에 후보로 넣는 코드와 테스트를 만들었다. **현재 auto 편입은 보류**다. 세 실동작 검증 전까지 닫힌 코드 수락 래치가 있어 `TTAKKARI_MUSE_ENABLED`·`TTAKKARI_MUSE_AUTO_ROUTING` 환경변수만으로 단계표에 나타나지 않는다. 요청 완료 기준의 코딩·MCP·재개 Run을 운영 런타임에서 실행하지 못했기 때문이다.

## 실동작 검증 결과 (각각 별개 체크)

| 항목 | 결과 | 근거와 범위 |
|---|---|---|
| 간단한 코딩 작업 1건 | 미검증 | 이 Run은 중첩 worker(Antigravity 샌드박스, 네트워크 없음)이고 오케스트레이터 토큰이 없다. 운영 `POST /api/orchestrator/workers`를 호출할 수 없다. 이전 기록(`muse-code-investigation.md`)에서 중첩 환경의 Muse `sandboxAvailability`는 unavailable이었고 bridge는 sandbox 미관측을 거절한다. 같은 거절을 같은 환경에서 다시 시도하지 않았고 우회하지 않았다. |
| MCP 툴 호출 1건 | 미검증 | 위와 같은 이유. 단위·계약 테스트의 MCP 설정 검사는 있으나 실제 도구 호출 증거가 아니다. |
| 세션 재개 1건 | 미검증 | 위와 같은 이유. resume identity 검사는 단위 테스트뿐이다. |

확인한 것 (코딩·MCP·재개 검증이 아님):

- 운영 DB `engine_accounts`의 Muse 행 2개. `muse 1 (iuholic83@gmail.com)`은 enabled, 독립 프로필 디렉터리 `390567eb-…`, 생성 2026-10-17 21:56. `내 Muse`는 disabled, `config_dir=null`이라 `accounts/muse-default`를 쓰고 생성은 15:59다. 프로필 디렉터리가 달라 같은 행이 아니다. 같은 Meta 계정을 재인증했는지는 인증 파일을 읽지 않아 모른다. 켜는 여부는 바꾸지 않았다.
- 운영 프로세스의 `TTAKKARI_MUSE_ENABLED` 값은 확인하지 못했다(프로세스 환경에 명시 값 없음, `.env`는 읽지 않음). 기본값 false이고 이전 기록도 변경하지 않았다고 한다. 확인 필요.
- Muse 사용량 키는 계정 단위 `five_hour`/`seven_day`라 기존 `_usage_headroom`과 `usage_limit_until`이 그대로 적용된다.

## 변경

- [`worker_routing.py`](../../src/ttakkari/runs/worker_routing.py): `MUSE_FULL`(`muse-wonder-full-1.0`), `MUSE_SPARK`(`muse-spark-1.3`). 게이트 함수 `muse_auto_enabled()`와 `_routable()`. `availability`, `pinned_stages`, `policy`, `accepts_model`이 게이트를 따른다.
- `needs_independent_verification`에 Muse 추가. `verifier_recommendation(model)`은 Muse 결과에 `excluded_family="muse"`.
- [`config.py`](../../src/ttakkari/config.py): `muse_auto_routing: bool = False`.
- [`runtime.py`](../../src/ttakkari/orchestrator/runtime.py): 기획 세션 지침을 `model_routing_note()`로 호출 시점에 생성(기존 `MODEL_ROUTING_NOTE`는 호환용으로 유지). Muse 규칙 항목 추가. `manager.py`는 함수를 호출한다.
- [`account_status.py`](../../src/ttakkari/runs/account_status.py): `muse_auto_routing` 필드와 note 문장. `mcp_server.py` `spawn_worker` 설명 갱신.
- `docs/api-contract.md`에 편입 규칙 기록.

## 새 단계표 (게이트가 켜졌을 때)

| task_type | 단계 |
|---|---|
| general | [Antigravity Sonnet, **Muse Wonder Full**] → Flash → [native Sonnet, Luna] → Qwen local |
| routine | [Flash, **Muse Spark 1.3**] → Luna → Antigravity Sonnet → native Sonnet → Qwen local |
| design / verification / reasoning | 변경 없음 (Muse 없음) |

설계 근거:

- 같은 단계에 두면 기존 규칙(측정된 후보, Run당 상한 여유, 초기화 시각)이 그대로 작동한다. 사용량 0%인 Muse는 여유가 큰 쪽이라 자연히 쏠리고, 새 우선순위 규칙을 만들지 않는다.
- 미관측·stale Muse는 측정된 대안보다 앞서지 않는다. Muse 한도 도달은 그 계정만 막고 다른 풀은 영향이 없다.
- auto 세션의 기본 모델은 첫 단계 첫 항목(Sonnet)을 유지하도록 Muse를 뒤에 뒀다.
- Wonder Full 권한은 미검증이므로 routine에는 공급사가 코딩용으로 게시한 더 작은 Spark 1.3을 썼다. 이 계정에서 응답이 관측된 모델은 Spark 1.3(중첩 Run 최소 응답)뿐이다. 모델 접근 거부는 사용량 한도가 아니라서 자동 폴백이 되지 않는다. 그래서 검증 전에는 게이트를 켜지 않는다.
- verification은 Muse를 넣지 않는다. Muse 구현은 GPT·Claude가 교차 검증한다. 품질 기준선이 없어 Muse의 general/routine 결과는 Flash처럼 `verification_required=true`로 두었다. 벤치마크가 쌓이면 완화를 검토한다.
- 회사 범위: Muse 계정은 개인 풀이라 `work` 후보가 늘지 않는다(테스트 포함).

## 게이트를 켜기 전 필수 절차 (운영 런타임)

1. 운영에서 `TTAKKARI_MUSE_ENABLED=true`로 켜고 기획 세션에서 `spawn_worker(engine="muse", model="muse-wonder-full-1.0")`로 다음을 각각 별개 Run으로 확인한다: ① 작은 코드 수정과 테스트 실행 ② `ttakkari` MCP 도구 1회 호출(예: `recall`) ③ `message_worker`로 같은 세션 재개 후 표식 회수.
2. Wonder Full이 거부되면 `muse-spark-1.3`으로 같은 확인을 하고, `MUSE_FULL` 모델을 조정한다.
3. 세 항목을 모두 통과한 뒤 별도 코드 변경으로 `MUSE_AUTO_ROUTING_ACCEPTED = True`를 열고 두 설정을 켜서 배포 예약한다(Run 안에서 launchctl 금지).
4. 켠 뒤 `account_usage.muse_auto_routing=true`와 영수증의 `selected.engine`을 확인한다.

## 후속 확인 (2026-10-17 22:18 KST)

- `GET http://127.0.0.1:8787/api/version` 응답은 `revision=06096488e032274253732bf7e3e130850f6f17c5`, `pid=71193`이었다. 확인 시점의 배포 런타임은 Muse 라우팅 후보 커밋 전이었다.
- 이번 Run의 명령 셸에서 `command -v muse`는 경로를 출력하지 않았고 `muse --version`은 `zsh: command not found: muse`로 끝났다. 이는 이 셸의 PATH 관측이다. 코드의 배포 실행은 `TTAKKARI_MUSE_BIN` 기본 절대경로와 `.muse-runtime` Node SDK bridge를 사용하므로 이 결과로 백엔드의 CLI/SDK 부재를 결론내리지 않는다. 배포 백엔드의 실제 설정 경로·패키지 가용성은 미확인이다.
- `tests/test_muse_auto_routing.py`는 19 passed. 이는 격리된 pytest/가짜 계정 사용량의 라우팅 계약이며 실제 Muse coding·MCP·resume 수행 증거가 아니다.
- 운영 `/api/orchestrator/workers`를 호출할 실행 도구와 유효한 orchestrator session/MCP 컨텍스트가 이 Run에 노출되지 않았다. 세 가지 실동작 Run은 시작하지 않았다. sandbox 가용성 거부를 같은 환경에서 재시도하거나 우회하지 않았고, 인증 파일·키체인·`.env`도 읽지 않았다.
- `muse_enabled`와 `muse_auto_routing` 코드 기본값은 둘 다 false다. 배포 환경의 override 상태는 확인되지 않아 account_usage의 유효 `muse_auto_routing` 값도 미확인이다. 검증 완료 전에는 두 값을 변경하거나 활성화하지 않는다.
- `worker_routing.MUSE_AUTO_ROUTING_ACCEPTED`는 false로 닫혀 있다. 후보 경로 테스트에서만 true로 열어 quota/headroom/verification/fallback 계약을 검사한다. 실제 세 검증 완료 후 별도 코드 변경으로만 열 수 있다.
- 환경 override가 미확인인 점을 보완하기 위해 두 번째 후속 수정에서 코드 수락 래치를 추가했다. 앞선 후보 구현의 두 환경 설정만으로 auto Muse가 켜질 수 있던 상태를 닫아, 최종 예약 SHA에서는 승인 코드 변경 전까지 auto 단계표에 Muse가 노출되지 않는다.
- 계정 행에 대한 최신 근거는 이 문서 앞부분에 기록한 21:56 KST 조회다. 이번 후속 확인에서 DB/계정 API를 다시 조회하지 않았다. 그러므로 `muse 1` enabled, `내 Muse` disabled라는 값은 이전 관측으로만 보고하고 현재 상태로 단정하지 않는다.
