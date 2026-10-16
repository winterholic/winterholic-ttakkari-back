# 2026-10-16 Antigravity MCP 설정 수명 수정

사용자는 모델 우선 배정 변경과 연결 결함을 모두 반영하고, 무중단 재배포 뒤 운영 안정성까지 확인하도록 지시했다. 모델 정책/Luna 통합 기준은 c80b500602ff7b7a49fa7a8d10d7987f495c9154다. 별도 checkout `back-agy-lifecycle`에서 기존 작업을 보존해 후속 수정했다.

`RunManager._pump`의 Antigravity `init` 처리에서 MCP 설정을 삭제하던 코드를 제거했다. CLI는 init 이후에도 설정을 읽어 stdio MCP를 연결하므로 실제 로딩보다 삭제가 빨랐다. 기존 세션 UUID별 임시 프로젝트, 0700 디렉터리, 0600 설정 파일, 제한된 세션 토큰, 종료 시 삭제를 유지한다. 별도 서비스·인증 복사·권한 완화는 추가하지 않았다.

실제 검사 중 명령 sandbox가 `os.killpg(proc.pid, SIGTERM)`을 거부하는 경우 종료 정리도 건너뛰는 결함을 찾았다. 종료 처리의 finally 안에서 설정·로그 정리를 보장하도록 보완했다. 신호 오류는 전파하고 실행은 실패로 닫는다. 파일 정리 성공을 자식 종료 성공으로 해석하지 않는다.

검증 증거는 `/Users/winterholic/.ttakkari/data/outbox/26de315e-963b-47a0-bd01-487a5dc29fd2/`에 있다.

- `lifecycle-red.xml`: CLI가 init 이후 0.2초 뒤 설정을 읽는 자식 프로세스 회귀 검사가 기존 코드에서 실패했다.
- `cleanup-red.xml`: 신호 권한 오류를 주입하면 임시 설정이 남는 회귀 검사가 기존 코드에서 실패했다.
- `lifecycle-green.xml`: 수정 뒤 adapter/Run 검사 18개 통과(6.63초). 정상 종료·오류·시간 초과·취소·spawn 오류·종료 권한 오류 및 기존 재개 실패 닫힘/결과 보고를 포함한다.
- `backend-fixture/test_live_backend.py`: 이전 재현기를 현재 outbox로 복사했다. 제품 init 수명 동작을 바꾸는 fixture 대조 패치는 제거했다. 실제 CLI·격리 DB·서명된 fixture 토큰·TCP backend/MCP를 사용한다. 운영 프로세스 검사가 아니다.
- `live-product-fix.xml`, `live-backend-product-fix-results.json`: 4 통과 / 1 실패 / 재개 1 미실행. Flash 단발 응답, Flash·Argon 인증 MCP nonce 회수, 실제 MCP 자식 취소/heartbeat 중단/설정 삭제는 통과했다. Sonnet은 MCP 도구 호출 이벤트 뒤 `os.killpg` PermissionError로 실패했으며, 이는 명령 sandbox에서의 종료 처리 오류다. 이후 오류 경로 cleanup을 보완했고 권한 거부는 동일하게 재시도하지 않았다.
- `live-product-fix-final.xml`, `live-backend-product-fix-final-results.json`: 최종 cleanup 보완본에서 Flash·Argon의 실제 MCP 검사를 다시 실행해 2개 통과(31.29초). 요청 모델과 `agent.session` 보고 모델 ID가 일치하고, fixture DB에만 있는 nonce를 회수했으며 무인증/잘못된 토큰은 401, 종료 뒤 설정 삭제까지 확인했다.
- `full-regression.xml`, `full-regression.log`: 최종 제품 코드의 전체 검사 933 통과, 기존 xfail 2개, subtest 6개 통과(173.57초). Ruff 및 diff check 통과. 분리 checkout에서만 검사 중 기존 venv를 임시 연결하고 검사 종료 후 제거했다. 첫 `uv sync`는 명령 sandbox의 기본 uv cache 쓰기 제한으로 시작하지 못했다.
- `production-public-observation.json`: 실제 stable proxy의 health/version 12쌍 모두 HTTP 200/ok, 관측 SHA c80b500, 최대 응답 시간 11.7ms. 이는 모델 정책 버전의 약 22초 공개 경로 관측이며 MCP 수명 수정본의 운영 실행이나 장시간 안정성 증거가 아니다. 모델 정책 배포 d808d627-3c9e-4d45-a8f7-3bc55687c139는 추가 조회에서 draining으로 전환돼 신규 요청 반영을 확인했다.
- 외부 fixture를 처음 실행할 때 프로젝트 pytest 설정을 지정하지 않아 async 테스트 5개가 실행되지 않았다. `-c pyproject.toml`로 바로잡은 위 실행과 구분한다.
- 현재 명령 sandbox에서는 프로세스 전수 조회의 sysctl도 EPERM이다. 해당 Sonnet CLI의 현재 생존 여부는 관측 불가이며 다른 경로로 재시도하지 않았다. fixture 종료 시 설정 cleanup을 호출했지만, 해당 실패 Run의 잔여 파일 상태를 따로 관측한 증거는 없다.

운영 확인은 실제 `/api/version`의 SHA와 배포 상태를 기준으로 한다. 모델 정책 배포 d808d627-3c9e-4d45-a8f7-3bc55687c139는 이 작업 시작 시 testing이었다. 실제 운영 응답·인증 MCP·독립 프로세스 대화 재개·중단 및 장시간 안정성은 아직 미검증이며, 격리 검사를 운영 성공으로 대체하지 않는다. 검증된 clean SHA를 generations controller로 배포하며, 배포 대기만 하려고 Run을 유지하지 않는다.
