// 스튜디오 키트 단위·통합 테스트. 실행: node --test tests/studio_kit/
// 브라우저가 필요한 play 테스트는 playwright·브라우저가 없으면 사유와 함께 skip 한다(통과로 세지 않는다).
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import zlib from 'node:zlib';

const here = path.dirname(fileURLToPath(import.meta.url));
const KIT = path.resolve(here, '../../src/ttakkari/games/studio_kit/ttakkari-game-studio');
const BIN = path.join(KIT, 'bin/studio.mjs');
const BACK = path.resolve(here, '../..');
const { parseArgs } = await import(path.join(KIT, 'lib/util.mjs'));

function tmpRoot() {
  // 일부 샌드박스는 os.tmpdir()·/tmp 쓰기를 막는다(EPERM). 그때는 저장소 안 .tmp(git 제외)로 물러난다.
  for (const base of [process.env.STUDIO_TEST_TMP, os.tmpdir(), path.join(here, '.tmp')]) {
    if (!base) continue;
    try {
      fs.mkdirSync(base, { recursive: true });
      return fs.mkdtempSync(path.join(base, 'studio-kit-'));
    } catch (e) {
      if (e.code !== 'EPERM' && e.code !== 'EACCES') throw e;
    }
  }
  throw new Error('쓸 수 있는 임시 폴더가 없다(STUDIO_TEST_TMP 지정)');
}
const w = (root, rel, text) => {
  const f = path.join(root, rel);
  fs.mkdirSync(path.dirname(f), { recursive: true });
  fs.writeFileSync(f, text);
  return f;
};
function studio(root, ...args) {
  const r = spawnSync('node', [BIN, '--root', root, ...args], { encoding: 'utf8', env: { ...process.env, STUDIO_PLAYWRIGHT_DIR: process.env.STUDIO_PLAYWRIGHT_DIR ?? '' } });
  return { code: r.status, out: r.stdout, err: r.stderr };
}
const studioJson = (root, ...args) => {
  const r = studio(root, ...args, '--json');
  return { ...r, json: r.out.trim().startsWith('{') || r.out.trim().startsWith('[') ? JSON.parse(r.out) : null };
};

test('args: 지정하지 않은 singleton option은 문자열과 마지막 값 우선 계약을 유지한다', () => {
  const parsed = parseArgs(['--root', 'first', '--root=last', '--program', 'node'], { repeatable: ['input', 'manifest'] });
  assert.deepEqual(parsed.opt, { root: 'last', program: 'node' });
  assert.equal(typeof parsed.opt.root, 'string');
});

// --- 테스트용 최소 PNG 인코더(필터 0, RGBA) ---
function crc32(buf) {
  let c;
  let crc = 0xffffffff;
  for (let n = 0; n < buf.length; n++) {
    c = (crc ^ buf[n]) & 0xff;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    crc = (crc >>> 8) ^ c;
  }
  return (crc ^ 0xffffffff) >>> 0;
}
function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const td = Buffer.concat([Buffer.from(type), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(td));
  return Buffer.concat([len, td, crc]);
}
function png(w, h, pixel) {
  const raw = Buffer.alloc((w * 4 + 1) * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const [r, g, b, a] = pixel(x, y);
    raw.set([r, g, b, a], y * (w * 4 + 1) + 1 + x * 4);
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0);
  ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8;
  ihdr[9] = 6;
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk('IHDR', ihdr), chunk('IDAT', zlib.deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]);
}
const noisy = (x, y) => [(x * 37 + y * 11) % 256, (x * 5 + y * 91) % 256, (x * y) % 256, 255];
const flat = () => [10, 20, 30, 255];

test('png: 변화 있는 이미지와 단색 이미지를 구분한다', async () => {
  const { analyzePng, imageSize } = await import(path.join(KIT, 'lib/png.mjs'));
  const a = analyzePng(png(32, 32, noisy));
  const b = analyzePng(png(32, 32, flat));
  assert.equal(a.blank, false);
  assert.ok(a.buckets > 8);
  assert.equal(b.blank, true);
  assert.deepEqual([imageSize(png(7, 9, flat)).width, imageSize(png(7, 9, flat)).height], [7, 9]);
});

test('ledger: 마크다운 가져오기·범주 추정·중복 방지', () => {
  const root = tmpRoot();
  w(root, 'fb.md', '## 1장\n- 영역(충돌) 문제로 이동 불가 → 진행 못 함.\n- 오싹한 BGM\n## 정전 세계관\n- 대기업 배경 금지\n');
  const r = studioJson(root, 'ledger', 'import', 'fb.md');
  assert.equal(r.code, 0, r.err);
  const items = JSON.parse(fs.readFileSync(path.join(root, 'docs/studio/ledger.json'), 'utf8')).items;
  assert.equal(items.length, 3);
  assert.equal(items[0].category, 'bug');
  assert.equal(items[1].category, 'audio');
  assert.equal(items[2].category, 'narrative');
  assert.ok(items.every((i) => i.category_guess && i.status === 'open'));
  studio(root, 'ledger', 'import', 'fb.md');
  assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'docs/studio/ledger.json'), 'utf8')).items.length, 3, '같은 원문을 다시 넣지 않는다');
});

test('gate: 테스트 통과(machine-test)만으로는 bug 항목이 닫히지 않는다', () => {
  const root = tmpRoot();
  studio(root, 'ledger', 'add', '--quote', '1장에서 이동이 안 돼서 진행 못 함', '--category', 'bug');
  w(root, 'test.log', '17 passed');
  studio(root, 'ledger', 'status', 'F001', 'fixed');
  let g = studioJson(root, 'gate');
  assert.equal(g.code, 1);
  assert.match(g.json.blockers.join('\n'), /F001.*play-replay 또는 reach-check/);
  const ev = studio(root, 'ledger', 'evidence', 'F001', '--type', 'machine-test', '--ref', 'test.log');
  assert.notEqual(ev.code, 0, '수기 로그는 machine-test 실행 증거가 아니다');
  g = studioJson(root, 'gate');
  assert.equal(g.json.verdict, 'FAIL', 'machine-test 는 bug 요건이 아니다');
});

test('evidence: 등록과 gate 재평가 모두 root 밖 경로 및 symlink 교체를 거부한다', () => {
  const root = tmpRoot();
  const outside = path.join(path.dirname(root), `studio-outside-${path.basename(root)}.json`);
  w(root, 'inside.json', JSON.stringify({ tool: 'studio-reach', ok: true }));
  fs.writeFileSync(outside, JSON.stringify({ tool: 'studio-reach', ok: true }));
  studio(root, 'ledger', 'add', '--quote', '이동 막힘', '--category', 'bug');
  studio(root, 'ledger', 'status', 'F001', 'fixed');
  assert.notEqual(studio(root, 'ledger', 'evidence', 'F001', '--type', 'reach-check', '--ref', '../' + path.basename(outside)).code, 0);
  assert.notEqual(studio(root, 'ledger', 'evidence', 'F001', '--type', 'reach-check', '--ref', outside).code, 0);

  fs.symlinkSync('inside.json', path.join(root, 'link.json'));
  assert.notEqual(studio(root, 'ledger', 'evidence', 'F001', '--type', 'reach-check', '--ref', 'link.json').code, 0);
  fs.unlinkSync(path.join(root, 'link.json'));
  fs.writeFileSync(path.join(root, 'shot.png'), png(32, 32, noisy));
  assert.equal(studio(root, 'ledger', 'evidence', 'F001', '--type', 'screenshot', '--ref', 'shot.png').code, 0);
  fs.unlinkSync(path.join(root, 'shot.png'));
  fs.symlinkSync(outside, path.join(root, 'shot.png'));
  const gate = studioJson(root, 'gate');
  assert.equal(gate.json.verdict, 'FAIL');
  assert.match(gate.json.blockers.join('\n'), /root 밖|경계|symlink/i);
});

test('evidence: tool·ok 두 필드 수기 JSON과 문자열 기반 사용자 확인은 COMPLETE 근거가 아니다', () => {
  const root = tmpRoot();
  studio(root, 'ledger', 'add', '--quote', '그림이 어색해', '--category', 'ux');
  studio(root, 'ledger', 'status', 'F001', 'fixed');
  w(root, 'claimed.json', JSON.stringify({ tool: 'studio-play', ok: true }));
  assert.notEqual(studio(root, 'ledger', 'evidence', 'F001', '--type', 'play-replay', '--ref', 'claimed.json').code, 0);
  const result = studio(root, 'ledger', 'evidence', 'F001', '--type', 'user-confirmed', '--quote', '괜찮아', '--source', 'session://invented');
  assert.equal(result.code, 0, result.err);
  const gate = studioJson(root, 'gate');
  assert.notEqual(gate.json.verdict, 'COMPLETE');
  assert.match(gate.json.pending.join('\n'), /확인되지 않은 사용자 주장|사용자 확인 없음/);
});

test('evidence: 실제 reach CLI 결과는 gate 와 일치하고 입력 변경 뒤에는 FAIL이다', () => {
  const root = tmpRoot();
  const spec = { bounds: { w: 100, h: 100 }, spawns: [{ id: 'start', x: 50, y: 50 }], targets: [] };
  w(root, 'level.json', JSON.stringify(spec));
  studio(root, 'ledger', 'add', '--quote', '문까지 이동이 안 돼', '--category', 'bug');
  assert.equal(studio(root, 'reach', '--spec', 'level.json').code, 0);
  studio(root, 'ledger', 'status', 'F001', 'fixed');
  assert.equal(studio(root, 'reach', '--spec', 'level.json', '--attach', 'F001').code, 0);
  const reachOnly = studioJson(root, 'gate');
  assert.equal(reachOnly.json.verdict, 'NEEDS_HUMAN');
  assert.match(reachOnly.json.pending.join('\n'), /실제 게임 충돌체|실제 플레이 재생/);
  spec.bounds.w = 101;
  w(root, 'level.json', JSON.stringify(spec));
  const gate = studioJson(root, 'gate');
  assert.equal(gate.json.verdict, 'FAIL');
  assert.match(gate.json.blockers.join('\n'), /입력.*바뀌었다/);
});

test('paths: evidence input/output 및 ledger symlink 는 프로젝트 root 를 벗어나지 않는다', () => {
  const root = tmpRoot();
  const outside = path.join(path.dirname(root), `studio-path-${path.basename(root)}.json`);
  fs.writeFileSync(outside, JSON.stringify({ bounds: { w: 100, h: 100 }, spawns: [{ id: 's', x: 10, y: 10 }] }));
  assert.notEqual(studio(root, 'reach', '--spec', outside).code, 0);
  w(root, 'level.json', JSON.stringify({ bounds: { w: 100, h: 100 }, spawns: [{ id: 's', x: 10, y: 10 }] }));
  assert.notEqual(studio(root, 'reach', '--spec', 'level.json', '--out', '../outside.json').code, 0);
  w(root, 'docs/studio/ledger.json', JSON.stringify({ version: 1, items: [] }));
  fs.unlinkSync(path.join(root, 'docs/studio/ledger.json'));
  fs.symlinkSync(outside, path.join(root, 'docs/studio/ledger.json'));
  assert.notEqual(studio(root, 'ledger', 'add', '--quote', 'x').code, 0);
});

test('canon 증거는 검사한 정전과 소스가 바뀌면 gate 에서 stale 처리한다', () => {
  const root = tmpRoot();
  w(root, 'src/game.js', 'const x = 1;');
  w(root, 'docs/studio/canon.json', JSON.stringify({ include: ['src/**'], forbidden: [{ id: 'BANNED', pattern: 'banned' }] }));
  studio(root, 'ledger', 'add', '--quote', '세계관 확인', '--category', 'narrative');
  studio(root, 'ledger', 'status', 'F001', 'fixed');
  assert.equal(studio(root, 'canon', '--attach', 'F001').code, 0);
  assert.equal(studioJson(root, 'gate').json.verdict, 'NEEDS_HUMAN', 'canon 기계 검사는 통과하되 사용자 확인이 남는다');
  w(root, 'src/game.js', 'const banned = true;');
  const gate = studioJson(root, 'gate');
  assert.equal(gate.json.verdict, 'FAIL');
  assert.match(gate.json.blockers.join('\n'), /canon-check 실행 뒤 정전|정전 위반/);
});

test('reach: 끼인 스폰과 벽 뒤 상호작용을 잡고, 열린 방은 통과시킨다', () => {
  const root = tmpRoot();
  const spec = {
    name: 'r',
    bounds: { w: 400, h: 240 },
    player: { w: 20, h: 20, ox: -10, oy: -10 },
    step: 4,
    solids: [{ x: 200, y: 0, w: 16, h: 240 }, { x: 50, y: 50, w: 40, h: 40 }],
    spawns: [{ id: 'door', x: 40, y: 200 }, { id: 'inside', x: 70, y: 70 }],
    targets: [{ id: 'chest', kind: 'interact', x: 40, y: 200, reach: 20 }, { id: 'exit', kind: 'exit', x: 360, y: 120, reach: 20 }],
  };
  w(root, 'level.json', JSON.stringify(spec));
  const bad = studioJson(root, 'reach', '--spec', 'level.json');
  assert.equal(bad.code, 1);
  const msg = bad.json.problems.join('\n');
  assert.match(msg, /inside.*충돌체 안/);
  assert.match(msg, /door에서 exit.*닿을 수 없다/);
  // 벽을 열면 통과
  spec.solids = [{ x: 200, y: 0, w: 16, h: 100 }, { x: 200, y: 160, w: 16, h: 80 }, { x: 50, y: 50, w: 40, h: 40 }];
  spec.spawns = [{ id: 'door', x: 40, y: 200 }];
  w(root, 'level.json', JSON.stringify(spec));
  assert.equal(studio(root, 'reach', '--spec', 'level.json').code, 0);
});

test('canon: 금지어를 파일:줄로 지목하고 예외 줄은 허용한다', () => {
  const root = tmpRoot();
  w(root, 'docs/studio/canon.json', JSON.stringify({ include: ['src/**'], forbidden: [{ id: 'NO_MEGACORP', pattern: '대기업', why: '정전: 대기업 배경 금지', allowIfLineMatches: '금지' }] }));
  w(root, 'src/a.ts', 'const a = "동업";\nconst b = "대기업 본사";\nconst c = "대기업 금지 안내";\n');
  const r = studioJson(root, 'canon');
  assert.equal(r.code, 1);
  assert.deepEqual(r.json.violations.map((v) => [v.file, v.line]), [['src/a.ts', 2]]);
  assert.equal(studio(tmpRoot(), 'canon').code, 3, '정전 파일이 없으면 통과가 아니라 미설정(3)이다');
});

test('assets: 빈 이미지·규격 위반·중복·manifest 누락을 잡고 contact sheet 를 만든다', () => {
  const root = tmpRoot();
  fs.mkdirSync(path.join(root, 'public/assets'), { recursive: true });
  fs.writeFileSync(path.join(root, 'public/assets/ok.png'), png(32, 32, noisy));
  fs.writeFileSync(path.join(root, 'public/assets/copy.png'), png(32, 32, noisy));
  fs.writeFileSync(path.join(root, 'public/assets/blank.png'), png(32, 32, flat));
  w(root, 'public/assets/index.json', JSON.stringify({ ok: '/assets/ok.png', ghost: '/assets/ghost.png' }));
  w(root, 'docs/studio/asset-rules.json', JSON.stringify({ rules: [{ glob: 'public/assets/ok.png', width: 64 }] }));
  const r = studioJson(root, 'assets', '--manifest', 'public/assets/index.json', '--sheet');
  assert.equal(r.code, 1);
  const codes = r.json.issues.map((i) => i.code);
  for (const c of ['BLANK', 'RULE_WIDTH', 'DUPLICATE', 'MANIFEST_MISSING_FILE']) assert.ok(codes.includes(c), c);
  assert.ok(fs.existsSync(path.join(root, r.json.contactSheet)));
  assert.match(fs.readFileSync(path.join(root, r.json.contactSheet), 'utf8'), /class="error"/);
});

test('assets: 반복 manifest 의 공백형·등호형·혼합 인자를 모두 검사한다', () => {
  const root = tmpRoot();
  fs.mkdirSync(path.join(root, 'public/assets'), { recursive: true });
  fs.writeFileSync(path.join(root, 'public/assets/present.png'), png(32, 32, noisy));
  w(root, 'manifest-first.json', JSON.stringify({ missing: '/assets/missing.png' }));
  w(root, 'manifest-second.json', JSON.stringify({}));

  const missing = studioJson(root, 'assets', '--manifest', 'manifest-first.json', '--manifest=manifest-second.json');
  assert.equal(missing.code, 1);
  assert.ok(missing.json.issues.some((issue) => issue.code === 'MANIFEST_MISSING_FILE' && issue.file.endsWith('/manifest-first.json')), JSON.stringify(missing.json.issues));
  const gateMissing = studioJson(root, 'gate', '--manifest', 'manifest-first.json', '--manifest=manifest-second.json');
  assert.equal(gateMissing.json.verdict, 'FAIL');
  assert.match(gateMissing.json.blockers.join('\n'), /MANIFEST_MISSING_FILE.*manifest-first\.json/);

  w(root, 'manifest-first.json', JSON.stringify({ present: '/assets/present.png' }));
  const valid = studioJson(root, 'assets', '--manifest=manifest-first.json', '--manifest', 'manifest-second.json');
  assert.equal(valid.code, 0, JSON.stringify(valid.json.issues));
  assert.equal(valid.json.summary.errors, 0);
  assert.equal(studioJson(root, 'gate', '--manifest=manifest-first.json', '--manifest', 'manifest-second.json').json.verdict, 'NEEDS_HUMAN');
});

test('lint: 피드백에서 나온 실패 유형을 잡는다', () => {
  const root = tmpRoot();
  w(root, 'src/game/s.ts', "this.anims.create({ key: 'walk', frameRate: 40 });\nif (e.key === 'z') {}\nthis.input.keyboard.enabled = false;\nwindow.addEventListener('keydown', f);\n");
  const r = studioJson(root, 'lint');
  assert.equal(r.code, 1);
  const rules = new Set(r.json.findings.map((f) => f.rule));
  for (const x of ['ANIM_FPS', 'IME_KEY', 'INPUT_DISABLED_NO_RESTORE', 'LISTENER_LEAK']) assert.ok(rules.has(x), x);
  w(root, 'src/game/s.ts', "this.anims.create({ key: 'walk', frameRate: 8 });\nif (e.code === 'KeyZ') {}\n");
  assert.equal(studio(root, 'lint').code, 0);
});

test('gate: reach 와 주관 확인 claim 은 실플레이·인증 사용자 확인 전까지 NEEDS_HUMAN, 증거 변조는 FAIL', () => {
  const root = tmpRoot();
  w(root, 'src/game.ts', 'export const x = 1;\n');
  w(root, 'docs/studio/canon.json', JSON.stringify({ include: ['src/**'], forbidden: [{ id: 'X', pattern: '대기업', why: 'w' }] }));
  studio(root, 'ledger', 'add', '--quote', '이동 불가', '--category', 'bug');
  studio(root, 'ledger', 'add', '--quote', '그림 어색', '--category', 'art');
  assert.equal(studioJson(root, 'gate').json.verdict, 'FAIL', '열린 항목');
  studio(root, 'ledger', 'status', 'F001', 'fixed');
  studio(root, 'ledger', 'status', 'F002', 'fixed');

  // bug: 실패한 reach 진단은 evidence 가 아니며 통과한 명세 검사도 실플레이를 대신하지 않는다.
  const spec = { bounds: { w: 100, h: 100 }, player: { w: 10, h: 10, ox: -5, oy: -5 }, step: 5, solids: [{ x: 40, y: 0, w: 10, h: 100 }], spawns: [{ id: 's', x: 10, y: 50 }], targets: [{ id: 't', x: 90, y: 50, reach: 10 }] };
  w(root, 'level.json', JSON.stringify(spec));
  assert.equal(studio(root, 'reach', '--spec', 'level.json', '--out', 'docs/studio/evidence/reach-bad.json').code, 1);
  assert.notEqual(studio(root, 'ledger', 'evidence', 'F001', '--type', 'reach-check', '--ref', 'docs/studio/evidence/reach-bad.json').code, 0, '실패 결과는 reach-check 증거가 될 수 없다');
  assert.notEqual(studio(root, 'ledger', 'evidence', 'F001', '--type', 'repro-before', '--ref', 'docs/studio/evidence/reach-bad.json').code, 0);
  spec.solids = [{ x: 40, y: 0, w: 10, h: 30 }];
  w(root, 'level.json', JSON.stringify(spec));
  assert.equal(studio(root, 'reach', '--spec', 'level.json', '--attach', 'F001').code, 0);

  // art: 단색 캡처는 거부, 실제 화면은 수락
  w(root, 'flat.png', '');
  fs.writeFileSync(path.join(root, 'flat.png'), png(32, 32, flat));
  assert.notEqual(studio(root, 'ledger', 'evidence', 'F002', '--type', 'screenshot', '--ref', 'flat.png').code, 0);
  fs.writeFileSync(path.join(root, 'shot.png'), png(32, 32, noisy));
  assert.equal(studio(root, 'ledger', 'evidence', 'F002', '--type', 'screenshot', '--ref', 'shot.png').code, 0);

  let g = studioJson(root, 'gate');
  assert.equal(g.json.verdict, 'NEEDS_HUMAN', JSON.stringify(g.json));
  assert.match(g.json.pending.join('\n'), /F002.*사용자 확인 없음/);
  assert.match(g.json.wording, /사용자 확인 대기/);

  assert.notEqual(studio(root, 'ledger', 'evidence', 'F002', '--type', 'user-confirmed', '--quote', 'ok').code, 0, '출처 없는 사용자 확인은 거부');
  assert.equal(studio(root, 'ledger', 'evidence', 'F002', '--type', 'user-confirmed', '--quote', '그림 괜찮네', '--source', 'session:test#1').code, 0);
  g = studioJson(root, 'gate');
  assert.equal(g.json.verdict, 'NEEDS_HUMAN', JSON.stringify(g.json));
  assert.equal(g.json.counts.verified, 0, JSON.stringify(g.json.items));
  assert.equal(g.json.counts.needsHuman, 2, JSON.stringify(g.json.items));
  assert.match(g.json.pending.join('\n'), /문자열 quote\/source/);
  fs.writeFileSync(path.join(root, 'shot.png'), png(32, 32, (x, y) => [x * 7, y * 7, 0, 255]));
  g = studioJson(root, 'gate');
  assert.equal(g.json.verdict, 'FAIL', '증거 스크린샷을 나중에 바꾸면 잡아낸다');
});

test('gate: CLI 직접 실행한 테스트만 logic 증거가 되고, 수기 로그는 거부한다', () => {
  const root = tmpRoot();
  studio(root, 'ledger', 'add', '--quote', '점수 계산 오류', '--category', 'logic');
  w(root, 'log.txt', 'ok');
  assert.notEqual(studio(root, 'ledger', 'evidence', 'F001', '--type', 'machine-test', '--ref', 'log.txt', '--note', 'exit 0').code, 0);
  assert.notEqual(studioJson(root, 'gate').json.verdict, 'COMPLETE');
  w(root, 'calc.js', 'ok');
  const args = '["-e","const fs=require(\'fs\');if(fs.readFileSync(\'calc.js\',\'utf8\')!==\'ok\')process.exit(2)"]';
  assert.equal(studio(root, 'ledger', 'test', 'F001', '--program', 'node', '--input', 'calc.js', '--args-json', args).code, 0);
  studio(root, 'ledger', 'status', 'F001', 'fixed');
  assert.equal(studioJson(root, 'gate').json.verdict, 'COMPLETE');
  w(root, 'calc.js', 'changed');
  assert.equal(studioJson(root, 'gate').json.verdict, 'FAIL', '실행 입력이 바뀌면 이전 통과 결과는 stale');
  const failed = studio(root, 'ledger', 'test', 'F001', '--program', 'node', '--input', 'calc.js', '--args-json', '["-e","process.exit(2)"]');
  assert.equal(failed.code, 1);
  assert.equal(studioJson(root, 'gate').json.verdict, 'FAIL');
});

test('ledger: 반복 input 을 순서대로 receipt 에 넣고 첫 입력 변경도 stale 로 검출한다', () => {
  const root = tmpRoot();
  w(root, 'a.txt', 'a');
  w(root, 'b.txt', 'b');
  w(root, 'c.txt', 'c');
  studio(root, 'ledger', 'add', '--quote', '다중 입력 계산', '--category', 'logic');
  const args = '["-e","const fs=require(\\"fs\\");if(fs.readFileSync(\\"a.txt\\",\\"utf8\\")!==\\"a\\"||fs.readFileSync(\\"b.txt\\",\\"utf8\\")!==\\"b\\"||fs.readFileSync(\\"c.txt\\",\\"utf8\\")!==\\"c\\")process.exit(2)"]';
  const run = studio(root, 'ledger', 'test', 'F001', '--program', 'node', '--input', 'a.txt', '--input=b.txt', '--input', 'c.txt', '--args-json', args);
  assert.equal(run.code, 0, run.err);
  const receipt = JSON.parse(fs.readFileSync(path.join(root, 'docs/studio/ledger.json'), 'utf8')).items[0].evidence[0].execution;
  assert.deepEqual(receipt.inputs.map((input) => input.ref), ['a.txt', 'b.txt', 'c.txt']);
  studio(root, 'ledger', 'status', 'F001', 'fixed');
  assert.equal(studioJson(root, 'gate').json.verdict, 'COMPLETE');

  w(root, 'a.txt', 'changed');
  const gate = studioJson(root, 'gate');
  assert.equal(gate.json.verdict, 'FAIL');
  assert.match(gate.json.blockers.join('\n'), /입력 내용이 바뀌었다/);
});

test('play: 브라우저가 없으면 통과를 꾸미지 않고 unsupported(3)를 낸다', () => {
  const root = tmpRoot();
  const empty = tmpRoot();
  w(root, 'p.json', JSON.stringify({ steps: [{ op: 'wait', ms: 10 }] }));
  const r = spawnSync('node', [BIN, '--root', root, 'play', '--script', 'p.json', '--url', 'http://127.0.0.1:9/', '--json'], { encoding: 'utf8', cwd: empty, env: { ...process.env, STUDIO_PLAYWRIGHT_DIR: empty, TTAKKARI_PLAYWRIGHT_DIR: empty } });
  assert.equal(r.status, 3, r.stderr);
  const j = JSON.parse(r.stdout);
  assert.equal(j.status, 'unsupported');
  assert.equal(j.ok, false);
});

function playwrightDir() {
  for (const d of [process.env.STUDIO_PLAYWRIGHT_DIR, process.env.TTAKKARI_PLAYWRIGHT_DIR, path.join(BACK, '.browser-runtime'), '/Users/winterholic/development/projects/winterholic-ttakkari/back/.browser-runtime']) {
    if (d && fs.existsSync(path.join(d, 'node_modules/playwright'))) return d;
  }
  return null;
}

test('play: 실제 브라우저에서 재현 입력을 재생해 버그 있는 페이지는 실패, 고친 페이지는 통과시킨다', { timeout: 120000 }, (t) => {
  const pwDir = playwrightDir();
  if (!pwDir) return t.skip('playwright 를 찾지 못했다(STUDIO_PLAYWRIGHT_DIR 지정)');
  const root = tmpRoot();
  const page = (moves) => `<!doctype html><canvas id=c width=200 height=100></canvas><script>
const c=document.getElementById('c').getContext('2d');window.px=10;
addEventListener('keydown',e=>{ if(${moves}) window.px+=20; });
setInterval(()=>{c.fillStyle='#223';c.fillRect(0,0,200,100);c.fillStyle='#f55';c.fillRect(window.px,40,20,20);},30);
</script>`;
  w(root, 'broken/index.html', page("e.key==='ArrowRight' && false"));
  w(root, 'fixed/index.html', page("e.code==='ArrowRight'"));
  w(root, 'move.json', JSON.stringify({ name: 'move-right', viewport: { width: 300, height: 200 }, steps: [{ op: 'wait', ms: 200 }, { op: 'key', code: 'ArrowRight', holdMs: 120 }, { op: 'shot', name: 'after' }, { op: 'expect', expr: 'window.px > 10', label: '오른쪽으로 움직임' }, { op: 'expectNoConsoleErrors' }] }));
  studio(root, 'ledger', 'add', '--quote', '오른쪽으로 이동 불가', '--category', 'bug');
  const env = { ...process.env, STUDIO_PLAYWRIGHT_DIR: pwDir };
  // 샌드박스는 브라우저의 loopback 접속을 막을 수 있어 file:// 로 연다(서빙 핸들러는 아래 별도 테스트).
  const run = (dir, phase) => spawnSync('node', [BIN, '--root', root, 'play', '--script', 'move.json', '--url', 'file://' + path.join(root, dir, 'index.html'), '--attach', 'F001', '--phase', phase, '--json'], { encoding: 'utf8', env });
  const before = run('broken', 'before');
  if (before.status === 3) return t.skip('브라우저를 실행할 수 없다: ' + JSON.parse(before.stdout).reason);
  assert.equal(before.status, 1, before.stdout + before.stderr);
  const bj = JSON.parse(before.stdout);
  assert.equal(bj.status, 'failed');
  assert.match(bj.attached, /repro-before/);
  assert.equal(bj.shots.length, 1);
  assert.equal(bj.shots[0].pixels.blank, false);
  const after = run('fixed', 'after');
  assert.equal(after.status, 0, after.stdout + after.stderr);
  assert.match(JSON.parse(after.stdout).attached, /play-replay/);
  studio(root, 'ledger', 'status', 'F001', 'fixed');
  const g = studioJson(root, 'gate');
  assert.equal(g.json.items[0].state, 'verified', JSON.stringify(g.json));
});

test('serve: 정적 핸들러는 경로 탈출을 막고 없는 경로는 index.html 로 되돌린다', async () => {
  const { makeHandler } = await import(path.join(KIT, 'lib/play.mjs'));
  const root = tmpRoot();
  w(root, 'dist/index.html', '<h1>hi</h1>');
  w(root, 'dist/a.js', '1');
  w(root, 'secret.txt', 'x');
  const call = (url) => {
    const res = { status: null, writeHead(s) { this.status = s; return this; }, end() {}, on() { return this; }, once() { return this; }, emit() {}, write() {} };
    makeHandler(path.join(root, 'dist'))({ url }, res);
    return res;
  };
  assert.equal(call('/a.js').status, 200);
  assert.equal(call('/nope/route').status, 200, 'SPA fallback');
  assert.equal(call('/../secret.txt').status, 403);
  assert.equal(call('/%2e%2e/secret.txt').status, 403);
});
