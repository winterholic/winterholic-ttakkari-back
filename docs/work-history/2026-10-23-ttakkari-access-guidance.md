# .ttakkari 보호 영역 재탐색 방지

사용자 요청: .ttakkari 접근 거부 뒤 토큰을 낭비하는 반복 탐색을 줄이고, 기존 권한 처리의 부족 여부를 확인해 하네스를 개선한다.

## 확인 범위

- 기준: origin/main `11f5da273f2ccf23160dfd457c40f9229575397f`, 별도 worktree `~/development/ttakkari-access-guidance-20261023`.
- `security/seatbelt.py`는 .ttakkari 기본 거부, 현재 Run outbox·업로드·인박스 및 엔진 런타임의 제한된 예외를 정의한다. `security/guard.py`도 보호 영역과 Run 쓰기 경로를 구분한다. 권한 폴더 등록은 별도 OS 샌드박스의 허용을 보증하지 않는다.
- MCP `ops_list_deploys`에서 마지막 성공 예약은 `ada16c70-16dd-4a01-b0bb-3aec74661f0c` / `ba359c88`. 현재 Run에 주입된 브라우저 사전검사 경로도 해당 release다. 해당 SHA의 `SYSTEM_NOTE`를 git show로 확인했고, 보호 영역 지도와 작업별 대체 조회 경로가 없었다.
- `read_session`의 최근 보고에서 다른 Run outbox 원문과 실제 하네스 설정 접근 불가 사례를 확인했다. 이는 보고 이력이며 원래 실패 명령/출력은 이 도구에 없어 개별 실패 원인 확정 근거로 쓰지 않았다.
- Codex 명령 환경에서 `curl -fsS http://127.0.0.1:18987/api/version`은 HTTP 401. 버전 엔드포인트 인증을 우회하지 않았고, 현재 활성 revision 직접 조회는 미검증이다.

## 변경

- `runs/instructions.access_note`: 설정에서 경로 메타데이터만 가져와 현재 Run outbox, 전송 경로, 보호 영역, 로그/배포/다른 작업 조회 도구를 짧게 안내한다. 다른 Run 경로·MCP 메타데이터가 파일 접근 허가가 아니라는 점과 재개 시 outbox 변경을 명시한다.
- `_system_note_parts`의 첫 레이어에 매 Run 주입. 하네스 조회 본문도 같은 함수 사용. 접수는 기존대로 이 공통 프롬프트를 받지 않는다.
- Qwen은 MCP 없는 짧은 계약을 유지하면서 보호 영역 탐색·거부 후 재시도 방지 문장만 추가한다.
- 경로 도구·셸·MCP 가드의 보호 영역 거부 메시지에 대체 도구와 해당 검증만 미실행으로 보고하라는 안내 추가.
- 권한·샌드박스 예외는 확대하지 않았다. guard.py의 기존 Ruff SIM102/103 두 건은 의미를 보존해 정리했다.

## 실행 검증

- 변경 전 새 단위 검사 4개 실패(안내 함수 부재, 기존 거부 메시지에 대체 경로 없음). Qwen 검사도 추가 후 실패를 확인했다.
- `TTAKKARI_NO_ENV_FILE=1 PYTHONPATH=src <기존 back/.venv/bin/python> -m pytest unit_tests/test_ttakkari_access_guidance.py unit_tests/test_artifact_delivery_guidance.py unit_tests/test_qwen_local_contract.py -q` → **10 passed**.
- `TTAKKARI_NO_ENV_FILE=1 PYTHONPATH=src <기존 back/.venv/bin/python> -m pytest tests/test_guard.py tests/test_seatbelt.py tests/test_harness_view_api.py -q --timeout=120` → **168 passed, 1 skipped, 2 xfailed**. 격리 임시 DB 사용. 보호 경로 차단 유지, 현재 outbox 허용, 실제 주입과 조회 레이어 일치 확인.
- 변경 Python 7개 Ruff → **All checks passed!**, `git diff --check` 통과.
- 운영 배포 및 실제 모델이 거부 후 재시도를 줄이는지에 대한 행동 측정은 이 검사로 입증하지 않았다. 프롬프트는 행동 안내이며 OS 정책을 강제 변경하지 않는다.

검증 후 main push와 generation 배포 예약을 수행한다. 예약 접수와 배포 성공을 구분한다.

## Push 및 배포 전 추가 확인

- `f85481f` main push: 변경 Python 7개 Ruff, 계약 **84 passed**, 변경 테스트 **25 passed**. 전체 suite 통과를 뜻하지 않는다.
- 첫 push 시 Codex 명령 샌드박스의 uv sync에서 기본 캐시 `~/.cache/uv/sdists-v9/.git` 접근이 `Operation not permitted`로 거부됐다. 같은 경로를 재시도하지 않고 `UV_CACHE_DIR=/private/tmp/ttakkari-harness-uv-cache`를 명시해 허용된 임시 경로로 바꿔 통과했다. 훅·샌드박스를 끄지 않았다.
- 최근 두 실패 배포의 기존 ADR 문구 검사를 별도로 실행해 동일 실패를 확인했다. ADR의 `2026-10-23 사용자 지시`를 기존 테스트가 요구하는 `2026-10-23 사용자의 명시 지시`로만 맞췄다. 기능·품질 gate 미통과 서술은 보존했다. 별도 커밋으로 배포 차단 원인을 제거한다.
