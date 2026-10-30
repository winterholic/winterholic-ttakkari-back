// 제작 완료 계약. '테스트 통과'가 아니라 원장·정전·에셋·린트·실행 증거를 합쳐 세 단계로만 말한다.
//   FAIL         막는 문제가 있다(요건 증거 부족·정전 위반·에셋 오류·열린 항목·증거 변조).
//   NEEDS_HUMAN  기계가 확인할 수 있는 것은 다 확인됐다. 재미·시각 품질·서사·청감은 사용자 확인이 남았다.
//   COMPLETE     모든 항목이 요건을 채웠고 주관 항목에 사용자 확인 증거가 있다.
// 어떤 경우에도 '완성·해결' 이라는 말은 COMPLETE 일 때만 쓴다.
import { auditAssets } from './assets.mjs';
import { checkCanon } from './canon.mjs';
import { evaluateItem, loadLedger } from './ledger.mjs';
import { lintProject } from './lint.mjs';

export function runGate(root, opts = {}) {
  const blockers = [];
  const pending = [];
  const notes = [];
  const l = loadLedger(root);
  const items = l.data.items;
  const evals = items.map((i) => evaluateItem(root, i));
  for (const e of evals) {
    for (const p of e.problems) blockers.push(`${e.id}: ${p}`);
    for (const u of e.unverified) pending.push(`${e.id}: ${u}`);
  }
  if (!items.length) notes.push('피드백 원장이 비었다: 요구사항이 항목으로 추적되지 않았다. 신규 개발이 아니라면 studio ledger import/add 로 먼저 채운다. 이 상태로는 COMPLETE 가 나올 수 없다');
  for (const i of items) if (i.category_guess) notes.push(`${i.id}: 범주(${i.category})가 키워드 추정이다. 맞는지 확인하고 ledger add 로 바로잡는다`);

  const canon = checkCanon(root);
  if (canon.status === 'unconfigured') notes.push('정전 파일이 없다: 세계관 위반을 기계로 검사하지 못했다. 사용자 정전이 있으면 docs/studio/canon.json 에 옮긴다');
  else if (!canon.ok) {
    for (const v of canon.violations.slice(0, 20)) blockers.push(`정전 위반 ${v.rule} ${v.file}:${v.line} "${v.match}" — ${v.why}`);
    for (const m of canon.missing) blockers.push(`정전 필수 요소 부족 ${m.rule}: ${m.found}/${m.min} — ${m.why}`);
  }
  const assets = auditAssets(root, { manifests: opts.manifests ?? [] });
  for (const i of assets.issues.filter((x) => x.level === 'error')) blockers.push(`에셋 ${i.code} ${i.file}: ${i.msg}`);
  const warnA = assets.issues.filter((x) => x.level === 'warn').length;
  if (warnA) notes.push(`에셋 경고 ${warnA}건(asset audit --sheet 로 contact sheet 를 보고 판단)`);
  const lint = lintProject(root);
  for (const f of lint.findings.filter((x) => x.level === 'error')) blockers.push(`린트 ${f.rule} ${f.file}:${f.line}: ${f.msg}`);
  const warnL = lint.findings.filter((x) => x.level === 'warn').length;
  if (warnL) notes.push(`린트 경고 ${warnL}건(studio lint 로 목록 확인 — 단서이지 결함 판정이 아님)`);

  const verdict = blockers.length ? 'FAIL' : pending.length || !items.length ? 'NEEDS_HUMAN' : 'COMPLETE';
  return {
    tool: 'studio-gate',
    ok: verdict !== 'FAIL',
    verdict,
    counts: { items: items.length, verified: evals.filter((e) => e.state === 'verified').length, needsHuman: evals.filter((e) => e.state === 'needs-human').length, open: evals.filter((e) => e.state === 'open').length, fail: evals.filter((e) => e.state === 'fail').length },
    blockers,
    pending,
    notes,
    wording: verdict === 'COMPLETE' ? '완료라고 말해도 된다. 증거 위치를 함께 적는다.' : verdict === 'NEEDS_HUMAN' ? "'기계 검증 통과, 재미·시각·서사는 사용자 확인 대기'라고만 말한다. '완성·해결'이라고 쓰지 않는다." : "'아직 아니다'라고 말하고 blockers 를 먼저 보고한다.",
    items: evals,
  };
}

export function gateText(g) {
  const lines = [`판정: ${g.verdict}  (항목 ${g.counts.items} · 검증 ${g.counts.verified} · 사용자확인대기 ${g.counts.needsHuman} · 열림 ${g.counts.open} · 실패 ${g.counts.fail})`];
  if (g.blockers.length) lines.push('\n[막는 문제]', ...g.blockers.map((b) => ' - ' + b));
  if (g.pending.length) lines.push('\n[검증되지 않은 것]', ...g.pending.map((b) => ' - ' + b));
  if (g.notes.length) lines.push('\n[참고]', ...g.notes.map((b) => ' - ' + b));
  lines.push('\n보고 문구: ' + g.wording);
  return lines.join('\n');
}
