// 정적 린트: 실제 사용자 피드백에서 나온 실패 유형 중 '코드에서 단서가 보이는 것'만 잡는다.
// 린트 경고는 결함 판정이 아니라 사람이 볼 단서다. 잡지 못하는 것(화면 밖 적, 오브젝트 겹침, 연출 품질)은 도달성 검사·플레이 재생·사용자 확인으로 간다.
import fs from 'node:fs';
import path from 'node:path';
import { lineCol, resolveInRoot, walk } from './util.mjs';

const RULES = [
  {
    id: 'ANIM_FPS',
    source: 'ceopark 1장 피드백: 발 움직임(걷기)이 이상하다. 원인은 걷기 프레임 속도 40fps(약 8fps 로 수정).',
    run(text) {
      const out = [];
      for (const m of text.matchAll(/frameRate\s*:\s*(\d+(?:\.\d+)?)/g)) {
        const fps = Number(m[1]);
        const around = text.slice(Math.max(0, m.index - 160), m.index + 80);
        const walky = /walk|run|idle|걷|달/i.test(around);
        if (fps > 30) out.push({ index: m.index, level: 'error', msg: `frameRate ${fps}: 프레임 애니메이션이 너무 빠르다(보통 6~14). 실제 화면으로 확인` });
        else if (walky && fps > 16) out.push({ index: m.index, level: 'warn', msg: `걷기/달리기 frameRate ${fps}: 발이 미끄러져 보일 수 있다(보통 6~14)` });
      }
      return out;
    },
  },
  {
    id: 'IME_KEY',
    source: 'ceopark 입력: 한글 IME 상태에서 event.key 는 한글 자모가 와서 키가 안 먹는다. e.code(물리 키)로 매핑해야 한다.',
    run(text) {
      const out = [];
      for (const m of text.matchAll(/\b(?:e|ev|event|evt)\.key\s*===?\s*['"][A-Za-z]{1}['"]/g)) out.push({ index: m.index, level: 'warn', msg: '문자 키를 event.key 로 비교한다: 한글 IME 에서 안 먹는다. event.code(KeyZ 등)를 쓴다' });
      return out;
    },
  },
  {
    id: 'INPUT_DISABLED_NO_RESTORE',
    source: 'ceopark 대화창 뒤 입력 막힘: 대화·메뉴를 열 때 입력을 끄고 닫을 때 복원하지 않아 키가 영구 유실됐다.',
    run(text) {
      const off = [...text.matchAll(/keyboard\s*\.\s*enabled\s*=\s*false|keyboard\s*\.\s*removeAllKeys\s*\(|input\s*\.\s*enabled\s*=\s*false/g)];
      const on = /keyboard\s*\.\s*enabled\s*=\s*true|input\s*\.\s*enabled\s*=\s*true|addKey\s*\(|createCursorKeys\s*\(/.test(text);
      return off.length && !on ? off.map((m) => ({ index: m.index, level: 'warn', msg: '입력을 끄는데 같은 파일에서 복원(enabled=true·addKey)이 보이지 않는다: 창을 닫은 뒤 입력이 막히는지 재생으로 확인' })) : [];
    },
  },
  {
    id: 'LISTENER_LEAK',
    source: 'ceopark 입력 렉·키 유실: 장면 전환 때 window/document 리스너가 쌓이거나 풀리지 않는다.',
    run(text) {
      const add = [...text.matchAll(/(?:window|document)\s*\.\s*addEventListener\s*\(/g)];
      const rem = [...text.matchAll(/(?:window|document)\s*\.\s*removeEventListener\s*\(/g)];
      return add.length > rem.length ? [{ index: add[rem.length].index, level: 'warn', msg: `addEventListener ${add.length}회, removeEventListener ${rem.length}회: 장면 종료(shutdown) 때 해제하는지 확인` }] : [];
    },
  },
  {
    id: 'INTERVAL_NO_CLEAR',
    source: '장면 전환 뒤에도 남는 타이머는 이전 장면 로직을 계속 돌려 입력·상태를 오염시킨다.',
    run(text) {
      const a = [...text.matchAll(/\bsetInterval\s*\(/g)];
      const c = [...text.matchAll(/\bclearInterval\s*\(/g)];
      return a.length > c.length ? [{ index: a[c.length].index, level: 'warn', msg: `setInterval ${a.length}회, clearInterval ${c.length}회: 장면 종료 때 정리하는지 확인(가능하면 Phaser time 이벤트 사용)` }] : [];
    },
  },
];

export function lintProject(root, { dir = 'src' } = {}) {
  const base = resolveInRoot(root, dir, { mustExist: false });
  if (!fs.existsSync(base)) return { tool: 'studio-lint', ok: true, status: 'no-src-dir', dir, findings: [], rules: RULES.map((r) => r.id) };
  const findings = [];
  const files = walk(base).filter((f) => /\.(ts|tsx|js|mjs)$/.test(f));
  for (const rel of files) {
    const text = fs.readFileSync(path.join(base, rel), 'utf8');
    for (const rule of RULES) {
      for (const f of rule.run(text)) {
        const { line } = lineCol(text, f.index);
        findings.push({ rule: rule.id, level: f.level, file: `${dir}/${rel}`, line, msg: f.msg });
      }
    }
  }
  const errors = findings.filter((f) => f.level === 'error').length;
  return {
    tool: 'studio-lint',
    ok: errors === 0,
    status: 'checked',
    dir,
    scanned: files.length,
    summary: { errors, warnings: findings.length - errors },
    findings,
    rules: RULES.map((r) => ({ id: r.id, source: r.source })),
    limits: ['정규식 휴리스틱이다. 경고는 단서이지 결함 판정이 아니다.', '화면 밖 적·오브젝트 겹침·연출 품질은 잡지 못한다.'],
  };
}
