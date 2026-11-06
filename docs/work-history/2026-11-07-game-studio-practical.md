# 게임 스튜디오 실사용 개선

사용자가 2026-11-06 연구 계획대로 실제 개선 진행을 승인했다. 기존 공유 main 체크아웃은 미통합 변경이 있어 보존하고 origin/main 5f5cbd99 기반의 전용 작업 트리에서 구현했다.

## 구현한 계약

- 사용자 전용 `GET /api/games/discover`: games_root의 바로 아래 폴더만 읽기 전용으로 검사한다. 임의 경로·설치·빌드·스크립트 실행은 없다. 설정·지원 엔진·package.json build·Git 저장소를 확인하고, 등록 가능한 후보와 기존 게임 ID를 구분한다. 심볼릭 링크·깨진 설정은 이유를 표시한다. 후보는 최대 100개이며 초과 여부를 응답한다. 실제 플레이·브리지 호환성 검증을 보장하지 않는다.
- 요청 응답의 `instruction`: 기존 versions.instruction_of로 사용자 요청 요약만 제공한다. 전체 시스템 프롬프트를 노출하지 않는다.
- 요청의 `review_before_apply`(기본 false): 스튜디오 UI가 true로 보낸 자동 빌드는 첫 성공본 이후 기존 활성 버전을 유지한다. 후보 채택은 사용자 확인을 받는 기존 rollback API로 포인터만 전환한다. 자동 타입 수리 요청에도 검토 설정을 전파한다. 기존 호출자의 자동 적용 계약은 보존했다.

## 실행 증거

- 발견 API RED: discover가 UUID 라우트로 해석되어 422였다. 변경 후 인증·읽기 전용·오류·기등록·제한 4건 통과.
- 후보 유지 RED: 새 성공 빌드가 이전 활성 빌드를 덮었다. 변경 후 후보 빌드와 활성 포인터가 분리되고 사용자 선택 후 전환되는 시험 통과.
- `TTAKKARI_NO_ENV_FILE=1 PYTHONPATH=src ../back/.venv/bin/python -m pytest tests/test_game_autobuild.py tests/test_game_rollback.py tests/test_game_worker_flow.py tests/test_game_discovery.py tests/test_route_inventory.py -q --tb=short` → 43 passed in 15.95s.
- 앞선 연결·기본 API·worker·인벤토리 묶음 32 passed in 22.25s도 실행했다. 이후 검증은 변경 표면에 맞춰 재사용한다.

## 이어갈 일

Studio Kit 별도 브랜치를 필요한 단위로 통합하고 설치 경계·브라우저 실행 계약을 재검토한다. 프론트는 전용 front-game-studio-practical-20261107에서 구현했으며 새 API가 포함된 백엔드 배포를 먼저 확인하고 게시해야 한다. 실제 게임의 격리 빌드·플레이 여정 및 모바일 실기기는 아직 미검증이다.

UI 검증의 첫 로컬 Vite 서버(5427)는 파일 변경 후 이전 변환 모듈을 제공했다. 이 Codex 실행 환경에서 PID 50821의 kill -TERM은 operation not permitted였다. 다른 방법으로 동일 PID 종료를 재시도하지 않았다. 별도 5428 검증 서버는 polling watcher를 사용하고 5분 후 스스로 닫는다. 이 관측은 로컬 검증 환경에 한정한다.
