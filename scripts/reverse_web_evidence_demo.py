#!/usr/bin/env python3
"""Serve a small, independently authored replica page on loopback for browser capture."""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLICA_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>주문 준비 | Reverse E2E 원본</title><link rel="icon" href="data:,">
<style>
:root{color-scheme:light;--ink:#172b3a;--muted:#627482;--line:#d6e0e6;--blue:#145b73;--pale:#eaf4f6;--green:#216b50}
*{box-sizing:border-box}body{margin:0;background:#f3f6f7;color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{width:min(100% - 32px,720px);margin:40px auto}.eyebrow{color:var(--blue);font-size:.78rem;font-weight:750;letter-spacing:.1em}
h1{font-size:clamp(1.8rem,6vw,2.5rem);line-height:1.15;margin:.55rem 0}.intro{color:var(--muted);margin:0 0 24px}
.panel{background:white;border:1px solid var(--line);border-radius:16px;padding:24px;box-shadow:0 8px 24px #172b3a0c}
label{display:block;font-weight:700;margin:0 0 8px}input{width:100%;min-height:48px;border:1px solid #9aabb5;border-radius:9px;padding:10px 12px;font:inherit;color:inherit}
input:focus{outline:3px solid #145b7330;border-color:var(--blue)}.hint{font-size:.88rem;color:var(--muted);margin:7px 0 18px}
button{min-height:46px;border:0;border-radius:9px;padding:10px 18px;background:#145b73;color:white;font:700 1rem inherit;cursor:pointer}
.status{margin-top:18px;padding:13px 14px;background:var(--pale);border-radius:9px;color:var(--blue);font-weight:700}
.status[data-kind="success"]{background:#edf7f1;color:var(--green)}.log-title{margin:23px 0 6px;font-size:.95rem}.events{margin:0;padding-left:20px;color:var(--muted);font-size:.9rem;min-height:28px}.events li+li{margin-top:3px}
@media(max-width:600px){main{width:calc(100% - 28px);margin:20px auto}.panel{padding:18px;border-radius:13px}button{width:100%}}
@media(max-width:390px){main{width:calc(100% - 24px);margin:16px auto}.intro{font-size:.94rem}.panel{padding:16px}}
</style></head><body><main>
<div class="eyebrow">LOCAL REVERSE E2E · ORIGINAL</div><h1>주문 준비</h1>
<p class="intro">수량을 확인하고 준비 요청을 보내세요. 요청 수와 최근 사용자 이벤트가 아래에 표시됩니다.</p>
<section class="panel" aria-labelledby="form-title"><h2 id="form-title" style="font-size:1.1rem;margin:0 0 18px">준비 요청</h2>
<form id="request-form"><label for="quantity">수량</label><input id="quantity" name="quantity" type="number" min="1" max="5" step="1" value="1" required aria-describedby="quantity-hint">
<p class="hint" id="quantity-hint">1개부터 5개까지 입력할 수 있습니다.</p><button type="submit">준비 요청</button></form>
<div class="status" id="status" data-kind="idle" role="status" aria-live="polite">대기 중 · 요청 0건</div>
<h3 class="log-title">사용자 이벤트</h3><ol class="events" id="events" aria-live="polite"></ol></section></main>
<script>
const form=document.getElementById('request-form'), quantity=document.getElementById('quantity');
const statusLine=document.getElementById('status'), eventList=document.getElementById('events'); let submitted=0;
const appendEvent=text=>{const row=document.createElement('li');row.append(document.createTextNode(text));eventList.prepend(row)};
quantity.onchange=()=>appendEvent('수량 변경: '+(quantity.value||'비움'));
form.onsubmit=e=>{e.preventDefault();if(!form.reportValidity()){statusLine.dataset.kind='error';statusLine.textContent='입력 오류 · 요청 '+submitted+'건';appendEvent('잘못된 수량을 제출함');return}
submitted+=1;statusLine.dataset.kind='success';statusLine.textContent='준비 완료 · '+quantity.value+'개 · 요청 '+submitted+'건';appendEvent('요청 성공: '+quantity.value+'개')};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in {"/", "/index.html"}:
            self.send_error(404)
            return
        body = REPLICA_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if args.host != "127.0.0.1" or not 0 <= args.port <= 65535:
        parser.error("only 127.0.0.1 and a valid port are allowed")
    with ThreadingHTTPServer((args.host, args.port), Handler) as server:
        print(json.dumps({"ready": True, "host": args.host, "port": server.server_address[1],
                          "url": f"http://{args.host}:{server.server_address[1]}/"}, separators=(",", ":")),
              flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
    print(json.dumps({"stopped": True, "host": args.host, "port": server.server_address[1]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
