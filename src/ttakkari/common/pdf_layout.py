"""PDF 쪽 레이아웃 분석: 다단 읽기 순서, 표, 제목을 글자 좌표로 복원한다.

pypdf 의 기본 추출은 콘텐츠 스트림 순서대로 이어 붙여서 다단 문서는 줄이 섞이고 표는 한 줄로 깨진다.
여기서는 글자 조각의 좌표로 줄을 만들고, 줄 안의 큰 간격으로 칸을 나눈 뒤 표·다단·제목을 가려 낸다.
글꼴 폭 정보를 읽지 않으므로 폭은 글자 종류별 추정치다. 정확한 조판이 아니라 구조 복원이 목적이다.
"""

from __future__ import annotations

import contextlib
import dataclasses
import statistics
import unicodedata
from typing import Any

# 한 줄로 보는 y 차이(글자 크기 배수), 같은 구절로 이어 붙이는 간격, 칸을 가르는 간격
_LINE_TOL = 0.55
_JOIN_GAP = 0.9
_CELL_GAP = 0.8
_MIN_TABLE_ROWS = 3
_COLUMN_MIN_CHARS = 28
_HEADING_RATIO = 1.22
_HEADING_MAX_CHARS = 90


@dataclasses.dataclass
class Fragment:
    x0: float
    x1: float
    y: float
    size: float
    text: str


@dataclasses.dataclass
class Cell:
    x0: float
    x1: float
    text: str
    # 합치기 전 글자 조각 (x0, x1, text). 열 띠가 정해진 뒤 한 셀로 뭉친 줄을 다시 가르는 데 쓴다.
    parts: list[tuple[float, float, str]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Row:
    y: float
    size: float
    cells: list[Cell]

    @property
    def text(self) -> str:
        return " ".join(c.text for c in self.cells)

    @property
    def x0(self) -> float:
        return self.cells[0].x0

    @property
    def x1(self) -> float:
        return self.cells[-1].x1


@dataclasses.dataclass
class Block:
    kind: str  # prose | table | heading
    text: str
    level: int = 0


@dataclasses.dataclass
class PageLayout:
    blocks: list[Block]
    columns: bool
    tables: int
    headings: int

    @property
    def structured(self) -> bool:
        return self.columns or self.tables > 0 or self.headings > 0


def _char_width(ch: str, size: float) -> float:
    if ch.isspace():
        return size * 0.3
    return size * (1.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 0.52)


def _simple_font_widths(font: Any) -> tuple[int, list[float]] | None:
    """단순 글꼴(Type1/TrueType)의 /FirstChar 와 /Widths(1000 단위)를 읽는다. 복합 글꼴은 None."""
    try:
        font = font.get_object() if hasattr(font, "get_object") else font
        first = font.get("/FirstChar")
        widths = font.get("/Widths")
        if first is None or widths is None:
            return None
        widths = widths.get_object() if hasattr(widths, "get_object") else widths
        return int(first), [float(w) for w in widths]
    except Exception:  # noqa: BLE001
        return None


def _text_width(text: str, size: float, table: tuple[int, list[float]] | None) -> float:
    if table is None:
        return sum(_char_width(ch, size) for ch in text)
    first, widths = table
    known = [w for w in widths if w > 0]
    fallback = (sum(known) / len(known)) if known else 500.0
    total = 0.0
    for ch in text:
        code = ord(ch)
        idx = code - first
        if 0 <= idx < len(widths) and widths[idx] > 0:
            total += widths[idx]
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            total += 1000.0
        else:
            total += fallback
    return total / 1000.0 * size


def collect_fragments(page: Any) -> list[Fragment]:
    """visitor_text 로 글자 조각과 좌표를 모은다. 좌표계는 PDF 사용자 공간(위쪽이 큰 y)."""
    frags: list[Fragment] = []
    tables: dict[int, tuple[int, list[float]] | None] = {}

    def visit(text: str, cm: list[float], tm: list[float], font: Any, size: float) -> None:
        if not text or not text.strip():
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        scale = abs(tm[0] * cm[0]) or 1.0
        eff = max(1.0, (size or 10.0) * scale)
        key = id(font)
        if key not in tables:
            tables[key] = _simple_font_widths(font) if font is not None else None
        frags.append(Fragment(x, x + _text_width(text, eff, tables[key]), y, eff, text))

    page.extract_text(visitor_text=visit)
    return frags


def build_rows(frags: list[Fragment]) -> list[Row]:
    if not frags:
        return []
    ordered = sorted(frags, key=lambda f: (-f.y, f.x0))
    lines: list[list[Fragment]] = []
    for frag in ordered:
        if lines and abs(lines[-1][0].y - frag.y) <= _LINE_TOL * max(frag.size, lines[-1][0].size):
            lines[-1].append(frag)
        else:
            lines.append([frag])
    rows: list[Row] = []
    for line in lines:
        line.sort(key=lambda f: f.x0)
        size = statistics.median(f.size for f in line)
        cells: list[Cell] = []
        for frag in line:
            text = frag.text.replace("\n", " ").strip()
            if not text:
                continue
            if cells and frag.x0 - cells[-1].x1 < _CELL_GAP * size:
                gap = frag.x0 - cells[-1].x1
                joiner = " " if gap > 0.15 * size else ""
                prev = cells[-1]
                cells[-1] = Cell(
                    prev.x0, max(prev.x1, frag.x1), prev.text + joiner + text, [*prev.parts, (frag.x0, frag.x1, text)],
                )
            else:
                cells.append(Cell(frag.x0, frag.x1, text, [(frag.x0, frag.x1, text)]))
        if cells:
            rows.append(Row(statistics.fmean(f.y for f in line), size, cells))
    return rows


def _bands(rows: list[Row], tol: float) -> list[tuple[float, float]]:
    """셀의 가로 구간을 쪽 전체로 투영해 겹치는 것끼리 합친 열 띠. 정렬 방식(왼쪽·오른쪽·가운데)과 무관하다."""
    spans = sorted((c.x0, c.x1) for r in rows for c in r.cells)
    merged: list[list[float]] = []
    for x0, x1 in spans:
        if merged and x0 <= merged[-1][1] + tol:
            merged[-1][1] = max(merged[-1][1], x1)
        else:
            merged.append([x0, x1])
    return [(a, b) for a, b in merged]


def _band_index(bands: list[tuple[float, float]], cell: Cell) -> int:
    centre = (cell.x0 + cell.x1) / 2
    for k, (a, b) in enumerate(bands):
        if a <= centre <= b:
            return k
    return min(range(len(bands)), key=lambda k: min(abs(centre - bands[k][0]), abs(centre - bands[k][1])))


def _resplit(row: Row, bands: list[tuple[float, float]]) -> Row | None:
    """한 셀로 뭉친 줄도 글자 조각이 서로 다른 열 띠에 놓이면 열별 셀로 다시 가른다."""
    if len(row.cells) >= 2:
        return row if len({_band_index(bands, c) for c in row.cells}) == len(row.cells) else None
    parts = row.cells[0].parts
    if len(parts) < 2:
        return None
    by_band: dict[int, list[tuple[float, float, str]]] = {}
    for part in parts:
        by_band.setdefault(_band_index(bands, Cell(part[0], part[1], part[2])), []).append(part)
    if len(by_band) < 2:
        return None
    cells = [Cell(g[0][0], g[-1][1], " ".join(t for _, _, t in g), list(g)) for _, g in sorted(by_band.items())]
    return Row(row.y, row.size, cells)


def _extend_run(rows: list[Row], i: int, j: int, bands: list[tuple[float, float]]) -> tuple[int, int]:
    """표 앞뒤로 인접한 줄(제목줄·마지막 줄)이 같은 열 띠에 들어맞으면 표에 포함한다."""
    size = statistics.median(r.size for r in rows[i:j])
    while i > 0 and rows[i].y - rows[i - 1].y < 0 and rows[i - 1].y - rows[i].y < 2.4 * size:
        fixed = _resplit(rows[i - 1], bands)
        if fixed is None:
            break
        rows[i - 1] = fixed
        i -= 1
    while j < len(rows) and rows[j - 1].y - rows[j].y < 2.4 * size:
        fixed = _resplit(rows[j], bands)
        if fixed is None:
            break
        rows[j] = fixed
        j += 1
    return i, j


def _table_runs(rows: list[Row]) -> list[tuple[int, int, list[tuple[float, float]]]]:
    """셀이 둘 이상인 줄이 이어지고 열 띠가 줄마다 일관되게 갈라지면 표로 본다. (시작, 끝(미포함), 열 띠)"""
    runs: list[tuple[int, int, list[tuple[float, float]]]] = []
    i = 0
    while i < len(rows):
        if len(rows[i].cells) < 2:
            i += 1
            continue
        j = i
        while j < len(rows) and len(rows[j].cells) >= 2:
            j += 1
        if j - i >= _MIN_TABLE_ROWS:
            size = statistics.median(r.size for r in rows[i:j])
            bands = _bands(rows[i:j], 0.3 * size)
            # 한 줄의 두 셀이 같은 열 띠에 들어가면 열이 갈라지지 않은 것이다(문장 속 큰 공백).
            clean = sum(1 for r in rows[i:j] if len({_band_index(bands, c) for c in r.cells}) == len(r.cells))
            if len(bands) >= 2 and clean >= 0.9 * (j - i):
                start, end = _extend_run(rows, i, j, bands)
                if runs and start < runs[-1][1]:
                    start = runs[-1][1]
                if end - start >= _MIN_TABLE_ROWS:
                    runs.append((start, end, bands))
        i = max(j, runs[-1][1] if runs else j)
    return runs


def _is_text_columns(rows: list[Row], bands: list[tuple[float, float]]) -> bool:
    """열이 둘뿐이고 셀이 문장 길이면 표가 아니라 다단 본문이다."""
    if len(bands) != 2:
        return False
    cells = [c for r in rows for c in r.cells]
    long_cells = sum(1 for c in cells if len(c.text) >= _COLUMN_MIN_CHARS)
    return all(len(r.cells) <= 2 for r in rows) and long_cells >= 0.7 * len(cells)


def _cell_len_median(rows: list[Row]) -> float:
    return statistics.median(len(c.text) for r in rows for c in r.cells)


def _table_fill(rows: list[Row], bands: list[tuple[float, float]]) -> float:
    """표 칸 중 글자가 있는 칸의 비율. 수식·도식 조각은 칸이 듬성듬성해 낮게 나온다."""
    filled = sum(len({_band_index(bands, c) for c in r.cells}) for r in rows)
    return filled / max(1, len(rows) * len(bands))


def _markdown_table(rows: list[Row], bands: list[tuple[float, float]]) -> str:
    grid: list[list[str]] = []
    for r in rows:
        line = [""] * len(bands)
        for c in r.cells:
            idx = _band_index(bands, c)
            line[idx] = (line[idx] + " " + c.text).strip()
        grid.append([cell.replace("|", "\\|") for cell in line])
    out = ["| " + " | ".join(grid[0]) + " |", "| " + " | ".join(["---"] * len(bands)) + " |"]
    out += ["| " + " | ".join(g) + " |" for g in grid[1:]]
    return "\n".join(out)


def _body_size(rows: list[Row]) -> float:
    weighted: list[float] = []
    for r in rows:
        weighted.extend([r.size] * max(1, len(r.text)))
    return statistics.median(weighted) if weighted else 10.0


def _heading_level(row: Row, body: float) -> int:
    if len(row.cells) != 1 or len(row.text) > _HEADING_MAX_CHARS or row.size < body * _HEADING_RATIO:
        return 0
    ratio = row.size / body
    return 1 if ratio >= 1.7 else 2 if ratio >= 1.4 else 3


def _paragraphs(rows: list[Row]) -> list[str]:
    """연속한 본문 줄을 줄 간격이 벌어지는 곳에서 문단으로 나눈다."""
    out: list[str] = []
    cur: list[str] = []
    prev: Row | None = None
    for r in rows:
        if prev is not None and (prev.y - r.y) > 1.9 * max(prev.size, r.size) and cur:
            out.append("\n".join(cur))
            cur = []
        cur.append(r.text)
        prev = r
    if cur:
        out.append("\n".join(cur))
    return out


def analyze_rows(rows: list[Row], *, table_allowed: bool = True, body: float | None = None) -> PageLayout:
    """한 단(또는 단이 없는 쪽)의 줄 목록에서 제목·표·본문 블록을 읽기 순서대로 만든다.

    table_allowed 가 False 면(문서 구조 태그가 이 쪽에 표가 없다고 알려 줄 때) 표 후보를 본문으로 둔다.
    """
    if not rows:
        return PageLayout([], False, 0, 0)
    body = body or _body_size(rows)
    runs = _table_runs(rows)
    skip_tables = not table_allowed
    table_at = {s: (e, a) for s, e, a in runs}

    blocks: list[Block] = []
    pending: list[Row] = []
    columns = False
    tables = headings = 0

    def flush() -> None:
        nonlocal headings
        if not pending:
            return
        buf: list[Row] = []
        for r in pending:
            level = _heading_level(r, body)
            if level:
                for para in _paragraphs(buf):
                    blocks.append(Block("prose", para))
                buf = []
                blocks.append(Block("heading", r.text, level))
                headings += 1
            else:
                buf.append(r)
        for para in _paragraphs(buf):
            blocks.append(Block("prose", para))
        pending.clear()

    i = 0
    while i < len(rows):
        if i in table_at:
            end, bands = table_at[i]
            segment = rows[i:end]
            if _is_text_columns(segment, bands):
                columns = True
                flush()
                mid = (bands[0][1] + bands[1][0]) / 2
                left = [Row(r.y, r.size, [c for c in r.cells if (c.x0 + c.x1) / 2 < mid]) for r in segment]
                right = [Row(r.y, r.size, [c for c in r.cells if (c.x0 + c.x1) / 2 >= mid]) for r in segment]
                for side in (left, right):
                    pending.extend(r for r in side if r.cells)
                    flush()
            elif skip_tables or _table_fill(segment, bands) < 0.6 or _cell_len_median(segment) < 3:
                pending.extend(segment)
            else:
                flush()
                blocks.append(Block("table", _markdown_table(segment, bands)))
                tables += 1
            i = end
            continue
        pending.append(rows[i])
        i += 1
    flush()
    return PageLayout(blocks, columns, tables, headings)


def detect_gutter(frags: list[Fragment], page_width: float) -> float | None:
    """좌우 두 단 사이의 세로 빈 띠 위치(x)를 찾는다. 단이 아니면 None.

    글자 조각이 거의 가로지르지 않는 x 가 있고, 양쪽 모두 문장 길이의 줄이 충분할 때만 단으로 본다.
    셀이 짧은 두 칸 표는 여기서 걸러져 표 탐지로 넘어간다.
    """
    if len(frags) < 20 or page_width <= 0:
        return None
    size = statistics.median(f.size for f in frags)
    margin = 0.5 * size
    best: tuple[int, float] | None = None
    x = page_width * 0.30
    while x <= page_width * 0.70:
        crossing = sum(1 for f in frags if f.x0 < x - margin and f.x1 > x + margin)
        if best is None or crossing < best[0]:
            best = (crossing, x)
        x += max(1.0, size * 0.5)
    if best is None or best[0] > 0.05 * len(frags):
        return None
    # 같은 최소값을 갖는 연속 구간의 가운데를 띠 위치로 쓴다.
    lo = hi = best[1]
    step = max(1.0, size * 0.5)
    while lo - step >= page_width * 0.30 and sum(1 for f in frags if f.x0 < lo - step - margin and f.x1 > lo - step + margin) <= best[0]:
        lo -= step
    while hi + step <= page_width * 0.70 and sum(1 for f in frags if f.x0 < hi + step - margin and f.x1 > hi + step + margin) <= best[0]:
        hi += step
    gutter = (lo + hi) / 2
    left = [f for f in frags if (f.x0 + f.x1) / 2 < gutter]
    right = [f for f in frags if (f.x0 + f.x1) / 2 >= gutter]
    if len(left) < 0.25 * len(frags) or len(right) < 0.25 * len(frags):
        return None

    def lengths(side: list[Fragment]) -> float:
        rows = build_rows(side)
        return statistics.median(len(r.text) for r in rows) if rows else 0.0

    def span(side: list[Fragment]) -> tuple[float, float]:
        ys = [f.y for f in side]
        return min(ys), max(ys)

    (l0, l1), (r0, r1) = span(left), span(right)
    overlap = min(l1, r1) - max(l0, r0)
    if overlap < 0.6 * min(l1 - l0, r1 - r0) or min(lengths(left), lengths(right)) < _COLUMN_MIN_CHARS:
        return None
    return gutter


def analyze_page(frags: list[Fragment], page_width: float, *, table_allowed: bool = True) -> PageLayout:
    """글자 조각에서 쪽 전체의 읽기 순서 블록을 만든다. 다단이면 위에서 아래 띠마다 왼쪽 단, 오른쪽 단 순이다."""
    if not frags:
        return PageLayout([], False, 0, 0)
    all_rows = build_rows(frags)
    body = _body_size(all_rows)
    gutter = detect_gutter(frags, page_width)
    if gutter is None:
        return analyze_rows(all_rows, table_allowed=table_allowed, body=body)

    size = statistics.median(f.size for f in frags)
    margin = 0.5 * size
    full_frags = [f for f in frags if f.x0 < gutter - margin and f.x1 > gutter + margin]
    full_rows = build_rows(full_frags)
    full_ids = {id(f) for f in full_frags}
    rest = [f for f in frags if id(f) not in full_ids]

    def band_of(f: Fragment) -> int:
        return sum(1 for r in full_rows if r.y > f.y + _LINE_TOL * f.size)

    blocks: list[Block] = []
    tables = headings = 0
    for k in range(len(full_rows) + 1):
        band = [f for f in rest if band_of(f) == k]
        for side in ([f for f in band if (f.x0 + f.x1) / 2 < gutter], [f for f in band if (f.x0 + f.x1) / 2 >= gutter]):
            sub = analyze_rows(build_rows(side), table_allowed=table_allowed, body=body)
            blocks += sub.blocks
            tables += sub.tables
            headings += sub.headings
        if k < len(full_rows):
            sub = analyze_rows([full_rows[k]], table_allowed=False, body=body)
            blocks += sub.blocks
            headings += sub.headings
    return PageLayout(blocks, True, tables, headings)


def structure_table_pages(reader: Any, *, node_limit: int = 200_000) -> set[int] | None:
    """태그된 PDF 의 구조 트리에서 표(Table)가 있는 쪽 번호(0부터) 집합을 돌려준다. 태그가 없으면 None.

    태그는 제작 도구가 직접 적은 구조라 좌표 추정보다 믿을 만하다. 표가 없다고 적힌 쪽은 표 탐지를 끈다.
    """
    try:
        root = reader.trailer["/Root"]
        mark = root.get("/MarkInfo")
        mark = mark.get_object() if hasattr(mark, "get_object") else mark
        tree = root.get("/StructTreeRoot")
        if not mark or not mark.get("/Marked") or tree is None:
            return None
        tree = tree.get_object()
        pages: set[int] = set()
        stack = [(tree, None)]
        seen = 0
        while stack and seen < node_limit:
            node, page_ref = stack.pop()
            seen += 1
            node = node.get_object() if hasattr(node, "get_object") else node
            if not hasattr(node, "get"):
                continue
            here = node.get("/Pg", page_ref)
            if node.get("/S") == "/Table" and here is not None:
                with contextlib.suppress(Exception):
                    pages.add(reader.get_page_number(here.get_object()))
            kids = node.get("/K")
            kids = kids.get_object() if hasattr(kids, "get_object") else kids
            if isinstance(kids, list):
                stack.extend((k, here) for k in kids)
            elif kids is not None:
                stack.append((kids, here))
        return pages
    except Exception:  # noqa: BLE001
        return None
