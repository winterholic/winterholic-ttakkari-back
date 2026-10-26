# 단일 도서관의 운영 통합 경로

기존 `merge_scopes`는 이력·원문과 접근 pool을 보존하는 통합을 구현했지만, 운영 실행 경로는 CLI뿐이었다.
기존 개인 전용 operations API/MCP에 `collection_scope_preview`, `collection_scope_apply` 두 고정 종류를 연결했다.
쉘·경로·DB URL 입력이나 새 실행 인프라는 추가하지 않았다.

- preview는 실제 통합 SQL을 실행한 뒤 롤백한다. apply는 미해소 충돌이 있으면 전체를 롤백하고, 정상 적용은 멱등이다.
- 단일 `0075_collection_research_merge` 스키마, 요청을 처리하는 활성 세대와 control 일치, 이전 세대의 종료 및 lease/process group 부재를 확인한다.
- control 행을 잠근 트랜잭션에서 통합하므로 검사 이후의 세대 전환도 커밋/롤백까지 대기한다. 잠금 대기는 2초, SQL은 30초, 작업은 60초로 제한한다.
- 회사 세션은 기존 개인 전용 API 경계에서 거절한다. 결과에는 테이블별 수와 통합 식별자만 있으며 원문·비밀·접속 주소는 넣지 않는다.

검증 원시는 `review/collection-operations-20261026/`에 보존한다.
최초 누락 동작 RED 5개/API 종류 RED 2개와 offline executor 허용 반례 RED 1개를 보존했다.
관련 DB/API/inventory 검사는 23 passed, 별도 실제 control 행 잠금 검사는 1 passed였다.
변경 후 push gate가 최종 커밋의 검사를 다시 수행한다.

확대 검사 `green.txt`는 86 passed / 10 failed이며 전체 GREEN 기록이 아니다.
실패 지점은 기존 performance probe의 OSError, runner preexec_fn, 자식 관측 대기, psutil sysctl PermissionError 등이다.
기존 `tests/test_host_operations.py`와 runner 구현은 변경하지 않았다. 새 API 테스트를 별도 security 파일로 두고
기존 전체 host 검사 및 새 두 파일 모두 필수 배포 보안 목록에 유지했다. skip/단언 삭제로 실패를 숨기지 않았다.

운영 apply는 이 문서를 작성할 때 미실행이며, 새 버전의 배포·drain을 확인한 뒤 지원 MCP로 preview/apply를 이어간다.
