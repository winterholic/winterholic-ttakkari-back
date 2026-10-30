// 에셋 감사: 규격·중복·빈 이미지·manifest 불일치를 잡고, 사람이 한눈에 보는 contact sheet(HTML)를 만든다.
// 이 도구는 '그림이 예쁜가'를 판정하지 않는다. 판정은 contact sheet 를 실제 브라우저로 캡처해 눈으로 보고 사용자에게 맡긴다.
import fs from 'node:fs';
import path from 'node:path';
import { analyzePng, imageKind, imageSize } from './png.mjs';
import { globToRegex, readJson, sha256Buf, walk } from './util.mjs';

const IMG_EXT = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif']);
export const RULES_PATH = 'docs/studio/asset-rules.json';

function collectManifestPaths(value, out = new Set()) {
  if (typeof value === 'string') {
    if (/^\/?assets\//.test(value) || IMG_EXT.has(path.extname(value).toLowerCase())) out.add(value.replace(/^\//, ''));
  } else if (value && typeof value === 'object') for (const v of Object.values(value)) collectManifestPaths(v, out);
  return out;
}

export function auditAssets(root, { dir = 'public/assets', manifests = [], publicDir = 'public' } = {}) {
  const base = path.join(root, dir);
  if (!fs.existsSync(base)) return { tool: 'studio-assets', ok: true, status: 'no-assets-dir', dir, files: [], issues: [], summary: {} };
  const rulesFile = path.join(root, RULES_PATH);
  const rules = fs.existsSync(rulesFile) ? readJson(rulesFile).rules ?? [] : [];
  const files = [];
  const issues = [];
  const byHash = new Map();
  for (const rel of walk(base)) {
    if (!IMG_EXT.has(path.extname(rel).toLowerCase())) continue;
    const full = path.join(base, rel);
    const buf = fs.readFileSync(full);
    const size = imageSize(buf);
    const f = { file: `${dir}/${rel}`, bytes: buf.length, ...size, sha256: sha256Buf(buf), pixels: null };
    if (!imageKind(buf)) issues.push({ level: 'error', code: 'NOT_AN_IMAGE', file: f.file, msg: '확장자는 이미지인데 내용이 이미지가 아니다' });
    else if (size.kind === 'png') {
      f.pixels = analyzePng(buf);
      if (f.pixels?.blank) issues.push({ level: 'error', code: 'BLANK', file: f.file, msg: '한 색이거나 완전 투명한 이미지다(미완성·자리표시자 의심)' });
      else if (f.pixels && f.pixels.buckets <= 2 && f.pixels.transparentFraction < 0.5) issues.push({ level: 'warn', code: 'LOW_VARIETY', file: f.file, msg: `색이 ${f.pixels.buckets}종뿐이다(자리표시자·체커 의심). 의도한 단색 UI 면이면 무시` });
    }
    if (buf.length < 300) issues.push({ level: 'warn', code: 'TINY_FILE', file: f.file, msg: `${buf.length}바이트: 비정상적으로 작다` });
    for (const r of rules) {
      if (!globToRegex(r.glob).test(f.file)) continue;
      if (r.width && size.width && size.width !== r.width) issues.push({ level: 'error', code: 'RULE_WIDTH', file: f.file, msg: `규격 ${r.glob}: 너비 ${r.width} 요구, 실제 ${size.width}` });
      if (r.height && size.height && size.height !== r.height) issues.push({ level: 'error', code: 'RULE_HEIGHT', file: f.file, msg: `규격 ${r.glob}: 높이 ${r.height} 요구, 실제 ${size.height}` });
      if (r.maxBytes && buf.length > r.maxBytes) issues.push({ level: 'warn', code: 'RULE_BYTES', file: f.file, msg: `규격 ${r.glob}: ${r.maxBytes}바이트 이하 요구, 실제 ${buf.length}` });
    }
    (byHash.get(f.sha256) ?? byHash.set(f.sha256, []).get(f.sha256)).push(f.file);
    files.push(f);
  }
  for (const group of byHash.values()) if (group.length > 1) issues.push({ level: 'warn', code: 'DUPLICATE', file: group[0], msg: `내용이 같은 파일 ${group.length}개: ${group.join(', ')}` });
  const referenced = new Set();
  for (const m of manifests) {
    const mf = path.join(root, m);
    if (!fs.existsSync(mf)) {
      issues.push({ level: 'error', code: 'MANIFEST_NOT_FOUND', file: m, msg: 'manifest 파일이 없다' });
      continue;
    }
    for (const p of collectManifestPaths(readJson(mf))) {
      const rel = p.startsWith('assets/') ? `${publicDir}/${p}` : p;
      referenced.add(rel);
      if (!fs.existsSync(path.join(root, rel))) issues.push({ level: 'error', code: 'MANIFEST_MISSING_FILE', file: m, msg: `manifest 가 가리키는 파일이 없다: ${rel}` });
    }
  }
  if (manifests.length) for (const f of files) if (!referenced.has(f.file) && !f.file.endsWith('index.json')) issues.push({ level: 'info', code: 'ORPHAN', file: f.file, msg: 'manifest 에서 참조되지 않는다(코드가 직접 불러오면 정상)' });
  const count = (lv) => issues.filter((i) => i.level === lv).length;
  return {
    tool: 'studio-assets',
    ok: count('error') === 0,
    status: 'checked',
    dir,
    summary: { files: files.length, errors: count('error'), warnings: count('warn'), info: count('info'), unanalyzedPixels: files.filter((f) => f.kind !== 'png').length },
    files,
    issues,
    limits: ['JPEG·WebP 는 화소 통계를 내지 않는다(크기·중복·존재만 확인).', '화풍 일관성·품질은 contact sheet 를 눈으로 보고 판단한다. 도구는 판정하지 않는다.'],
  };
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

/** 폴더·접두사별로 묶은 contact sheet. 문제 있는 타일은 빨간(오류)·주황(경고) 테두리. */
export function contactSheetHtml(result, htmlDir, root) {
  const bad = new Map();
  for (const i of result.issues) if (i.level !== 'info') bad.set(i.file, i.level === 'error' ? 'error' : bad.get(i.file) ?? 'warn');
  const groups = new Map();
  for (const f of result.files) {
    const parts = f.file.split('/');
    const key = parts.slice(0, -1).join('/') + ' / ' + (path.basename(f.file).split('_')[0] || '-');
    (groups.get(key) ?? groups.set(key, []).get(key)).push(f);
  }
  const body = [...groups.entries()]
    .map(([k, fs_]) => `<section><h2>${esc(k)} <small>${fs_.length}</small></h2><div class="g">${fs_
      .map((f) => {
        const src = path.relative(htmlDir, path.join(root, f.file)).split(path.sep).join('/');
        return `<figure class="${bad.get(f.file) ?? ''}"><img loading="lazy" src="${esc(src)}" alt=""><figcaption>${esc(path.basename(f.file))}<br>${f.width ?? '?'}×${f.height ?? '?'} · ${Math.round(f.bytes / 1024)}KB</figcaption></figure>`;
      })
      .join('')}</div></section>`)
    .join('\n');
  return `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>contact sheet</title>
<style>body{font:12px system-ui;background:#14161c;color:#ddd;margin:8px}h2{font-size:13px;margin:14px 0 6px}small{opacity:.6}.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:6px}
figure{margin:0;padding:4px;background:#1f222b;border:2px solid #2b2f3a;border-radius:4px}figure.error{border-color:#e5484d}figure.warn{border-color:#f5a524}
img{width:100%;height:84px;object-fit:contain;background:repeating-conic-gradient(#2a2d36 0 25%,#1c1e25 0 50%) 0 0/12px 12px}figcaption{margin-top:3px;word-break:break-all;font-size:10px;line-height:1.3}</style>
<h1 style="font-size:15px">${esc(result.dir)} · ${result.files.length}개 · 오류 ${result.summary.errors} · 경고 ${result.summary.warnings}</h1>${body}`;
}
