#!/usr/bin/env python3
"""Generate a local two-page web fixture; optionally serve it on loopback."""
from __future__ import annotations

import argparse
import functools
import http.server
from pathlib import Path

ORIGINAL = '''<!doctype html>
<html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>반응 비교 샘플</title><link rel="icon" href="data:,">
<style>body{font:16px system-ui,sans-serif;max-width:36rem;margin:3rem auto;padding:1rem;color:#172554}button{background:#1d4ed8;color:white;border:0;border-radius:.5rem;padding:.75rem 1rem}output{display:block;margin-top:1rem}</style>
<h1>요청 처리</h1><p>공통 동작을 실행하고 상태를 확인합니다.</p><button id="run">실행</button><output id="state" aria-live="polite">대기</output>
<script>let count=0;document.querySelector('#run').addEventListener('click',()=>{count+=1;document.querySelector('#state').textContent=`완료 ${count}`;});</script>
'''
REPLICA = ORIGINAL.replace("background:#1d4ed8", "background:#b91c1c")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--serve", action="store_true", help="serve the generated fixture on 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = args.output.resolve()
    (root / "original").mkdir(parents=True, exist_ok=True)
    (root / "replica").mkdir(parents=True, exist_ok=True)
    (root / "original" / "index.html").write_text(ORIGINAL, encoding="utf-8")
    (root / "replica" / "index.html").write_text(REPLICA, encoding="utf-8")
    print(f"Generated: {root / 'original/index.html'} and {root / 'replica/index.html'}")
    if args.serve:
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
        with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
            print(f"Original: http://127.0.0.1:{args.port}/original/")
            print(f"Replica:  http://127.0.0.1:{args.port}/replica/")
            print("Stop with Ctrl-C after browser evidence collection.")
            server.serve_forever()


if __name__ == "__main__":
    main()
