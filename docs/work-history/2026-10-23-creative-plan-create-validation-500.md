# 2026-10-23 Creative Studio plan_create HTTP 500 원인과 수정

## 증상과 운영 증거

- 기존 계획 `5809bb7f-4800-44d9-aad6-fcdd9a1d03b8`의 실생성 영상은 별도로 `qa_passed`까지 완료됐다. 기존 계획·미디어와 `2ac86aa4…needs_reconcile`은 이 작업에서 변경하지 않았다.
- 다양한 유형 영상의 첫 계획 요청은 2026-10-23 09:21:37Z, API 로그 `LOG-189` / event 528에서 HTTP 500으로 기록됐다. 이벤트가 속한 release는 `81a95a588f85ba8bf9028dbdb8d7625e2d144430`; `backend_deploy_status(2439620c-68e7-40c6-a9d5-f98dd9555fd6)`는 같은 revision을 `draining`/활성으로 확인했다.
- 요청 입력 중 민감 내용을 되풀이하지 않고 검증 오류의 관련 부분만 보면 `style`에 `{entry_id: ...}`인 세 항목(`C03.vector_motion_graphics`, `C10.cut`, `C10.subtitles`)이 들어왔다. 운영 stack은 `creative_mcp.py`의 `PlanCreate.model_validate(data)`에서 `style.0..2.style_id`와 `version` 누락, `entry_id` 추가 필드 오류 총 9건을 기록했다.
- 이 지점은 기존 스타일 도구의 의미와 MCP 계약 불일치다. `style`은 Style DNA 참조(`style_id`, `version`)인데 호출자는 콘텐츠 카탈로그 ID를 넣었다. MCP 입력 스키마가 `list[dict[str, Any]]`라 이 입력을 허용했고, bridge가 Pydantic `ValidationError`를 HTTP 예외로 바꾸지 않아 500이 됐다.
- 예외가 발생한 줄은 기존 `creative_v2.plan_create` 서비스 호출보다 앞이다. 따라서 이 요청은 계획 handler, recipe registry/matcher, StyleStore/PlanStore, 설정·생성 runtime까지 도달하지 않았다. 이 건의 증거는 API validation stack이며, 이 범위에서 그 하위 구성요소의 이상을 시사하는 증거는 없다.

## 수정

- MCP `creative_v2_plan_create.style`를 `style_id`와 `version`이 필수인 구조화 ref로 공개하고 설명에 catalog entry ID를 넣지 않는다고 명시했다.
- bridge에서 `PlanCreate` 검증 실패를 HTTP 422로 변환한다. 오류 응답은 필드 경로와 검증 메시지만 담고 원래 입력값은 반환하지 않는다.
- 계획 matcher, recipe 지원 조건, approval/render gate는 변경하지 않았다. 유효하지 않은 recipe를 허용하거나 계획을 다른 방식으로 강등하는 변경도 없다.

## 검증

- TDD RED에서 (1) MCP 스타일 스키마에 필수 ref 필드가 없고 (2) 잘못된 ref가 bridge에서 `ValidationError`로 그대로 전파되는 것을 확인했다.
- GREEN 회귀는 catalog ID ref가 bridge에서 422로 거절되고 계획 서비스가 호출되지 않는지, 유효한 `style_id`/`version`이 전달되는지, MCP schema와 정상 전송이 일치하는지 검사한다.
- 계획 입력 계약 회귀에 한국어 기술 설명·여행·제품·감성·뉴스 brief, 20초·60초·150초 목표 길이, 다섯 개의 내장 Style DNA 참조를 넣어 검증했다. 이 검사는 service transport를 mock하므로 실제 계획 생성·GPU·영상 생성 증거는 아니다.
- 운영에 `plan_create`를 재호출하지 않았다. 수정 전 운영 입력을 반복해서 500을 확인하지 않았으며, 실제 GPU 생성도 수행하지 않았다.

## 남은 검증 범위

- 이 수정의 backend contract/변경 테스트 통과와 배포 후 새 runtime/MCP schema 로딩을 구분한다. 배포 뒤 잘못된 style ref가 422로 끝나는지, 올바른 요청이 계획 생성을 통과하는지 확인이 필요하다.
- 다양한 다섯 편 실제 영상 생성, 생성 receipt, render/QA 및 파일 공유는 별도 후속 범위다. 재개 시 배포된 수정 SHA와 MCP schema를 확인한 뒤 시작한다.
