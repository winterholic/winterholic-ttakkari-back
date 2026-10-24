# 보관함 분류 및 의도 분리 체계 (Library Intent & Classification)

## 1. 개요 및 배경

사용자의 핵심 요구:
> "내 보관함은 내가 의도적으로 넣은 파일만 모아두는 곳이어야 한다. AI가 결과물을 만들거나 공유했다는 이유만으로 자동으로 들어오면 안 된다."

기존 구조에서는 `Artifact.area`('files' vs 'agent')가 제작 출처와 혼재되어, AI가 `share_file`을 부르거나 대화 세션 outbox 파일이 생성되면 무조건 'files'로 올라가는 문제가 있었습니다.

이를 해결하기 위해 다음 개념을 완전히 분리했습니다:
1. **보관 상태 (area)**: `files` (내 보관함) vs `agent` (AI 작업 파일) vs `all` (전체)
2. **실제 출처 (origin)**: `user` (사용자 제공) vs `agent` (AI 생성·수정) vs `external` (외부 수집) vs `unknown` (확인 불가)
3. **보관 사유 (keep_reason)**: `direct_upload` (직접 업로드), `direct_register` (직접 등록), `user_button` (화면 버튼 보관), `chat_request` (대화 보관 요청), `None` (미보관)
4. **보관 근거 (keep_evidence)**: 대화 보관 요청 시 사용자가 실제로 한 말의 인용(quote), 그 발화를 담은 메시지 ID·출처·작성 시각, 세션 ID, 보관을 실행한 Run ID
5. **탐색용 카테고리 (category)**: 뷰어용 `kind`와 분리된 파일 탐색 및 필터 분류

---

## 2. 동작 규칙 및 생애주기

| 상황 | 내 보관함 (`area`) | 실제 출처 (`origin`) | 보관 사유 (`keep_reason`) |
|---|---|---|---|
| 사용자 직접 업로드 (`/api/uploads`) | `files` | `user` | `direct_upload` |
| 워크스페이스 직접 등록 (`/api/artifacts` POST) | `files` | `unknown` (등록은 보관 의도이지 제작 증거 아님) | `direct_register` |
| 화면에서 '내 보관함에 보관' 클릭 (`PATCH /api/artifacts/{id}`) | `files` | 기존 유지 | `user_button` |
| 대화에서 "만들어서 보관함에 넣어줘" 요청 (`keep_artifact`) | `files` | `agent` | `chat_request` (실제 사용자 발화 인용·메시지 ID·세션·Run·시각 근거 기록) |
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
- `keep_artifact(path: str, quote: str)`:
  - 사용자가 대화에서 보관을 요청했을 때("만들어서 보관함에 넣어줘" 등) 내 보관함(`area='files'`)에 영구 보관합니다. 에이전트가 요청 여부를 스스로 판단해 호출하지 않습니다.
  - `quote` 는 사용자가 보관을 요청한 말을 문장 하나(또는 한 줄) 그대로 인용한 것입니다. 서버는 모델에 묻거나 의도를 추론하지 않고, 아래 세 가지 문자열 규칙만 확인합니다. 하나라도 어긋나면 거부하고 아무것도 바꾸지 않습니다.
  - **1. 인용 단위(`422 quote_not_found`)**: 이 세션에 실제로 저장된 사용자 발화의 한 문장(또는 한 줄)과 정규화(공백·유니코드·대소문자·끝 문장부호 무시) 후 정확히 같아야 합니다. 단어나 문장의 일부는 안 됩니다. 문장 경계는 줄바꿈, `.` `!` `?`(뒤에 공백이나 끝이 올 때만, `보고서.md` 의 점은 경계가 아님), 한국어 종결 어미(다·요·죠·까·줘·라·자·네) 뒤 공백입니다. 공백을 뺀 8자 이상이어야 합니다. 여러 문장을 이어 붙인 인용은 한 줄 전체가 아니면 안 됩니다.
  - **2. 부정 방어(`422 quote_negated`)**: 인용 문장에 보관 부정 표현(`보관하지`, `보관 말`, `보관 안`, `넣지 마/말`, `저장하지 마/말`, `올리지 마`, 보관 필요 없음, don't/do not/never ... keep/save/store 등)이 있으면 거부합니다. 의도를 추론하는 것이 아니라 명백한 반대 의사를 막는 심층 방어입니다. 정상 요청이 막히는 쪽으로 틀리는 것은 감수하며, 그때는 사용자가 앱의 '내 보관함에 보관' 버튼으로 직접 보관하면 됩니다.
  - **3. 대상 파일 결속(`422 quote_not_bound`)**: 아래 둘 중 하나여야 합니다.
    - `filename`: 인용 문장에 대상 파일명 전체, 확장자를 뺀 이름(3자 이상일 때), 또는 `상위폴더/파일명` 이 들어 있다(대소문자·공백 정규화).
    - `same_run`: 대상 파일을 그 인용 발화 뒤에 이 세션의 Run, 또는 이 세션이 띄운 작업 세션(`parent_id` 가 이 세션)의 Run 이 만들었고 파일 수정 시각도 발화 뒤다. 만든 Run 은 Run outbox 경로(`outbox/<run id>/…`)나 등록된 행의 `run_id` 로 확인합니다. "만들어서 보관함에 넣어줘" 같은 문장은 이 경우에만 파일명 없이 통과합니다.
    - 기획 세션에서 작업 세션이 만든 파일은 작업 세션이 이 세션의 자식이라 `same_run` 이 성립합니다. 다른 세션의 Run 이 만든 파일, 발화 앞 Run 이 만든 파일, 어떤 Run outbox 에도 없는 작업공간 파일은 사용자 문장에 파일명이 있어야 합니다.
  - 사용자 메시지는 별도 테이블이 아니라 `Run.prompt` 로 저장됩니다. 인정하는 발화는 둘뿐입니다.
    - `message`: 이 세션에 사용자가 직접 보낸 메시지(`origin=user` Run 의 prompt).
    - `run_prompt`: 접수가 기획 세션에 넘긴 사용자 요청(`origin=desk` Run 의 `[접수에서 넘긴 사용자 요청]` 본문). 같은 Run 의 `[최근 대화 — 참고만]` 이하와 `따까리:` 응답은 사용자 발화로 보지 않습니다.
  - 찾는 범위는 호출한 Run 이후에 생긴 메시지를 뺀, 그 시점까지의 최근 사용자·접수 Run 20개입니다. 다른 세션의 메시지, 기획이 작업 세션에 쓴 지시(`origin=orchestrator`), 작업 보고(`report`)는 근거가 되지 않습니다. 작업 세션은 사용자 발화가 없으므로 기획 세션이 직접 보관해야 합니다.
  - `keep_evidence` 에는 `message_id`(발화를 담은 Run ID), `message_source`(`message`|`run_prompt`), `message_origin`, `message_at`, `quote`, `quote_sentence`(정규화한 인용 문장), `binding`(`filename`|`same_run`), `binding_detail`(맞은 파일명 표기 또는 만든 Run ID), `user_message`(발화 발췌), `session_id`, `run_id`(보관을 실행한 Run), `kept_at` 이 남습니다.
  - 이미 내 보관함에 있는 파일은 처음의 보관 사유·근거를 덮어쓰지 않고 그대로 돌려줍니다(중복 호출해도 행은 1개). 사용자가 앱에서 뺀 파일은 그 뒤에 한 새 요청이 있어야 다시 보관됩니다(`409 removed_after_request`).
- `set_artifact_area(artifact_id: str, area: Literal["agent"])`:
  - AI 작업 파일('agent')로 빼기만 가능합니다. 내 보관함('files')으로의 임의 승격은 409 거부되며(keep_artifact 사용 필수), 사용자가 보관한 파일(`keep_reason` 존재)이나 직접 올린 파일은 에이전트가 뺄 수 없습니다(403 거부).

---

## 5. 기존 데이터 정리 및 마이그레이션 (0068_library_intent)

- **유지/이동 기준**: 직접 업로드(`source='upload'`)만 `files` 에 남기고 `origin='user'`, `keep_reason='direct_upload'` 를 부여합니다. 그 외 `files` 행(AI 산출물·링크 등록 등)은 `agent` 로 옮기고, `origin` 은 `agent_output`/`agent_modified` 면 `agent`, 그 밖은 `unknown` 으로 채웁니다.
- **운영 DB 사본 드라이런(2026-10-25, 임시 클러스터에 복원, 운영 DB는 읽기 전용 pg_dump 만 사용)**:
  - 사본: 17,243행(스키마 0067), 삭제 안 된 행 17,234.
  - 전: `files/upload` 81, `files/agent_output` 1,869, `agent/agent_output` 11,380, `agent/agent_modified` 3,913.
  - 후(0068): `files/upload/user/direct_upload` 81 유지, `agent/agent_output/agent` 13,249(1,869 이동 포함), `agent/agent_modified/agent` 3,913. 백업 표 `artifact_area_backup_0068` 17,243행.
  - 파일명·MIME 기반 `category` 백필: archive 126, audio 129, code 8,769, document 2,722, image 3,275, other 2,064, presentation 3, spreadsheet 88, video 67.
  - 원본 경로·저장 사본 경로·sha256·세션/Run 연결·크기·생성/삭제 시각 등 변경 대상이 아닌 컬럼의 전체 행 체크섬이 upgrade 전후 같습니다. 대표 12건(이동 6, 유지 3, 기존 agent 3)은 저장 파일이 존재하고 DB sha256 과 파일 sha256 이 같습니다.
  - downgrade 로 `area×source` 집계와 체크섬이 사전 상태와 같아지고 새 컬럼 4개와 백업 표가 사라집니다. 재 upgrade 도 같은 결과입니다.
- **재실행 안전**: 같은 migration 을 `alembic_version` 밖에서 다시 실행하면 새 컬럼이 이미 있는 것을 보고 데이터 정리를 건너뜁니다. 그 사이 사용자가 보관한 파일이 `agent` 로 내려가지 않습니다. downgrade 도 이미 내려간 상태에서 오류 없이 지나갑니다.
- **온라인 배포**: 0068 은 기본값 있는 컬럼 추가와 행 이동뿐이라 `generation-contract.json` 의 `online_migration_from` 에 0067 을 넣었습니다. 전환 중 이전 세대가 승격한 `files` 행은 `keep_reason` 이 없고, 목록 API 는 `files` 탭에서 `keep_reason` 이 있는 행만 보여 줍니다.
