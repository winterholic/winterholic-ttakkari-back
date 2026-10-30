// 피드백 원장: 사용자 피드백을 '장면·원문·기대 결과·재현 입력·완료 증거' 한 줄로 묶는다.
// 목적: 같은 피드백이 '테스트 통과' 한 줄로 닫히지 않게, 범주마다 필요한 증거를 강제하고 증거 파일이 바뀌면 알아챈다.
import fs from 'node:fs';
import path from 'node:path';
import { analyzePng, imageKind } from './png.mjs';
import { now, readJson, sha256File, writeJson } from './util.mjs';

export const LEDGER_PATH = 'docs/studio/ledger.json';
export const CATEGORIES = ['bug', 'logic', 'balance', 'art', 'ux', 'narrative', 'feel', 'audio'];
export const EVIDENCE_TYPES = ['play-replay', 'repro-before', 'reach-check', 'machine-test', 'canon-check', 'screenshot', 'user-confirmed', 'not-run'];

/** 범주별 기계 증거 요건(any-of). 주관 범주는 기계 증거가 다 있어도 사용자 확인이 있어야 COMPLETE 다. */
export const POLICY = {
  bug: { machine: ['play-replay', 'reach-check'], subjective: false, why: '진행불가·입력·충돌 버그는 실제로 실행한 증거가 필요하다. 단위 테스트 통과는 실제 플레이 가능을 뜻하지 않는다.' },
  logic: { machine: ['machine-test'], subjective: false, why: '데이터·저장·계산 로직은 테스트 증거로 닫을 수 있다.' },
  balance: { machine: ['machine-test', 'play-replay'], subjective: true, why: '난이도는 봇 통과만으로 재미를 보증하지 못한다.' },
  art: { machine: ['screenshot'], subjective: true, why: '그림·UI 는 실제 렌더 화면 캡처를 보고 판단해야 하고, 취향은 사용자만 확인한다.' },
  ux: { machine: ['screenshot', 'play-replay'], subjective: true, why: '화면·조작 흐름은 렌더 화면이나 재생한 플레이가 필요하다.' },
  narrative: { machine: ['canon-check'], subjective: true, why: '정전 위반은 기계가 잡지만 서사의 재미는 사용자만 판단한다.' },
  feel: { machine: ['play-replay', 'screenshot'], subjective: true, why: '타격감·연출은 실행 증거가 필요하고 느낌은 사용자만 확인한다.' },
  audio: { machine: ['play-replay'], subjective: true, why: '소리는 헤드리스에서 들을 수 없다. 재생 호출 확인까지만 기계가 하고 청취는 사용자가 한다.' },
};

const KEYWORDS = [
  ['narrative', /세계관|스토리|시나리오|서사|대사|정전|설정|배경이 어색|엔딩/],
  ['audio', /BGM|소리|사운드|음악|효과음/i],
  ['art', /그림|아트|이미지|아이콘|스프라이트|배경 ?화면|초상|CG|색감|디자인|UI 디테일/],
  ['ux', /UI|창|화면|버튼|조작 설명|결과 화면|React|잔해|표시|메뉴|수첩/],
  ['balance', /난이도|밸런스|너프|쿨타임|강해|약해|어렵|쉽/],
  ['feel', /타격|연출|이펙트|손맛|맛이|재미없|도파민|애니메이션|움직|걷기|발 /],
  ['bug', /안 ?됨|못 ?함|불가|막힘|막히|버그|오류|안 나오|멈춤|렉|softlock|끼임|빠져나|진행/i],
];

export function guessCategory(text) {
  for (const [cat, re] of KEYWORDS) if (re.test(text)) return cat;
  return 'bug';
}

export function loadLedger(root) {
  const file = path.join(root, LEDGER_PATH);
  const data = readJson(file, { version: 1, items: [] });
  if (!Array.isArray(data.items)) throw new Error('원장 형식 오류: items 배열이 없다');
  return { file, data };
}

export function saveLedger(l) {
  writeJson(l.file, l.data);
}

function nextId(items) {
  const max = items.reduce((m, i) => Math.max(m, Number(String(i.id).replace(/\D/g, '')) || 0), 0);
  return 'F' + String(max + 1).padStart(3, '0');
}

/** 마크다운 피드백 문서의 '## 장면' 제목과 '- 항목' 줄을 원장 항목으로 옮긴다. 같은 원문은 다시 넣지 않는다. */
export function importMarkdown(root, mdFile) {
  const l = loadLedger(root);
  const text = fs.readFileSync(mdFile, 'utf8');
  let scene = '공통';
  const added = [];
  const seen = new Set(l.data.items.map((i) => i.quote));
  for (const raw of text.split('\n')) {
    const h = /^#{2,3}\s+(.*)$/.exec(raw);
    if (h) {
      scene = h[1].trim();
      continue;
    }
    const b = /^\s*[-*]\s+(.*\S)\s*$/.exec(raw);
    if (!b || seen.has(b[1])) continue;
    const quote = b[1];
    seen.add(quote);
    const item = {
      id: nextId(l.data.items),
      scene,
      quote,
      expectation: '',
      category: guessCategory(scene + ' ' + quote),
      category_guess: true,
      status: 'open',
      repro: null,
      evidence: [],
      created: now(),
    };
    l.data.items.push(item);
    added.push(item);
  }
  saveLedger(l);
  return added;
}

export function addItem(root, { scene = '공통', quote, expectation = '', category }) {
  if (!quote) throw new Error('--quote(사용자 원문)이 필요하다');
  const l = loadLedger(root);
  if (category && !CATEGORIES.includes(category)) throw new Error(`범주는 ${CATEGORIES.join('|')} 중 하나`);
  const item = {
    id: nextId(l.data.items),
    scene,
    quote,
    expectation,
    category: category ?? guessCategory(scene + ' ' + quote),
    category_guess: !category,
    status: 'open',
    repro: null,
    evidence: [],
    created: now(),
  };
  l.data.items.push(item);
  saveLedger(l);
  return item;
}

export function getItem(l, id) {
  const item = l.data.items.find((i) => i.id === id);
  if (!item) throw new Error(`원장에 ${id} 가 없다`);
  return item;
}

/** 증거 하나를 검증하고 붙인다. 파일은 sha256 으로 고정해 나중에 바뀌면 gate 가 잡는다. */
export function addEvidence(root, id, { type, ref, note = '', source = '', quote = '', reason = '' }) {
  if (!EVIDENCE_TYPES.includes(type)) throw new Error(`증거 type 은 ${EVIDENCE_TYPES.join('|')} 중 하나`);
  const l = loadLedger(root);
  const item = getItem(l, id);
  const ev = { type, note, at: now() };
  if (type === 'user-confirmed') {
    if (!quote || !source) throw new Error('user-confirmed 는 --quote(사용자 발화 원문)와 --source(세션·메시지 위치)가 필요하다');
    Object.assign(ev, { quote, source });
  } else if (type === 'not-run') {
    if (!reason) throw new Error('not-run 은 --reason(실행하지 못한 이유)이 필요하다');
    ev.reason = reason;
  } else {
    if (!ref) throw new Error('--ref(증거 파일 경로)가 필요하다');
    const file = path.resolve(root, ref);
    if (!fs.existsSync(file) || !fs.statSync(file).isFile()) throw new Error(`증거 파일이 없다: ${ref}`);
    ev.ref = path.relative(root, file).split(path.sep).join('/');
    ev.sha256 = sha256File(file);
    if (type === 'screenshot') {
      const buf = fs.readFileSync(file);
      if (!imageKind(buf)) throw new Error('screenshot 증거는 PNG/JPEG/GIF/WebP 이미지여야 한다');
      const st = analyzePng(buf);
      if (st?.blank) throw new Error('screenshot 이 빈 화면(단색·투명)이다. 실제로 그려진 화면을 캡처한다');
      ev.pixels = st;
    } else if (['play-replay', 'repro-before', 'reach-check', 'canon-check'].includes(type)) {
      const r = readJson(file);
      if (typeof r.ok !== 'boolean' || !r.tool) throw new Error(`${type} 증거는 studio 도구가 낸 결과 JSON(tool·ok 필드)이어야 한다`);
      if (type === 'repro-before' ? r.ok : !r.ok) throw new Error(type === 'repro-before' ? 'repro-before 는 고치기 전에 실패해야 한다(ok=false). 재현이 실패를 못 잡으면 재현 스크립트가 틀렸다' : `${type} 결과가 ok=false 다. 통과한 결과만 증거로 붙인다`);
      ev.tool = r.tool;
    } else if (type === 'machine-test') {
      ev.exit = note.match(/exit[= ]?(\d+)/)?.[1] ?? null;
    }
  }
  item.evidence.push(ev);
  saveLedger(l);
  return ev;
}

export function setStatus(root, id, status, reason = '') {
  if (!['open', 'fixed', 'wontfix'].includes(status)) throw new Error('status 는 open|fixed|wontfix');
  if (status === 'wontfix' && !reason) throw new Error('wontfix 는 --reason 이 필요하다(사용자 결정 인용)');
  const l = loadLedger(root);
  const item = getItem(l, id);
  item.status = status;
  if (reason) item.status_reason = reason;
  saveLedger(l);
  return item;
}

/** 항목 하나가 요건을 채웠는지. 증거 파일은 지금도 같은 해시인지 다시 확인한다. */
export function evaluateItem(root, item) {
  const problems = [];
  const pol = POLICY[item.category];
  if (!pol) return { id: item.id, state: 'fail', problems: [`알 수 없는 범주 ${item.category}`], unverified: [] };
  const live = [];
  for (const ev of item.evidence) {
    if (ev.ref) {
      const f = path.resolve(root, ev.ref);
      if (!fs.existsSync(f)) {
        problems.push(`증거 파일이 사라졌다: ${ev.ref}`);
        continue;
      }
      if (sha256File(f) !== ev.sha256) {
        problems.push(`증거 파일이 기록 뒤에 바뀌었다(해시 불일치): ${ev.ref}. 다시 실행해 새 증거를 붙인다`);
        continue;
      }
    }
    live.push(ev);
  }
  const types = new Set(live.map((e) => e.type));
  const unverified = [];
  if (item.status === 'wontfix') return { id: item.id, state: 'wontfix', problems, unverified };
  if (item.status !== 'fixed') {
    problems.push('아직 열려 있다(status=open)');
    return { id: item.id, state: 'open', problems, unverified };
  }
  const machineOk = pol.machine.some((t) => types.has(t));
  const notRun = live.filter((e) => e.type === 'not-run');
  if (!machineOk) {
    if (notRun.length) unverified.push(`실행하지 못함: ${notRun.map((n) => n.reason).join(' / ')}`);
    else problems.push(`${item.category} 범주는 증거 ${pol.machine.join(' 또는 ')} 가 필요하다. ${pol.why}`);
  }
  if (types.has('play-replay') && !types.has('repro-before')) unverified.push('고치기 전 재현 실패(repro-before)가 없어 재현 스크립트가 버그를 실제로 잡는지는 증명되지 않았다');
  if (item.category === 'narrative' && !types.has('canon-check')) problems.push('narrative 는 canon-check 증거도 필요하다');
  if (pol.subjective && !types.has('user-confirmed')) unverified.push('사용자 확인 없음: ' + pol.why);
  let state = 'verified';
  if (problems.length) state = 'fail';
  else if (unverified.length) state = 'needs-human';
  return { id: item.id, state, problems, unverified };
}
