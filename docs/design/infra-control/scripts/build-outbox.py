"""검토용 사본을 outbox 로 묶는다. prototype.html 의 CSS 경로만 assets/ 로 바꾸고 캡처·명세·검증 보고서를 함께 복사한다.
사용: python3 build-outbox.py <outbox/infra-design 경로>
"""
import shutil
import sys
from pathlib import Path

here = Path(__file__).resolve().parent.parent
front_vendor = here.parents[3] / "front" / "src" / "vendor" / "ttakkari"
out = Path(sys.argv[1])
(out / "assets").mkdir(parents=True, exist_ok=True)
(out / "captures").mkdir(exist_ok=True)

for name in ("tokens.css", "typography.css", "components.css"):
    shutil.copy(front_vendor / name, out / "assets" / name)

html = (here / "prototype.html").read_text()
html = html.replace("../../../../front/src/vendor/ttakkari/", "assets/")
(out / "prototype.html").write_text(html)
shutil.copy(here / "spec.md", out / "spec.md")
for f in (here / "captures").iterdir():
    shutil.copy(f, out / "captures" / f.name)

groups = [
    ("정상", "normal"), ("문제", "problem"), ("로딩", "loading"), ("권한 없음", "denied"),
    ("갱신 실패", "failed"), ("부분 성공", "partial"), ("빈 값", "empty"), ("오래된 값", "stale"),
]
rows = []
for label, key in groups:
    cells = []
    for view, w in (("m390", 195), ("d1280", 480)):
        for theme in ("light", "dark"):
            f = f"captures/{key}-{view}-{theme}.png"
            cells.append(f'<figure><img src="{f}" width="{w}" alt="{label} {view} {theme}"><figcaption>{view} · {theme}</figcaption></figure>')
    rows.append(f"<section><h2>{label}</h2><div class=row>{''.join(cells)}</div></section>")
extra = "".join(
    f'<figure><img src="captures/{n}.png" width="195" alt="{n}"><figcaption>{n}</figcaption></figure>'
    for n in ("problem-detail-m390-light", "problem-detail-m390-dark", "partial-detail-m390-light", "partial-detail-m390-dark")
)
rows.append(f"<section><h2>모바일 상세</h2><div class=row>{extra}</div></section>")
(out / "gallery.html").write_text(
    '<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
    "<title>인프라 관제 시안 캡처</title><style>body{font:14px/1.5 system-ui;margin:16px;background:#f4f4ed;color:#111}"
    "h1{font-size:18px}h2{font-size:15px;margin:24px 0 8px}.row{display:flex;gap:12px;overflow-x:auto;align-items:flex-start}"
    "figure{margin:0;flex:none}img{display:block;border:1px solid #ccc;border-radius:8px;height:auto}figcaption{font-size:12px;color:#555}</style>"
    "<h1>인프라 관제 검토용 시안 캡처 (예시 데이터 · 실제 상태 아님)</h1>"
    '<p><a href="prototype.html#s=problem">시안 열기</a> · <a href="spec.md">명세</a> · <a href="captures/verify-report.json">검증 보고서</a></p>'
    + "".join(rows)
)
print("built", out)
