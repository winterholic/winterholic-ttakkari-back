"""ArtCraft 작업 범주 카탈로그(ADR 0039). 사용자 언어의 범주, 담당 앱, 되는 일/안 되는 일, 지시 템플릿.

이 파일은 코드와 함께 리뷰하는 데이터다. ``can``/``cannot``/``maturity`` 는 2026-10-18 이 Mac Studio 에서 공식 릴리스 CLI 를
직접 돌려 확인한 결과와 각 앱의 자기 평가(registry 의 사이트 상태)를 합친 값이다. 앱이 업데이트되면 다시 검증해 고친다.
앱 이름은 registry.json 의 이름과 같아야 하고 ``validate()`` 가 그것을 검사한다(테스트가 호출).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SECTIONS = (
    ("image", "이미지"),
    ("av", "영상·소리"),
    ("doc", "문서"),
    ("cad", "도면"),
)

LEVELS = {
    "ready": ("기본 작업 가능", "대표 작업을 실제로 돌려 결과 파일이 나왔어요."),
    "limited": ("제한적", "변환·출력은 되지만 편집 기능은 아직 거칠어요. 결과를 꼭 눈으로 확인하세요."),
    "experimental": ("시험 단계", "앱이 아직 프리알파예요. 되는 일만 시키고 실패할 수 있어요."),
}

SPEEDS = {"fast": "low", "normal": None, "careful": "high"}


@dataclass(frozen=True)
class Option:
    id: str
    label: str
    choices: tuple[tuple[str, str], ...]
    default: str


@dataclass(frozen=True)
class Template:
    id: str
    title: str
    prompt: str
    example: str
    min_files: int = 0
    options: tuple[Option, ...] = ()
    apps: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppRole:
    app: str
    title: str
    role: str
    # MCP 서버를 가둘 때 쓰는 옵션 종류. rootflags=--automation-*-root, root=--root, none=지원 안 함
    confine: str
    compact: bool = False


@dataclass(frozen=True)
class Category:
    id: str
    section: str
    title: str
    tagline: str
    icon: str
    level: str
    apps: tuple[AppRole, ...]
    can: tuple[str, ...]
    cannot: tuple[str, ...]
    accepts: tuple[str, ...]
    outputs: tuple[str, ...]
    templates: tuple[Template, ...]
    cli: tuple[str, ...] = field(default=())


def _fmt(*choices: str, default: str | None = None, id: str = "format", label: str = "저장 형식") -> Option:
    return Option(id, label, tuple((c, c.upper()) for c in choices), default or choices[0])


IMAGE_EXTS = ("png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "heic", "heif", "avif")
RAW_EXTS = ("dng", "raw", "arw", "cr2", "cr3", "nef", "orf", "rw2")

CATEGORIES: tuple[Category, ...] = (
    Category(
        id="photo", section="image", title="사진 편집", tagline="사진 밝기·색 보정, 필터, 크기·형식 바꾸기",
        icon="image", level="ready",
        apps=(
            AppRole("photocraft", "PhotoCraft", "레이어·필터·변환(포토샵 방식)", "rootflags"),
            AppRole("lightcraft", "LightCraft", "사진 색 보정·RAW 현상(라이트룸 방식)", "none", compact=True),
        ),
        can=("밝기·대비·채도·색감 보정, 자동 톤", "흑백·블러·샤프 같은 필터", "크기 줄이기·자르기·회전·뒤집기",
             "PNG·JPG·WEBP·TIFF·PSD 형식 바꾸기", "글자·도형 레이어 넣기", "RAW 사진을 현상해 JPG·PNG·TIFF·WEBP·AVIF로 저장"),
        cannot=("AI 배경 지우기·채우기·확대", "얼굴 자동 보정", "HEIC로 저장(읽기만 시도 가능)", "컴퓨터에 설치된 글꼴(내장 글꼴만 써요)"),
        accepts=IMAGE_EXTS + RAW_EXTS + ("psd",),
        outputs=("PNG", "JPG", "WEBP", "TIFF", "PSD"),
        templates=(
            Template("auto-enhance", "자동 보정", "첨부한 사진을 자연스럽게 보정해 주세요. 밝기와 대비, 색감을 손보고 결과를 새 파일로 저장해 주세요.",
                     "예: 어두운 사진을 밝고 선명하게", 1, (_fmt("jpg", "png", "webp"),), ("lightcraft",)),
            Template("resize", "크기 줄이기·형식 바꾸기", "첨부한 사진을 긴 변 1600px로 줄이고 지정한 형식으로 저장해 주세요.",
                     "예: 메신저로 보내게 작게 줄여줘", 1, (_fmt("jpg", "png", "webp"),), ("photocraft",)),
            Template("bw-filter", "흑백·필터 적용", "첨부한 사진을 흑백으로 바꾸고 대비를 살짝 높여 주세요.",
                     "예: 흑백 인물 사진 느낌으로", 1, (_fmt("jpg", "png"),), ("photocraft",)),
            Template("blur-bg", "흐리게·샤프닝", "첨부한 사진에 가우시안 블러를 적용해 주세요. 반경은 알아서 자연스러운 값으로 고르세요.",
                     "예: 배경용으로 흐리게", 1, (_fmt("jpg", "png"),), ("photocraft",)),
            Template("to-psd", "PSD로 변환", "첨부한 이미지를 PSD 파일로 변환해 주세요.", "예: 포토샵에서 열 수 있게", 1, (), ("photocraft",)),
            Template("raw-develop", "RAW 현상", "첨부한 RAW 사진을 현상해서 JPG로 저장해 주세요. 노출과 화이트밸런스는 자연스럽게 맞춰 주세요.",
                     "예: DNG 파일을 JPG로", 1, (), ("lightcraft",)),
        ),
        cli=(
            "photocraft: `artcraft run photocraft convert in.png out.psd` / `run in.png --cmd filter.blur.gaussianBlur --params '{\"radius\":4}' --out o.png` / `commands --filter adjust` (명령 ID 목록, 리사이즈 등)",
            "lightcraft: `artcraft run lightcraft render in.jpg -o out.jpg --set light.exposure=0.5 --size 1600`",
        ),
    ),
    Category(
        id="vector", section="image", title="일러스트·벡터", tagline="SVG 그림 만들기·고치기, PNG·PDF로 뽑기",
        icon="pen-tool", level="ready",
        apps=(AppRole("vectorcraft", "VectorCraft", "벡터 일러스트(일러스트레이터 방식)", "rootflags"),),
        can=("SVG를 PNG·JPG·PDF·SVG로 변환", "도형·경로·글자 그리기와 색 채우기", "패스파인더·변형·효과 적용"),
        cannot=("사진을 벡터로 자동 변환", "AI 일러스트 생성", ".ai 원본 완전 호환(확인 못함)"),
        accepts=("svg", "pdf", "eps", "ai", "png", "jpg", "jpeg"),
        outputs=("SVG", "PNG", "JPG", "PDF"),
        templates=(
            Template("svg-export", "SVG를 PNG·PDF로", "첨부한 SVG를 지정한 형식으로 변환해 주세요.", "예: 로고를 PNG로 뽑아줘", 1, (_fmt("png", "pdf", "jpg"),)),
            Template("draw-logo", "간단한 로고 그리기", "도형과 글자로 간단한 로고 SVG를 그려 주세요. 이름과 분위기는 아래에 적은 대로 하고 PNG 미리보기도 함께 저장해 주세요.",
                     "예: 파란 원 안에 흰 글자 'ARI' 로고"),
            Template("recolor", "색 바꾸기", "첨부한 SVG의 주요 색을 아래에 적은 색으로 바꿔 새 파일로 저장해 주세요.", "예: 파란색을 주황색으로", 1),
            Template("add-text", "글자 넣기", "첨부한 SVG에 아래 문구를 눈에 띄게 넣고 결과를 저장해 주세요.", "예: 하단에 '2026 여름 세일' 넣기", 1),
        ),
        cli=("vectorcraft: `artcraft run vectorcraft convert in.svg out.png|pdf|jpg|svg` / `info in.svg` / `commands`",),
    ),
    Category(
        id="ui", section="image", title="화면·UI 디자인", tagline="앱·웹 화면 시안, 아이콘",
        icon="smartphone", level="limited",
        apps=(AppRole("figcraft", "FigCraft", "화면 디자인(피그마 방식)", "none"),),
        can=("SVG·이미지 열기", "프레임·도형·글자 만들기", "PNG·JPG·SVG·WEBP·PDF 내보내기"),
        cannot=("여럿이 함께 편집", "Figma 클라우드 파일 열기", "플러그인 실행", "프로토타입 시연"),
        accepts=("svg", "png", "jpg", "jpeg", "webp", "gif", "bmp", "fig", "figcraft", "json"),
        outputs=("PNG", "JPG", "SVG", "PDF"),
        templates=(
            Template("mobile-screen", "모바일 화면 시안", "아래 설명대로 모바일 앱 화면 시안 하나를 만들고 PNG로 내보내 주세요.", "예: 로그인 화면, 이메일·비밀번호 입력칸과 버튼"),
            Template("icon", "아이콘 만들기", "아래 설명대로 간단한 아이콘을 만들어 SVG와 PNG로 저장해 주세요.", "예: 둥근 모서리 사각형 안에 번개 모양"),
            Template("render-svg", "SVG를 이미지로", "첨부한 파일을 PNG로 내보내 주세요.", "예: 2배 크기로", 1, (_fmt("png", "jpg", "webp"),)),
        ),
        cli=("figcraft: `artcraft run figcraft render in.svg -o out.png --scale 2` / `export f.figcraft -o out.pdf --format pdf` / `info in.svg`",),
    ),
    Category(
        id="layout", section="image", title="인쇄물·편집 디자인", tagline="포스터·브로셔·여러 쪽 레이아웃",
        icon="layout", level="experimental",
        apps=(AppRole("designcraft", "DesignCraft", "인쇄물 편집(인디자인 방식)", "none"),),
        can=("여러 쪽 문서 레이아웃 만들기", "글 흐름 편집", "이미지 배치", "PNG·PDF 내보내기"),
        cannot=("인쇄소 제출용 색 관리·PDF/X", "InDesign 파일 호환(확인 못함)", "안정성 보장(프리알파)"),
        accepts=("png", "jpg", "jpeg", "svg", "pdf", "txt", "md", "docx"),
        outputs=("PNG", "PDF"),
        templates=(
            Template("poster", "한 장짜리 포스터", "아래 내용으로 한 장짜리 포스터를 만들어 PDF와 PNG로 저장해 주세요.", "예: 동네 벼룩시장 안내, 날짜·장소·시간"),
            Template("brochure", "여러 쪽 안내서", "첨부한 글과 이미지로 2~4쪽 안내서를 만들어 PDF로 저장해 주세요.", "예: 제품 소개서", 1),
        ),
        cli=("designcraft: `artcraft run designcraft run --sample --export out.pdf` / `run --in file --cmd ID=JSON --export out.png` (명령 ID는 `commands`)",),
    ),
    Category(
        id="video", section="av", title="영상 편집", tagline="영상 자르기·이어 붙이기·내보내기",
        icon="film", level="limited",
        apps=(AppRole("filmcraft", "FilmCraft", "영상 편집(프리미어 방식)", "none"),),
        can=("영상·소리 파일 가져오기", "타임라인에서 자르고 이어 붙이기", "MP4(H.264·HEVC)·ProRes·GIF·WAV 내보내기", "한 장면을 PNG로 뽑기", "영상 정보 읽기"),
        cannot=("자동 자막", "AI 편집", "큰 4K 편집 속도 보장(측정 안 함)", "복잡한 자막·전환 자동 구성"),
        accepts=("mp4", "mov", "m4v", "webm", "mkv", "avi", "mp3", "wav", "aac", "m4a", "png", "jpg", "jpeg"),
        outputs=("MP4", "GIF", "PNG", "WAV"),
        templates=(
            Template("trim", "영상 구간 자르기", "첨부한 영상에서 아래 구간만 잘라 MP4로 저장해 주세요.", "예: 5초부터 15초까지", 1, (_fmt("mp4", "gif"),)),
            Template("join", "영상 이어 붙이기", "첨부한 영상들을 올린 순서대로 이어 붙여 MP4 하나로 저장해 주세요.", "예: 클립 3개를 하나로", 2),
            Template("frame", "한 장면 이미지로 뽑기", "첨부한 영상에서 아래 시점의 장면을 PNG로 저장해 주세요.", "예: 3초 지점", 1),
            Template("info", "영상 정보 확인", "첨부한 영상의 길이·해상도·코덱 정보를 알려 주세요.", "예: 해상도와 길이", 1),
        ),
        cli=("filmcraft: `artcraft run filmcraft probe in.mp4` / `export out.mp4 --format h264 --start 0 --end 1` / 편집은 MCP `media_import`, `command_run` (명령 ID는 `commands`)",),
    ),
    Category(
        id="motion", section="av", title="모션·영상 효과", tagline="글자 움직임·합성·효과 영상",
        icon="sparkles", level="limited",
        apps=(AppRole("effectcraft", "EffectCraft", "모션 그래픽(애프터이펙트 방식)", "rootflags"),),
        can=("레이어·키프레임·효과 합성", "한 장면 PNG 렌더", "구간을 H.264·WebM·GIF 등 영상으로 렌더"),
        cannot=("After Effects 프로젝트(.aep) 열기", "외부 플러그인"),
        accepts=("png", "jpg", "jpeg", "svg", "mp4", "mov", "webm", "wav", "mp3", "ecproj"),
        outputs=("PNG", "MP4", "WEBM", "GIF"),
        templates=(
            Template("title-card", "제목 카드 영상", "아래 문구가 부드럽게 나타났다 사라지는 3초짜리 제목 영상을 만들어 MP4로 저장해 주세요.", "예: '2026 여름 여행' 흰 글자, 어두운 배경"),
            Template("still-frame", "합성 장면 이미지", "첨부한 이미지 위에 아래 문구를 얹은 장면을 PNG로 렌더해 주세요.", "예: 사진 위에 큰 제목", 1),
        ),
        cli=("effectcraft: `artcraft run effectcraft --demo render-frame --out f.png` / `--project X.ecproj render --out o.mp4` / `commands`",),
    ),
    Category(
        id="audio", section="av", title="오디오", tagline="소리 파일 변환·크기 맞추기·믹스",
        icon="music", level="experimental",
        apps=(AppRole("soundcraft", "SoundCraft", "오디오 편집(프로툴스 방식)", "rootflags"),),
        can=("WAV·AIFF·FLAC 정보 보기와 서로 변환", "비트 깊이·샘플레이트 바꾸기", "소리 크기 맞추기(노멀라이즈)", "여러 트랙 믹스를 WAV로 내보내기"),
        cannot=("MP3·M4A로 저장(직접 확인: 불가)", "음성 인식·합성", "AI 노이즈 제거"),
        accepts=("wav", "aif", "aiff", "flac", "mp3", "m4a", "aac", "ogg", "scraft"),
        outputs=("WAV", "AIFF", "FLAC"),
        templates=(
            Template("normalize", "소리 크기 맞추기", "첨부한 오디오의 소리 크기를 맞춰(노멀라이즈) 지정한 형식으로 저장해 주세요.", "예: 너무 작은 녹음을 키워줘", 1, (_fmt("wav", "flac"),)),
            Template("convert", "형식 바꾸기", "첨부한 오디오를 지정한 형식과 24비트로 변환해 주세요.", "예: WAV를 FLAC으로", 1, (_fmt("flac", "wav"),)),
            Template("info", "오디오 정보 확인", "첨부한 오디오의 길이·채널·샘플레이트·최대 음량을 알려 주세요.", "예: 샘플레이트가 몇이야?", 1),
        ),
        cli=("soundcraft: `artcraft run soundcraft info f.wav` / `convert in out --bit-depth 24 --normalize` (저장 가능: wav, aif, aiff, flac) / `run --demo --bounce out.wav`",),
    ),
    Category(
        id="pdf", section="doc", title="PDF 편집", tagline="PDF 합치기·나누기·페이지 정리·내용 읽기",
        icon="file-pdf", level="ready",
        apps=(AppRole("pdfcraft", "PdfCraft", "PDF 도구(아크로뱃 방식)", "root", compact=True),),
        can=("텍스트 읽기·페이지 미리보기 이미지", "합치기·나누기·쪽 뽑기", "회전·삭제·순서 바꾸기·빈 쪽 넣기", "제목·작성자 고치기",
             "쪽 번호·책갈피·스탬프", "양식 채우기", "가린 척한 글자(가짜 가림) 검사"),
        cannot=("본문 글자를 자유롭게 고치기(제한적)", "인증서 전자서명", "클라우드 서명"),
        accepts=("pdf", "png", "jpg", "jpeg", "txt", "md", "docx"),
        outputs=("PDF", "PNG"),
        templates=(
            Template("merge", "PDF 합치기", "첨부한 PDF들을 올린 순서대로 하나로 합쳐 주세요.", "예: 계약서 3개를 한 파일로", 2),
            Template("extract", "필요한 쪽만 뽑기", "첨부한 PDF에서 아래 쪽만 뽑아 새 PDF로 저장해 주세요.", "예: 1쪽, 3쪽, 5쪽만", 1),
            Template("split", "나누기", "첨부한 PDF를 아래 기준으로 나눠 주세요. 결과는 파일 여러 개로 저장해 주세요.", "예: 5쪽마다 나누기", 1),
            Template("reorder", "페이지 정리", "첨부한 PDF의 페이지를 아래 대로 정리해 주세요(회전·삭제·순서 바꾸기).", "예: 2쪽 삭제, 3쪽을 맨 앞으로", 1),
            Template("summarize", "내용 요약", "첨부한 PDF의 내용을 읽고 핵심을 항목별로 요약해 문서(마크다운)로 저장해 주세요.", "예: 한 장 요약", 1),
            Template("preview-images", "쪽을 이미지로", "첨부한 PDF의 각 쪽을 PNG 이미지로 저장해 주세요.", "예: 첫 3쪽만 이미지로", 1),
        ),
        cli=("pdfcraft: `artcraft run pdfcraft info|text|render f.pdf` / `combine a.pdf b.pdf --out c.pdf` / `extract in.pdf --pages 1,3 --out o.pdf` / `split in.pdf --every 5 --out-dir d` / `edit in.pdf --out o.pdf --rotate 2:90 --delete 3 --move 5:1 --title T`",),
    ),
    Category(
        id="doc", section="doc", title="문서 작성", tagline="보고서·편지 만들기, 워드(DOCX) 변환",
        icon="file-text", level="ready",
        apps=(AppRole("wordcraft", "WordCraft", "문서 작성(워드 방식)", "rootflags"),),
        can=("마크다운·텍스트로 DOCX 만들기", "DOCX를 PDF·ODT·RTF·HTML·MD·TXT로 변환", "문서 내용 읽기", "쪽을 이미지로 확인", "제목·표·변경 추적·메일 머지 같은 편집 명령"),
        cannot=("HWP(한글) 파일 저장(직접 확인: 불가)", "복잡한 서식의 완벽한 재현"),
        accepts=("docx", "doc", "odt", "rtf", "md", "markdown", "txt", "html", "htm", "pdf", "tex", "json", "png", "jpg", "jpeg"),
        outputs=("DOCX", "PDF", "ODT", "MD", "HTML"),
        templates=(
            Template("write-report", "보고서 쓰기", "아래 주제로 한 쪽에서 두 쪽 분량의 보고서를 써서 DOCX와 PDF로 저장해 주세요. 제목·소제목·요약 목록이 있으면 좋아요.",
                     "예: 2분기 판매 현황 보고서"),
            Template("md-to-docx", "마크다운을 워드로", "첨부한 파일을 DOCX와 PDF로 변환해 주세요.", "예: 메모를 보고서 파일로", 1),
            Template("docx-to-pdf", "워드를 PDF로", "첨부한 문서를 PDF로 변환하고 첫 쪽 미리보기 이미지도 함께 저장해 주세요.", "예: 제출용 PDF", 1),
            Template("edit-doc", "문서 고치기", "첨부한 문서를 아래 요청대로 고쳐 새 파일로 저장해 주세요. 원본은 그대로 두세요.", "예: 맨 끝에 감사 인사 문단 추가", 1),
            Template("letter", "편지·공문 쓰기", "아래 내용으로 정중한 편지(또는 공문)를 써서 DOCX로 저장해 주세요.", "예: 거래처에 납기 연기를 알리는 편지"),
        ),
        cli=("wordcraft: `artcraft run wordcraft convert in.md out.docx` / `convert out.docx out.pdf` / `text f.docx` / `render f.docx out.png` / `run --file f.docx --cmd 'ID={json}' --save out.docx` (명령 ID는 `commands`)",),
    ),
    Category(
        id="slides", section="doc", title="발표 자료", tagline="슬라이드 만들기, 이미지·PDF로 뽑기",
        icon="presentation", level="limited",
        apps=(AppRole("deckcraft", "DeckCraft", "발표 자료(파워포인트 방식)", "none"),),
        can=("PPTX 만들기·열기·저장", "슬라이드 추가", "글상자·도형·표·차트·그림 넣기", "슬라이드를 PNG로, 전체를 PDF·ODP로 내보내기"),
        cannot=("애니메이션 결과를 영상으로 뽑기", "AI 디자인 추천", "발표 중 원격 조작"),
        accepts=("pptx", "ppt", "odp", "pdf", "png", "jpg", "jpeg", "svg", "md", "txt", "csv", "xlsx"),
        outputs=("PPTX", "PDF", "PNG"),
        templates=(
            Template("make-deck", "발표 자료 만들기", "아래 주제로 5장 안팎의 발표 자료를 만들어 PPTX와 PDF로 저장해 주세요. 각 장은 제목과 핵심 문장 위주로요.",
                     "예: 신입 사원 안내 5장"),
            Template("deck-from-doc", "문서로 슬라이드 만들기", "첨부한 문서의 내용을 슬라이드로 정리해 PPTX와 PDF로 저장해 주세요.", "예: 보고서를 발표용으로", 1),
            Template("deck-to-pdf", "슬라이드를 PDF로", "첨부한 발표 자료를 PDF로 내보내고 첫 장 미리보기 이미지도 저장해 주세요.", "예: 메일 첨부용 PDF", 1),
            Template("add-slide", "슬라이드 추가", "첨부한 발표 자료 뒤에 아래 내용의 슬라이드를 추가해 새 파일로 저장해 주세요.", "예: 마지막에 질의응답 장 추가", 1),
        ),
        cli=("deckcraft: `artcraft run deckcraft run --sample --save out.pptx` / `render f.pptx --slide 1 out.png` / `run --in f.pptx --export out.pdf` / `convert in.pptx out.odp` (명령 ID는 `commands`)",),
    ),
    Category(
        id="sheet", section="doc", title="스프레드시트", tagline="표 정리, 수식 계산, CSV·XLSX 변환",
        icon="sheet", level="ready",
        apps=(AppRole("gridcraft", "GridCraft", "스프레드시트(엑셀 방식)", "none"),),
        can=("CSV·TSV·XLSX·JSON·HTML 변환", "수식 계산", "셀 값 쓰기", "정렬·필터·서식·표·차트 명령"),
        cannot=("매크로(VBA)", "외부 데이터 연결", "PDF로 직접 저장(문서 앱으로 옮겨 PDF화)"),
        accepts=("csv", "tsv", "xlsx", "xls", "json", "html", "txt"),
        outputs=("XLSX", "CSV", "JSON", "HTML"),
        templates=(
            Template("csv-to-xlsx", "CSV를 엑셀로", "첨부한 파일을 XLSX로 변환해 주세요. 첫 줄은 제목 줄로 굵게 해 주세요.", "예: 내보낸 CSV를 엑셀 파일로", 1),
            Template("add-total", "합계·평균 넣기", "첨부한 표에 숫자 열의 합계와 평균 행을 수식으로 추가해 새 XLSX로 저장해 주세요.", "예: 수량·단가 합계", 1),
            Template("clean-sort", "정렬·정리", "첨부한 표를 아래 기준으로 정렬하고 중복 행을 제거해 새 파일로 저장해 주세요.", "예: 단가 높은 순으로", 1),
            Template("make-table", "표 만들기", "아래 내용으로 표를 만들어 XLSX로 저장해 주세요. 합계 수식도 넣어 주세요.", "예: 월별 지출표, 항목 5개"),
        ),
        cli=("gridcraft: `artcraft run gridcraft convert in.csv out.xlsx` / `cat f.xlsx --csv` / `info f.xlsx --json` / `eval '=SUM(1,2,3)'` / `run --in f.xlsx --cmd 'cell.set={\"cell\":\"D1\",\"value\":\"합계\"}' --out o.xlsx` (명령 ID는 `commands`)",),
    ),
    Category(
        id="cad", section="cad", title="도면·CAD(2D)", tagline="2D 도면 보기·그리기, SVG·PDF 변환",
        icon="ruler", level="experimental",
        apps=(AppRole("cadcraft", "CADCraft", "2D 도면(오토캐드 방식)", "none"),),
        can=("DXF 열기·내용 조회", "샘플 도면 만들기", "선·원·치수·해치 같은 도면 명령", "SVG·PNG·PDF로 변환"),
        cannot=("3D 모델링(STEP·STL)", "DWG 파일(확인 못함)", "안정성 보장(프리알파)"),
        accepts=("dxf", "svg", "png", "pdf"),
        outputs=("DXF", "SVG", "PNG", "PDF"),
        templates=(
            Template("dxf-to-pdf", "도면을 PDF·PNG로", "첨부한 DXF를 PDF와 PNG로 변환해 주세요.", "예: 도면 제출용 PDF", 1),
            Template("dxf-info", "도면 내용 조회", "첨부한 DXF에 들어 있는 도형 종류와 개수, 치수 정보를 정리해 알려 주세요.", "예: 원이 몇 개야?", 1),
            Template("sample-bracket", "샘플 도면 만들기", "샘플 브래킷 도면을 만들어 DXF와 PNG로 저장해 주세요.", "예: 연습용 도면"),
        ),
        cli=("cadcraft: `artcraft run cadcraft sample bracket out.dxf` / `info f.dxf` / `convert f.dxf out.svg|png|pdf` (`--help`는 exit 1이지만 정상)",),
    ),
)

_BY_ID = {c.id: c for c in CATEGORIES}

# 지시문 한도(입력 검증용). 첨부 개수는 한 번에 10개까지.
PROMPT_MAX = 8000
FILES_MAX = 10


def category(category_id: str) -> Category | None:
    return _BY_ID.get(category_id)


def template(cat: Category, template_id: str | None) -> Template | None:
    if not template_id:
        return None
    return next((t for t in cat.templates if t.id == template_id), None)


def app_ids(cat: Category) -> tuple[str, ...]:
    return tuple(a.app for a in cat.apps)


def public(cat: Category) -> dict[str, Any]:
    label, summary = LEVELS[cat.level]
    return {
        "id": cat.id, "section": cat.section, "title": cat.title, "tagline": cat.tagline, "icon": cat.icon,
        "maturity": {"level": cat.level, "label": label, "summary": summary},
        "apps": [{"app": a.app, "title": a.title, "role": a.role} for a in cat.apps],
        "can": list(cat.can), "cannot": list(cat.cannot),
        "accepts": list(cat.accepts), "outputs": list(cat.outputs),
        "templates": [{
            "id": t.id, "title": t.title, "prompt": t.prompt, "example": t.example, "min_files": t.min_files,
            "apps": list(t.apps) or list(app_ids(cat)),
            "options": [{"id": o.id, "label": o.label, "default": o.default,
                         "choices": [{"value": v, "label": lab} for v, lab in o.choices]} for o in t.options],
        } for t in cat.templates],
    }


def validate(registry_apps: set[str]) -> list[str]:
    """카탈로그와 registry 의 불일치를 문자열 목록으로 돌려준다(비면 정상)."""
    problems: list[str] = []
    seen: set[str] = set()
    sections = {s for s, _ in SECTIONS}
    for cat in CATEGORIES:
        if cat.id in seen:
            problems.append(f"중복 범주: {cat.id}")
        seen.add(cat.id)
        if cat.section not in sections:
            problems.append(f"{cat.id}: 알 수 없는 묶음 {cat.section}")
        if cat.level not in LEVELS:
            problems.append(f"{cat.id}: 알 수 없는 성숙도 {cat.level}")
        for a in cat.apps:
            if a.app not in registry_apps:
                problems.append(f"{cat.id}: registry 에 없는 앱 {a.app}")
            if a.confine not in ("rootflags", "root", "none"):
                problems.append(f"{cat.id}/{a.app}: 알 수 없는 confine {a.confine}")
        ids = [t.id for t in cat.templates]
        if len(ids) != len(set(ids)):
            problems.append(f"{cat.id}: 템플릿 ID 중복")
        for t in cat.templates:
            for app in t.apps:
                if app not in app_ids(cat):
                    problems.append(f"{cat.id}/{t.id}: 범주 밖 앱 {app}")
            for o in t.options:
                if o.default not in {v for v, _ in o.choices}:
                    problems.append(f"{cat.id}/{t.id}/{o.id}: 기본값이 선택지에 없음")
    return problems
