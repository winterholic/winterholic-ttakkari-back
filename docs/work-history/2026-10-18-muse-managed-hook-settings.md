# Muse managed PreToolUse 설치 경로 수정

운영 worker `47079c71-7215-42d8-9b23-8935724aa5cd`, Run `c434edb5-4657-48cf-84a4-43fe282eaf8d`가 `Muse managed PreToolUse guard was not installed`로 종료됐다. 현재 `/api/version`은 `63e257fd8b26ec09e39c93cd2c373e81dfa957a9`; ops 이슈는 LOG-70이다. 모델 턴 시작 전 bridge의 `hook/list` 검사에서 거절한 것이며, 모델 응답·코딩·MCP·재개 성공을 의미하지 않는다.

설치 CLI 1.4.10과 pinned SDK로 격리 echo host를 실행해 재현했다. `-c managed_hooks_path=...`는 에러 없이 시작하지만 `hook/list`는 빈 배열을 반환했다. 동일 hooks 파일을 공식 settings.json의 managed_hooks_path로 지정하면 enabled=true, handlerKind=managed, event=preToolUse가 관측됐다. echo 재현은 모델과 도구를 호출하지 않는다. 공식 규격: https://meta-models.github.io/muse-code-sdk/next/guides/extend/hooks/#managed-file

bridge가 private launch 디렉터리 아래에 0700 config overlay와 0600 settings.json을 생성하도록 변경했다. 기존 settings 내용은 유지하고 managed_hooks_path만 주입한다. 다른 config 항목은 원본으로 symlink해 상대 참조를 유지한다. 인증 파일은 읽거나 복사하지 않으며 XDG_DATA_HOME·XDG_CACHE_HOME은 변경하지 않는다. 원본 settings.json은 수정하지 않는다. 기존 managed_hooks_path가 있으면 교체를 거절한다. overlay는 기존 adapter cleanup과 함께 제거된다. 필수 훅 inventory 검사와 native sandbox 검사는 유지했다.

검증:
- Node 계약 검사: `node --test .muse-runtime/*.test.mjs` → 8 passed. 원본 불변·상대 경로·파일 권한·기존 관리 정책 거절·설정 부재를 포함한다.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s unit_tests -p 'test_muse*.py'` → 40 tests / OK.
- 격리 DB의 `pytest -q --tb=short --timeout=120 tests/test_muse_api.py tests/test_muse_auto_routing.py` → 28 passed in 5.96s.
- 실제 CLI echo host에서 private overlay로 enabled managed preToolUse 항목을 관측했다.
- 활성 Muse 계정의 data/cache 경로를 유지하고 private overlay로 공식 SDK account/read 조회: accountLogin. 인증 정보의 직접 읽기·복사는 하지 않았다.
- Node 구문 검사, 관련 Python Ruff 검사, git diff --check 통과.

증거: outbox `feb4c9ca-be91-481f-9baa-c9e4699edd93`의 `muse-hook-inventory-evidence.json`, `muse-hook-auth-preservation.json`, Node/Python/API 검사 로그. 실제 모델 도구 호출의 guard 차단과 코딩·MCP·resume는 미검증이다. auto acceptance latch는 열지 않았다. clean 커밋을 push하고 generation 배포를 예약한다. 예약 수락은 운영 반영 완료가 아니다.
