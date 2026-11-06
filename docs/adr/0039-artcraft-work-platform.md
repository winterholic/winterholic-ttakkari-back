# ADR 0039: ArtCraft 작업 플랫폼

## 상태
채택(2026-10-18). 상세 기획은 `docs/artcraft/work-platform-plan.md`. ADR 0035(관제)는 그대로 두고 "관리" 화면이 계속 쓴다.

## 배경
관제 화면만으로는 ArtCraft 앱으로 할 수 있는 일이 없다. 앱은 사람이 파일을 주고 지시해야 쓸모가 생긴다. 앱 이름(PhotoCraft 등)도 무엇을 하는지 알기 어렵다.

## 결정
- 사용자 언어의 작업 범주 12개(사진 편집, 일러스트·벡터, 화면·UI 디자인, 인쇄물·편집 디자인, 영상 편집, 모션·영상 효과, 오디오, PDF 편집, 문서 작성, 발표 자료, 스프레드시트, 도면·CAD(2D))를 네 묶음(이미지, 영상·소리, 문서, 도면)으로 보여 준다. 범주마다 담당 앱, 되는 일/아직 안 되는 일, 성숙도, 지시 템플릿을 `artcraft_catalog.py`에 둔다. 값은 2026-10-18 이 Mac Studio에서 직접 돌려 확인한 결과이며 앱 업데이트 때 재검증한다.
- 라우트: `/artcraft`(허브), `/artcraft/c/:category`(지시·작업물·이력), `/artcraft/t/:taskId`(작업 상세), `/artcraft/manage`(기존 관제). 예전 `/artcraft?tab=...` 링크는 관리로 넘긴다.
- **새 실행기 없음.** 작업 = 일반 세션(`kind=chat`, 개인 pool의 실행 엔진, 전용 워크스페이스) + `artcraft_tasks` 행(범주·앱·옵션). 라운드 = 그 세션의 Run. 지시는 기존 `queue_run`으로 대기열에 넣어 같은 세션에서 resume한다. 작업 세션은 `/api/sessions` 목록(사이드바·작업 공간)에서 제외한다.
- **정확 계정 지정(2026-11-07):** `POST /api/artcraft/tasks`의 선택 필드 `account_id`는 EngineAccount UUID다. 생략 시 기존 자동 선택을 유지한다. 지정하면 API가 개인 pool·활성·실행 준비·계정 및 모델 한도를 검사하고, 세션의 nullable `account_id`와 각 Run의 `usage.artcraft.account_id`에 저장한다. DB FK는 두지 않아 계정 삭제 후에도 지시가 자동으로 다른 계정에 풀리지 않는다. Manager의 후보·재시도·quota wait 재검토·재시작 복구 모두 해당 ID 하나로 제한한다. 후속 라운드에 다른 UUID가 오면 409 `account_pin_immutable`; 지정 계정이 없거나 비활성/준비 실패/한도면 503 `account_unavailable` 또는 같은 계정만 대기한다. 이 필드는 사용자별 계정 선택을 위한 것이며 제품 코드에는 계정 ID를 고정하지 않는다.
- 실행 직전 `manager._run`이 `artcraft_tasks.prepare_run`을 한 번 부른다: 작업 폴더(`<워크스페이스>/<task_id>/in|prev|work|out`) 생성, 첨부·이전 결과를 복사, 앱별 MCP 서버(`artcraft_<app>`: `bin/artcraft mcp <app>` + 경로 제한 옵션)를 `LaunchSpec.mcp_servers`에 합치고, 지시문 머리말과 시스템 노트를 만든다. 실행 후 `sweep_outputs`가 `out/` 결과를 outbox로 복사하고 기존 `_collect_artifacts`가 Artifact로 등록한다.
- 경로 제한: 앱이 지원하면 `--automation-read/write-root`(PhotoCraft·VectorCraft·WordCraft·EffectCraft·SoundCraft)나 `--root`(PdfCraft)로 작업 폴더에 가둔다. 나머지 앱은 따까리 guard가 `mcp__artcraft_*` 도구 인자의 절대 경로가 보호 영역·민감 파일이면 막는다(부분 보호).
- 영상·오디오 미리보기를 위해 Artifact 종류 `audio`, `video`를 추가하고 뷰어에 재생 화면을 만든다(서명된 inline 링크, Range). `.dxf`·`.psd`는 브라우저가 그리지 못해 `other`로 둔다.
- 데이터: 새 테이블 `artcraft_tasks`(마이그레이션 0041, 새 테이블만이라 온라인 마이그레이션 허용으로 계약 갱신).
- API(`/api/artcraft/*`에 추가): `catalog`, `tasks`(POST·GET), `tasks/{id}`(GET·PATCH), `tasks/{id}/runs`(POST), `outputs`. 입력 검증은 범주의 받는 확장자, 파일 10개, 템플릿 최소 파일 수와 옵션 선택지다.

## 결과와 한계
- 계정을 지정하지 않은 작업은 기존 개인 계정 자동 선택과 manager fallback을 유지한다. 정확한 `account_id`가 지정된 작업은 해당 계정만 사용하고 한도 때 같은 계정으로 대기한다.
- 경로 격리가 없는 앱(DeckCraft, DesignCraft, GridCraft, CADCraft, FigCraft, FilmCraft, LightCraft)은 guard의 부분 보호뿐이다.
- 결과물은 보존 기간(기본 90일) 뒤 정리된다.
