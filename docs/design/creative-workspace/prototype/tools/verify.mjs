// 시안 검증 스크립트: 상태 매트릭스 캡처 + 가로 넘침·콘솔 오류 검사 + 키보드/흐름 검사.
// 사용: node tools/verify.mjs   (결과: shots/*.png, shots/report.json, 표준출력 요약)
import { createRequire } from 'node:module';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
const require = createRequire('/private/tmp/ttakkari-playwright-node/node_modules/');
const { chromium } = require('playwright');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const shots = path.join(root, 'shots');
fs.mkdirSync(shots, { recursive: true });
for (const f of fs.readdirSync(shots)) if (f.endsWith('.png') || f === 'report.json') fs.rmSync(path.join(shots, f));
const URL = (h) => 'file://' + root + '/index.html' + h;

const VIEWS = { m: { width: 390, height: 844 }, d: { width: 1440, height: 900 } };
const WORK = ['compare', 'new', 'notools', 'attach', 'running', 'cancelled', 'v1', 'approved', 'error', 'lost', 'unsupported'];
const MATRIX = [
  ...WORK.map((s) => ['work-' + s, '#work?s=' + s, 'work']),
  ['work-compare-overlay', '#work?s=compare&view=overlay', 'work'],
  ['work-emptyproject', '#work?p=p2', 'work'],
  ['work-t2-longrender', '#work?t=t2', 'work'],
  ['work-t3-cad', '#work?t=t3', 'work'],
  ['library-list', '#library', 'library'],
  ['library-loading', '#library?s=loading', 'library'],
  ['library-error', '#library?s=error', 'library'],
  ['library-emptyfilter', '#library?q=zzzz', 'library'],
  ['library-emptyproject', '#library?p=p2', 'library'],
  ...['l1', 'l2', 'l3', 'l4', 'l5', 'l6', 'l7', 'l8'].map((i) => ['library-item-' + i, '#library?item=' + i, 'library']),
  ['history-list', '#history', 'history'],
  ['history-loading', '#history?s=loading', 'history'],
  ['history-error', '#history?s=error', 'history'],
  ['history-empty', '#history?s=empty', 'history'],
  ...['r1', 'r2', 'r3', 'r4', 'r5', 'r6', 'r7', 'r8', 'r9', 'r10', 'r11'].map((i) => ['history-run-' + i, '#history?run=' + i, 'history']),
  ['tools-real', '#tools', 'tools'],
  ['tools-loading', '#tools?s=loading', 'tools'],
  ['tools-error', '#tools?s=error', 'tools'],
  ['tools-offline', '#tools?s=offline&tool=pdf', 'tools'],
  ...['photo', 'film', 'vector', 'pdf', 'fig', 'grid', 'design'].map((t) => ['tools-sample-' + t, '#tools?s=sample&tool=' + t, 'tools']),
  ['tools-real-photo', '#tools?tool=photo', 'tools'],
];
// 다크 캡처는 대표 상태만(화면당 기본 + 핵심)
const DARK_OK = new Set(['work-compare', 'work-error', 'work-notools', 'library-list', 'library-item-l1', 'library-item-l5', 'history-list', 'history-run-r2', 'history-run-r8', 'tools-real', 'tools-sample-photo', 'tools-sample-grid']);

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const report = { shots: 0, overflow: [], errors: [], checks: [], a11y: [] };
const log = (ok, name, detail = '') => { report.checks.push({ ok, name, detail }); console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? ' — ' + detail : '')); };

async function a11yOf(page) {
  return page.evaluate(() => {
    const out = [];
    const name = (el) => (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || (el.labels && el.labels[0] && el.labels[0].innerText) || el.innerText || el.getAttribute('title') || '').trim();
    for (const el of document.querySelectorAll('#cw-screen button, #cw-screen a[href], #cw-screen select, #cw-screen input, #cw-screen textarea, dialog[open] button')) {
      if (!name(el)) out.push('이름 없음: ' + el.tagName + '.' + String(el.className).slice(0, 30));
    }
    const ids = [...document.querySelectorAll('[id]')].map((e) => e.id); const dup = ids.filter((x, i) => ids.indexOf(x) !== i);
    if (dup.length) out.push('중복 id: ' + [...new Set(dup)].join(','));
    for (const img of document.querySelectorAll('img:not([alt])')) out.push('alt 없음 img');
    if (document.querySelectorAll('h1').length !== 1) out.push('h1 개수 ' + document.querySelectorAll('h1').length);
    return out;
  });
}
async function overflowOf(page, mobileDetail) {
  return page.evaluate((md) => {
    const vw = window.innerWidth;
    const out = [];
    if (document.documentElement.scrollWidth > vw + 1) out.push('page scrollWidth ' + document.documentElement.scrollWidth + ' > ' + vw);
    const roots = [document.querySelector('#cw-screen')];
    if (md) roots.push(document.querySelector('dialog[open]'));
    for (const r of roots.filter(Boolean)) {
      for (const el of r.querySelectorAll('*')) {
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
        const rc = el.getBoundingClientRect();
        if (rc.width === 0) continue;
        // 의도된 가로 스크롤 영역(칩 줄·탭) 안의 요소는 제외
        let p = el.parentElement, scroller = false;
        while (p && p !== r) { const o = getComputedStyle(p).overflowX; if ((o === 'auto' || o === 'scroll') && p.scrollWidth > p.clientWidth + 1) { scroller = true; break; } p = p.parentElement; }
        if (scroller) continue;
        if (rc.right > vw + 1 || rc.left < -1) out.push((el.className && String(el.className).slice(0, 40)) + '|' + el.tagName + ' right=' + Math.round(rc.right) + ' left=' + Math.round(rc.left));
        if (out.length > 5) break;
      }
    }
    return out;
  }, mobileDetail);
}

for (const [vk, vp] of Object.entries(VIEWS)) {
  for (const theme of ['light', 'dark']) {
    const ctx = await browser.newContext({ viewport: vp, colorScheme: theme });
    await ctx.addInitScript((t) => localStorage.setItem('tk-theme', t), theme);
    const page = await ctx.newPage();
    page.on('pageerror', (e) => report.errors.push(vk + theme + ' pageerror: ' + e.message));
    page.on('console', (m) => { if (m.type() === 'error') report.errors.push(vk + theme + ' console: ' + m.text()); });
    for (const [name, hash] of MATRIX) {
      if (theme === 'dark' && !DARK_OK.has(name)) continue;
      await page.goto(URL('#work')); // 해시만 바뀌어도 렌더되지만 깨끗한 상태를 위해 새로 로드
      await page.goto(URL(hash)); await page.reload(); await page.waitForTimeout(150);
      const md = vk === 'm' && /library-item|history-run/.test(name);
      const ov = await overflowOf(page, md);
      if (ov.length) report.overflow.push({ view: vk, theme, name, ov });
      const a11y = await a11yOf(page); if (a11y.length) report.a11y.push({ view: vk, theme, name, a11y });
      await page.screenshot({ path: path.join(shots, `${vk}-${theme}-${name}.png`) });
      report.shots++;
    }
    await ctx.close();
  }
}

/* ---------- 흐름·키보드 검사(데스크톱·모바일) ---------- */
async function flows() {
  const ctx = await browser.newContext({ viewport: VIEWS.d });
  const page = await ctx.newPage();
  page.on('pageerror', (e) => report.errors.push('flow pageerror: ' + e.message));
  const hash = () => page.evaluate(() => location.hash);
  const active = () => page.evaluate(() => (document.activeElement ? (document.activeElement.id || document.activeElement.getAttribute('aria-label') || document.activeElement.tagName) : ''));
  const live = () => page.evaluate(() => document.querySelector('#cw-live').textContent);

  // 1) 탭: 화살표 키 이동 + 접근성 속성
  await page.goto(URL('#work')); await page.waitForTimeout(100);
  await page.focus('#tab-work'); await page.keyboard.press('ArrowRight'); await page.waitForTimeout(80);
  log((await hash()).startsWith('#library'), '탭 ArrowRight → 결과물', await hash());
  const sel = await page.evaluate(() => [...document.querySelectorAll('[role=tab]')].map((t) => t.getAttribute('aria-selected')).join(','));
  log(sel === 'false,true,false,false', '선택된 탭 aria-selected', sel);
  await page.keyboard.press('End'); await page.waitForTimeout(80);
  log((await hash()).startsWith('#tools'), '탭 End → 도구', await hash());

  // 1b) 키보드만으로 첫 12개 포커스 대상이 순서대로 잡히고 포커스 링이 보이는지
  await page.goto(URL('#work?s=compare')); await page.waitForTimeout(100);
  await page.evaluate(() => document.activeElement && document.activeElement.blur());
  const seq = [];
  for (let i = 0; i < 14; i++) {
    await page.keyboard.press('Tab');
    seq.push(await page.evaluate(() => { const e = document.activeElement; const cs = getComputedStyle(e); return (e.getAttribute('aria-label') || e.innerText || e.id || e.tagName).trim().slice(0, 14).replace(/\n/g, ' ') + ((cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0) || cs.boxShadow !== 'none' ? '' : ' [링 없음]'); }));
  }
  const noRing = seq.filter((x) => x.includes('[링 없음]'));
  log(noRing.length === 0 && seq.some((x) => x.startsWith('작업실')) && seq.some((x) => x.includes('v2')), '키보드 Tab 순서 14칸 포커스 링 표시', seq.join(' → '));

  // 2) 승인: 모달 열기·Esc·다시 열기·중복 클릭 방지
  await page.goto(URL('#work?s=compare')); await page.waitForTimeout(100);
  await page.click('[data-dlg="approve"]'); await page.waitForTimeout(100);
  log(await page.evaluate(() => !!document.querySelector('dialog[open] #dlg-t') && /v2/.test(document.querySelector('#dlg-t').textContent)), '승인 모달에 대상 버전(v2) 표시');
  const body = await page.evaluate(() => document.querySelector('dialog[open]').innerText);
  log(/thumbnail\.png · v2/.test(body) && /5be0…c3d9/.test(body) && /초안/.test(body), '승인 모달: 파일·버전·해시·v1 보존 문구');
  await page.keyboard.press('Escape'); await page.waitForTimeout(100);
  log(await page.evaluate(() => !document.querySelector('dialog[open]')), '승인 모달 Esc 닫힘');
  await page.click('[data-dlg="approve"]'); await page.waitForTimeout(100);
  const btn = page.locator('[data-act="approve-do"]');
  await btn.dblclick(); await page.waitForTimeout(1300);
  log((await hash()).includes('s=approved'), '승인 완료 후 해시 s=approved', await hash());
  const approvedCount = await page.evaluate(() => document.querySelectorAll('.cw-approve[data-done]').length);
  log(approvedCount === 1, '더블클릭해도 승인 카드 1개', String(approvedCount));
  await page.goBack(); await page.waitForTimeout(150);
  log((await hash()).includes('s=compare') || !(await hash()).includes('approved'), '뒤로가기로 승인 전 화면 복원', await hash());
  await page.reload(); await page.waitForTimeout(150);
  log(await page.evaluate(() => !!document.querySelector('.cw-approve')), '새로고침 후에도 같은 화면(승인 영역)');

  // 3) 비교 방식 전환(키보드: 라디오 화살표) + 겹쳐보기 슬라이더
  await page.goto(URL('#work?s=compare&view=side')); await page.waitForTimeout(100);
  await page.focus('input[data-view][value="side"]'); await page.keyboard.press('ArrowRight'); await page.waitForTimeout(150);
  log((await hash()).includes('view=overlay'), '비교 방식 라디오 ArrowRight → 겹쳐보기', await hash());
  const ovOk = await page.evaluate(() => { const r = document.querySelector('#cw-split'); r.value = 20; r.dispatchEvent(new Event('input')); return getComputedStyle(document.querySelector('.cw-overlay__top')).getPropertyValue('--cw-split').trim(); });
  log(ovOk === '20%', '겹쳐보기 슬라이더 값 반영', ovOk);

  // 4) 작업 취소 확인
  await page.goto(URL('#work?s=running')); await page.waitForTimeout(100);
  await page.click('[data-dlg="cancelrun"]'); await page.waitForTimeout(100);
  const ct = await page.evaluate(() => document.querySelector('dialog[open]').innerText);
  log(/v2는 만들어지지 않아요/.test(ct) && /v1은 그대로/.test(ct), '취소 확인: 무엇이 남고 사라지는지 설명');
  await page.click('[data-act="cancel-do"]'); await page.waitForTimeout(900);
  log((await hash()).includes('s=cancelled'), '취소 후 s=cancelled', await hash());
  await page.click('[data-act="retry-run"]', { clickCount: 2, delay: 20 }); await page.waitForTimeout(1500);
  log((await hash()).includes('s=running'), '다시 시도(더블클릭) → running 1회', await hash());

  // 5) 도구 없음: 보내기 막힘 + 이유 문장
  await page.goto(URL('#work?s=notools')); await page.waitForTimeout(100);
  await page.fill('#cw-input', '썸네일 만들어줘');
  await page.press('#cw-input', 'Enter'); await page.waitForTimeout(200);
  log((await hash()).includes('notools'), '도구 없음: 보내기가 실행으로 넘어가지 않음', await hash());
  const why = await page.evaluate(() => document.querySelector('#cw-sendwhy').innerText);
  log(/도구가 아직 없어요/.test(why), '도구 없음 이유가 화면 문장으로 보임', why);

  // 6) 도구: 실제 모드에서 활성화·테스트 막힘 + 사유
  await page.goto(URL('#tools?tool=photo')); await page.waitForTimeout(100);
  const sw = await page.evaluate(() => document.querySelector('.tk-switch input').disabled);
  const swWhy = await page.evaluate(() => document.querySelector('#en-why').innerText);
  log(sw === true && /켤 수 없어요/.test(swWhy), '실제 모드: 활성화 스위치 비활성 + 사유 문장', swWhy.slice(0, 40));
  const rows = await page.evaluate(() => [...document.querySelectorAll('.cw-tool')].length);
  log(rows === 13, '도구 13개 후보 표시', String(rows));
  const unkText = await page.evaluate(() => document.querySelector('.cw-tool').innerText);
  log(/확인 안 됨/.test(unkText) && !/연결됨/.test(unkText), '미검증을 연결됨으로 표시하지 않음');
  await page.click('[data-act="test"]', { force: true }); await page.waitForTimeout(100);
  log(/지금은 쓸 수 없어요/.test(await live()), '비활성 테스트 클릭 시 안내 방송', await live());

  // 7) 샘플 도구: 테스트 중복 클릭 방지
  await page.goto(URL('#tools?s=sample&tool=photo')); await page.waitForTimeout(100);
  const before = await page.evaluate(() => document.querySelectorAll('#cw-testlog > li').length);
  await page.locator('[data-act="test"]').first().dblclick(); await page.waitForTimeout(1600);
  const after = await page.evaluate(() => document.querySelectorAll('#cw-testlog > li').length);
  log(after === before + 1, '연결 테스트 더블클릭해도 기록 1건 추가', before + '→' + after);

  // 8) 필터 유지: URL 보존, 새로고침, 필터 해제
  await page.goto(URL('#library')); await page.waitForTimeout(100);
  await page.click('.tk-chip:has-text("이미지")'); await page.waitForTimeout(150);
  await page.selectOption('[data-filter="role"]', 'approved'); await page.waitForTimeout(150);
  await page.reload(); await page.waitForTimeout(150);
  const filtState = await page.evaluate(() => ({ h: location.hash, role: document.querySelector('[data-filter="role"]').value, pressed: document.querySelector('.tk-chip[aria-pressed="true"]').innerText.trim(), count: document.querySelectorAll('.cw-item').length }));
  log(filtState.role === 'approved' && filtState.pressed === '이미지', '필터가 새로고침 후에도 유지', JSON.stringify(filtState));
  await page.selectOption('[data-filter="role"]', 'source'); await page.waitForTimeout(150);
  log(await page.evaluate(() => !!document.querySelector('[data-reset-filters]')), '필터 결과 0건 → "필터 모두 풀기" 제공');
  await page.click('[data-reset-filters]'); await page.waitForTimeout(150);
  log((await page.evaluate(() => document.querySelectorAll('.cw-item').length)) === 8, '필터 모두 풀기 → 8개 복원');

  // 9) 같은 파일명·긴 파일명
  const same = await page.evaluate(() => [...document.querySelectorAll('.cw-item__name')].filter((e) => e.textContent === 'thumbnail.png').length);
  log(same === 2, '같은 파일명 2건이 목록에 분리 표시(작업명·버전·해시로 구분)', String(same));

  // 10) 프로젝트 전환: 빈 프로젝트
  await page.goto(URL('#library')); await page.waitForTimeout(100);
  await page.selectOption('[data-ctx="p"]', 'p2'); await page.waitForTimeout(150);
  log(await page.evaluate(() => /아직 결과물이 없어요/.test(document.querySelector('#cw-panel').innerText)), '빈 프로젝트: 결과물 빈 상태');
  await page.click('[data-go="history"]'); await page.waitForTimeout(150);
  log((await hash()).includes('p=p2') && (await page.evaluate(() => /실행 기록이 없어요/.test(document.querySelector('#cw-panel').innerText))), '화면을 옮겨도 프로젝트 문맥 유지(p=p2)', await hash());

  // 11) 이력 → 라이브러리 교차 링크, 작업실 → 이력 링크
  await page.goto(URL('#history?run=r1')); await page.waitForTimeout(100);
  await page.click('.cw-detail a.tk-link'); await page.waitForTimeout(150);
  log((await hash()).startsWith('#library') && (await hash()).includes('item=l1'), '이력 결과 링크 → 결과물 상세', await hash());

  // 12) 이력 문구: 실패/부분/산출물없음/stderr 는 완료로 표시되지 않음
  await page.goto(URL('#history')); await page.waitForTimeout(100);
  const rowsTxt = await page.evaluate(() => [...document.querySelectorAll('.cw-run')].map((r) => r.innerText.replace(/\n/g, ' ')));
  const bad = rowsTxt.filter((t) => /경고만|0개|5개 중 3개|메모리|제한 시간|도구가 다른/.test(t) && /완료/.test(t.split('시도')[0]));
  log(bad.length === 0, '문제 있는 실행이 "완료" 배지로 보이지 않음', bad.join(' | '));
  const cnt = await page.evaluate(() => document.querySelector('.cw-split__list [role=status]').innerText);
  log(/문제 있음 7/.test(cnt), '목록 요약: 문제 있음 7건(완료로 세지 않음)', cnt.slice(0, 50));

  // 13) 로그 마스킹
  await page.goto(URL('#history?run=r10')); await page.waitForTimeout(100);
  const logTxt = await page.evaluate(() => document.querySelector('.tk-log').innerText);
  log(/••••/.test(logTxt) && !/sk-|Bearer [A-Za-z0-9]/.test(logTxt) && !/\/Users\/winterholic/.test(logTxt), '로그 민감값·경로 마스킹 표시');

  await ctx.close();

  /* 모바일 흐름 */
  const mctx = await browser.newContext({ viewport: VIEWS.m, hasTouch: true });
  const mp = await mctx.newPage();
  mp.on('pageerror', (e) => report.errors.push('mflow pageerror: ' + e.message));
  await mp.goto(URL('#library')); await mp.waitForTimeout(100);
  await mp.click('[data-fk="lib-l1"]'); await mp.waitForTimeout(250);
  log(await mp.evaluate(() => !!document.querySelector('dialog.cw-full[open]')), '모바일: 결과물 선택 → 전체 화면 상세');
  const mh = await mp.evaluate(() => location.hash);
  await mp.click('[data-back]'); await mp.waitForTimeout(250);
  log(await mp.evaluate(() => !document.querySelector('dialog.cw-full[open]')) && !(await mp.evaluate(() => location.hash)).includes('item='), '모바일: 뒤로 → 목록 복귀(해시 ' + mh + ' → ' + (await mp.evaluate(() => location.hash)) + ')');
  await mp.goto(URL('#library?item=l8')); await mp.reload(); await mp.waitForTimeout(250);
  log(await mp.evaluate(() => !!document.querySelector('dialog.cw-full[open]')), '모바일: 새로고침해도 상세 복원(긴 파일명)');
  const widths = await mp.evaluate(() => { const d = document.querySelector('dialog.cw-full'); return [d.scrollWidth, d.clientWidth, window.innerWidth]; });
  log(widths[0] <= widths[1] + 1, '모바일 긴 파일명 상세 가로 넘침 없음', widths.join('/'));
  // 모바일 터치 타깃 크기(주요 버튼 44px)
  await mp.goto(URL('#work?s=compare')); await mp.waitForTimeout(150);
  const small = await mp.evaluate(() => [...document.querySelectorAll('#cw-screen button, #cw-screen a, #cw-screen select, #cw-screen input:not([type=range]):not([type=radio]), #cw-screen textarea')].filter((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && (r.height < 32); }).map((e) => (e.getAttribute('aria-label') || e.innerText || e.tagName).trim().slice(0, 20) + ':' + Math.round(e.getBoundingClientRect().height)));
  log(small.length === 0, '모바일 작업실 상호작용 요소 높이 32px 이상(권장 44)', small.join(', '));
  await mctx.close();
}
await flows();
report.errors = [...new Set(report.errors)];
fs.writeFileSync(path.join(shots, 'report.json'), JSON.stringify(report, null, 1));
console.log('\nshots:', report.shots, '| overflow findings:', report.overflow.length, '| a11y findings:', report.a11y.length, '| console/page errors:', report.errors.length);
report.a11y.slice(0, 10).forEach((o) => console.log(' A11Y', o.view, o.theme, o.name, JSON.stringify(o.a11y.slice(0, 3))));
report.overflow.slice(0, 12).forEach((o) => console.log(' OVERFLOW', o.view, o.theme, o.name, JSON.stringify(o.ov.slice(0, 3))));
report.errors.slice(0, 8).forEach((e) => console.log(' ERR', e));
const fails = report.checks.filter((c) => !c.ok).length;
console.log('checks:', report.checks.length, 'fail:', fails);
await browser.close();
