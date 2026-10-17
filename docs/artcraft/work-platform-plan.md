# ArtCraft 작업 플랫폼 기획서

- 작성일: 2026-10-18
- 상태: 1단계 기획 확정 → 2단계 구현 시작
- 관련: ADR 0035(ArtCraft 관제, 유지), ADR 0039(이 기획의 결정 요약)
- 범위: 더보기 > ArtCraft 페이지(`/artcraft`)를 "에이전트에게 창작 작업을 시키는 플랫폼"으로 바꾼다. 디자인시스템 기능과는 섞지 않는다.

## 1. 왜 다시 만드나

지금 페이지는 설치된 앱 13개의 상태, 스모크, 버그 후보를 보는 관제 화면이다. ArtCraft 앱은 혼자 돌아가는 프로그램이 아니다. 사람이 파일을 주고 "이렇게 고쳐 줘"라고 시켜야 의미가 있다. 폰에서 사진 한 장, PDF 한 개를 올리고 말로 지시하면 에이전트가 앱을 써서 결과 파일을 돌려주는 흐름이 핵심이다.

또 `PhotoCraft`, `LightCraft` 같은 이름만으로는 무슨 일을 하는 앱인지 알기 어렵다. 앱 이름이 아니라 **하고 싶은 일**(사진 보정, PDF 합치기, 문서 만들기)로 길을 찾게 한다.

### 목표

1. 사용자 언어의 작업 범주 12개와 각 범주의 세부 페이지.
2. 세부 페이지마다 (a) 작업 지시, (b) 작업물 조회, (c) 진행 중 작업과 이력.
3. 지시 → 작업 세션 → 결과물이 끝까지 이어지는 흐름. 폰에서 파일 올리기, 결과 미리보기(이미지·PDF·영상·오디오), 다운로드, 이어서 수정 요청.
4. 기존 관제(앱 상태, 스모크, 버그 후보, 에이전트 연결)는 "관리" 아래로 옮긴다.

### 하지 않는 것

- 새 에이전트 실행기. 기존 세션·Run·outbox·Artifact 인프라를 그대로 쓴다.
- 앱 설치·업데이트 자동화, 업스트림 이슈·PR 자동 제출(ADR 0035 그대로).
- ArtCraft 앱의 클라우드·유료 AI 기능.
- 디자인시스템 도메인의 코드·데이터·라우트 사용.

## 2. 정보 구조

```
/artcraft                       작업 허브 (기본)
  ├─ 진행 중 작업 띠
  ├─ 범주 카드 12개 (이미지 / 영상·소리 / 문서 / 도면 4묶음)
  └─ 최근 작업물
/artcraft/c/:category           범주 세부 페이지 (탭: 지시 · 작업물 · 이력)
/artcraft/t/:taskId             작업 상세 (라운드별 지시·진행·결과, 이어서 수정)
/artcraft/manage                관리 (탭: 앱 상태 · 버그·기여 · 에이전트 연결)
```

- 기존 `/artcraft?tab=bugs&focus=BUG-001` 같은 링크는 `/artcraft/manage?tab=bugs&focus=...`로 넘긴다(알림·메모에 남은 링크 보호).
- 더보기 항목 이름은 "ArtCraft"(설명: 사진·PDF·문서·영상 같은 창작 작업을 에이전트에게 시키기). 허브 머리글의 톱니 버튼이 관리로 간다.
- 작업 세션은 사이드바 대화 목록과 작업 공간 목록에 나오지 않는다(`artcraft_tasks`에 걸린 세션은 제외). 전체 대화 기록이 필요하면 작업 상세의 "대화 기록 열기"로 `/chat/:sessionId`를 연다.

### 범주(사용자 언어)와 담당 앱

| 묶음 | 범주 | 사용자에게 보이는 설명 | 담당 앱(원 이름) | 성숙도 |
|---|---|---|---|---|
| 이미지 | 사진 편집 | 사진 밝기·색 보정, 필터, 크기·형식 바꾸기 | PhotoCraft, LightCraft | 기본 작업 가능 |
| 이미지 | 일러스트·벡터 | SVG 그림 만들기·고치기, PNG·PDF로 뽑기 | VectorCraft | 기본 작업 가능 |
| 이미지 | 화면·UI 디자인 | 앱·웹 화면 시안, 아이콘 | FigCraft | 제한적 |
| 이미지 | 인쇄물·편집 디자인 | 포스터·브로셔·여러 쪽 레이아웃 | DesignCraft | 시험 단계 |
| 영상·소리 | 영상 편집 | 영상 자르기·이어 붙이기·내보내기 | FilmCraft | 제한적 |
| 영상·소리 | 모션·영상 효과 | 글자 움직임·합성·효과 영상 | EffectCraft | 제한적 |
| 영상·소리 | 오디오 | 소리 파일 변환·크기 맞추기·믹스 | SoundCraft | 시험 단계 |
| 문서 | PDF 편집 | PDF 합치기·나누기·페이지 정리·내용 읽기 | PdfCraft | 기본 작업 가능 |
| 문서 | 문서 작성 | 보고서·편지 만들기, 워드(DOCX) 변환 | WordCraft | 기본 작업 가능 |
| 문서 | 발표 자료 | 슬라이드 만들기, 이미지·PDF로 뽑기 | DeckCraft | 제한적 |
| 문서 | 스프레드시트 | 표 정리, 수식 계산, CSV·XLSX 변환 | GridCraft | 기본 작업 가능 |
| 도면 | 도면·CAD(2D) | 2D 도면 보기·그리기·SVG·PDF 변환 | CADCraft | 시험 단계 |

성숙도 낱말 뜻:

- **기본 작업 가능**: 대표 작업을 실제로 돌려 결과 파일이 나왔다(2026-10-18, 이 Mac Studio, 공식 릴리스 CLI).
- **제한적**: 파일 변환·렌더는 되지만 편집 기능은 앱이 아직 거칠다. 결과를 꼭 눈으로 확인한다.
- **시험 단계**: 앱이 프리알파다(공식 ROADMAP 자기 평가). 되는 일만 시키고 실패할 수 있다.

성숙도는 앱 자체 단계(`registry.json`의 사이트 상태)와 이 기획에서 직접 돌려 본 결과를 합쳐 정한 값이다. 앱이 업데이트되면 `artcraft_catalog.py`의 `can`/`cannot`을 다시 검증해 바꾼다(코드와 함께 리뷰하는 데이터다).

### 범주별 "되는 일 / 아직 안 되는 일"

모든 결과는 새 파일로 저장되고 원본은 그대로 둔다.

**사진 편집** (PhotoCraft·LightCraft)
- 되는 일: 밝기·대비·채도·색감 보정, 자동 톤, 흑백·블러·샤프 같은 필터, 크기 줄이기·자르기·회전·뒤집기, PNG·JPG·WEBP·TIFF·PSD 변환, 글자·도형 레이어 넣기, RAW(DNG 등) 사진을 현상해 JPG·PNG·TIFF·WEBP·AVIF·JPEG XL로 내보내기.
- 아직 안 되는 일: AI 배경 지우기·채우기·확대, 얼굴 자동 보정, HEIC 저장(읽기만 시도 가능, 저장 불가), 컴퓨터에 설치된 글꼴 쓰기(내장 글꼴만).

**일러스트·벡터** (VectorCraft)
- 되는 일: SVG를 PNG·JPG·PDF·SVG로 변환, 도형·경로·글자 그리기, 색 채우기, 패스파인더·변형·효과 적용.
- 아직 안 되는 일: 사진을 벡터로 자동 변환, AI 일러스트 생성, 복잡한 .ai 원본 완전 호환(확인 못함).

**화면·UI 디자인** (FigCraft)
- 되는 일: SVG·이미지 열기, 프레임·도형·글자 만들기, PNG·JPG·SVG·WEBP·PDF 내보내기.
- 아직 안 되는 일: 여럿이 함께 편집, Figma 클라우드 파일 열기, 플러그인 실행, 프로토타입 시연.

**인쇄물·편집 디자인** (DesignCraft, 프리알파)
- 되는 일: 여러 쪽 문서 레이아웃 만들기, 글 흐름 편집, 이미지 배치, PNG·PDF 내보내기.
- 아직 안 되는 일: 인쇄소 제출용 색 관리·PDF/X, InDesign 파일 호환(확인 못함), 안정성 보장.

**영상 편집** (FilmCraft)
- 되는 일: 영상·소리 파일 가져오기, 타임라인에서 자르고 이어 붙이기, MP4(H.264·HEVC)·ProRes·GIF·WAV 내보내기, 한 장면 PNG 뽑기, 영상 정보 읽기.
- 아직 안 되는 일: 자동 자막, AI 편집, 큰 4K 편집 속도 보장(측정 안 함), 프로젝트 전체를 한 번에 자동 구성.

**모션·영상 효과** (EffectCraft)
- 되는 일: 레이어·키프레임·효과 합성, 한 장면 PNG와 구간 영상(H.264·WebM·GIF 등) 렌더.
- 아직 안 되는 일: After Effects 프로젝트(.aep) 열기, 외부 플러그인.

**오디오** (SoundCraft, 프리알파)
- 되는 일: WAV·AIFF·FLAC 정보 보기와 서로 변환, 비트 깊이·샘플레이트 바꾸기, 소리 크기 맞추기(노멀라이즈), 여러 트랙 믹스를 WAV로 내보내기.
- 아직 안 되는 일: MP3·M4A 저장(직접 확인: 저장 불가), 음성 인식·합성, 노이즈 제거 같은 AI 처리.

**PDF 편집** (PdfCraft)
- 되는 일: 텍스트 읽기·페이지 미리보기 이미지, 합치기·나누기·쪽 뽑기·회전·삭제·순서 바꾸기·빈 쪽 넣기, 제목·작성자 고치기, 쪽 번호·책갈피·스탬프, 양식 채우기, 가린 척한 글자(가짜 가림) 검사.
- 아직 안 되는 일: 본문 글자를 자유롭게 고치기(제한적), 인증서 전자서명, 클라우드 서명.

**문서 작성** (WordCraft)
- 되는 일: 마크다운·텍스트로 DOCX 만들기, DOCX를 PDF·ODT·RTF·HTML·MD·TXT로 변환, 내용 읽기, 쪽을 이미지로 확인, 제목·표·변경 추적·메일 머지 같은 편집 명령.
- 아직 안 되는 일: HWP(한글) 파일 저장(직접 확인: 저장 불가), 복잡한 서식의 완벽한 재현.

**발표 자료** (DeckCraft)
- 되는 일: PPTX 만들기·열기·저장, 슬라이드 추가, 글상자·도형·표·차트·그림 넣기, 테마·레이아웃, 슬라이드를 PNG로, 전체를 PDF·ODP로 내보내기.
- 아직 안 되는 일: 애니메이션 결과를 영상으로 뽑기, AI 디자인 추천, 발표 중 원격 조작.

**스프레드시트** (GridCraft)
- 되는 일: CSV·TSV·XLSX·JSON·HTML 변환, 수식 계산, 셀 값 쓰기, 정렬·필터·서식·표·차트 명령.
- 아직 안 되는 일: 매크로(VBA), 외부 데이터 연결, PDF로 직접 저장(직접 확인: 불가, 표를 문서 앱으로 옮겨 PDF화).

**도면·CAD(2D)** (CADCraft, 프리알파)
- 되는 일: DXF 열기·내용 조회, 샘플 도면, 선·원·치수·해치 같은 도면 명령, SVG·PNG·PDF 변환.
- 아직 안 되는 일: 3D 모델링(STEP·STL), DWG 파일(확인 못함), 안정성 보장.

## 3. 세부 페이지

모바일(폭 360~430px)을 기준으로 먼저 정하고 데스크톱은 같은 구성을 넓혀 보인다.

### 3.1 허브 `/artcraft`

1. 머리글: 제목 "ArtCraft", 관리 버튼(톱니).
2. **진행 중 작업 띠**: 실행 중·대기 중 작업이 있으면 한 줄씩(범주 아이콘, 제목, 상태). 누르면 작업 상세.
3. **범주 카드**: 묶음별 제목 아래 2열 카드. 카드에 이름, 한 줄 설명, 성숙도 점, 담당 앱 이름(작게). 누르면 범주 세부.
4. **최근 작업물**: 최근 결과 6개를 가로 스크롤 미리보기 타일로. 누르면 뷰어(`?artifact=`).

### 3.2 범주 세부 `/artcraft/c/:category`

머리글 아래 범주 소개와 성숙도 배지. 세 탭.

**지시 탭**
- "이런 일을 시킬 수 있어요" 템플릿 버튼(범주당 4~6개). 누르면 지시문이 채워지고 필요한 옵션·첨부 칸이 맞춰진다. 템플릿마다 짧은 예시 문장이 힌트로 보인다.
- 지시 입력칸(여러 줄, 한글 조합 Enter 보호), 파일 첨부(폰 사진첩·파일 앱에서 여러 개), 첨부 타일(썸네일·진행·실패 재시도).
- 옵션: 템플릿이 정의한 선택지(예: 출력 형식 PNG/JPG/WEBP), 작업 강도(빠르게/보통/꼼꼼하게).
- "되는 일 / 아직 안 되는 일" 접이식 안내(쉬운 말), 담당 앱 목록과 각 앱 성숙도.
- 보내기 버튼 "작업 시키기". 성공하면 작업 상세로 이동.
- 템플릿이 첨부를 요구하는데 비어 있거나 형식이 맞지 않으면 보내기 전에 알려 준다(확장자 기준, 서버도 같은 검사).

**작업물 탭**
- 이 범주 작업이 만든 결과 파일 갤러리(이미지 썸네일, PDF·문서·오디오·영상은 종류 아이콘 타일). 종류 필터, 검색, 최신순.
- 타일을 누르면 뷰어(이미지·PDF·문서·표·영상·오디오 재생). 뷰어에서 다운로드, 그리고 "이 결과로 이어서 수정"으로 작업 상세의 후속 지시로 간다.

**이력 탭**
- 이 범주 작업 목록(최신순): 제목, 상태(대기·작업 중·완료·실패·취소), 시각, 결과 파일 개수. 누르면 작업 상세. 보관(숨기기) 가능.

### 3.3 작업 상세 `/artcraft/t/:taskId`

- 머리글: 작업 제목, 범주 칩, 상태, 취소 버튼(진행 중일 때).
- **라운드 목록**(시간순): 라운드마다 ① 내 지시와 첨부 타일, ② 상태 줄(대기 순번·진행 라벨·소요 시간), ③ 에이전트의 마지막 답(요약), ④ 결과 파일 타일(썸네일·이름·크기·종류, 누르면 뷰어), ⑤ 실패 시 이유와 "다시 시도".
- 진행 중이면 3초 간격으로 갱신하고 완료되면 알림(기존 푸시)이 간다.
- **이어서 수정 요청** 입력칸(고정 하단): 지시문, 파일 더 붙이기, "이전 결과를 이어서 고치기" 체크(기본 켜짐: 직전 라운드 결과 파일을 에이전트에 넘긴다), 작업 강도. 같은 세션에서 Run을 이어 쓰므로 에이전트가 앞선 맥락을 기억한다.
- "대화 기록 열기" 링크(전체 로그).

### 3.4 관리 `/artcraft/manage`

기존 ArtCraft 관제 화면(앱 상태·스모크·실행/종료, 버그·기여, 에이전트 연결)을 그대로 옮긴다. 사용자가 고른 버그만 기여한다는 운영 방식은 그대로다. 머리글 뒤로 가기는 허브.

## 4. 데이터 모델

새 테이블 하나. 세션·Run·Artifact는 기존 것을 쓴다.

```
artcraft_tasks
  id            uuid PK
  session_id    uuid FK sessions(id) ON DELETE CASCADE, UNIQUE   -- 작업 세션 1개
  category      varchar(20)   -- photo, vector, ui, layout, video, motion, audio, pdf, doc, slides, sheet, cad
  apps          jsonb         -- 연결할 앱 이름 목록(registry 이름)
  title         varchar(200)
  template_id   varchar(40) null
  options       jsonb         -- 사용자가 고른 옵션(출력 형식 등)
  created_at, updated_at, archived_at
  index(category, updated_at)
```

- 작업 = 세션 1개 + 라운드(Run)들. 상태는 마지막 Run 상태에서 계산한다(`working`: queued/running, `done`, `failed`, `cancelled`).
- 결과물 = 그 세션 Run들의 `Artifact(source="agent_output")`. 입력 첨부 = 업로드 Artifact(`/api/uploads`).
- 작업 폴더는 DB에 두지 않는다. `<워크스페이스>/<task_id>/`로 항상 계산한다.
- 마이그레이션은 새 테이블만 만든다(0041). 세대 배포의 프로토콜 변경 없음.

## 5. 작업 세션 연동(재사용 방식)

```
폰 ──업로드──> POST /api/uploads (workspace=ArtCraft 워크스페이스)  -> Artifact ids
폰 ──지시────> POST /api/artcraft/tasks
                 1) 세션 생성(kind=chat, engine=claude, 작업 제목, ArtCraft 워크스페이스)
                 2) artcraft_tasks 행 생성
                 3) 기존 queue_run()으로 Run 대기열 등록 (context_artifact_ids=입력 파일)
manager._run (기존 실행기 그대로)
   └ ArtCraft 세션이면 artcraft_tasks.prepare_run() 한 번 호출
        - 작업 폴더 만들기: in/ (입력 복사본) prev/ (이전 결과 복사본) work/ out/
        - 앱별 MCP 서버 명세 만들기 -> LaunchSpec.mcp_servers에 합치기
        - 지시문 앞머리(파일 위치·옵션)와 시스템 노트(규칙·앱 요령) 만들기
   └ 실행 후 sweep_outputs(): out/ 의 새 결과를 이 Run의 outbox로 복사
   └ 기존 _collect_artifacts() 가 outbox를 Artifact로 등록(미리보기 변환 큐 포함)
폰 <─결과────── GET /api/artcraft/tasks/{id}  (라운드·진행·결과 Artifact)
폰 ──재지시──> POST /api/artcraft/tasks/{id}/runs  (queue_run 재사용, 같은 세션 resume)
```

### 5.1 워크스페이스

`TTAKKARI_ARTCRAFT_WORKSPACE_ROOT`(기본 `~/development/projects/artcraft-workspace`)를 첫 사용 때 만들고 워크스페이스 "ArtCraft 작업"으로 등록한다(`allowed_roots` 안, git 저장소 아님). 이렇게 하면 기존 개발 저장소에 체크포인트 커밋이 생기지 않는다. 폴더 구조:

```
<root>/<task_id>/in/    사용자가 올린 파일 복사본(읽기 전용으로 쓰라고 지시)
<root>/<task_id>/prev/  이전 라운드 결과 복사본(이어서 수정할 때)
<root>/<task_id>/work/  중간 파일
<root>/<task_id>/out/   최종 결과(여기에 저장하면 자동 수집)
```

### 5.2 MCP 연결

- 서버 이름 `artcraft_<app>`, 명령 `<artcraft_root>/bin/artcraft mcp <app> [안전 옵션]`. 인자는 registry에 있는 앱 이름과 고정 옵션뿐이다. 셸을 쓰지 않고 사용자 입력은 인자에 들어가지 않는다.
- 범주 기본 앱을 연결한다(사진 편집은 PhotoCraft와 LightCraft). 도구가 많은 앱은 `--compact`(LightCraft 330개, PdfCraft 163개).
- **경로 제한**: 앱이 지원하면 읽기·쓰기를 작업 폴더로 가둔다. `--automation-read-root/--automation-write-root`(PhotoCraft, VectorCraft, WordCraft, EffectCraft, SoundCraft), `--root`(PdfCraft). 직접 확인: WordCraft와 PdfCraft는 작업 폴더 밖 파일 열기를 거부했다.
- 제한 옵션이 없는 앱(DeckCraft, DesignCraft, GridCraft, CADCraft, FigCraft, FilmCraft, LightCraft)은 따까리 guard가 MCP 도구 인자의 절대 경로가 보호 영역(`~/.ttakkari`)이나 민감 파일이면 막는다. 완전한 경로 격리가 아니므로 한계에 적는다.
- 엔진은 `claude`로 둔다(MCP 도구를 승인 없이 쓴다). 한도 때문에 다른 엔진으로 넘어가면 MCP 대신 시스템 노트의 CLI 요령(`bin/artcraft run <app> ...`)으로 계속한다.
- 시스템 노트에 넣는 것: 작업 폴더 구조, 입력은 복사본만 쓰고 원본·`in/`는 고치지 않기, 결과는 `out/`에 의미 있는 이름으로 저장하기, 끝나기 전에 렌더·정보 확인으로 검증하기, 앱이 못 하는 일은 지어내지 말고 솔직히 알리기, 유료·클라우드 기능 쓰지 않기, 한국어로 짧게 답하기, 범주별 CLI 요령과 `AGENTS.md` 위치.

### 5.3 입력 파일

폰에서 `POST /api/uploads`(기존, 200MB 한도)로 올리면 Artifact가 된다. 지시에 `input_artifact_ids`로 넘기면 실행 직전에 `in/`으로 복사한다. 이름이 겹치면 `이름-2.ext`로 바꾼다. 업로드는 기존 보관함 "파일" 구역에도 보인다.

### 5.4 결과물 미리보기

기존 뷰어(`?artifact=<id>`)를 쓴다. 이미지·PDF·문서·표는 이미 된다. 영상·오디오가 없으므로 Artifact 종류 `audio`, `video`를 추가하고(백엔드 `kinds.py`, 프런트 `ArtifactKind`) 뷰어에 재생 화면을 만든다. 서명된 5분 inline 링크(`/api/view/{token}`, Range 지원)를 `<video>`/`<audio>` 소스로 쓴다. 다운로드는 기존 서명 링크.

### 5.5 이어서 수정

같은 세션에 새 Run을 넣는다(엔진 대화 resume). 후속 지시는 직전 라운드 결과를 `prev/`로 복사해 에이전트가 "이전 결과 파일"로 받게 한다(체크 해제 가능). 결과가 여러 번 쌓이므로 파일 이름에 버전을 붙이라고 노트에 쓴다. 작업 상세는 라운드별로 결과를 보여 준다.

## 6. API

모두 소유자 로그인(`User`) 전용. 기존 `/api/artcraft/*` 관제 경로는 그대로 두고 아래를 더한다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/artcraft/catalog` | 묶음·범주·템플릿·되는 일/안 되는 일·성숙도·앱 설치 여부·작업용 `workspace_id` |
| POST | `/api/artcraft/tasks` | `{category, prompt, template_id?, input_artifact_ids[], apps?, options?, speed?}` → 201 `TaskOut` |
| GET | `/api/artcraft/tasks?category=&state=active\|all&limit=` | 작업 요약 목록(결과 개수, 마지막 상태·진행 라벨) |
| GET | `/api/artcraft/tasks/{id}` | 라운드(Run 요약+입력 첨부+결과 Artifact)와 상태 |
| POST | `/api/artcraft/tasks/{id}/runs` | `{prompt, input_artifact_ids[], use_previous, options?, speed?}` → 202 라운드 |
| PATCH | `/api/artcraft/tasks/{id}` | `{title?, archived?}` |
| GET | `/api/artcraft/outputs?category=&kind=&q=&limit=&offset=` | 결과물 갤러리(작업·범주 정보 포함) |

- 취소는 기존 `POST /api/runs/{id}/cancel`, 업로드는 `POST /api/uploads`, 이벤트는 기존 `/api/runs/{id}/events`.
- 오류 본문은 기존 규약(`{code, message}`). 주요 코드: `unknown_category`, `unknown_app`, `bad_input`(확장자 불일치·첨부 누락), `artcraft_unavailable`(저장소·앱 없음), `task_busy`는 쓰지 않는다(Run 대기열이 순서를 처리).
- 입력 검증: 파일 개수 최대 10, 지시문 1~8000자, 범주가 받는 확장자(`accepts`) 확인, 앱 이름은 범주가 허용한 앱만.

## 7. 모바일 우선 화면 구성

- 하단 탭 안에서 스크롤하는 단일 컬럼. 입력칸·보내기 버튼은 엄지 영역(화면 아래)에 둔다. 키보드가 올라와도 가려지지 않게 기존 `useKeyboardOpen` 규칙을 따른다.
- 터치 대상 44px 이상, 템플릿 버튼은 가로 줄바꿈 칩, 카드 2열(360px에서도 안 깨지게 minmax).
- 영상·오디오는 기본 컨트롤. 영상은 폭에 맞추고 `playsinline`.
- 로딩·빈·오류 상태는 기존 `ListSkeleton`, `ErrorState`와 같은 문구 체계.
- 데스크톱(lg 이상): 허브 카드 3~4열, 범주 세부는 지시와 작업물을 좌우로 나눠 본다.

## 8. 보안·불변 조건

- 디자인시스템 코드와 섞지 않는다(import 없음).
- 토큰·키·control 파일은 읽지 않는다. 앱 실행은 고정 명령 목록과 registry의 앱 이름만. 사용자 입력은 셸에 들어가지 않는다(`create_subprocess_exec` 또는 에이전트 MCP 인자 배열).
- 작업 세션 환경에는 `TTAKKARI_*`가 없다(기존 manager 규칙).
- MCP 경로 제한은 가능한 앱에 적용하고 나머지는 guard로 보호 영역·민감 파일을 막는다.
- 업로드·결과물은 기존 보존 기간 정책(`artifact_retention_days`)과 반출 정책을 따른다.
- ArtCraft 저장소는 건드리지 않는다(읽기와 앱 실행만). 임시 서버는 자기 PID로만 종료한다.

## 9. 검증 계획

- 백엔드: 카탈로그 무결성(앱 이름이 registry와 일치), 작업 생성·후속 지시·목록·갤러리, 입력 검증 실패, 작업 폴더 스테이징·sweep, MCP 명세(경로 제한 옵션·compact), guard의 MCP 경로 검사, 목록 제외(사이드바). 전체 pytest.
- 프런트: 허브·범주·작업 상세·관리 라우팅과 레거시 링크 리다이렉트, 템플릿 채움, 첨부 검증, 오디오·영상 뷰어 모드. vitest, build, lint(다른 세션 기인 오류는 구분 보고).
- **실제 e2e**: 임시 백엔드(별도 DB·포트·PID)에서 실제 claude로 (1) PDF: 여러 PDF 합치기+쪽 뽑기+요약, (2) 문서: 마크다운을 DOCX·PDF로 만들고 이어서 수정. 결과를 화면(폰 크기 스크린샷)에서 확인한다.
- 스크린샷은 outbox에 저장한다.

## 10. 알려진 한계(착수 시점)

- 경로 격리가 없는 앱은 guard의 부분 보호뿐이다.
- 성숙도와 "되는 일" 목록은 이 날짜 기준 수동 검증이며 앱 업데이트 때 재검증이 필요하다.
- 에이전트가 앱을 잘못 쓰면 결과가 어색할 수 있다. 완료 보고가 아니라 결과 파일을 눈으로 확인하는 것이 기준이다.
- 영상·큰 파일은 처리 시간이 길 수 있다. Run 제한시간(`run_timeout_seconds`)을 따른다.
- 동시에 한 세션에 Run이 하나만 돈다(기존 대기열). 작업끼리는 병렬이다.
- 결과물은 보존 기간(기본 90일) 뒤 정리된다.

## 11. 구현 검증 기록(2026-10-18)

임시 백엔드(별도 DB `ttakkari_artcraft_e2e`, 포트 8799, 자기 PID)와 실제 claude, 실제 ArtCraft 앱으로 확인했다.

- **PDF 편집**: PDF 2개를 올려 "PDF 합치기" 템플릿으로 지시 → 작업 세션 생성 → 에이전트가 PdfCraft MCP(`doc_open`, `doc_combine`)로 `merged.pdf`(2쪽)와 `merged-page1.png`를 만들어 `out/`에 저장 → 결과물로 등록 → 화면 뷰어에서 2쪽 확인. 이어서 수정 요청("2쪽만 뽑기")이 같은 세션의 라운드 2로 돌고, 이전 결과가 `prev/r2/`로 복사돼 `merged-page2.pdf`(1쪽, "Hello")가 나왔다.
- **문서 작성**: "보고서 쓰기" 템플릿 → WordCraft로 DOCX·PDF 생성(1쪽, 한글 정상) → 이어서 수정 요청("감사합니다 문단 추가, v2") → v2 DOCX·PDF. 이 작업은 에이전트가 MCP 대신 CLI(`artcraft run wordcraft ...`)를 골랐다.
- 알게 된 점: 사용자의 전역 Claude Stop 훅(work-history)이 작업 세션에도 돌아 마지막 답이 딴 이야기가 될 수 있어, 작업 상세는 라운드의 마지막 답 3개를 함께 보여 준다. 서버의 DOCX 미리보기 변환기는 한글 글꼴이 없어 글자가 빠진다(기존 변환기 한계). 결과물 PDF는 정상이므로 템플릿이 DOCX와 PDF를 함께 요청한다.
