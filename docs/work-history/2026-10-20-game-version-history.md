# G32 버전 이력 탐색·필터·비교 (back)

설계서 §13.3 G32. 결정과 이유는 ADR 0061.

- 새 표·migration 없음. `games/versions.py` 와 `api/games.py` 에 읽기 전용 API 2개를 더했다.
  - `GET /api/games/{id}/versions`: `GameBuild.run_id → Run`, `Run.usage.game` 표식과 prompt 의 `[사용자 요청]` 본문에서 만든 요청·피드백 요약(화면 표시·오브젝트·영상 프레임 개수)·기준 버전 순번·이 버전에 걸린 요청 수.
  - `GET /api/games/{id}/compare?base=&head=`: 두 빌드 source commit 사이 `diff --numstat -z --no-renames --no-ext-diff --no-textconv`, `log base..head`. 기존 `run_git`(훅 끔, 사용자·시스템 git 설정 미사용). 작업 트리·인덱스·ref 를 바꾸지 않는다.
- `GameFeedback` 표는 여전히 아무 곳에서도 쓰지 않는다. "피드백 유무"는 Run 표식에서 읽는다(ADR 0061 §설계서와 다르게 정한 점).
- 안전: 저장소에 `diff.external`·`diff.<driver>.textconv` 를 설정해도 실행되지 않는 것을 테스트가 확인한다. 경로·커밋 제목은 제어 문자를 지우고 길이를 자른다. 다른 게임의 빌드 ID 는 404.

검증: `uv run pytest tests/test_game_versions.py -q` → 9 passed. 게임 관련 전체 `uv run pytest tests -q -k game` → 152 passed, 7 skipped, 1945 deselected in 195.47s(새 테스트 9개 포함. 원격 main 리베이스 뒤 재실행도 같은 결과). `uv run ruff check`(변경 3파일) 통과. 운영·사용자 게임 저장소는 만들지도 바꾸지도 않았다(임시 git 저장소만 사용, 합성 Run 표식).

미검증: 실제 worker 가 만든 Run·빌드가 쌓인 운영 데이터(prompt 머리말 규칙 의존), 빌드 수백 개에서의 응답 시간. 이 API 는 재시작 배포 후에 프론트가 쓴다(그 전에는 프론트가 빌드 기록으로 폴백).
