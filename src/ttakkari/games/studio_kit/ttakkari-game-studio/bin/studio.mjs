#!/usr/bin/env node
// 따까리 게임 스튜디오 키트 CLI. 의존성 없음(node 20+). 사용법: node bin/studio.mjs help
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { auditAssets, contactSheetHtml } from '../lib/assets.mjs';
import { checkCanon } from '../lib/canon.mjs';
import { gateText, runGate } from '../lib/gate.mjs';
import { CATEGORIES, EVIDENCE_TYPES, addEvidence, addItem, importMarkdown, loadLedger, evaluateItem, setStatus } from '../lib/ledger.mjs';
import { lintProject } from '../lib/lint.mjs';
import { loadPlaywright, runPlay, serveDir } from '../lib/play.mjs';
import { checkReach } from '../lib/reach.mjs';
import { parseArgs, writeJson } from '../lib/util.mjs';

const KIT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const HELP = `studio — 따까리 게임 스튜디오 도구 (게임 저장소 루트에서 실행, --root 로 변경)

  init                                  docs/studio/ 에 원장·정전·에셋 규격·아트 바이블·버티컬 슬라이스 템플릿을 만든다(있으면 건드리지 않음)
  ledger import <feedback.md>           '## 장면' 제목 아래 '- 항목' 줄을 원장으로 옮긴다(범주는 추정, 확인 필요)
  ledger add --quote 원문 [--scene S] [--expect 기대] [--category ${CATEGORIES.join('|')}]
  ledger evidence <ID> --type ${EVIDENCE_TYPES.join('|')} [--ref 파일] [--note] [--quote --source] [--reason]
  ledger status <ID> open|fixed|wontfix [--reason]
  ledger list [--open] | ledger check
  canon                                 docs/studio/canon.json 의 금지어·필수 요소를 소스·문서에서 검사
  assets [--manifest f.json ...] [--sheet]   에셋 규격·중복·빈 이미지·manifest 검사, contact sheet(HTML) 생성
  reach --spec level.json               레벨 명세로 스폰 끼임·상호작용/문 도달성 검사
  lint [--dir src]                      실패 유형별 정적 린트(단서)
  play --script s.json (--url U | --serve dist) [--attach ID] [--phase before|after]
                                        재현 입력을 헤드리스 브라우저에서 재생(스크린샷·콘솔 오류·단언)
  doctor                                도구 실행 환경(node·playwright·브라우저) 점검
  gate [--manifest f.json] [--json]     제작 완료 계약 판정(FAIL | NEEDS_HUMAN | COMPLETE)

종료 코드: 0 통과 · 1 실패 · 2 사용법 오류 · 3 실행 환경 미지원(통과로 취급하지 않는다)
키트: ${KIT}`;

const { pos, opt } = parseArgs(process.argv.slice(2));
const root = path.resolve(opt.root && opt.root !== true ? opt.root : process.cwd());
const asJson = !!opt.json;
const out = (o, text) => process.stdout.write(asJson ? JSON.stringify(o, null, 2) + '\n' : text + '\n');
const arr = (v) => (v === undefined ? [] : Array.isArray(v) ? v : [v]);

async function main() {
  const cmd = pos[0];
  if (!cmd || cmd === 'help' || opt.help) return out({ help: HELP }, HELP), 0;

  if (cmd === 'init') {
    const made = [];
    const dest = path.join(root, 'docs/studio');
    fs.mkdirSync(dest, { recursive: true });
    for (const f of fs.readdirSync(path.join(KIT, 'templates'))) {
      const to = path.join(dest, f);
      if (fs.existsSync(to)) continue;
      fs.copyFileSync(path.join(KIT, 'templates', f), to);
      made.push(`docs/studio/${f}`);
    }
    out({ made }, made.length ? '만든 파일:\n' + made.map((m) => ' - ' + m).join('\n') : '이미 모두 있다. 건드리지 않았다.');
    return 0;
  }

  if (cmd === 'ledger') {
    const sub = pos[1];
    if (sub === 'import') {
      if (!pos[2]) return console.error('ledger import <feedback.md>'), 2;
      const added = importMarkdown(root, path.resolve(root, pos[2]));
      out({ added }, `${added.length}개 항목을 추가했다(범주는 키워드 추정이니 확인):\n` + added.map((i) => ` ${i.id} [${i.category}] ${i.scene}: ${i.quote.slice(0, 60)}`).join('\n'));
      return 0;
    }
    if (sub === 'add') {
      const item = addItem(root, { quote: opt.quote, scene: opt.scene, expectation: opt.expect, category: opt.category });
      out(item, `추가: ${item.id} [${item.category}]`);
      return 0;
    }
    if (sub === 'evidence') {
      const ev = addEvidence(root, pos[2], { type: opt.type, ref: opt.ref, note: opt.note === true ? '' : opt.note, source: opt.source, quote: opt.quote, reason: opt.reason });
      out(ev, `증거 추가: ${pos[2]} ← ${ev.type}${ev.ref ? ' ' + ev.ref : ''}`);
      return 0;
    }
    if (sub === 'status') {
      const item = setStatus(root, pos[2], pos[3], opt.reason === true ? '' : opt.reason);
      out(item, `${item.id} → ${item.status}`);
      return 0;
    }
    if (sub === 'list' || sub === 'check') {
      const l = loadLedger(root);
      const rows = l.data.items.filter((i) => !opt.open || i.status === 'open').map((i) => ({ ...i, eval: evaluateItem(root, i) }));
      out(rows, rows.map((i) => ` ${i.id} [${i.category}${i.category_guess ? '?' : ''}] ${i.status} → ${i.eval.state}  ${i.scene}: ${i.quote.slice(0, 50)}`).join('\n') || '(비었다)');
      return 0;
    }
    return console.error('ledger import|add|evidence|status|list|check'), 2;
  }

  if (cmd === 'canon') {
    const r = checkCanon(root, opt.canon && opt.canon !== true ? path.resolve(root, opt.canon) : undefined);
    out(r, r.status === 'unconfigured' ? r.reason : `정전 검사: ${r.ok ? '통과' : '위반 ' + r.violations.length + '건·필수 부족 ' + r.missing.length + '건'} (파일 ${r.scanned}개)\n` + [...r.violations.map((v) => ` ✗ ${v.rule} ${v.file}:${v.line} "${v.match}" — ${v.why}`), ...r.missing.map((m) => ` ✗ 필수 ${m.rule} ${m.found}/${m.min}`)].join('\n'));
    return r.status === 'unconfigured' ? 3 : r.ok ? 0 : 1;
  }

  if (cmd === 'assets') {
    const r = auditAssets(root, { manifests: arr(opt.manifest), dir: opt.dir && opt.dir !== true ? opt.dir : undefined });
    if (opt.sheet) {
      const html = path.join(root, 'docs/studio/evidence/contact-sheet.html');
      fs.mkdirSync(path.dirname(html), { recursive: true });
      fs.writeFileSync(html, contactSheetHtml(r, path.dirname(html), root));
      r.contactSheet = path.relative(root, html);
    }
    out(r, `에셋 ${r.summary.files ?? 0}개 · 오류 ${r.summary.errors ?? 0} · 경고 ${r.summary.warnings ?? 0}${r.contactSheet ? '\ncontact sheet: ' + r.contactSheet + ' (실제 브라우저로 열어 캡처해서 눈으로 본다)' : ''}\n` + r.issues.filter((i) => i.level !== 'info').map((i) => ` ${i.level === 'error' ? '✗' : '!'} ${i.code} ${i.file}: ${i.msg}`).join('\n'));
    return r.ok ? 0 : 1;
  }

  if (cmd === 'reach') {
    if (!opt.spec || opt.spec === true) return console.error('reach --spec level.json'), 2;
    const r = checkReach(path.resolve(root, opt.spec));
    out(r, `도달성: ${r.ok ? '통과' : '문제 ' + r.problems.length + '건'}\n` + r.problems.map((p) => ' ✗ ' + p).join('\n'));
    if (opt.out && opt.out !== true) writeJson(path.resolve(root, opt.out), r);
    return r.ok ? 0 : 1;
  }

  if (cmd === 'lint') {
    const r = lintProject(root, { dir: opt.dir && opt.dir !== true ? opt.dir : 'src' });
    out(r, `린트: 오류 ${r.summary?.errors ?? 0} · 경고 ${r.summary?.warnings ?? 0}\n` + r.findings.map((f) => ` ${f.level === 'error' ? '✗' : '!'} ${f.rule} ${f.file}:${f.line} ${f.msg}`).join('\n'));
    return r.ok ? 0 : 1;
  }

  if (cmd === 'doctor') {
    const pw = loadPlaywright(root);
    const info = { node: process.version, playwright: pw.pw ? pw.from : null, playwrightReason: pw.reason ?? null };
    out(info, `node ${info.node}\nplaywright: ${info.playwright ?? '없음 — ' + info.playwrightReason}\n(브라우저 실행 가능 여부는 play 를 실제로 돌려 봐야 안다)`);
    return pw.pw ? 0 : 3;
  }

  if (cmd === 'play') {
    if (!opt.script || opt.script === true) return console.error('play --script s.json (--url U | --serve dist)'), 2;
    let url = opt.url;
    let srv = null;
    if (opt.serve && opt.serve !== true) {
      srv = await serveDir(path.resolve(root, opt.serve));
      url = srv.url;
    }
    if (!url || url === true) return console.error('--url 또는 --serve 가 필요하다'), 2;
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    const name = path.basename(String(opt.script), '.json');
    const outDir = path.join(root, 'docs/studio/evidence', `${name}-${opt.phase ?? 'run'}-${stamp}`);
    let r;
    try {
      r = await runPlay({ root, script: path.resolve(root, opt.script), url, outDir });
    } finally {
      srv?.server.close();
    }
    const resultFile = path.join(outDir, 'result.json');
    fs.mkdirSync(outDir, { recursive: true });
    writeJson(resultFile, r);
    r.resultFile = path.relative(root, resultFile);
    if (opt.attach && opt.attach !== true && r.status !== 'unsupported') {
      const type = opt.phase === 'before' ? 'repro-before' : 'play-replay';
      try {
        addEvidence(root, opt.attach, { type, ref: r.resultFile, note: `play ${name}` });
        r.attached = `${opt.attach} ← ${type}`;
      } catch (e) {
        r.attachError = e.message;
      }
    } else if (opt.attach && r.status === 'unsupported') {
      r.attachError = '실행 환경 미지원이라 증거를 붙이지 않았다. ledger evidence --type not-run --reason ... 로 기록한다';
    }
    out(r, `play ${r.name}: ${r.status}${r.reason ? ' — ' + r.reason : ''}\n` + r.steps.filter((s) => !s.ok).map((s) => ` ✗ #${s.i} ${s.op} ${s.msg}`).join('\n') + `\n결과: ${r.resultFile}${r.attached ? '\n증거: ' + r.attached : ''}${r.attachError ? '\n증거 안 붙음: ' + r.attachError : ''}`);
    return r.status === 'passed' ? 0 : r.status === 'unsupported' ? 3 : 1;
  }

  if (cmd === 'gate') {
    const g = runGate(root, { manifests: arr(opt.manifest) });
    out(g, gateText(g));
    return g.verdict === 'FAIL' ? 1 : 0;
  }

  console.error(`알 수 없는 명령: ${cmd}\n\n${HELP}`);
  return 2;
}

main().then(
  (code) => process.exit(code),
  (e) => {
    console.error('오류: ' + (e?.message ?? e));
    process.exit(1);
  },
);
