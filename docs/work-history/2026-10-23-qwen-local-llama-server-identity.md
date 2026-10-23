# 2026-10-23 qwen-local "unidentified llama-server" 근본 원인과 수정

'로컬 LLM 작업자 리팩토링' 2단계. 이 저장소의 코드는 계약 테스트만 바뀌고, 수정 본체는 별도 저장소 `~/development/projects/qwen-local-harness`(로컬 전용, 원격 없음, master)에 있다.

## 증상
10-21 23:12~23:19 KST, `engine.fallback`(reason `qwen_failed`) 5건 전부 `to_engine=codex`로 처리됐다.
| Run | 사유 |
|---|---|
| c4911f14, d57e95b2, c89f524c, 3dc828d9 | `qwen-local error [guard]: unidentified llama-server process(es) are running: [88340]` |
| ec0fe8a5 | `qwen-local usage_error [usage]: task exceeds 16384 UTF-8 bytes` (이 수정과 무관한 별개 사유, 미수정) |

기획 세션이 전한 21:34~22:39 KST와 시각이 다르다. DB 조회는 `Asia/Seoul`로 변환했다. 확인 필요(기획 쪽 시각 기준).

## 오류를 내는 곳
`qwen-local-harness/src/qwen_local/runner.py` `preflight()`. 실행 중 llama-server 중 (a) `verified_intake_servers()`가 보존한 pid도 아니고 (b) `is_harness_lineage()`(하네스 자신의 Flash-Next: 모델 경로, `--alias`, 더미 토큰)도 아닌 것을 `unknown`으로 보고 거부한다. back 쪽 `agents/qwen_local.py`의 readiness는 `qwen-worker doctor`를 쓰며 이 판정을 호출하지 않는다. 그래서 readiness는 통과하고 Run 시작 시점 preflight에서 막힌다(`bin/qwen-worker doctor`는 수정 전에도 ok=true).

## 근본 원인 (명령과 출력)
`verified_intake_servers()`는 접수용 Qwen3.5-4B를 `/Users/winterholic/.ttakkari/qwen/*.json` pid 기록으로만 소유 증명했다. 백엔드는 기록을 `TTAKKARI_HOME/qwen`에 쓴다(`desk_qwen_service._state_dir`). `TTAKKARI_HOME`이 다른 백엔드의 자식은 기록이 다른 디렉터리에 있어 항상 '미식별'이다.

수정 전 재현(읽기 전용, 모델 미적재):
```
$ bin/qwen-local doctor
Inference preflight: BLOCKED: unidentified llama-server process(es) are running: [88340]
```
프로세스 대조(10-23 10:34 KST):
| PID | 포트 | 부모 | 기록 위치 | 판정(수정 전) |
|---|---|---|---|---|
| 28857 | 18987 | 1 | 없음. Flash-Next, `--alias`/`--api-key` | 하네스 계통(foreign_harness_servers). 건드리지 않음 |
| 79216 | 60352 | 79200 (세대 7626a91, 10-23 01:31) | 공용 `~/.ttakkari/qwen` | 식별됨 |
| 88315 | 58215 | 88287 (활성 세대 710f184, `/api/version`의 pid) | 공용 | 식별됨 |
| 88340 | 51011 | 88279 | `/tmp/ttk-gsp3/home/qwen/88279-3784c4da.json` | **미식별** |

- 88279는 `back-games-p3` 체크아웃에서 10-19 23:51에 띄운 `ttakkari serve --port 18890`(부모 88273 `uv run`)이고 `ps eww`로 본 `TTAKKARI_HOME=/tmp/ttk-gsp3/home`다.
- `/tmp/ttk-gsp3/home/qwen/88279-3784c4da.json`의 `owner_pid`=88279, `child_pid`=88340, `port`=51011, `server`와 `model`이 정상 값이고 `owner_start`가 실제 시작 시각과 일치한다. 즉 소유권은 기록상 입증되는 정당한 접수 서버인데 하네스가 그 디렉터리를 보지 못했다.
- 88340은 고아가 아니다(소유자 88279 생존). 기획 세션이 본 PID 29917(52345)은 이후 세대 교체로 사라졌다. 같은 모델이 둘 이상 보이는 것은 세대(+개발 인스턴스)마다 자기 자식을 갖는 설계 때문이다.
- ~/.ttakkari/qwen 안의 기록은 보호 영역이라 직접 읽지 못했다. 79216, 88315가 식별되고 88340만 걸린다는 doctor 출력과 코드 경로로 추론했다(직접 확인 아님).

## 수정 (하네스 `b278b96`)
소유 증명 경로를 하나 추가했다. 기존 기록 경로, 활성 세대 게이트, 미식별 거부는 그대로다.
- `managed_intake_signature`: argv가 백엔드의 접수 자식(`desk_qwen_service._server_args`)과 정확히 같을 때만(서버 경로, `--device none`, `--gpu-layers 0`, `--host 127.0.0.1`, `--no-webui`, Qwen3.5-4B, `--alias`/`--api-key` 없음).
- `live_owner_intake_servers`: 위 서명 + 살아 있는 직계 부모가 `ttakkari serve` 백엔드이고 자식보다 늦게 시작하지 않았으며(`hostmon.process_lineage`, `ps lstart`) + 라이브 `/props`가 같은 모델. 부모가 죽은 고아, 서명 불일치, 조회 실패는 모두 미식별(fail closed).
- 활성 세대 게이트: 기록으로 검증된 활성 세대 서버가 하나도 없으면 이 경로도 쓰지 않는다(조건 완화 아님).
- 서버 하나의 `/props` 실패가 모든 서버의 증명을 날리던 문제를 서버별로 격리했다. 기록 파일 자체가 깨진 경우는 종전대로 fail closed.

수정 후:
```
$ bin/qwen-local doctor
Inference preflight: READY: {... "preserved_intake_server_pids": [79216, 88315, 88340] ...}
```

## 이 저장소의 변경
`tests/test_desk_qwen_service.py`에 계약 테스트 2개. 접수 서버 argv와 pid 기록 키, 소유자=직계 부모가 하네스의 식별 조건과 어긋나지 않게 고정한다. `_server_args`를 바꾸면 하네스의 `managed_intake_signature`도 같이 고쳐야 한다(양쪽 주석에 명시).

## 검증
- 하네스: `PYTHONPATH=src pytest tests/test_intake_lineage.py ...` 신규 21개 포함 관련 67개 통과. 수정 전 소스(HEAD)로 신규 테스트를 돌리면 20개 실패. `tests/test_stage1_verifier.py` 4개는 수정 전 소스에서도 실패하는 기존 문제(미수정). 그 파일 제외 전체 142개 통과.
- back: `tests/test_desk_qwen_service.py` 26개, `test_qwen_local_adapter/run`, `test_host_gpu`, `test_qwen_worker_settings`, `test_qwen_usage` 72개 통과. ruff 통과.

## 하지 않은 것, 남은 것
- 88279, 88340을 포함해 어떤 프로세스도 종료하지 않았다. 88279는 10-19부터 남은 개발용 백엔드(`/tmp` 홈)이므로 소유자가 불필요하면 직접 정리하는 편이 낫다. 정리해도 이 수정은 영향 없다.
- 18987(28857), 57170 서버는 건드리지 않았다. 참고로 `config/harness.conf`의 포트는 18990이고 실행 중 서버는 18987이다(상주 서버 포트와 설정 불일치, 영향 미확인).
- **미검증**: 실제 qwen-local Run이 모델 적재까지 포함해 끝까지 통과하는지. 이번에는 모델 적재가 금지라 `doctor`(preflight, 모델 미적재)까지만 확인했다. 다음 qwen-local 라우팅 Run에서 `engine.fallback`에 `qwen_failed`가 다시 없는지 보면 된다.
- 별개 사유 `task exceeds 16384 UTF-8 bytes`는 이번 범위 밖이다.
- `host_gpu.py`, `qwen_slot.py`에는 이 판정과 같은 식별 로직이 없어 변경하지 않았다.
