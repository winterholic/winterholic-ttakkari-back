// 따까리 게임 스튜디오 키트 공용 유틸. 의존성 없음(node 20+).
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

export function parseArgs(argv) {
  const pos = [];
  const opt = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const eq = a.indexOf('=');
      if (eq > 0) opt[a.slice(2, eq)] = a.slice(eq + 1);
      else if (i + 1 < argv.length && !argv[i + 1].startsWith('--')) opt[a.slice(2)] = argv[++i];
      else opt[a.slice(2)] = true;
    } else pos.push(a);
  }
  return { pos, opt };
}

export const now = () => new Date().toISOString();
export const sha256Buf = (b) => crypto.createHash('sha256').update(b).digest('hex');
export const sha256File = (f) => sha256Buf(fs.readFileSync(f));

/** Resolve an existing path inside a project root, rejecting lexical and symlink escapes. */
export function resolveInRoot(root, ref, { file = false, mustExist = true } = {}) {
  const base = fs.realpathSync(root);
  const candidate = path.resolve(base, ref);
  const inside = (p) => p === base || p.startsWith(base + path.sep);
  if (!inside(candidate)) throw new Error(`경로가 프로젝트 root 밖이다: ${ref}`);
  let current = base;
  for (const part of path.relative(base, candidate).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    let st;
    try { st = fs.lstatSync(current); } catch (e) {
      if (e.code === 'ENOENT' && !mustExist) {
        let ancestor = path.dirname(current);
        while (!fs.existsSync(ancestor) && ancestor !== path.dirname(ancestor)) ancestor = path.dirname(ancestor);
        if (!inside(fs.realpathSync(ancestor))) throw new Error(`경로가 프로젝트 root 밖이다: ${ref}`);
        return candidate;
      }
      if (e.code === 'ENOENT') throw new Error(`경로가 없다: ${ref}`);
      throw e;
    }
    if (st.isSymbolicLink()) throw new Error(`symlink 경로는 증거로 쓸 수 없다: ${ref}`);
    if (!inside(fs.realpathSync(current))) throw new Error(`경로가 프로젝트 root 밖이다: ${ref}`);
  }
  if (file && !fs.statSync(candidate).isFile()) throw new Error(`파일이 아니다: ${ref}`);
  return candidate;
}

export function readJson(file, fallback = undefined) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    if (fallback !== undefined && e.code === 'ENOENT') return fallback;
    throw new Error(`JSON 을 읽을 수 없다: ${file} (${e.message})`);
  }
}

export function writeJson(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(data, null, 2) + '\n');
}

/** glob(**, *, ?) 을 정규식으로. 경로는 / 구분자 기준. */
export function globToRegex(glob) {
  let re = '';
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i];
    if (c === '*') {
      if (glob[i + 1] === '*') {
        i++;
        if (glob[i + 1] === '/') {
          i++;
          re += '(?:.*/)?';
        } else re += '.*';
      } else re += '[^/]*';
    } else if (c === '?') re += '[^/]';
    else re += c.replace(/[.+^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp('^' + re + '$');
}

const ALWAYS_SKIP = new Set(['.git', 'node_modules', 'dist', 'out', '.ttakkari-feedback', '.claude', '.agents']);

/** 상대경로(/ 구분) 목록. 정본이 아닌 폴더는 건너뛴다. */
export function walk(root, { skip = ALWAYS_SKIP } = {}) {
  const out = [];
  const rec = (dir) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries.sort((a, b) => a.name.localeCompare(b.name))) {
      if (e.isSymbolicLink()) continue;
      const full = path.join(dir, e.name);
      if (e.isDirectory()) {
        if (!skip.has(e.name)) rec(full);
      } else if (e.isFile()) out.push(path.relative(root, full).split(path.sep).join('/'));
    }
  };
  rec(root);
  return out;
}

export function matchAny(rel, globs) {
  return globs.some((g) => globToRegex(g).test(rel));
}

export function lineCol(text, index) {
  const before = text.slice(0, index);
  const line = before.split('\n').length;
  return { line, col: index - before.lastIndexOf('\n') };
}

export function say(obj, asJson, human) {
  if (asJson) process.stdout.write(JSON.stringify(obj, null, 2) + '\n');
  else process.stdout.write(human + '\n');
}
