# ArtCraft 작업·업데이트 동시 실행 방지 최소 계약

## 1. 목적
동일한 ArtCraft 앱에 대해 제작 작업(Run 실행 및 MCP 도구 호출)과 앱 업데이트(바이너리 교체, 원자적 스왑, 롤백)가 동시에 실행될 때 발생할 수 있는 프로세스 충돌, 바이너리 잠금, 파일 손상을 상호 배타적으로 방지한다.

## 2. 파일 소유권 및 역할 분담
- **실행 경로 작업자 (artcraft_tasks.py 소유)**:
  - 활성 제작 작업 앱 목록 조회 계약(`active_task_apps`) 제공
  - 새 작업 생성 및 라운드 추가 시 앱별 조정 잠금 안에서 영속 업데이트 상태를 재검사하고, DB 커밋까지 진입을 직렬화
- **업데이트 작업자 (artcraft_update.py 소유)**:
  - 앱 업데이트/롤백 시작 전 `check_busy(app)`에서 `active_task_apps(db)`를 검사하여 제작 중인 앱의 교체 차단
  - Job 등록과 DB 커밋, 전체 교체/롤백 수명 동안 같은 앱별 조정 잠금을 유지

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
- **실제 연결 확인**: update/rollback 시작 경로는 `artcraft_update.py`에서 새 DB 세션으로 `active_task_apps(db)`를 조회한다. API route는 queued 등록과 감사 이벤트의 DB 커밋까지 앱 잠금을 잡고, background runner도 전체 업데이트/롤백 동안 동일 잠금을 유지한다.

### B. 업데이트 -> 제작 충돌 방지 (제작 경로가 호출)
```python
# api.artcraft.create_task 및 add_round route
async with artcraft.task_admission_lock(apps):
    task, run = await artcraft_tasks.create_task(...)
    await db.commit()
```

- **함수**: `artcraft.task_admission_lock(apps)`와 `check_app_not_updating(apps)`
- **위치**: `ttakkari.artcraft` / `ttakkari.artcraft_tasks`
- **동작**: 프로세스 안의 잠금과 세대 간 `flock`으로 앱별 등록 구간을 직렬화한다. 잠금 안에서 영속 `queued/running` 업데이트 상태를 읽고, 상태를 읽을 수 없으면 503으로 닫는다. 같은 앱 업데이트 중이면 409 `app_updating`을 반환한다. 작업 측 DB 커밋까지 잠금을 유지한다.

## 4. 검증 상태와 경계
- 방향별 검사: 업데이트 등록이 먼저면 create/add-round 요청이 409로 거부되고, create/add-round 커밋이 먼저면 updater가 DB의 활성 Run을 확인해 409로 거부된다. `test_create_commit_wins_same_app_update_race` 및 `test_add_round_commit_wins_same_app_update_race`가 작업 우선 경쟁을 각각 확인한다.
- 세대 간 검사: 다른 generation의 영속 업데이트 상태가 create/add-round 모두를 차단한다. 테스트는 `JOBS`만 검사하는 로컬 상태를 전제로 하지 않는다.
