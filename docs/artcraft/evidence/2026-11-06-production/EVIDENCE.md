# ArtCraft 운영 환경 실환경 작업·수정·내보내기 품질 평가 보고서 및 재현 증거

- **평가 일시**: 2026-11-06 (한국시간 기준 2026-11-06T23:50~23:56)
- **운영 백엔드 Revision**: `c396370f` (배포 job: `a0af482c-ba0c-420a-a4cc-8cd351d2eee9`, status: `succeeded`)
- **실행 호스트**: Mac Studio (Apple Silicon M-series, macOS 27.0)
- **작업 워크스페이스**: `/Users/winterholic/development/projects/artcraft-workspace` (`43172ea9-9cd7-4a32-98f2-5d7435f7bd84`)
- **Worker Outbox**: `/Volumes/Backup/ttakkari-data/outbox/67bd167c-3ae4-47da-9b8a-727cfc976efb`

---

## 1. 운영 API 및 Task / Run / Artifact 전수 기록

### 1.1 사용 운영 API 목록
- `GET /api/artcraft/catalog`: 12개 카테고리, 4개 섹션, 13개 앱 설치 상태 및 작업 워크스페이스 조회
- `GET /api/artcraft/apps`: 13개 앱 릴리스/CLI/MCP/GUI 상태 및 헬스 체크
- `POST /api/artcraft/tasks`: 신규 창작 작업 생성 (세션 및 1차 라운드 Run 생성)
- `GET /api/artcraft/tasks`: 전체 작업 목록 및 라운드별 상태 조회
- `GET /api/artcraft/tasks/{id}`: 작업 상세, 라운드 메시지, 산출물(Artifact) 목록 조회
- `POST /api/artcraft/tasks/{id}/runs`: 이전 라운드 산출물 기반 후속 수정(이어서 작업) Run 생성
- `GET /api/artcraft/outputs`: 산출물 갤러리 조회
- `GET /api/accounts` & `PATCH /api/accounts/{id}`: 실행 엔진(claude) 활성화 상태 확인 및 패치

### 1.2 실행 작업 전수 매핑표

| 범주 (Category) | 담당 앱 | Task ID | 라운드 | Run ID | 상태 | 산출물 파일명 (Size, SHA256 앞 8자리) | Artifact ID |
|---|---|---|---|---|---|---|---|
| **문서 (doc)** | WordCraft | `fe3854a2-2d53-4ca4-be4f-037a7790f9bc` | R1 | `e39d2ba8-098b-45ee-a241-83f89e0cb262` | done | `weekly-engineering-report.docx` (8.7KB, `0938a826`)<br>`weekly-engineering-report.pdf` (47.5KB, `a5acbdbc`) | `fd0396f1-8faa...`<br>`65fd6414-93ae...` |
| **문서 v2 (수정)** | WordCraft | `fe3854a2-2d53-4ca4-be4f-037a7790f9bc` | R2 | `20899049-0ebb-44ca-a44d-a7d6fdb359f9` | done | `weekly-engineering-report-v2.docx` (9.1KB, `47b37a57`)<br>`weekly-engineering-report-v2.pdf` (54.8KB, `d928813e`) | `d2c8ba25-48cb...`<br>`60be12ab-d712...` |
| **시트 (sheet)** | GridCraft | `ad517d4f-75b0-48c9-90b2-2c1c7c8b1a31` | R1 | `0c6df1f2-c8ff-4759-9c77-f28f23681b7c` | done | `2026-Q3-dept-expense-settlement.xlsx` (4.6KB, `bd6d0d24`) | `2eb02a89-21bd...` |
| **시트 v2 (수정)** | GridCraft | `ad517d4f-75b0-48c9-90b2-2c1c7c8b1a31` | R2 | `4d8e6c78-8053-46e5-a16b-ef799ea1230a` | done | `2026-Q3-dept-expense-settlement-v2.xlsx` (4.7KB, `6db106ca`) | `d9b33e01-9b3c...` |
| **발표 (slides)** | DeckCraft | `e76aa925-3586-40a2-8c64-d494952f7c0d` | R1 | `fd38e20a-d20f-401b-a625-53a42a2d6976` | done | `ai-agent-strategy.pptx` (28.0KB, `605f6e04`)<br>`ai-agent-strategy.pdf` (574.9KB, `f726aa89`) | `c6c9fdad-827a...`<br>`bad6ea69-d848...` |
| **CAD (cad)** | CADCraft | `a8313357-c7b1-4f44-85d4-db54754c4eaa` | R1 | `9ea0b276-a425-4c78-b457-8a39193cff55` | done | `bracket.dxf` (41.1KB, `4c355b1e`)<br>`bracket.png` (345.2KB, `0ffc0947`) | `72e21441-6d88...`<br>`ef0da715-4bcb...` |
| **벡터 (vector)** | VectorCraft | `cf0a8a92-2cac-4808-b8ba-b3e558aaf3ad` | R1 | `9e78a6b6-db46-4e06-b39e-d1745a1cb030` | done | `minimal-tech-logo.svg` (1.0KB, `49fe9250`)<br>`minimal-tech-logo.png` (19.3KB, `ed73c5fd`) | `679b44c7-2d42...`<br>`7aac7817-bb30...` |
| **모션 (motion)** | EffectCraft | `0689733b-5e4c-4459-a0b8-a2ad57aff637` | R1 | `cbc8ab8c-81d7-4718-ac71-86ac4827f3fe` | done | `ttakkari-studio-2026-title-card.png` (76.7KB, `2c2ba689`)<br>`ttakkari-studio-2026-title-card.ecproj` (83.5KB, `a93c2333`) | `ca6be41d-a43a...`<br>`bcd22ecf-81b1...` |

---

## 2. 6가지 실제 작업 및 2개 이어서 수정 여정 검증

### 2.1 [작업 1] 문서 작성 및 1차 이어서 수정 여정 (WordCraft)
- **R1 지시**: `[ArtCraft QA] 한글 주간 업무 보고서를 작성하여 docx와 pdf로 저장해 주세요. 제목은 주간 엔지니어링 보고서, 내용은 개요, 핵심 성과, 다음 주 계획을 포함해 주세요.`
  - 결과: `weekly-engineering-report.docx` (1쪽 표 포함), `weekly-engineering-report.pdf` (1쪽) 생성 완료.
- **R2 이어서 수정 지시 (`use_previous: true`)**: `[ArtCraft QA] 맺음말(감사합니다) 및 주의사항 문단을 추가하고 제목을 주간 엔지니어링 보고서 (v2)로 변경하여 v2 docx 및 pdf로 저장해 주세요.`
  - 동작 방식: 에이전트가 `prev/r2/`에 전달된 1라운드 결과물을 참조하여 제목을 "주간 엔지니어링 보고서 (v2)"로 변경하고 "5. 주의사항", "맺음말"을 추가하여 2쪽 분량의 `weekly-engineering-report-v2.docx`, `weekly-engineering-report-v2.pdf`를 성공적으로 산출함.

### 2.2 [작업 2] 스프레드시트 수식 계산 및 2차 이어서 수정 여정 (GridCraft)
- **R1 지시**: `[ArtCraft QA] 2026년 3분기 부서별 비용 정산표를 작성해 주세요. 부서명, 인원수, 식대, 비품비, 소계 열을 만들고 =SUM(...) 수식으로 소계와 총합계를 계산하여 XLSX 파일로 저장해 주세요.`
  - 결과: 행별 소계 `=SUM(C2:D2)`~`=SUM(C6:D6)`, 7행 열 총합계 `=SUM(B2:B6)`~`=SUM(E2:E6)` 수식이 실제 Excel 수식 문법으로 정확히 삽입된 `2026-Q3-dept-expense-settlement.xlsx` 생성.
- **R2 이어서 수정 지시 (`use_previous: true`)**: `[ArtCraft QA] 총합계 행 아래에 부서별 평균(=AVERAGE(...)) 행을 추가하고, 파일명을 2026-Q3-dept-expense-settlement-v2.xlsx로 저장해 주세요.`
  - 동작 방식: 에이전트가 1라운드 엑셀 구조를 읽어 8행에 "부서별 평균" 행을 추가하고 `=AVERAGE(B2:B6)`부터 `=AVERAGE(E2:E6)`까지 4개 열 평균 수식을 산출함 (총합계 행을 제외하고 부서 5개 행만 범위 지정). `2026-Q3-dept-expense-settlement-v2.xlsx` 생성 완료.

### 2.3 [작업 3] 발표 자료 제작 (DeckCraft)
- **지시**: `[ArtCraft QA] AI 에이전트 도입 전략 4장 발표 자료를 작성해 주세요. 1장 표지, 2장 도입 배경, 3장 실행 과제, 4장 기대 효과로 구성하여 PPTX와 PDF로 저장해 주세요.`
- **결과**: `ai-agent-strategy.pptx` (슬라이드 4개 정상 구성, 카드 형태 디자인 및 로드맵), `ai-agent-strategy.pdf` (4쪽 분량 읽기용 PDF) 생성 완료.

### 2.4 [작업 4] 2D CAD 기계 도면 제작 (CADCraft)
- **지시**: `[ArtCraft QA] 샘플 브래킷 기계 도면을 생성하고 DXF와 PNG 파일로 저장해 주세요.`
- **결과**: 엔티티 46개(선 16, 원 7, 폴리라인 6, 치수 6, 해치 4, 텍스트 7), 레이어 9개로 구성된 정밀 도면 `bracket.dxf`와 이를 2400×1600 초고해상도로 렌더링한 `bracket.png` 생성 완료.

### 2.5 [작업 5] 벡터 로고 디자인 (VectorCraft)
- **지시**: `[ArtCraft QA] 기하학적 심볼과 텍스트가 조화된 미니멀 테크 로고를 작성하여 SVG와 PNG 파일로 저장해 주세요.`
- **결과**: 1200×480 해상도의 네이비 배경 + 육각형 프레임 + 시안색 노드 심볼 + `NODEX` 워드마크가 조합된 `minimal-tech-logo.svg` 및 `minimal-tech-logo.png` 생성 완료.

### 2.6 [작업 6] 모션 타이틀 카드 렌더링 (EffectCraft)
- **지시**: `[ArtCraft QA] Ttakkari Studio 2026 모션 타이틀 카드를 렌더링하여 프레임 이미지 PNG 파일로 저장해 주세요.`
- **결과**: 5초/30fps 애니메이션 프로젝트 파일 `ttakkari-studio-2026-title-card.ecproj`, 0.6초 등장 중간 프레임 `ttakkari-studio-2026-title-card-t0.6.png` (1920×1080), 3초 완성 프레임 `ttakkari-studio-2026-title-card.png` (1920×1080) 산출 완료.

---

## 3. 산출물 형식별 품질 검사 결과

| 형식 | 검사 대상 파일 | 검사 기준 | 실제 검사 결과 | 판정 |
|---|---|---|---|---|
| **DOCX** | `weekly-engineering-report-v2.docx` | OpenXML 유효성, 한글 유니코드 깨짐 없음, 표(Table) 및 문단 구조 | XML 텍스트 엘리먼트 39개, 표 1개, 한글 정상("주간 엔지니어링 보고서 (v2)", "주의사항", "맺음말" 포함), UTF-8 정상 인코딩 | **우수 (Pass)** |
| **PDF** | `weekly-engineering-report-v2.pdf`<br>`ai-agent-strategy.pdf` | 유효한 PDF 헤더(`%PDF`), 페이지 수 충족, 폰트 임베딩 | 보고서: 2쪽 정상 생성 (54.8KB)<br>발표자료: 4쪽 정상 생성 (574.9KB), 글자 잘림 없음 | **우수 (Pass)** |
| **XLSX** | `2026-Q3-dept-expense-settlement-v2.xlsx` | OpenXML Spreadsheet 구조, 계산 수식 보존(=SUM, =AVERAGE) | 총 13개 수식 보존 (`SUM(C2:D2)`~`SUM(C6:D6)`, `SUM` 열합계 4개, `AVERAGE` 열평균 4개), 천단위 서식 적용 | **우수 (Pass)** |
| **PPTX** | `ai-agent-strategy.pptx` | PresentationML 구조, 슬라이드 개수, 레이아웃/도형 | `ppt/slides/slide1~4.xml` 정확히 4개 슬라이드 포함, 카드/로드맵 도형 정상 배치 | **우수 (Pass)** |
| **DXF** | `bracket.dxf` | 표준 DXF 헤더 및 섹션 구조, 엔티티/레이어 정합성 | 6,590행 유효 DXF (`HEADER`, `ENTITIES`), 46개 엔티티(치수 6, 해치 4 포함), 9개 레이어 | **우수 (Pass)** |
| **SVG** | `minimal-tech-logo.svg` | W3C SVG XML 유효성, viewBox 및 반응형 규격 | `viewBox="0 0 1200 480"`, `width=1200`, `height=480`, `path`, `rect`, `text` 정상 | **우수 (Pass)** |
| **PNG** | `bracket.png`<br>`ttakkari-studio-2026-title-card.png`<br>`minimal-tech-logo.png` | IHDR 규격, 목표 해상도 충족, 투명도/색상 | `bracket.png`: 2400×1600 (초고해상도 도면)<br>`title-card.png`: 1920×1080 (FHD 모션 프레임)<br>`logo.png`: 1200×480 (정밀 로고) | **우수 (Pass)** |

---

## 4. 13앱 CLI/MCP 헬스 및 미검증 범위 구분

| 앱 이름 | 설치 버전 | CLI 헬스 | MCP 헬스 (도구 수) | 이번 실검증 여부 | 미검증 범위 및 한계 |
|---|---|---|---|---|---|
| `photocraft` | 0.10.0 | 정상 (`8407d9ccb`) | 정상 (25 tools) | 이전 e2e 및 status | 시스템 폰트 스캔 지연(BUG-001, `PHOTOCRAFT_NO_SYSTEM_FONTS=1` 필수), UI 스크린샷 화면 잠김 한계(BUG-005) |
| `vectorcraft` | 0.13.1 | 정상 (`904f0fe`) | 정상 (32 tools) | **실제 작업 완료 (로고 SVG/PNG)** | 인증 없는 제어 포트 7979(BUG-002), 폰트 아웃라인 미변환 시 시스템 종속 |
| `filmcraft` | 0.9.0 | 정상 (`4a69a03`) | 정상 (17 tools) | status 헬스 체크 | 대용량 비디오 렌더 시 CPU/메모리 부하 및 장시간 런 검증 미실시, 제어 포트 9876 무인증(BUG-002) |
| `lightcraft` | 0.9.1 | 정상 (`4f0b110`) | 정상 (388 tools) | status 헬스 체크 | MCP 도구 388개로 컨텍스트 과다 소비 (`--compact` 필요), RAW 파일(CR3/NEF 등) 실제 현상 미검증 |
| `pdfcraft` | 0.9.0 | 정상 (`645016a`) | 정상 (170 tools) | 이전 e2e 검증 | 170개 도구로 프롬프트 부담 큼, 대용량 PDF OCR/양식 채우기 미실시 |
| `effectcraft` | 0.11.1 | 정상 (`2a01226`) | 정상 (27 tools) | **실제 작업 완료 (타이틀 카드 PNG/.ecproj)** | **MCP에 video export(mp4) 도구 미노출** (프레임 렌더만 가능), 제어 포트 9877 무인증(BUG-002) |
| `designcraft` | 0.9.0 | 정상 (`1a38cf0`) | 정상 (26 tools) | status 헬스 체크 | 여러 쪽 브로슈어 인쇄 규격(CMYK, 도련선 등) 미검증, 제어 포트 7982 무인증(BUG-002) |
| `figcraft` | 0.7.0 | 정상 (`47438dd`) | 정상 (18 tools) | status 헬스 체크 (태스크 큐 대기) | 모바일 화면 벡터 구조 및 인터랙션 시안 품질 미검증, 제어 포트 7983 무인증(BUG-002) |
| `wordcraft` | 0.8.0 | 정상 (`f2e15cf`) | 정상 (16 tools) | **실제 작업 완료 (R1/R2 왕복, DOCX/PDF)** | 서버 DOCX 미리보기 변환기 한글 글꼴 부족으로 PDF 동시 산출 필수 |
| `soundcraft` | 0.10.0 | 정상 (`e894076`) | 정상 (17 tools) | status 헬스 체크 | 멀티트랙 DAW 믹싱, 마스터링 바운스 및 비트 심도(24bit) 실제 음질 미검증, 포트 7801 무인증(BUG-002) |
| `deckcraft` | 0.8.0 | 정상 (`22db926`) | 정상 (25 tools) | **실제 작업 완료 (4장 슬라이드 PPTX/PDF)** | 슬라이드 내 특정 서식(예: 소제목 볼드체) 렌더링 미반영 현상 관찰됨, 포트 7990 무인증(BUG-002) |
| `cadcraft` | 0.8.0 | 정상 (`714111d`) | 정상 (12 tools) | **실제 작업 완료 (46개 엔티티 DXF/PNG)** | 프리알파 단계로 복잡한 3D 도면 미지원, `--help` exit code 1(BUG-003), 포트 7984 무인증(BUG-002) |
| `gridcraft` | 0.9.0 | 정상 (`a2cc4b4`) | 정상 (21 tools) | **실제 작업 완료 (R1/R2 왕복, =SUM/=AVERAGE)** | 외부 데이터 연결 및 복잡한 피벗/매크로 미지원, 포트 7985 무인증(BUG-002) |

---

## 5. 작업 중 발생한 실패 및 자체 복구 사례

1. **[복구 사례 1] CADCraft 명령어 변수 치환 차단 및 직접 호출 복구**:
   - 상황: CADCraft 에이전트가 `bracket.dxf`와 `bracket.png`를 생성하기 위해 셸 스크립트 변수로 명령어를 조립해 실행하려다 하네스 보안 가드(`PreToolUse`)에 의해 차단됨.
   - 복구: 에이전트가 "변수로 만든 명령 이름이 차단돼서, 전체 경로를 그대로 써서 다시 실행합니다"라며 절대 경로 직접 실행으로 전환하여 성공함.
2. **[복구 사례 2] VectorCraft 첫 번째 파일 쓰기 차단 및 Write 도구 전환**:
   - 상황: VectorCraft 에이전트가 `minimal-tech-logo.svg`를 셸 리다이렉션으로 저장하려다 파일 생성 전 차단됨.
   - 복구: 에이전트가 "The first command was blocked before the file was written, so I'll write it with the Write tool."이라며 네이티브 파일 쓰기 도구(`write_file`)로 전환하여 성공함.
3. **[복구 사례 3] 이어서 수정 시 파일 격리 및 참조 (`use_previous: true`)**:
   - 상황: WordCraft 및 GridCraft 2차 라운드에서 이전 결과물이 `prev/r2/` 디렉터리에 격리 제공됨.
   - 복구: 에이전트가 이전 버전의 마크다운/엑셀 원본 구조를 정확히 역참조하여 변경사항(문단 추가, AVERAGE 수식 추가)만 원자적으로 반영한 v2 파일들을 성공적으로 산출함.

---

## 6. 백엔드 작업 흐름 개선 작업자에게 전달할 실제 막힘 및 재현 단계

### [이슈 1 - P0 긴급] Claude 계정 꺼짐 시 ArtCraft Task 전체가 즉시 영구 실패
- **발견 위치**: `ttakkari/artcraft_tasks.py:167`
  ```python
  sess = ChatSession(workspace_id=ws.id, title=_title(prompt, tpl), engine="claude", kind=SessionKind.chat, pool="personal")
  ```
- **현상**: 세션의 엔진이 `"claude"`로 하드코딩되어 있는데, 운영 설정에서 사용자의 개인 claude 계정이 `enabled: false` 상태이면 `POST /api/artcraft/tasks` 호출 직후 백엔드 런 실행기가 `"켜진 개인 claude 계정이 없습니다. 설정 > 계정의 개인 계정에서 켜 주세요."` 에러를 반환하며 작업이 즉시 `state="failed"`로 중단됨.
- **재현 단계**:
  1. `ttk patch /api/accounts/{claude_id} '{"enabled": false}'`로 claude 계정 비활성화
  2. `ttk post /api/artcraft/tasks '{"category":"doc","template_id":"write-report","prompt":"테스트"}'` 실행
  3. `ttk get /api/artcraft/tasks/{id}` 조회 시 0초 만에 `state="failed"`, error 발생
- **개선 제안**:
  1. 현재 활성화된 대체 엔진(`antigravity`, `codex` 등)으로 fallback 실행 지원.
  2. 만약 claude만 지원해야 한다면, `GET /api/artcraft/catalog` 응답에서 `available: false`, `unavailable_reason: "개인 claude 계정이 비활성화되어 있습니다."`를 반환하여 프론트엔드에서 작업 생성을 사전에 안내·차단할 것.

### [이슈 2 - P2] EffectCraft MCP 도구에 Video Export(MP4 렌더링) 미노출
- **현상**: 모션/영상 효과(`motion`) 카테고리에서 에이전트가 `ttakkari-studio-2026-title-card.ecproj`와 단일 프레임 PNG는 정상 생성했으나, "불러온 effectcraft 도구에는 영상 렌더 항목이 없어서 mp4는 만들지 않았습니다"라고 보고함.
- **원인**: `effectcraft-cli`는 `--project X.ecproj render --out o.mp4` CLI 명령을 지원하지만, stdio MCP 서버 도구 목록에는 단일 프레임 렌더링(`render_frame`)만 노출되어 있음.
- **개선 제안**: EffectCraft MCP 서버에 video render tool(`export_video` 또는 `render_animation`)을 추가 노출하거나, 시스템 프롬프트(노트)에 CLI 렌더 명령 사용 요령을 제공할 것.

### [이슈 3 - P2] 에이전트 셸 변수 조립 시 하네스 가드 차단으로 인한 재시도 비용 발생
- **현상**: CADCraft/VectorCraft 작업 시 에이전트가 셸 변수(`APP_BIN=...; $APP_BIN ...`)를 사용하다 보안 가드에 막혀 불필요한 라운드 내 재시도가 발생함.
- **개선 제안**: `artcraft_tasks.prepare_run`에서 주입하는 시스템 노트에 "명령어 실행 시 셸 변수 조립을 피하고 바이너리 전체 경로를 리터럴로 직접 호출할 것" 지침을 추가할 것.

---

## 7. Outbox 공유 완료 목록 (User Deliverables)

Worker Outbox `/Volumes/Backup/ttakkari-data/outbox/67bd167c-3ae4-47da-9b8a-727cfc976efb`에 저장 및 `share_file` 완료된 대표 파일:
1. `weekly-engineering-report-v2.pdf` (`2dc27910-cc5d-46a2-ad7a-2396425e6e0b`)
2. `weekly-engineering-report-v2.docx` (`f5809264-a51a-434b-a7f6-2aca7b5d2e9d`)
3. `2026-Q3-dept-expense-settlement-v2.xlsx` (`5d0293e8-e6f5-4015-94fc-85f192a33454`)
4. `ai-agent-strategy.pdf` (`73414e60-5ea7-47c5-94e6-0a997905aea7`)
5. `ai-agent-strategy.pptx` (`2ee242a6-e2e0-4522-8e23-68a028ddc82f`)
6. `bracket.png` (`2817db65-641c-4bde-a6f2-11d0c957b3aa`)
7. `bracket.dxf` (`31e81b5a-d608-4689-9114-fa118a266551`)
8. `minimal-tech-logo.png` (`69e29963-50a6-4aac-80f0-3bd149cd7663`)
9. `minimal-tech-logo.svg` (`09bc7e42-48eb-4c17-89e7-3b64e9c7c327`)
10. `ttakkari-studio-2026-title-card.png` (`b7c93e79-3c40-423e-baf8-731eb945d5e2`)
