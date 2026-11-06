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
- **실제 연결 확인**: 이 계약은 `artcraft_update.py`의 업데이트·롤백 진입점이 앱별 `check_busy(app, db)`에서 위 함수를 호출해야 성립한다. `active_task_apps` 구현이나 제작측 테스트만으로 업데이트측 호출을 검증했다고 볼 수 없다. 현재 체크아웃에는 `artcraft_update.py`가 없고 `artcraft.py`/`api/artcraft.py`에도 업데이트·롤백 진입 호출이 검색되지 않아 업데이트측 연결은 미검증이다.
- **업데이트 작업 소유자 후속**: 모든 update/rollback 시작 경로에서 `await artcraft_tasks.active_task_apps(db)` 결과를 검사하고, 대상 앱 충돌 시 `409 busy`를 반환한다. 그 호출부 검증과 업데이트 파일 변경은 `artcraft_update.py` 소유자가 수행한다.

### B. 업데이트 -> 제작 충돌 방지 (제작 경로가 호출)
```python
# artcraft_tasks.create_task 및 add_round 내부
check_app_not_updating(chosen_apps)
```

- **함수**: `check_app_not_updating(apps: list[str]) -> None`
- **위치**: `ttakkari.artcraft_tasks`
- **동작**: `artcraft.JOBS`에서 `state in ("queued", "running")`이고 `kind in ("update", "rollback")`인 활성 작업의 대상 앱과 대조. 충돌 시 `ArtcraftError(409, "app_updating", ...)` 발생.
- **실제 연결 확인**: `create_task`는 앱/입력 검증 뒤 세션 생성 전에, `add_round`는 보관 여부 확인 직후 입력 조회 전에 함수를 호출한다. 테스트는 두 공개 실행 함수를 실제로 호출하고 활성 update/rollback Job에서 409를 확인한다.

## 4. 검증 상태와 경계
- 제작측 검사: `test_create_task_checks_real_active_update_job_before_queue`, `test_add_round_checks_real_active_update_job_before_queue`가 실제 호출 경로의 update/rollback 차단을 확인한다.
- 업데이트측 검사: 담당 파일이 이 체크아웃에 없어 실제 `check_busy` 호출을 확인하지 못했다. 해당 호출 경로의 통합 검증은 업데이트 소유자가 추가해야 한다.
- `active_task_apps`는 DB Run 상태를 조회하므로 `artcraft.JOBS` 검사와 별개다. 작업 경로별 단위 테스트 통과만으로 양방향 경합이 모두 검증됐다고 주장하지 않는다.
