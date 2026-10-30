// 정전(canon) 검사: 사용자가 못 박은 세계관·금지어를 소스·문서 텍스트에서 기계로 잡는다.
// 실패 사례: 6장에 '대기업' 배경이 되살아남(정전은 대기업 금지). 같은 위반이 장마다 반복돼서 도구로 뺀다.
import fs from 'node:fs';
import path from 'node:path';
import { globToRegex, lineCol, matchAny, readJson, resolveInRoot, sha256Buf, sha256File, walk } from './util.mjs';

export const CANON_PATH = 'docs/studio/canon.json';
const TEXT_EXT = new Set(['.ts', '.tsx', '.js', '.mjs', '.json', '.md', '.txt', '.html', '.css', '.csv', '.yaml', '.yml']);

export function checkCanon(root, canonFile = path.join(root, CANON_PATH)) {
  const canonRef = path.isAbsolute(canonFile) ? path.relative(path.resolve(root), path.resolve(canonFile)) : canonFile;
  canonFile = resolveInRoot(root, canonRef, { mustExist: false });
  if (!fs.existsSync(canonFile)) {
    return { tool: 'studio-canon', ok: false, status: 'unconfigured', reason: `정전 파일이 없다: ${path.relative(root, canonFile)}. studio init 으로 템플릿을 만들고 사용자 정전을 채운다`, violations: [], missing: [] };
  }
  const canon = readJson(canonFile);
  const include = canon.include?.length ? canon.include : ['src/**', 'docs/**'];
  const exclude = [...(canon.exclude ?? []), 'docs/studio/**'];
  const files = walk(root).filter((f) => TEXT_EXT.has(path.extname(f)) && matchAny(f, include) && !matchAny(f, exclude));
  const inputs = [{ file: path.relative(fs.realpathSync(root), canonFile).split(path.sep).join('/'), sha256: sha256File(canonFile) }];
  const violations = [];
  const counts = new Map((canon.required ?? []).map((r) => [r.id, 0]));
  for (const rel of files) {
    const full = path.join(root, rel);
    if (fs.statSync(full).size > 1_000_000) continue;
    inputs.push({ file: rel, sha256: sha256File(full) });
    const text = fs.readFileSync(full, 'utf8');
    for (const rule of canon.forbidden ?? []) {
      if (rule.files && !globToRegex(rule.files).test(rel)) continue;
      const re = new RegExp(rule.pattern, (rule.flags ?? '') + 'g');
      for (const m of text.matchAll(re)) {
        const { line, col } = lineCol(text, m.index);
        const lineText = text.split('\n')[line - 1] ?? '';
        if (rule.allowIfLineMatches && new RegExp(rule.allowIfLineMatches).test(lineText)) continue;
        violations.push({ rule: rule.id, why: rule.why ?? '', file: rel, line, col, match: m[0], excerpt: lineText.trim().slice(0, 140) });
      }
    }
    for (const rule of canon.required ?? []) {
      const re = new RegExp(rule.pattern, (rule.flags ?? '') + 'g');
      counts.set(rule.id, counts.get(rule.id) + [...text.matchAll(re)].length);
    }
  }
  const missing = (canon.required ?? []).filter((r) => counts.get(r.id) < (r.min ?? 1)).map((r) => ({ rule: r.id, why: r.why ?? '', found: counts.get(r.id), min: r.min ?? 1 }));
  inputs.sort((a, b) => a.file.localeCompare(b.file));
  return { tool: 'studio-canon', ok: violations.length === 0 && missing.length === 0, status: 'checked', scanned: files.length, inputFingerprint: sha256Buf(JSON.stringify(inputs)), violations, missing };
}
