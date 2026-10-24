# 보관함 분류 및 의도 분리 체계 (Library Intent & Classification)

## 1. 개요 및 배경

사용자의 핵심 요구:
> "내 보관함은 내가 의도적으로 넣은 파일만 모아두는 곳이어야 한다. AI가 결과물을 만들거나 공유했다는 이유만으로 자동으로 들어오면 안 된다."

기존 구조에서는 `Artifact.area`('files' vs 'agent')가 제작 출처와 혼재되어, AI가 `share_file`을 부르거나 대화 세션 outbox 파일이 생성되면 무조건 'files'로 올라가는 문제가 있었습니다.

이를 해결하기 위해 다음 개념을 완전히 분리했습니다:
1. **보관 상태 (area)**: `files` (내 보관함) vs `agent` (AI 작업 파일) vs `all` (전체)
2. **실제 출처 (origin)**: `user` (사용자 제공) vs `agent` (AI 생성·수정) vs `external` (외부 수집) vs `unknown` (확인 불가)
3. **보관 사유 (keep_reason)**: `direct_upload` (직접 업로드), `direct_register` (직접 등록), `user_button` (화면 버튼 보관), `chat_request` (대화 보관 요청), `None` (미보관)
4. **보관 근거 (keep_evidence)**: 대화 보관 요청 시의 요청 지시문 원문, 세션 ID, Run ID 메타데이터
5. **탐색용 카테고리 (category)**: 뷰어용 `kind`와 분리된 파일 탐색 및 필터 분류

---

## 2. 동작 규칙 및 생애주기

| 상황 | 내 보관함 (`area`) | 실제 출처 (`origin`) | 보관 사유 (`keep_reason`) |
|---|---|---|---|
| 사용자 직접 업로드 (`/api/uploads`) | `files` | `user` | `direct_upload` |
| 워크스페이스 직접 등록 (`/api/artifacts` POST) | `files` | `unknown` (등록은 보관 의도이지 제작 증거 아님) | `direct_register` |
| 화면에서 '내 보관함에 보관' 클릭 (`PATCH /api/artifacts/{id}`) | `files` | 기존 유지 | `user_button` |
| 대화에서 "만들어서 보관함에 넣어줘" 요청 (`keep_artifact`) | `files` | `agent` | `chat_request` (지시문·세션·Run·메시지ID·요청시각 근거 기록) |
| AI 파일 생성·수정·자동 수집 (outbox 수집) | `agent` | `agent` | `None` |
| AI 자발적 결과물 전달 (`share_file`) | `agent` | `agent` | `None` |
| 대화 링크 열기 / 다운로드 링크 발급 / 뷰어 열람 (`resolve`) | `agent` | `unknown` (비-outbox) / `agent` (outbox) | `None` |
| 사용자가 '내 보관함에서 빼기' 클릭 (`PATCH /api/artifacts/{id}`) | `agent` | 기존 유지 | `None` (근거 제거) |
| **보관함에서 뺀 파일에 대한 AI 재공유/자동 수집** | **`agent` 유지 (절대 files로 재유입되지 않음)** | 기존 유지 | `None` |

- **'내 보관함에서 빼기'와 '파일 삭제'의 구분**:
  - '내 보관함에서 빼기': 파일 및 관리 사본은 보존되고, `area`가 `agent`로 이동하며 `keep_reason`이 해제됩니다. 문구: '내 보관함에서 빼기 (파일은 AI 작업 파일에 남아요)'.
  - '파일 삭제': 사용자 확인 다이얼로그 후 파일 저장소 및 DB에서 삭제(`deleted_at` 설정).
  - 에이전트는 사용자가 보관한 파일(`keep_reason` 존재)을 임의로 뺄 수 없습니다(403 거부).

---

## 3. 탐색 카테고리(`category`) 및 애매 유형 판별 규칙

뷰어용 `kind` (Monaco, PDF.js, SheetJS, react-markdown, Video/Audio Player 등)와 탐색용 `category`를 완전히 분리했습니다.

### 카테고리 정의
- `document` (문서): `.md`, `.markdown`, `.txt`, `.pdf`, `.docx`, `.doc`, `.rtf`, `.odt`, `.pages`, `.hwp`
- `spreadsheet` (표·데이터): `.xlsx`, `.xls`, `.csv`, `.tsv`, `.ods`, `.parquet`, `.numbers`
- `presentation` (발표 자료): `.pptx`, `.ppt`, `.key`, `.odp`
- `image` (이미지): MIME `image/*` (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg`, `.bmp`, `.ico` 등)
- `video` (동영상): MIME `video/*` 및 `.mp4`, `.mov`, `.mkv`, `.webm`, `.avi`, `.wmv` 등
- `audio` (음성·오디오): MIME `audio/*` 및 `.mp3`, `.wav`, `.m4a`, `.flac`, `.ogg`, `.aif`, `.aiff`, `.aac` 등
- `code` (코드): 소스 코드, 설정, 빌드/시스템 로그, 구조화 데이터 (`.py`, `.ts`, `.tsx`, `.js`, `.json`, `.yaml`, `.yml`, `.toml`, `.sql`, `.sh`, `.html`, `.css`, `.log` 등)
- `archive` (압축파일): `.zip`, `.tar`, `.gz`, `.tgz`, `.bz2`, `.tbz2`, `.xz`, `.txz`, `.7z`, `.rar`
- `other` (기타): 그 외 바이너리 형식

### 애매 유형 판별 규칙
1. **일반 텍스트 (`.txt`)**:
   - 뷰어: `code` (Monaco 에디터로 깨짐 없이 열람)
   - 카테고리: `document` (사용자 관점에서는 메모/텍스트 문서이므로 문서 탭에서 탐색)
2. **웹 마크업 (`.html`, `.htm`)**:
   - 뷰어: `html` (보안 CSP 샌드박스 인라인 iframe 렌더링)
   - 카테고리: `code` (웹 소스 코드로 취급)
3. **JSON (`.json`)**:
   - 뷰어: `code` (Monaco 구문 하이라이팅)
   - 카테고리: `code` (설정 및 구조화 소스 데이터)
4. **시스템/빌드 로그 (`.log`)**:
   - 뷰어: `code` (Monaco 텍스트 뷰어)
   - 카테고리: `code` (실행/빌드 로그)
5. **CSV/TSV (`.csv`, `.tsv`)**:
   - 뷰어: `sheet` (SheetJS 그리드)
   - 카테고리: `spreadsheet` (표·데이터)
6. **압축 파일 (`.zip`, `.tar.gz` 등)**:
   - 뷰어: `other` (다운로드 유도)
   - 카테고리: `archive` (압축파일 필터)

---

## 4. MCP 도구 계약

- `share_file(path: str)`:
  - 대화에서 사용자가 열람/다운로드할 수 있도록 결과물을 전달합니다.
  - 파일은 `area='agent'`(AI 작업 파일)로 등록/유지되며, 내 보관함에 자동으로 보관되지 않습니다.
- `keep_artifact(path: str, instruction: str)`:
  - 사용자가 대화에서 명시적으로 보관을 요청했을 때("만들어서 보관함에 넣어줘", "내 보관함에 보관해줘" 등) 사용자의 내 보관함(`area='files'`)에 영구 보관합니다.
  - 요청 지시문(`instruction` 필수)을 시스템 근거(세션 ID, Run ID, 요청 메시지 ID, 사용자 프롬프트, 요청 시각)와 함께 `keep_evidence`로 영구 기록합니다. Run이 특정되지 않으면 409 거부합니다.
  - 사용자 명시적 보관 요청이 없는 한 호출하지 않습니다.
- `set_artifact_area(artifact_id: str, area: Literal["agent"])`:
  - AI 작업 파일('agent')로 빼기만 가능합니다. 내 보관함('files')으로의 임의 승격은 409 거부되며(keep_artifact 사용 필수), 사용자가 보관한 파일(`keep_reason` 존재)이나 직접 올린 파일은 에이전트가 뺄 수 없습니다(403 거부).

---

## 5. 기존 데이터 정리 및 마이그레이션 (0068_library_intent)

- **유지/이동 기준 (운영 DB 실측 기준)**:
  - `source = 'upload'` (직접 업로드): 81건 -> `area='files'` (내 보관함) 유지, `origin='user'`, `keep_reason='direct_upload'`
  - 그 외 (`source = 'agent_output'` 등): 1,857건 -> `area='agent'` (AI 작업 파일)로 일괄 이동, `origin='agent'`, `keep_reason=NULL`
  - 기존 `area='agent'`: 15,274건 -> `area='agent'` 유지
  - 총 결과물: 17,212건
- **데이터 보존**:
  - 원본 경로, 파일 저장 사본, SHA256 해시, 세션 연결, 다운로드 링크는 100% 온전히 유지됩니다.
  - 변경 전 상태는 `artifact_area_backup_0068` 테이블(id, source, area)에 보존되어 downgrade 시 100% 롤백 가능합니다.
