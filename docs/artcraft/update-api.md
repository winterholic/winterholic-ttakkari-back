# ArtCraft 앱별 업데이트 API 계약

> **문서 상태**: 확정 (2026-11-07)
> **대상**: 프론트엔드 관제 UI, 백엔드 라우터, 오케스트레이터/리드 에이전트  
> **기반 ADR**: ADR 0035 (ArtCraft 관제) 개정 및 신규 업데이트 명세

---

## 1. 개요 및 설계 원칙

본 계약은 Mac Studio에 설치된 13개 공식 ArtCraft 앱(`photocraft`, `vectorcraft`, `filmcraft`, `lightcraft`, `wordcraft`, `pdfcraft`, `effectcraft`, `designcraft`, `figcraft`, `soundcraft`, `deckcraft`, `cadcraft`, `gridcraft`)의 버전 확인, 사전 검증, 안전한 원자적 적용, 그리고 이전 버전으로의 복구(롤백) 인터페이스를 정의합니다.

### 불변 조건 및 안전 원칙
1. **공식 Storytold 자산 한정**: 임의 URL, 비공식 저장소, 임의 셸 입력은 차단하며 오직 `storytold/<app>` 공식 릴리스의 macOS Universal/ARM64 CLI 자산 및 `SHA256SUMS.txt`만 다운로드·검증합니다.
2. **Busy Guard**: 대상 앱 GUI가 켜져 있거나(`running` 또는 `listening`), DB의 활성 제작 Run, 로컬 Job 또는 공유 상태 파일에 기록된 업데이트가 확인되면 409 Conflict로 거절합니다. 활성 제작 Run 조회나 상태 확인이 실패하면 업데이트를 허용하지 않고 503으로 돌려보냅니다. 인메모리 `JOBS`는 프로세스 안의 충돌만 찾으며 서버 세대 간 배타성을 보장하지 않습니다. 상태 확인과 Job 등록도 단일 원자 연산이 아니므로 여러 세대가 동시에 시작하는 경쟁까지 배제하는 계약은 아닙니다.
3. **원자적 스왑 & 백업 보존**: 스테이징 디렉터리에서 압축 해제 및 무결성 검증을 마친 후 `dist/cli/<app>` 및 `bin/<app>-cli` 심링크를 원자적으로 교체하며, 직전 버전을 항상 보존합니다. 동일 버전 재설치 시에도 기존 디렉터리를 즉시 삭제하지 않고 임시 보존 이동 후 사후 검증 성공 시 정리합니다.
4. **사후 검증 및 자동 롤백**: 교체 직후 CLI `--version` 및 정상 JSON-RPC 2.0 MCP `initialize` 성공 `result` 구조를 검증하며, 실패 시 사용자 개입 없이 이전 바이너리 바이트와 디렉터리를 복원합니다. 수동 롤백 후보도 검증에 실패하면 현재 설치로 심링크를 되돌립니다.
5. **거짓 성공 금지**: 서버 재시작이나 프로세스 중단 발생 시 비정상 종료된 작업이 성공으로 표시되지 않도록 상태 파일(`state.json`)에서 세대 불일치/미완료를 감지하여 `interrupted`/`failed`로 마킹합니다.

---

## 2. 엔드포인트 목록

| 메서드 | 경로 | 인증 | 설명 | 상태 코드 |
|---|---|---|---|---|
| `GET` | `/api/artcraft/apps/{app}/update` | User | 앱별 업데이트 미리보기 및 백업/Busy 상태 조회 | 200, 404, 502, 503 |
| `POST` | `/api/artcraft/apps/{app}/update` | User | 최신/지정 릴리스로 업데이트 실행 (비동기 Job) | 202, 400, 404, 409, 428, 503 |
| `POST` | `/api/artcraft/apps/{app}/update/rollback` | User | 이전 버전으로 롤백 실행 (비동기 Job) | 202, 404, 409, 428, 503 |
| `GET` | `/api/artcraft/apps/{app}/update/status` | User | 앱별 업데이트 진행 상태 및 이력 조회 | 200, 404 |
| `GET` | `/api/artcraft/jobs/{job_id}` | User | 표준 Job 상세 조회 (기존 API 활용) | 200, 404 |

---

## 3. 엔드포인트 상세 규격

### 3.1. 업데이트 미리보기 (`GET /api/artcraft/apps/{app}/update`)

앱의 현재 설치 버전, 최신 릴리스 정보, 롤백 가능 여부, 현재 업데이트 가능 여부(Busy 상태)를 반환합니다.

#### 요청
- 경로 파라미터: `app` (string, `registry.json`에 정의된 13개 앱 이름)
- 쿼리 파라미터: `refresh` (boolean, optional, 기본값 `false`) — `true` 지정 시 GitHub 릴리스 메타데이터와 실시간 앱 상태를 즉시 재조회합니다. 미리보기 갱신은 ZIP 바이너리나 체크섬 파일을 받지 않습니다. 원격 조회 실패는 이전 캐시로 대체하지 않고 오류로 반환합니다.

#### 응답 (`200 OK`)
```json
{
  "app": "photocraft",
  "title": "PhotoCraft",
  "installed_version": "0.10.0",
  "cli_version_line": "photocraft-cli 0.10.0",
  "latest_release": {
    "tag": "v0.10.0",
    "version": "0.10.0",
    "published_at": "2026-10-18T11:41:00Z",
    "asset_name": "photocraft-cli-0.10.0-macos-universal.zip"
  },
  "update_available": false,
  "can_update": true,
  "can_rollback": true,
  "previous_version": "0.9.0",
  "busy": false,
  "busy_reason": null,
  "latest_job": {
    "id": "abc123456789",
    "kind": "update",
    "action": "update-apply",
    "state": "succeeded",
    "created_at": "2026-11-06T10:00:00Z",
    "finished_at": "2026-11-06T10:00:15Z",
    "summary": "PhotoCraft 0.10.0 업데이트 완료"
  }
}
```

- `can_update`: `true`일 때 업데이트 버튼 활성화. `busy`이거나 최신 릴리스 정보가 없으면 `false`.
- `busy`: 앱 GUI, DB에서 조회한 활성 제작 Run, 로컬 활성 Job 또는 공유 상태 파일의 활성 업데이트가 확인되면 `true`.
- `busy_reason`: busy 상태의 구체적 사유(예: `"photocraft 앱이 현재 실행 중입니다."` 또는 `"작업 진행 중: smoke-full"`).
- `can_rollback`: `true`일 때 롤백 버튼 활성화. 이전 백업 디렉터리가 존재할 때만 `true`.
- `refresh=true`에서 릴리스 조회 실패 시 `502` 등 원격 오류를 반환합니다. 활성 제작 작업 DB 조회나 공유 상태 조회 실패는 `503 busy_check_unavailable`을 반환하며 업데이트 가능 여부를 추정하지 않습니다.

---

### 3.2. 업데이트 실행 (`POST /api/artcraft/apps/{app}/update`)

해당 앱을 공식 릴리스로 업데이트합니다.

#### 요청 본문
```json
{
  "tag": "v0.10.0",
  "confirm": true
}
```
- `tag` (string, optional): 특정 태그 지정(형식: `^v?\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$`). 생략 시 GitHub latest 릴리스.
- `confirm` (boolean, required): `true`여야 실행됨. 미전송/false 시 428 Precondition Required.

#### 응답 (`202 Accepted`)
```json
{
  "id": "job-upd-1234",
  "kind": "update",
  "action": "update-apply",
  "apps": ["photocraft"],
  "mode": "apply",
  "state": "queued",
  "created_at": "2026-11-06T14:40:00Z",
  "started_at": null,
  "finished_at": null,
  "summary": null,
  "output": ""
}
```

#### 에러 응답
- `404 Not Found`: 등록되지 않은 앱 (`unknown_app`)
- `409 Conflict`: 앱이 현재 실행 중이거나(`app_running`), 다른 스모크/업데이트가 진행 중임 (`busy`)
- `422 Unprocessable Entity`: 잘못된 태그 형식 (`validation`)
- `428 Precondition Required`: `confirm: true` 미포함 (`confirm_required`)
- `503 Service Unavailable`: 활성 제작 작업 또는 공유 업데이트 상태를 확인할 수 없음 (`busy_check_unavailable`)

---

### 3.3. 이전 버전으로 롤백 (`POST /api/artcraft/apps/{app}/update/rollback`)

보존된 이전 버전으로 즉시 복구합니다.

#### 요청 본문
```json
{
  "confirm": true
}
```
- `confirm` (boolean, required): 확인 플래그.

#### 응답 (`202 Accepted`)
```json
{
  "id": "job-upd-5678",
  "kind": "update",
  "action": "update-rollback",
  "apps": ["photocraft"],
  "mode": "rollback",
  "state": "queued",
  "created_at": "2026-11-06T14:45:00Z",
  "started_at": null,
  "finished_at": null,
  "summary": null,
  "output": ""
}
```

#### 에러 응답
- `404 Not Found`: 롤백할 이전 버전 백업이 없음 (`no_backup_available`)
- `409 Conflict`: 앱 실행 중 또는 다른 작업 진행 중 (`busy` / `app_running`)
- `428 Precondition Required`: `confirm: true` 미포함 (`confirm_required`)
- `503 Service Unavailable`: 활성 제작 작업 또는 공유 업데이트 상태를 확인할 수 없음 (`busy_check_unavailable`)

---

### 3.4. 업데이트 상세 상태 및 이력 (`GET /api/artcraft/apps/{app}/update/status`)

영속화된 상태 파일(`state.json`)의 업데이트 이력 및 현재 진행 상태를 반환합니다.

#### 응답 (`200 OK`)
```json
{
  "app": "photocraft",
  "current_version": "0.10.0",
  "previous_version": "0.9.0",
  "state": "idle",
  "error": null,
  "last_status": {
    "job_id": "job-upd-1234",
    "action": "update-apply",
    "from_version": "0.9.0",
    "to_version": "0.10.0",
    "status": "succeeded",
    "error": null,
    "at": "2026-11-06T14:40:15Z"
  },
  "history": [
    {
      "job_id": "job-upd-1234",
      "action": "update-apply",
      "from_version": "0.9.0",
      "to_version": "0.10.0",
      "status": "succeeded",
      "error": null,
      "at": "2026-11-06T14:40:15Z"
    }
  ]
}
```
- `state`: `"idle"`, `"running"`, `"queued"`, 또는 `"interrupted"` (서버 비정상 종료 시 재시작 직후 정정됨).
- `error`: 최신 작업 실패/중단 시 오류 메시지.

---

## 4. 기존 관제 API 호환성 보장

기존 `GET /api/artcraft/apps` 응답의 기본 스키마는 100% 유지됩니다.
각 앱 카드에 다음 선택적 필드가 추가되어 클라이언트가 별도 호출 없이도 롤백 가능 여부를 표시할 수 있습니다:
- `can_rollback`: boolean (이전 백업이 있어 즉시 롤백 가능)
- `previous_version`: string | null (보존된 이전 버전)
- `update_busy`: boolean (현재 해당 앱 업데이트 또는 스모크 실행 중)

기존 클라이언트는 이 필드를 무시해도 정상 작동하며, 신규 UI는 즉시 이를 활용할 수 있습니다.
