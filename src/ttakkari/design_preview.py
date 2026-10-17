"""Self-contained design examples: committed blobs only, opaque-origin iframe.

This produces HTML for the owner UI, never executes source on the server, and
never passes the larger preview payload through the bounded MCP text tools.
"""
from __future__ import annotations

import base64
import html
import json
import posixpath
import re
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from ttakkari.design_system import NotFound, Unavailable, _git

MIMES = {'.css': 'text/css', '.js': 'text/javascript', '.svg': 'image/svg+xml',
         '.png': 'image/png', '.webp': 'image/webp', '.jpg': 'image/jpeg',
         '.jpeg': 'image/jpeg', '.ico': 'image/x-icon', '.woff2': 'font/woff2'}
CSP = ("default-src 'none'; script-src 'unsafe-inline' data:; "
       "style-src 'unsafe-inline' data: https://cdn.jsdelivr.net https://fonts.googleapis.com; "
       "font-src data: https://cdn.jsdelivr.net https://fonts.gstatic.com; "
       "img-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; "
       "base-uri 'none'; form-action 'none'")
MAX_FILE = 2 * 1024 * 1024
MAX_BUNDLE = 12 * 1024 * 1024


class Bundle:
    def __init__(self, root: Path, revision: str, system: str):
        self.root, self.revision, self.system = root, revision, system
        self.files = {}
        for row in filter(None, _git(root, 'ls-tree', '-rlz', revision, '--', system).decode().split('\0')):
            meta, path = row.split('\t', 1)
            mode, typ, oid, size = meta.split()
            if mode in ('100644', '100755') and typ == 'blob':
                self.files[path] = (oid, int(size))
        self.cache: dict[str, str] = {}
        self.warnings: set[str] = set()
        self.total = 0

    def read(self, path: str) -> bytes:
        if path not in self.files:
            raise NotFound('미리보기 파일이 없습니다: ' + path)
        oid, size = self.files[path]
        if size > MAX_FILE:
            raise Unavailable('미리보기 파일이 2 MiB 한도를 넘었습니다: ' + path)
        self.total += size
        if self.total > MAX_BUNDLE:
            raise Unavailable('미리보기 자산이 12 MiB 한도를 넘었습니다.')
        return _git(self.root, 'cat-file', 'blob', oid)

    def resource(self, value: str, source: str, seen: tuple[str, ...] = ()) -> str:
        url = urlsplit(value)
        if value.startswith(('data:image/', 'data:font/')):
            return value
        if value.startswith('#'):
            return value
        if url.scheme or url.netloc or value.startswith('/') or '\\' in value:
            self.warnings.add('외부 자산 생략: ' + value[:160])
            return 'data:,'
        path = posixpath.normpath(posixpath.join(posixpath.dirname(source), unquote(url.path)))
        parts = PurePosixPath(path).parts
        suffix = PurePosixPath(path).suffix.lower()
        allowed = (len(parts) >= 3 and parts[0] == self.system and
                   parts[1] in ('dist', 'examples', 'assets') and suffix in MIMES)
        if not allowed or path not in self.files or path in seen or len(seen) >= 12:
            self.warnings.add('자산을 읽지 못했어요: ' + path[:160])
            return 'data:,'
        if path not in self.cache:
            data = self.read(path)
            if suffix == '.css':
                data = self.css(data.decode('utf-8'), path, (*seen, path)).encode()
            self.cache[path] = 'data:' + MIMES[suffix] + ';base64,' + base64.b64encode(data).decode()
        return self.cache[path] + ('#' + url.fragment if url.fragment else '')

    def css(self, value: str, source: str, seen: tuple[str, ...] = ()) -> str:
        value = re.sub(r'url\(\s*([\'"]?)(.*?)\1\s*\)',
                       lambda m: 'url("' + self.resource(m[2], source, seen) + '")', value, flags=re.IGNORECASE)
        return re.sub(r'@import\s+([\'"])(.*?)\1',
                      lambda m: '@import url("' + self.resource(m[2], source, seen) + '")', value, flags=re.IGNORECASE)


class PreviewHTML(HTMLParser):
    def __init__(self, bundle: Bundle, source: str, theme: str):
        super().__init__(convert_charrefs=False)
        self.bundle, self.source, self.theme = bundle, source, theme
        self.out: list[str] = []
        self.sections: list[dict] = []
        self.heading: dict | None = None
        self.in_style = False
        self.section_id: str | None = None

    def handle_decl(self, decl):
        self.out.append('<!' + decl + '>')

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('base', 'iframe', 'object', 'embed') or (tag == 'meta' and a.get('http-equiv', '').lower() in ('refresh', 'content-security-policy')):
            return
        if tag == 'link':
            href = a.get('href', '')
            if a.get('rel') == 'preconnect':
                return
            url = urlsplit(href)
            if url.scheme or url.netloc:
                # Original typography may rely on these public, CSS-only font CDNs.
                if a.get('rel') != 'stylesheet' or url.scheme != 'https' or url.netloc not in ('cdn.jsdelivr.net', 'fonts.googleapis.com'):
                    return
            else:
                a['href'] = self.bundle.resource(href, self.source)
        elif tag in ('script', 'img', 'source') and 'src' in a:
            a['src'] = self.bundle.resource(a['src'] or '', self.source)
        if tag == 'html':
            a['data-theme'] = self.theme
        if tag == 'section':
            self.section_id = a.get('id')
        if tag in ('h1', 'h2') and (a.get('id') or self.section_id):
            self.heading = {'id': a.get('id') or self.section_id, 'title': ''}
            self.section_id = None
        self.in_style = tag == 'style' or self.in_style
        # No external browsing, forms, credentialed loads or embedded frames.
        for key in ('srcset', 'loading', 'action', 'formaction', 'ping', 'target', 'poster', 'background', 'xlink:href'):
            a.pop(key, None)
        if tag in ('a', 'use') and 'href' in a and not (a['href'] or '').startswith('#'):
            a.pop('href')
            a['title'] = '다른 예제는 따까리의 화면 예제 목록에서 열어 주세요.'
        if 'style' in a:
            a['style'] = self.bundle.css(a['style'] or '', self.source)
        attributes = ''.join(' ' + k + ('' if v is None else '="' + html.escape(v, quote=True) + '"') for k, v in a.items())
        self.out.append('<' + tag + attributes + '>')
        if tag == 'head':
            self.out.append('<meta http-equiv="Content-Security-Policy" content="' + html.escape(CSP, quote=True) + '">')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in ('base', 'iframe', 'object', 'embed'):
            return
        if tag in ('h1', 'h2') and self.heading:
            self.heading['title'] = self.heading['title'].strip()[:100]
            self.sections.append(self.heading)
            self.heading = None
        if tag == 'style':
            self.in_style = False
        self.out.append('</' + tag + '>')

    def handle_data(self, data):
        if self.heading:
            self.heading['title'] += data
        self.out.append(self.bundle.css(data, self.source) if self.in_style else data)

    def handle_entityref(self, name):
        self.out.append('&' + name + ';')

    def handle_charref(self, name):
        self.out.append('&#' + name + ';')


def build_preview(root: Path, revision: str, system: str, source: str, theme: str) -> dict:
    bundle = Bundle(root, revision, system)
    parser = PreviewHTML(bundle, source, theme)
    parser.feed(bundle.read(source).decode('utf-8'))
    document = ''.join(parser.out)
    # Component libraries can reset data-theme while initializing without storage.
    # Apply the owner-selected theme once after those scripts finish; original
    # in-preview theme toggles can still be used afterwards.
    document += ("<script>addEventListener('load',()=>{document.documentElement.dataset.theme="
                 + json.dumps(theme) + ";document.dispatchEvent(new Event('tk:themechange'));});</script>")
    if '<head' not in document.lower():
        document = '<head><meta http-equiv="Content-Security-Policy" content="' + html.escape(CSP, quote=True) + '"></head>' + document
    if len(document.encode()) > 20 * 1024 * 1024:
        raise Unavailable('미리보기 응답이 20 MiB 한도를 넘었습니다.')
    return {'revision': revision, 'system': system, 'source': source, 'html': document,
            'sections': parser.sections, 'warnings': sorted(bundle.warnings)}
