// 시안 실브라우저 검증. 설치된 Google Chrome 을 CDP 로 직접 제어한다(도구 설치 없음).
// 사용: node verify.mjs <prototype.html 경로> <출력 디렉터리> [captures 만]
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

const [htmlArg, outArg] = process.argv.slice(2);
if (!htmlArg || !outArg) throw new Error("usage: verify.mjs <html> <outDir>");
const html = resolve(htmlArg);
const out = resolve(outArg);
mkdirSync(out, { recursive: true });
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9400 + Math.floor(Math.random() * 300);
const STATES = ["normal", "problem", "loading", "denied", "failed", "partial", "empty", "stale"];
const VIEWS = [
  { id: "m390", w: 390, h: 844, dsf: 2, mobile: true },
  { id: "d1280", w: 1280, h: 900, dsf: 1, mobile: false },
];
const THEMES = ["light", "dark"];

const proc = spawn(CHROME, [`--remote-debugging-port=${PORT}`, "--headless=new", "--user-data-dir=/tmp/chrome-ic-" + PORT, "--no-first-run", "--disable-gpu", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function connect() {
  for (let i = 0; i < 60; i++) {
    try {
      const j = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" })).json();
      return j.webSocketDebuggerUrl;
    } catch { await sleep(250); }
  }
  throw new Error("chrome not reachable");
}
const ws = new WebSocket(await connect());
await new Promise((r) => (ws.onopen = r));
let id = 0;
const pending = new Map();
const events = [];
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (d.id && pending.has(d.id)) { const { res, rej } = pending.get(d.id); pending.delete(d.id); d.error ? rej(new Error(JSON.stringify(d.error))) : res(d.result); }
  else if (d.method) events.push(d);
};
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async (expr) => { const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails)); return r.result.value; };
await send("Page.enable"); await send("Runtime.enable"); await send("Log.enable");

async function open(state, theme, view, extra = "") {
  await send("Emulation.setDeviceMetricsOverride", { width: view.w, height: view.h, deviceScaleFactor: view.dsf, mobile: view.mobile });
  await send("Emulation.setEmulatedMedia", { features: [{ name: "pointer", value: view.mobile ? "coarse" : "fine" }] });
  const url = pathToFileURL(html).href + `#s=${state}&t=${theme}&chrome=0${extra}`;
  await send("Page.navigate", { url: "about:blank" });
  await send("Page.navigate", { url });
  await sleep(120);
  await ev(`document.fonts.ready.then(()=>true)`);
  await ev(`location.reload(), true`).catch(() => {});
  await sleep(700);
  await ev(`document.fonts.ready.then(()=>true)`);
}

const checkExpr = `(() => {
  const parse = (c) => { const m = c.match(/rgba?\\(([^)]+)\\)/); if (m) { const p = m[1].split(/[ ,\\/]+/).filter(Boolean).map(Number); return { r: p[0], g: p[1], b: p[2], a: p[3] ?? 1 }; }
    const s = c.match(/color\\(srgb ([^)]+)\\)/); if (s) { const p = s[1].split(/[ \\/]+/).filter(Boolean).map(Number); return { r: p[0]*255, g: p[1]*255, b: p[2]*255, a: p[3] ?? 1 }; } return null; };
  const lum = ({r,g,b}) => { const f = v => { v/=255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4); }; return 0.2126*f(r)+0.7152*f(g)+0.0722*f(b); };
  const over = (top, bot) => ({ r: top.r*top.a+bot.r*(1-top.a), g: top.g*top.a+bot.g*(1-top.a), b: top.b*top.a+bot.b*(1-top.a), a: 1 });
  const bgOf = (el) => { const stack = []; for (let e = el; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor); if (c && c.a > 0) { stack.push(c); if (c.a >= 1) break; } } let base = { r:255, g:255, b:255, a:1 }; if (!stack.length || stack[stack.length-1].a < 1) { const cv = parse(getComputedStyle(document.body).backgroundColor); base = cv && cv.a>0 ? cv : base; } return stack.reverse().reduce((acc, c) => over(c, acc), base); };
  const bad = [], seen = new Set(); let n = 0;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) { const t = walker.currentNode; if (!t.textContent.trim()) continue; const el = t.parentElement; if (!el || seen.has(el)) continue; seen.add(el);
    const cs = getComputedStyle(el); const r = el.getBoundingClientRect(); if (cs.visibility==='hidden'||cs.display==='none'||r.width===0||r.height===0) continue; if (el.closest('.pt-bar,.sr-only,script,style')) continue;
    const fg = parse(cs.color); if (!fg) continue; const bg = bgOf(el); const f2 = over(fg, bg); const L1 = lum(f2), L2 = lum(bg); const ratio = (Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);
    const size = parseFloat(cs.fontSize), bold = parseInt(cs.fontWeight) >= 700; const large = size >= 24 || (size >= 18.66 && bold); const need = large ? 3 : 4.5; n++;
    if (ratio < need) bad.push({ text: t.textContent.trim().slice(0,30), ratio: +ratio.toFixed(2), need, size, fg: cs.color, bg: 'rgb('+[bg.r,bg.g,bg.b].map(Math.round)+')' }); }
  const small = []; const mob = matchMedia('(pointer: coarse)').matches;
  document.querySelectorAll('main button, main a, main summary, header button, header a').forEach(e => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e); if (r.width===0||r.height===0||cs.visibility==='hidden') return; const dis = e.disabled; if (dis) return; if (Math.min(r.width, r.height) < (mob ? 44 : 32)) small.push({ el: (e.className||e.tagName)+':'+(e.textContent.trim().slice(0,20)||e.getAttribute('aria-label')), w: Math.round(r.width), h: Math.round(r.height) }); });
  const noName = []; document.querySelectorAll('button, a[href], summary, [role=button]').forEach(e => { if (e.closest('.pt-bar')) return; const name = (e.getAttribute('aria-label') || e.textContent || '').trim(); if (!name) noName.push(e.outerHTML.slice(0,80)); });
  const heads = [...document.querySelectorAll('main h1, main h2, main h3, header h1')].map(h => +h.tagName[1]); let skip = false; for (let i=1;i<heads.length;i++) if (heads[i] - heads[i-1] > 1) skip = true;
  const h1 = document.querySelectorAll('h1').length;
  return { overflowX: document.documentElement.scrollWidth - innerWidth, textChecked: n, contrastFails: bad, smallTargets: small, noName, headingLevels: heads, headingSkip: skip, h1, mains: document.querySelectorAll('main').length, lang: document.documentElement.lang, title: document.title, busy: document.getElementById('main').getAttribute('aria-busy'), live: document.getElementById('live').textContent };
})()`;

async function shot(file, view) {
  const m = await send("Page.getLayoutMetrics");
  const h = Math.ceil(m.cssContentSize.height);
  const r = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true, clip: { x: 0, y: 0, width: view.w, height: Math.max(h, view.h), scale: 1 } });
  writeFileSync(file, Buffer.from(r.data, "base64"));
  return h;
}

const report = { when: new Date().toISOString(), chrome: (await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json()).Browser, results: [], keyboard: null, reflow320: [] };
for (const st of STATES) for (const th of THEMES) for (const view of VIEWS) {
  await open(st, th, view);
  const res = await ev(checkExpr);
  const name = `${st}-${view.id}-${th}.png`;
  const h = await shot(`${out}/${name}`, view);
  report.results.push({ state: st, theme: th, view: view.id, file: name, pageHeight: h, ...res });
}
// 모바일 상세 화면(문제 상태)
for (const th of THEMES) {
  await open("problem", th, VIEWS[0], "&d=loki");
  const res = await ev(checkExpr); const name = `problem-detail-m390-${th}.png`; await shot(`${out}/${name}`, VIEWS[0]);
  report.results.push({ state: "problem-detail", theme: th, view: "m390", file: name, ...res });
}
// 부분 성공 상세 (docker 권한)
for (const th of THEMES) {
  await open("partial", th, VIEWS[0], "&d=docker");
  const res = await ev(checkExpr); const name = `partial-detail-m390-${th}.png`; await shot(`${out}/${name}`, VIEWS[0]);
  report.results.push({ state: "partial-detail", theme: th, view: "m390", file: name, ...res });
}
// 320px 리플로우
for (const st of STATES) {
  await open(st, "light", { id: "m320", w: 320, h: 640, dsf: 2, mobile: true });
  const o = await ev(`document.documentElement.scrollWidth - innerWidth`);
  report.reflow320.push({ state: st, overflowX: o });
}
await open("problem", "light", { id: "m320", w: 320, h: 640, dsf: 2, mobile: true }, "&d=loki");
report.reflow320.push({ state: "problem-detail", overflowX: await ev(`document.documentElement.scrollWidth - innerWidth`) });
// 키보드 흐름(모바일 문제 상태: Tab 순서 → Enter → 상세 포커스 → Escape → 행 복귀)
async function key(k, code, vk, mods = 0) {
  // Enter 는 text 가 있어야 버튼 click 으로 이어진다.
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: k, code, windowsVirtualKeyCode: vk, modifiers: mods, ...(k === "Enter" ? { text: "\r" } : {}) });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: k, code, windowsVirtualKeyCode: vk, modifiers: mods });
}
const active = () => ev(`(() => { const e = document.activeElement; if (!e || e === document.body) return null; const cs = getComputedStyle(e); return { tag: e.tagName, text: (e.getAttribute('aria-label') || e.textContent || '').trim().slice(0, 24), outline: cs.outlineStyle + ' ' + cs.outlineWidth, boxShadow: cs.boxShadow !== 'none' }; })()`);
await open("problem", "light", VIEWS[0]);
const order = [];
for (let i = 0; i < 9; i++) { await key("Tab", "Tab", 9); order.push(await active()); }
await open("problem", "light", VIEWS[0]);
await ev(`document.querySelector('.ic-row').focus()`);
await key("Enter", "Enter", 13);
await sleep(150);
const afterEnter = await ev(`({ focus: document.activeElement.id, detailVisible: !!document.getElementById('detail'), listVisible: !!document.querySelector('.ic-list'), title: document.getElementById('d-title')?.textContent })`);
await key("Escape", "Escape", 27);
await sleep(150);
const afterEsc = await ev(`({ focusText: document.activeElement.textContent.trim().slice(0, 24), cls: document.activeElement.className, detailVisible: !!document.getElementById('detail') })`);
report.keyboard = { tabOrder: order, afterEnter, afterEsc };
// 데스크톱 기본 선택 + 행 전환
await open("problem", "light", VIEWS[1]);
report.desktopSplit = await ev(`({ cols: !!document.querySelector('.ic-cols[data-split="1"]'), selected: document.querySelector('.ic-row[aria-current="true"] .ic-row__name')?.textContent, detailTitle: document.getElementById('d-title')?.textContent })`);
await ev(`document.querySelectorAll('.ic-row')[1].click()`); await sleep(100);
report.desktopSplit.afterClick = await ev(`({ selected: document.querySelector('.ic-row[aria-current="true"] .ic-row__name')?.textContent, detailTitle: document.getElementById('d-title')?.textContent })`);
report.consoleErrors = events.filter((e) => (e.method === "Runtime.exceptionThrown") || (e.method === "Log.entryAdded" && e.params.entry.level === "error")).map((e) => JSON.stringify(e.params).slice(0, 200));
writeFileSync(`${out}/verify-report.json`, JSON.stringify(report, null, 2));
ws.close(); proc.kill();
const fails = report.results.filter((r) => r.overflowX > 0 || r.contrastFails.length || r.smallTargets.length || r.noName.length || r.headingSkip || r.h1 !== 1);
console.log(`captures=${report.results.length} issues=${fails.length} consoleErrors=${report.consoleErrors.length} reflow320Overflow=${report.reflow320.filter((r) => r.overflowX > 0).length}`);
for (const f of fails) console.log("ISSUE", f.state, f.view, f.theme, JSON.stringify({ overflowX: f.overflowX, contrast: f.contrastFails.slice(0, 3), small: f.smallTargets.slice(0, 3), noName: f.noName, headingSkip: f.headingSkip, h1: f.h1 }));
process.exit(0);
