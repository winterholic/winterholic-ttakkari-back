# ArtCraft 작업·업데이트 동시 실행 방지 최소 계약

## 1. 목적
동일한 ArtCraft 앱에 대해 제작 작업(Run 실행 및 MCP 도구 호출)과 앱 업데이트(바이너리 교체, 원자적 스왑, 롤백)가 동시에 실행될 때 발생할 수 있는 프로세스 충돌, 바이너리 잠금, 파일 손상을 상호 배타적으로 방지한다.

## 2. 파일 소유권 및 역할 분담
- **실행 경로 작업자 (artcraft_tasks.py 소유)**:
  - 활성 제작 작업 앱 목록 조회 계약(`active_task_apps`) 제공
  - 새 작업 생성 및 라운드 추가 시 활성 업데이트 상태 검사(`check_app_not_updating`) 수행
- **업데이트 작업자 (artcraft_update.py 소유)**:
  - 앱 업데이트/롤백 시작 전 `check_busy(app)`에서 `active_task_apps(db)`를 검사하여 제작 중인 앱의 교체 차단
  - 활성 `update` 및 `rollback` Job을 `artcraft.JOBS`에 등록/유지하여 제작 경로의 검사에 노출

## 3. 인터페이스 명세

### A. 제작 -> 업데이트 충돌 방지 (업데이트 백엔드가 호출)
```python
from ttakkari import artcraft_tasks

# artcraft_update.check_busy(app, db) 내부
active_apps = await artcraft_tasks.active_task_apps(db)
if app in active_apps:
    raise ArtcraftError(409, "busy", f"'{app}' 앱을 사용하는 제작 작업이 현재 실행 중입니다.")
```

- **함수**: `active_task_apps(db: AsyncSession) -> set[str]`
- **위치**: `ttakkari.artcraft_tasks`
- **동작**: 현재 `RunStatus.queued` 또는 `RunStatus.running` 상태인 Run을 가진 `ArtcraftTask`의 `apps` 목록을 집합으로 반환.

### B. 업데이트 -> 제작 충돌 방지 (제작 경로가 호출)
```python
# artcraft_tasks.create_task 및 add_round 내부
check_app_not_updating(chosen_apps)
```

- **함수**: `check_app_not_updating(apps: list[str]) -> None`
- **위치**: `ttakkari.artcraft_tasks`
- **동작**: `artcraft.JOBS`에서 `state in ("queued", "running")`이고 `kind in ("update", "rollback")`인 활성 작업의 대상 앱과 대조. 충돌 시 `ArtcraftError(409, "app_updating", ...)` 발생.

## 4. 검증 결과
- `unit_tests/test_artcraft_tasks.py`의 `test_active_task_apps_returns_apps_of_active_runs` 및 `test_create_task_rejects_updating_apps` 테스트로 양방향 차단 계약 검증 완료 (10개 단위 테스트 전원 통과).
