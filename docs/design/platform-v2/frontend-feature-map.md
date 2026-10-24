# 따까리 플랫폼 V2: Frontend 전체 Feature Mapping 및 계약 감사 보고서 (교정본)

> **감사 담당**: Worker C (Frontend 전체 feature mapping / test architecture / baseline audit 교정)  
> **검증자 및 판정 대조**: Verifier `fed2c630-a867-42f1-9574-4a92905de864` (FAILED 판정 후 교정 반영)  
> **감사 시각**: 2026-10-24 22:30 KST  
> **대상 저장소 및 기준선 바인딩**:  
> - Frontend Local HEAD: `630040b1b7007a219a93c9d3d4491fab714c555d`  
> - Frontend Remote Ref: `dbe0c9f042b006e2a627b8e59c295ad14b7a2caf` (`origin/main`, ahead 3 / behind 105, dirty fingerprint: `f7d9a949fa...`)  
> - Backend Target SHA: `c8f10156845d007ec6496a65d6d8417be19260bd` (`origin/main`, A 정본의 base origin/main)  
> - Backend Worktree: `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-c-fix2` (branch: `audit/platform-v2-c-correction2-20261024`, base: `8663d64ce05b5c9739758025e0170e7842104c8c`)  
> **동반 산출물**:  
> - JSON: `back/docs/design/platform-v2/frontend-feature-map.json`  
> - CSV: `back/docs/design/platform-v2/frontend-feature-map.csv`  
> - Raw Evidence: `back/review/platform-v2/20261024/frontend-tests/`  

---

## 1. 개요 및 감사 교정 이력

본 문서는 따까리 플랫폼 V2 감사 교정 후보(Candidate)로 작성된 프론트엔드 전체 기능 지형도다.
독립 검증자(`fed2c630`)의 검증 실패 보고서(`frontend-test-audit-verification.md`)를 반영하여 다음 사항들을 전면 교정했다:

1. **Route 전수 추출 정합성 확보**:
   - TypeScript JSX AST 파서를 도입하여 주석 안의 `<Route>` 요소를 완전 배제하고, 멀티라인 선언인 `/context-library`, `/calendar`를 온전히 인식.
   - 기계 추출 JSON과 문서 모두 **38개 path 선언 (concrete path 37개 + wildcard 1개)**, **중첩 레이아웃 라우트 1개(`<Route element={<AppShell />}>`)**, **리다이렉트 1개(`* -> Navigate to="/chat"`)**, **실제 페이지 컴포넌트 29종**으로 완벽하게 일치시킴.
   - 계산 불가 dynamic 경로 발생 시 침묵 누락(silent skip)하지 않고 `unresolved_ledger`에 기록하도록 보장 (`App.tsx` 기준 0건).
2. **API 함수 경계 해석 및 HTTP Method 정상화**:
   - 함수 스코프 내 호출 경계 및 HTTP 메서드 기본값(GET) 해석을 정밀화하여 `getSession` (GET), `getArtifact` (GET), `getOpsIssue` (GET)의 오분류를 바로잡음.
   - `client.ts` 외 `src/api/stream.ts`의 SSE 엔드포인트(`/api/runs/${runId}/stream`, GET, line 45) 및 `src/shell/useConnection.ts`의 `fetchHealth`(`/api/health`, GET, line 25)를 포함하여 총 **184개 API 호출**을 전수 수집.
   - 직접 fetch 호출인 `refreshAccessToken` (POST `/api/auth/refresh`) 및 `publicShareRequest` 헬퍼 래퍼(`getPublicMeetingShare`, `downloadPublicMeetingShare`)를 원장에 포함.
   - 모든 API 원장에 `frontend_module`, `plane`, `version`, `source_file`, `line_number` 필드를 추가.
   - 백엔드 25개 실제 모듈 인벤토리(`backend-module-inventory.json`)에 바인딩하고, 임의의 가상 ID 생성을 전면 폐기. 백엔드 B 인벤토리 역시 교정 중이므로 매핑 상태를 `provisional`(잠정) 또는 `unknown`으로 기록하고 stable 계약으로 과장하지 않음. 매핑 미확정 엔드포인트는 `unmatched_ledger`에 보존.
3. **MD / JSON / CSV 동일 중간 레코드 집합 생성**:
   - CSV의 route 38개가 일괄 `verified,core.system_routing`으로 오염되었던 결함을 제거하고, MD/JSON/CSV가 동일한 중간 레코드 집합으로부터 정확한 도메인·모듈·플레인·잠정 ID 매핑을 생성하도록 보장.
4. **UI Worker 커밋 및 Vercel 배포 상태 한계 명시**:
   - UI worker 커밋 `dbe0c9f042b006e2a627b8e59c295ad14b7a2caf`가 Git 상에서 `origin/main`의 ancestor임은 확인했으나, Vercel 실제 deployment receipt는 접근 가능한 도구로 확인되지 않았으므로 **배포 완료는 미검증(Unverified)**으로 범위를 한정.

---

## 2. 라우트 및 페이지 기능 매핑 (전수 조사: 38개 path 선언)

- **선언된 path 총수**: 38개 (Concrete: 37개, Wildcard: 1개)
- **레이아웃 라우트 (path 없음)**: 1개 (`<Route element={<AppShell />}>`)
- **실제 페이지 컴포넌트**: 29종 (Suspense 내부 지연 로딩 페이지 포함)
- **리다이렉트 라우트**: 1개 (`*` -> `Navigate to="/chat"`)
- **미해결 라우트 (Unresolved Ledger)**: 0건

| No | 라우트 경로 | 컴포넌트 | 담당 도메인 / 설명 | 상태 관리 및 쿼리 키 | Backend Module ID (25종 바인딩) | 상태 | Fallback / 오류 처리 |
|---|---|---|---|---|---|---|---|
| 1 | `/login` | `LoginPage` | 인증 로그인 (비밀번호 기반 Bearer 토큰 발급) | `useAuth, tk-token` | `core.auth_session` | provisional | 인라인 에러 메시지, 로그인 루프 방지 |
| 2 | `/share/:shareId` | `PublicMeetingPage` | 공개 회의록 외부 열람 (단독 공유 열람) | `useQuery(['public-share', id])` | `domain.shares_artifacts` | provisional | PublicMeetingError, 404/만료 상태 |
| 3 | `/chat` | `DeskHome` | 개인 대화 홈 캔버스 및 데스크 접수 | `useQuery(['sessions']), useQuery(['runs'])` | `core.auth_session` | provisional | CanvasBoundary, 빈 상태 안내 |
| 4 | `/chat/thread` | `ChatPage` | 개인 대화 전체 화면 스레드 | `useRunStream (SSE), useQuery(['runs'])` | `exec.run_engine` | provisional | 연결 단절 시 재연결, 500 에러 배너 |
| 5 | `/chat/:sessionId` | `ChatPage` | 특정 개인 대화 세션 스레드 | `useRunStream (SSE)` | `exec.run_engine` | provisional | 세션 부재 시 404 후 /chat 리다이렉트 |
| 6 | `/work` | `DeskHome` | 회사 업무 대화 홈 캔버스 | `useQuery(['sessions', 'work'])` | `core.auth_session` | provisional | CanvasBoundary, 빈 상태 안내 |
| 7 | `/work/thread` | `ChatPage` | 회사 업무 전체 화면 스레드 | `useRunStream (SSE)` | `exec.run_engine` | provisional | 에러 바운더리, 재시도 버튼 |
| 8 | `/work/:sessionId` | `ChatPage` | 특정 회사 업무 세션 스레드 | `useRunStream (SSE)` | `exec.run_engine` | provisional | 에러 바운더리 |
| 9 | `/tasks` | `TasksPage` | 활성/완료 작업 계층 트리 및 일별 그룹 | `useQuery(['tasks', 'active'])` | `domain.pms_tasks` | provisional | 로딩 스켈레톤, 빈 작업 안내 |
| 10 | `/tasks/failed` | `FailedTasksPage` | 실패한 작업 전용 추적 및 원인 진단 | `useQuery(['tasks', 'failed'])` | `domain.pms_tasks` | provisional | 재시도 버튼 (Run 재배정) |
| 11 | `/workspace` | `WorkspacePage` | Mac Studio 작업 공간 파일/폴더 브라우저 | `useQuery(['workspaces'])` | `core.workspaces_transfers` | provisional | 디렉터리 접근 불가 오류 표시 |
| 12 | `/workspace/:sessionId` | `WorkspacePage` | 특정 세션의 산출물 및 작업 공간 | `useQuery(['workspace', sessionId])` | `core.workspaces_transfers` | provisional | 세션별 파일 트리 뷰어 |
| 13 | `/library` | `LibraryPage` | 모든 산출물 날짜별 아카이브 보관함 | `useQuery(['library', 'artifacts'])` | `domain.shares_artifacts` | provisional | 날짜 필터, 빈 상태 마스코트 |
| 14 | `/context-library` | `ContextLibraryPage` | 맥락 도서관 RAG 색인·온톨로지 그래프 | `useQuery(['context-library'])` | `domain.knowledge_rag` | provisional | RouteFallback (스피너) |
| 15 | `/context-library/items/:kind/:id` | `ContextLibraryItemPage` | 맥락 도서관 개별 지식 노드 상세 | `useQuery(['context-item', kind, id])` | `domain.knowledge_rag` | provisional | RouteFallback, 404 안내 |
| 16 | `/files` | `FilesPage` | Mac Studio 파일 탐색 및 모바일 전송 | `useTransfers (ExternalStore)` | `core.workspaces_transfers` | provisional | 청크 전송 재시도(최대 8회), WakeLock |
| 17 | `/settings` | `SettingsPage` | 엔진 계정/사용량/시스템 환경 설정 | `useQuery(['accounts']), useQuery(['system'])` | `core.accounts_quota` | provisional | 탭 분리, 계정별 오류 배지 |
| 18 | `/assistant` | `AssistantPage` | 비서 페르소나 및 하네스 지침 관리 | `useQuery(['assistant', 'prompt'])` | `core.system_routing` | provisional | 저장 실패 토스트 알림 |
| 19 | `/more` | `MorePage` | 모바일 전용 부가 기능 허브 | `정적 링크 목록 (FEATURES)` | `core.system_routing` | provisional | 즉시 렌더링 |
| 20 | `/notifications` | `NotificationsPage` | 카테고리별 알림함 (따까리/작업/일정) | `useInfiniteQuery(['notifications'])` | `core.system_routing` | provisional | 읽음 일괄 처리, 페이징 |
| 21 | `/weather` | `WeatherPage` | 수원 오늘 날씨 + 서울 출퇴근/주간 예보 | `useQuery(['weather', 'briefing'])` | `domain.personal_tools` | provisional | 캐시 우선, 갱신 버튼 |
| 22 | `/resources` | `ResourcesPage` | 호스트 CPU/메모리/GPU 가속기 모니터링 | `useQuery(['resources', 'live'])` | `exec.host_gpu` | provisional | 폴링(5초), 꺾은선 차트 |
| 23 | `/logs` | `OpsLogsPage` | 운영 오류 이슈 및 배포 이력 트래킹 | `useInfiniteQuery(['ops', 'issues']), ['ops', 'deploys']` | `ops.monitoring` | provisional | 원인 로그 인라인 확장 뷰 |
| 24 | `/logs/issues/:id` | `OpsIssuePage` | 개별 운영 이슈 상세 및 스택트레이스 | `useQuery(['ops', 'issue', id])` | `ops.monitoring` | provisional | 상태 변경(해결/무시) |
| 25 | `/meetings` | `MeetingsPage` | 회의록 목록 및 녹음/전사 현황 | `useQuery(['meetings'])` | `domain.meetings_stt` | provisional | 로컬 STT 상태 감지 |
| 26 | `/meetings/new` | `MeetingNewPage` | 새 회의 생성 및 녹음 파일 업로드 | `폼 로컬 상태` | `domain.meetings_stt` | provisional | 파일 검증 에러 |
| 27 | `/meetings/:meetingId` | `MeetingDetailPage` | 회의록 상세, 전사 텍스트, 결정사항 | `useQuery(['meeting', id])` | `domain.meetings_stt` | provisional | 전사 실패 시 원본 평문 대체 |
| 28 | `/pms` | `PmsListPage` | PMS 프로젝트 목록 | `useQuery(['pms', 'projects'])` | `domain.pms_tasks` | provisional | RouteFallback |
| 29 | `/pms/:projectId` | `PmsProjectPage` | PMS 프로젝트 상세 (칸반 보드/로드맵) | `useQuery(['pms', 'project', id])` | `domain.pms_tasks` | provisional | RouteFallback, DnD 에러 롤백 |
| 30 | `/pms/:projectId/:tab` | `PmsProjectPage` | PMS 프로젝트 특정 탭 (board/milestones/notes) | `useQuery(['pms', 'project', id, tab])` | `domain.pms_tasks` | provisional | RouteFallback |
| 31 | `/wbs` | `WbsListPage` | WBS 프로젝트 분해 목록 | `useQuery(['wbs', 'projects'])` | `domain.pms_tasks` | provisional | RouteFallback |
| 32 | `/wbs/:projectId` | `WbsProjectPage` | WBS 작업 분해 트리 및 간트 차트 | `useQuery(['wbs', 'project', id])` | `domain.pms_tasks` | provisional | RouteFallback, 트리 접힘 상태 |
| 33 | `/wbs/:projectId/:view` | `WbsProjectPage` | WBS 뷰 전환 (tree/gantt) | `useQuery(['wbs', 'project', id])` | `domain.pms_tasks` | provisional | RouteFallback |
| 34 | `/checklists` | `ChecklistsPage` | 일일 체크리스트 및 반복 템플릿 | `useQuery(['checklists', day])` | `domain.pms_tasks` | provisional | 체크 즉시 optimistic 반영 |
| 35 | `/memos` | `MemosPage` | 카테고리/태그 기반 메모 관리 | `useQuery(['memos'])` | `domain.memory_ontology` | provisional | 에디터 자동 저장, 캐럿 복원 |
| 36 | `/memos/:memoId` | `MemosPage` | 특정 메모 선택 뷰 | `useQuery(['memo', memoId])` | `domain.memory_ontology` | provisional | 메모 삭제 시 목록 뷰 복귀 |
| 37 | `/calendar` | `CalendarPage` | FullCalendar 기반 월간/주간/일간 캘린더 | `useQuery(['events']), 한국 천문연구원 음력` | `domain.personal_tools` | provisional | RouteFallback, 오프라인 표시 |
| 38 | `*` | `Navigate(to=/chat)` | 미정의 경로 fallback redirect | `N/A` | `core.system_routing` | provisional | /chat 리다이렉트 |

---

## 3. 핵심 서브시스템 및 계약 감사

### 3.1 API Client 인벤토리 (`client.ts` 및 `stream.ts`, `useConnection.ts`)
- **총 엔드포인트 수집**: 184개 (미매칭 원장: 50개)
- **HTTP Method 정확 판정**:
  - 함수 스코프 내 AST 호출 블록의 `method` 속성 유무를 정밀 해석.
  - `getSession`: `GET /api/sessions/${id}` (GET 정상 판정)
  - `getArtifact`: `GET /api/artifacts/${id}` (GET 정상 판정)
  - `getOpsIssue`: `GET /api/ops-logs/issues/${encodeURIComponent(id)}` (GET 정상 판정)
- **추가 소스 통합**:
  - `src/api/stream.ts`: `fetchEventSource(apiUrl('/api/runs/${runId}/stream...'))` -> GET SSE 스트림 엔드포인트 수집 원장에 포함 (line 45).
  - `src/shell/useConnection.ts`: `fetchHealth` -> GET 30초 주기 heartbeat 수집 원장에 포함 (line 25).
  - `src/api/client.ts`: `refreshAccessToken` -> POST 직접 fetch 토큰 재발급 원장에 포함 (line 114).
  - `src/api/client.ts`: `publicShareRequest` -> `getPublicMeetingShare` 및 `downloadPublicMeetingShare` 원장에 포함.

### API callsites

Extraction coverage: `incomplete`; unresolved: 1.

| Source | Line | Function | Method | Endpoint | Backend module | Mapping |
|---|---:|---|---|---|---|---|
| src/api/client.ts | 114 | refreshAccessToken | POST | /api/auth/refresh | core.auth_session | provisional |
| src/api/client.ts | 195 | listNotifications | GET | /api/notifications | core.system_routing | provisional |
| src/api/client.ts | 197 | readNotification | PATCH | /api/notifications/${id}/read | core.system_routing | provisional |
| src/api/client.ts | 199 | readAllNotifications | POST | /api/notifications/read-all | core.system_routing | provisional |
| src/api/client.ts | 204 | previewMeetingShare | POST | /api/shares/preview | domain.shares_artifacts | provisional |
| src/api/client.ts | 205 | listMeetingShares | GET | /api/shares | domain.shares_artifacts | provisional |
| src/api/client.ts | 206 | publishMeetingShare | POST | /api/shares | domain.shares_artifacts | provisional |
| src/api/client.ts | 207 | republishMeetingShare | PUT | /api/shares/${id}/publication | domain.shares_artifacts | provisional |
| src/api/client.ts | 208 | revokeMeetingShare | DELETE | /api/shares/${id} | domain.shares_artifacts | provisional |
| src/api/client.ts | 209 | exportMeetingDocument | POST | /api/exports | domain.shares_artifacts | provisional |
| src/api/client.ts | 219 | getPublicMeetingShare | GET | /api/public/shares/${id} | domain.shares_artifacts | provisional |
| src/api/client.ts | 222 | downloadPublicMeetingShare | GET | /api/public/shares/${id}/download | domain.shares_artifacts | provisional |
| src/api/client.ts | 226 | login | POST | /api/auth/login | core.auth_session | provisional |
| src/api/client.ts | 232 | logout | POST | /api/auth/logout | core.auth_session | provisional |
| src/api/client.ts | 239 | me | GET | /api/auth/me | core.auth_session | provisional |
| src/api/client.ts | 243 | listWeatherBriefings | GET | /api/weather/briefings | domain.personal_tools | provisional |
| src/api/client.ts | 245 | getWeatherBriefing | GET | /api/weather/briefings/${day} | domain.personal_tools | provisional |
| src/api/client.ts | 247 | generateWeatherBriefing | POST | /api/weather/generate | domain.personal_tools | provisional |
| src/api/client.ts | 251 | listWorkspaces | GET | /api/workspaces | core.workspaces_transfers | provisional |
| src/api/client.ts | 253 | createWorkspace | POST | /api/workspaces | core.workspaces_transfers | provisional |
| src/api/client.ts | 254 | getWorkspace | GET | /api/workspaces/${id} | core.workspaces_transfers | provisional |
| src/api/client.ts | 256 | getDefaultWorkspace | GET | /api/workspaces/default | core.workspaces_transfers | provisional |
| src/api/client.ts | 258 | patchWorkspace | PATCH | /api/workspaces/${id} | core.workspaces_transfers | provisional |
| src/api/client.ts | 260 | listFiles | GET | /api/workspaces/${id}/files | core.workspaces_transfers | provisional |
| src/api/client.ts | 262 | searchFiles | GET | /api/workspaces/${id}/search | core.workspaces_transfers | provisional |
| src/api/client.ts | 266 | listSessions | GET | /api/sessions | core.auth_session | provisional |
| src/api/client.ts | 267 | createSession | POST | /api/sessions | core.auth_session | provisional |
| src/api/client.ts | 268 | getSession | GET | /api/sessions/${id} | core.auth_session | provisional |
| src/api/client.ts | 270 | ensureDesk | POST | /api/desk | core.auth_session | provisional |
| src/api/client.ts | 272 | patchSession | PATCH | /api/sessions/${id} | core.auth_session | provisional |
| src/api/client.ts | 276 | activeTasks | GET | /api/tasks/active | domain.pms_tasks | provisional |
| src/api/client.ts | 278 | taskLibrary | GET | /api/tasks/library | domain.pms_tasks | provisional |
| src/api/client.ts | 279 | searchTasks | GET | /api/tasks/search | domain.pms_tasks | provisional |
| src/api/client.ts | 280 | taskTree | GET | /api/tasks/${sessionId}/tree | domain.pms_tasks | provisional |
| src/api/client.ts | 285 | listRuns | GET | /api/sessions/${sessionId}/runs | core.auth_session | provisional |
| src/api/client.ts | 287 | listFailedRuns | GET | /api/runs/failed | exec.run_engine | provisional |
| src/api/client.ts | 289 | createRun | POST | /api/sessions/${sessionId}/runs | core.auth_session | provisional |
| src/api/client.ts | 290 | getRun | GET | /api/runs/${id} | exec.run_engine | provisional |
| src/api/client.ts | 291 | cancelRun | POST | /api/runs/${id}/cancel | exec.run_engine | provisional |
| src/api/client.ts | 293 | putDeskFeedback | POST | /api/runs/${runId}/desk-feedback | exec.run_engine | provisional |
| src/api/client.ts | 295 | runEvents | GET | /api/runs/${id}/events | exec.run_engine | provisional |
| src/api/client.ts | 297 | runDiff | GET | /api/runs/${id}/diff | exec.run_engine | provisional |
| src/api/client.ts | 302 | listArtifacts | GET | /api/artifacts | domain.shares_artifacts | provisional |
| src/api/client.ts | 303 | getArtifact | GET | /api/artifacts/${id} | domain.shares_artifacts | provisional |
| src/api/client.ts | 305 | registerArtifact | POST | /api/artifacts | domain.shares_artifacts | provisional |
| src/api/client.ts | 308 | resolveArtifactLink | POST | /api/artifacts/resolve | domain.shares_artifacts | provisional |
| src/api/client.ts | 310 | moveArtifact | PATCH | /api/artifacts/${id} | domain.shares_artifacts | provisional |
| src/api/client.ts | 311 | deleteArtifact | DELETE | /api/artifacts/${id} | domain.shares_artifacts | provisional |
| src/api/client.ts | 313 | retryPreview | POST | /api/artifacts/${id}/preview/retry | domain.shares_artifacts | provisional |
| src/api/client.ts | 315 | contentBlob | GET | /api/artifacts/${id}/content | domain.shares_artifacts | provisional |
| src/api/client.ts | 319 | downloadLink | POST | /api/artifacts/${id}/download-link | domain.shares_artifacts | provisional |
| src/api/client.ts | 329 | uploadFile | POST | /api/uploads | unknown | unknown |
| src/api/client.ts | 333 | systemInfo | GET | /api/system/info | core.system_routing | provisional |
| src/api/client.ts | 334 | stopAll | POST | /api/system/stop-all | core.system_routing | provisional |
| src/api/client.ts | 337 | listAccounts | GET | /api/accounts | core.accounts_quota | provisional |
| src/api/client.ts | 338 | createAccount | POST | /api/accounts | core.accounts_quota | provisional |
| src/api/client.ts | 340 | patchAccount | PATCH | /api/accounts/${id} | core.accounts_quota | provisional |
| src/api/client.ts | 341 | deleteAccount | DELETE | /api/accounts/${id} | core.accounts_quota | provisional |
| src/api/client.ts | 344 | reorderAccounts | POST | /api/accounts/reorder | core.accounts_quota | provisional |
| src/api/client.ts | 346 | clearAccountCooldown | POST | /api/accounts/${id}/cooldown/clear | core.accounts_quota | provisional |
| src/api/client.ts | 392 | startTransfer | POST | /api/transfers | unknown | unknown |
| src/api/client.ts | 393 | transferStatus | GET | /api/transfers/${id} | unknown | unknown |
| src/api/client.ts | 395 | putTransferChunk | PUT | /api/transfers/${id} | unknown | unknown |
| src/api/client.ts | 396 | completeTransfer | POST | /api/transfers/${id}/complete | unknown | unknown |
| src/api/client.ts | 397 | cancelTransfer | DELETE | /api/transfers/${id} | unknown | unknown |
| src/api/client.ts | 398 | transferFolders | GET | /api/transfers/folders | unknown | unknown |
| src/api/client.ts | 399 | makeTransferFolder | POST | /api/transfers/folders | unknown | unknown |
| src/api/client.ts | 400 | listTransferTokens | GET | /api/transfer-tokens | unknown | unknown |
| src/api/client.ts | 401 | createTransferToken | POST | /api/transfer-tokens | unknown | unknown |
| src/api/client.ts | 402 | revokeTransferToken | DELETE | /api/transfer-tokens/${id} | unknown | unknown |
| src/api/client.ts | 493 | meetingProviders | GET | /api/meetings/providers | domain.meetings_stt | provisional |
| src/api/client.ts | 495 | listMeetings | GET | /api/meetings | domain.meetings_stt | provisional |
| src/api/client.ts | 496 | createMeeting | POST | /api/meetings | domain.meetings_stt | provisional |
| src/api/client.ts | 497 | getMeeting | GET | /api/meetings/${id} | domain.meetings_stt | provisional |
| src/api/client.ts | 499 | patchMeeting | PATCH | /api/meetings/${id} | domain.meetings_stt | provisional |
| src/api/client.ts | 500 | deleteMeeting | DELETE | /api/meetings/${id} | domain.meetings_stt | provisional |
| src/api/client.ts | 502 | putMeetingTranscript | POST | /api/meetings/${id}/transcript | domain.meetings_stt | provisional |
| src/api/client.ts | 504 | requestMeetingTranscription | POST | /api/meetings/${id}/transcribe | domain.meetings_stt | provisional |
| src/api/client.ts | 505 | analyzeMeeting | POST | /api/meetings/${id}/analyze | domain.meetings_stt | provisional |
| src/api/client.ts | 506 | cancelMeetingAnalysis | POST | /api/meetings/${id}/analyze/cancel | domain.meetings_stt | provisional |
| src/api/client.ts | 508 | patchMeetingItem | PATCH | /api/meetings/${id}/items/${itemId} | domain.meetings_stt | provisional |
| src/api/client.ts | 510 | resolveMeetingProposal | POST | /api/meetings/${id}/proposals/${proposalId}/${action} | domain.meetings_stt | provisional |
| src/api/client.ts | 569 | listMeetingSttUtterances | GET | /api/meetings/${id}/stt/${jobId}/utterances | domain.meetings_stt | provisional |
| src/api/client.ts | 572 | retranscribeMeeting | POST | /api/meetings/${id}/retranscribe | domain.meetings_stt | provisional |
| src/api/client.ts | 579 | probeRetranscribe | POST | /api/meetings/${id}/retranscribe | domain.meetings_stt | provisional |
| src/api/client.ts | 589 | listMeetingSttJobs | GET | /api/meetings/${id}/stt | domain.meetings_stt | provisional |
| src/api/client.ts | 591 | listMeetingSttSegments | GET | /api/meetings/${id}/stt/${jobId}/segments | domain.meetings_stt | provisional |
| src/api/client.ts | 667 | assistantStatus | GET | /api/assistant/status | unknown | unknown |
| src/api/client.ts | 676 | getInstructions | GET | /api/assistant/instructions | unknown | unknown |
| src/api/client.ts | 678 | putInstructions | PUT | /api/assistant/instructions | unknown | unknown |
| src/api/client.ts | 680 | resetInstructions | DELETE | /api/assistant/instructions | unknown | unknown |
| src/api/client.ts | 682 | listMemories | GET | /api/assistant/memories | unknown | unknown |
| src/api/client.ts | 684 | addMemory | POST | /api/assistant/memories | unknown | unknown |
| src/api/client.ts | 686 | patchMemory | PATCH | /api/assistant/memories/${id} | unknown | unknown |
| src/api/client.ts | 687 | deleteMemory | DELETE | /api/assistant/memories/${id} | unknown | unknown |
| src/api/client.ts | 688 | listEntities | GET | /api/assistant/entities | unknown | unknown |
| src/api/client.ts | 689 | getEntity | GET | /api/assistant/entities/${id} | unknown | unknown |
| src/api/client.ts | 691 | addEntity | POST | /api/assistant/entities | unknown | unknown |
| src/api/client.ts | 693 | patchEntity | PATCH | /api/assistant/entities/${id} | unknown | unknown |
| src/api/client.ts | 694 | deleteEntity | DELETE | /api/assistant/entities/${id} | unknown | unknown |
| src/api/client.ts | 696 | addRelation | POST | /api/assistant/relations | unknown | unknown |
| src/api/client.ts | 697 | listSources | GET | /api/assistant/sources | unknown | unknown |
| src/api/client.ts | 699 | addSource | POST | /api/assistant/sources | unknown | unknown |
| src/api/client.ts | 700 | reindexSource | POST | /api/assistant/sources/${id}/reindex | unknown | unknown |
| src/api/client.ts | 701 | deleteSource | DELETE | /api/assistant/sources/${id} | unknown | unknown |
| src/api/client.ts | 702 | listTools | GET | /api/assistant/tools | unknown | unknown |
| src/api/client.ts | 783 | libOverview | GET | /api/assistant/library/overview | unknown | unknown |
| src/api/client.ts | 784 | libTree | GET | /api/assistant/library/tree | unknown | unknown |
| src/api/client.ts | 785 | libSourceDocuments | GET | /api/assistant/library/sources/${id}/documents | unknown | unknown |
| src/api/client.ts | 786 | libDocumentChunks | GET | /api/assistant/library/documents/${id}/chunks | unknown | unknown |
| src/api/client.ts | 787 | libGraph | GET | /api/assistant/library/graph | unknown | unknown |
| src/api/client.ts | 788 | libEntity | GET | /api/assistant/library/entities/${id} | unknown | unknown |
| src/api/client.ts | 789 | libQuality | GET | /api/assistant/library/quality | unknown | unknown |
| src/api/client.ts | 791 | libSearchLab | POST | /api/assistant/library/search-lab | unknown | unknown |
| src/api/client.ts | 793 | libItem | GET | /api/assistant/library/items/${kind}/${id} | unknown | unknown |
| src/api/client.ts | 857 | getOverride | GET | /api/accounts/override | core.accounts_quota | provisional |
| src/api/client.ts | 859 | putOverride | PUT | /api/accounts/override | core.accounts_quota | provisional |
| src/api/client.ts | 861 | deleteOverride | DELETE | /api/accounts/override | core.accounts_quota | provisional |
| src/api/client.ts | 862 | accountUsage | GET | /api/accounts/usage | core.accounts_quota | provisional |
| src/api/client.ts | 864 | singleAccountUsage | GET | /api/accounts/${id}/usage | core.accounts_quota | provisional |
| src/api/client.ts | 866 | refreshSingleAccountUsage | POST | /api/accounts/${id}/usage/refresh | core.accounts_quota | provisional |
| src/api/client.ts | 867 | qwenUsage | GET | /api/accounts/qwen-usage | core.accounts_quota | provisional |
| src/api/client.ts | 893 | qwenFlashUsage | GET | /api/accounts/qwen-flash-usage | core.accounts_quota | provisional |
| src/api/client.ts | 896 | codexReset | POST | /api/accounts/${id}/codex-reset | core.accounts_quota | provisional |
| src/api/client.ts | 957 | systemResources | GET | /api/system/resources | core.system_routing | provisional |
| src/api/client.ts | 981 | systemResourceHistory | GET | /api/system/resources/history | core.system_routing | provisional |
| src/api/client.ts | 1064 | listMemos | GET | /api/memos | domain.memory_ontology | provisional |
| src/api/client.ts | 1066 | listMemoTags | GET | /api/memos/tags | domain.memory_ontology | provisional |
| src/api/client.ts | 1068 | createMemo | POST | /api/memos | domain.memory_ontology | provisional |
| src/api/client.ts | 1069 | getMemo | GET | /api/memos/${id} | domain.memory_ontology | provisional |
| src/api/client.ts | 1070 | patchMemo | PATCH | /api/memos/${id} | domain.memory_ontology | provisional |
| src/api/client.ts | 1071 | deleteMemo | DELETE | /api/memos/${id} | domain.memory_ontology | provisional |
| src/api/client.ts | 1072 | listMemoCategories | GET | /api/memo-categories | unknown | unknown |
| src/api/client.ts | 1074 | createMemoCategory | POST | /api/memo-categories | unknown | unknown |
| src/api/client.ts | 1076 | patchMemoCategory | PATCH | /api/memo-categories/${id} | unknown | unknown |
| src/api/client.ts | 1078 | orderMemoCategories | PUT | /api/memo-categories/order | unknown | unknown |
| src/api/client.ts | 1079 | deleteMemoCategory | DELETE | /api/memo-categories/${id} | unknown | unknown |
| src/api/client.ts | 1082 | listEvents | GET | /api/calendar/events | domain.personal_tools | provisional |
| src/api/client.ts | 1084 | listHolidays | GET | /api/calendar/holidays | domain.personal_tools | provisional |
| src/api/client.ts | 1085 | getEvent | GET | /api/calendar/events/${id} | domain.personal_tools | provisional |
| src/api/client.ts | 1086 | createEvent | POST | /api/calendar/events | domain.personal_tools | provisional |
| src/api/client.ts | 1088 | patchEvent | PATCH | /api/calendar/events/${id} | domain.personal_tools | provisional |
| src/api/client.ts | 1089 | deleteEvent | DELETE | /api/calendar/events/${id} | domain.personal_tools | provisional |
| src/api/client.ts | 1094 | listPmsProjects | GET | /api/pms/projects | domain.pms_tasks | provisional |
| src/api/client.ts | 1095 | createPmsProject | POST | /api/pms/projects | domain.pms_tasks | provisional |
| src/api/client.ts | 1096 | getPmsProject | GET | /api/pms/projects/${id} | domain.pms_tasks | provisional |
| src/api/client.ts | 1098 | patchPmsProject | PATCH | /api/pms/projects/${id} | domain.pms_tasks | provisional |
| src/api/client.ts | 1099 | deletePmsProject | DELETE | /api/pms/projects/${id} | domain.pms_tasks | provisional |
| src/api/client.ts | 1100 | getPmsDashboard | GET | /api/pms/projects/${id}/dashboard | domain.pms_tasks | provisional |
| src/api/client.ts | 1101 | listPmsTasks | GET | /api/pms/projects/${id}/tasks | domain.pms_tasks | provisional |
| src/api/client.ts | 1103 | createPmsTask | POST | /api/pms/projects/${id}/tasks | domain.pms_tasks | provisional |
| src/api/client.ts | 1105 | patchPmsTask | PATCH | /api/pms/tasks/${id} | domain.pms_tasks | provisional |
| src/api/client.ts | 1107 | movePmsTask | POST | /api/pms/tasks/${id}/move | domain.pms_tasks | provisional |
| src/api/client.ts | 1108 | deletePmsTask | DELETE | /api/pms/tasks/${id} | domain.pms_tasks | provisional |
| src/api/client.ts | 1109 | listPmsMilestones | GET | /api/pms/projects/${id}/milestones | domain.pms_tasks | provisional |
| src/api/client.ts | 1111 | createPmsMilestone | POST | /api/pms/projects/${id}/milestones | domain.pms_tasks | provisional |
| src/api/client.ts | 1113 | patchPmsMilestone | PATCH | /api/pms/milestones/${id} | domain.pms_tasks | provisional |
| src/api/client.ts | 1114 | deletePmsMilestone | DELETE | /api/pms/milestones/${id} | domain.pms_tasks | provisional |
| src/api/client.ts | 1116 | listPmsNotes | GET | /api/pms/projects/${id}/notes | domain.pms_tasks | provisional |
| src/api/client.ts | 1117 | createPmsNote | POST | /api/pms/projects/${id}/notes | domain.pms_tasks | provisional |
| src/api/client.ts | 1118 | patchPmsNote | PATCH | /api/pms/notes/${id} | domain.pms_tasks | provisional |
| src/api/client.ts | 1119 | deletePmsNote | DELETE | /api/pms/notes/${id} | domain.pms_tasks | provisional |
| src/api/client.ts | 1125 | listChecklist | GET | /api/checklists | domain.pms_tasks | provisional |
| src/api/client.ts | 1126 | listChecklistTemplates | GET | /api/checklists/templates | domain.pms_tasks | provisional |
| src/api/client.ts | 1127 | createChecklistItem | POST | /api/checklists/items | domain.pms_tasks | provisional |
| src/api/client.ts | 1128 | patchChecklistItem | PATCH | /api/checklists/items/${id} | domain.pms_tasks | provisional |
| src/api/client.ts | 1129 | deleteChecklistItem | DELETE | /api/checklists/items/${id} | domain.pms_tasks | provisional |
| src/api/client.ts | 1130 | createChecklistTemplate | POST | /api/checklists/templates | domain.pms_tasks | provisional |
| src/api/client.ts | 1131 | patchChecklistTemplate | PATCH | /api/checklists/templates/${id} | domain.pms_tasks | provisional |
| src/api/client.ts | 1132 | deleteChecklistTemplate | DELETE | /api/checklists/templates/${id} | domain.pms_tasks | provisional |
| src/api/client.ts | 1136 | knowledgeStatus | GET | /api/assistant/knowledge/status | unknown | unknown |
| src/api/client.ts | 1137 | knowledgeAssertions | GET | /api/assistant/knowledge/assertions | unknown | unknown |
| src/api/client.ts | 1138 | knowledgeRecall | POST | /api/assistant/knowledge/recall | unknown | unknown |
| src/api/client.ts | 1139 | knowledgeReview | POST | /api/assistant/knowledge/assertions/${id}/reviews | unknown | unknown |
| src/api/client.ts | 1140 | knowledgeCorrect | POST | /api/assistant/knowledge/assertions/${id}/corrections | unknown | unknown |
| src/api/client.ts | 1141 | knowledgeAssertion | GET | /api/assistant/knowledge/assertions/${id} | unknown | unknown |
| src/api/client.ts | 1146 | listOpsIssues | GET | /api/ops-logs/issues | ops.monitoring | provisional |
| src/api/client.ts | 1147 | getOpsIssue | GET | /api/ops-logs/issues/${id} | ops.monitoring | provisional |
| src/api/client.ts | 1149 | patchOpsIssue | PATCH | /api/ops-logs/issues/${id} | ops.monitoring | provisional |
| src/api/client.ts | 1150 | opsTrend | GET | /api/ops-logs/trend | ops.monitoring | provisional |
| src/api/client.ts | 1151 | listOpsDeploys | GET | /api/ops-logs/deploys | ops.monitoring | provisional |
| src/api/client.ts | 1152 | getOpsDeployLog | GET | /api/ops-logs/deploys/${id}/log | ops.monitoring | provisional |
| src/api/stream.ts | 45 | subscribeRun | GET | /api/runs/${runId}/stream | exec.run_engine | provisional |
| src/shell/useConnection.ts | 25 | fetchHealth | GET | /api/health | core.system_routing | provisional |
- Unresolved: `src/api/stream.ts`: url is a bare parameter of the enclosing function (not recognised as a request wrapper)
