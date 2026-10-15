/* Creative Workspace 시안 렌더러. 정적 HTML + 바닐라 JS. 모든 화면 상태는 해시로 복원된다(#work?s=compare&p=p1&t=t1).
   표시되는 도구·파일·실행은 전부 가상 예시다. 실제 서버·도구와 통신하지 않는다. */
(function () {
  const D = window.CWD;
  const SCREENS = ['work', 'library', 'history', 'tools'];
  const TAB_LABEL = { work: '작업실', library: '결과물', history: '실행 이력', tools: '도구' };
  const SCENARIOS = {
    work: [['compare', 'v1↔v2 비교(기본)'], ['new', '새 요청(빈 작업)'], ['notools', '도구 없음(지금 실제)'], ['attach', '첨부 부분 실패'], ['running', '긴 렌더 진행'], ['cancelled', '취소 뒤'], ['v1', 'v1만 있음'], ['approved', '승인 완료'], ['error', '도구 오류'], ['lost', '연결 끊김'], ['unsupported', '미리보기 불가']],
    library: [['list', '목록(기본)'], ['loading', '로딩'], ['error', '불러오기 오류'], ['emptyfilter', '필터 결과 0']],
    history: [['list', '목록(기본)'], ['loading', '로딩'], ['error', '불러오기 오류'], ['empty', '이력 없음']],
    tools: [['real', '실제 지금: 13개 전부 미검증'], ['sample', '상태 예시(가상)'], ['loading', '로딩'], ['error', '레지스트리 오류'], ['offline', 'Mac Studio 끊김']],
  };
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const ic = (n, c = '') => `<svg class="tk-icon${c ? ' ' + c : ''}" aria-hidden="true"><use href="#i-${n}"/></svg>`;

  /* ---------- 라우팅 ---------- */
  function parse() {
    const h = location.hash.slice(1) || 'work';
    const [scr, q = ''] = h.split('?');
    return { scr: SCREENS.includes(scr) ? scr : 'work', p: Object.fromEntries(new URLSearchParams(q)) };
  }
  function href(patch, scr, keep) {
    const cur = parse();
    const base = scr && scr !== cur.scr ? Object.fromEntries(Object.entries(cur.p).filter(([k]) => ['p', 't', 'theme'].includes(k))) : cur.p;
    const p = { ...(keep === false ? {} : base), ...patch };
    Object.keys(p).forEach((k) => (p[k] == null || p[k] === '') && delete p[k]);
    const qs = new URLSearchParams(p).toString();
    return '#' + (scr || cur.scr) + (qs ? '?' + qs : '');
  }
  function go(patch, scr) { const h = href(patch, scr); if (h === location.hash) render(); else location.hash = h; }
  const setQ = (qs) => Object.fromEntries(new URLSearchParams(qs));
  function announce(msg) { const l = $('#cw-live'); l.textContent = ''; setTimeout(() => (l.textContent = msg), 30); }

  /* ---------- 공통 조각 ---------- */
  const badge = (tone, text, icon) => `<span class="tk-badge${tone ? ' tk-badge--' + tone : ''}">${icon ? ic(icon) : ''}${esc(text)}</span>`;
  const runBadge = (state, text) => `<span class="tk-badge" data-state="${state}">${state === 'running' ? '<span class="tk-dot tk-dot--live" aria-hidden="true"></span>' : ''}${esc(text)}</span>`;
  const sampleTag = (t = '가상 예시') => `<span class="cw-sample-tag">${ic('dashed')}${esc(t)}</span>`;
  const KIND = { image: ['image', 'image'], video: ['slide', 'video'], doc: ['pdf', 'file-pdf'], audio: ['doc', 'audio'], cad: ['code', 'cad'], sheet: ['sheet', 'sheet'] };
  const KIND_LABEL = { image: '이미지', video: '영상', doc: '문서', audio: '오디오', cad: 'CAD·벡터', sheet: '표' };
  const glyph = (k, sm) => `<span class="tk-glyph${sm ? ' tk-glyph--sm' : ''}" data-type="${KIND[k][0]}">${ic(KIND[k][1])}</span>`;
  const ROLE = { source: ['source', '원본', 'folder'], draft: ['draft', '초안', 'pencil'], approved: ['approved', '최종 승인', 'check-check'] };
  const role = (r) => `<span class="cw-role" data-role="${ROLE[r][0]}">${ic(ROLE[r][2])}${ROLE[r][1]}</span>`;
  const axis = (a) => `<li class="cw-axis" data-tone="${a[2]}">${ic(a[3])}<span class="cw-axis__k">${a[0]}</span>${esc(a[1])}</li>`;
  const empty = (icon, title, body, actions = '') => `<div class="tk-empty">${ic(icon)}<p class="tk-empty__title">${title}</p>${body ? `<p>${body}</p>` : ''}${actions ? `<div class="tk-button-group">${actions}</div>` : ''}</div>`;
  const skeletonRows = (n = 5) => `<div class="tk-list" aria-busy="true"><p class="tk-status tk-status--agent cw-pad" role="status"><span class="tk-spinner tk-spinner--xs" aria-hidden="true"></span>불러오는 중</p>${Array.from({ length: n }, () => `<div class="tk-list__item" aria-hidden="true"><span class="tk-skeleton tk-skeleton--glyph"></span><span class="tk-list__body"><span class="tk-skeleton tk-skeleton--text"></span><span class="tk-skeleton tk-skeleton--text"></span></span></div>`).join('')}</div>`;
  const errorBox = (what, code, actions) => `<div class="tk-empty tk-empty--boxed cw-pad" role="alert">${ic('warning')}<p class="tk-empty__title">${what}을(를) 불러오지 못했어요</p><p>Mac Studio가 10초 동안 응답하지 않았어요. 받아 둔 내용은 그대로예요. <code>${code}</code></p><div class="tk-button-group">${actions}</div></div>`;
  const thumb = (v) => `<div class="cw-thumb" data-v="${v}" role="img" aria-label="썸네일 v${v}: ${v === 1 ? '밝은 배경, 작은 제목' : '어두운 배경, 큰 제목'}"><span class="cw-thumb__bar"></span><p class="cw-thumb__kicker">EP.42 · 신규 시리즈</p><p class="cw-thumb__title">${v === 1 ? '인트로 편집, 10분 컷' : '인트로 편집, 10분 컷'}</p></div>`;

  /* ---------- 문맥 ---------- */
  function ctx(p, scr) {
    const proj = D.PROJECTS.find((x) => x.id === p.p) || D.PROJECTS[0];
    const tasks = proj.tasks.map((id) => D.TASKS[id]);
    let task = tasks.find((x) => x.id === p.t);
    if (!task && scr === 'work') task = tasks[0] || null;
    return { proj, tasks, task, none: tasks.length === 0 };
  }
  function contextBar(c, scr) {
    const optsP = D.PROJECTS.map((x) => `<option value="${x.id}"${x.id === c.proj.id ? ' selected' : ''}>${esc(x.name)}</option>`).join('');
    const allOpt = scr === 'work' ? '' : `<option value=""${!c.task ? ' selected' : ''}>모든 작업</option>`;
    const optsT = c.tasks.map((x) => `<option value="${x.id}"${c.task && x.id === c.task.id ? ' selected' : ''}>${esc(x.name)}</option>`).join('');
    const taskSel = scr === 'tools'
      ? `<span class="cw-small cw-muted cw-trunc">도구는 모든 프로젝트가 같이 써요</span>`
      : `<span class="tk-select"><select aria-label="작업" data-ctx="t"${c.none ? ' disabled' : ''}>${c.none ? '<option>작업 없음</option>' : allOpt + optsT}</select></span>`;
    return `<div class="cw-context" role="group" aria-label="프로젝트와 작업"><span class="tk-select"><select aria-label="프로젝트" data-ctx="p">${optsP}</select></span><span class="cw-context__sep">${ic('chevron-right')}</span>${taskSel}<a class="tk-link cw-context__link tk-hide-below-md" href="#" data-mock="캔버스">작업 캔버스에서 보기</a></div>`;
  }

  /* ---------- 셸 ---------- */
  function shell(scr, c, inner, sampleWorld) {
    const tabs = SCREENS.map((s) => `<button class="tk-tab" role="tab" id="tab-${s}" aria-controls="cw-panel" aria-selected="${s === scr}" tabindex="${s === scr ? 0 : -1}" data-go="${s}" type="button">${TAB_LABEL[s]}</button>`).join('');
    return `
      <header class="tk-header"><div class="tk-header__inner">
        <div class="tk-header__title"><h1>크리에이티브</h1><p class="tk-header__meta">${TAB_LABEL[scr]} · 검토용 시안</p></div>
        <div class="tk-header__actions">
          <button class="tk-icon-button" type="button" data-dlg="review" aria-label="시안 보기 설정(상태·테마)">${ic('settings')}</button>
          <button class="tk-icon-button" type="button" data-act="theme" aria-label="다크 모드 전환">${ic('moon')}</button>
        </div></div></header>
      <p class="tk-banner tk-banner--neutral" role="note">${ic('info')}<span class="tk-banner__text">검토용 시안 · 도구·파일·실행은 모두 <strong>가상 예시</strong>예요</span></p>
      <div class="cw-tabs"><div class="tk-tablist" role="tablist" aria-label="크리에이티브 화면">${tabs}</div></div>
      ${contextBar(c, scr)}
      <div class="cw-body" role="tabpanel" id="cw-panel" aria-labelledby="tab-${scr}" tabindex="-1">${inner}</div>`;
  }

  /* ---------- 실행 카드 ---------- */
  function stepLi(s) {
    const [st, name, target, time, detail] = s;
    const mark = st === 'running' ? '<span class="tk-spinner tk-spinner--sm" aria-hidden="true"></span>' : st === 'queued' ? '<span class="tk-step__mark" aria-hidden="true"></span>' : ic(st === 'succeeded' ? 'circle-check' : st === 'failed' ? 'circle-x' : st === 'cancelled' ? 'ban' : 'hand');
    return `<li data-state="${st}">${mark}<span class="tk-step__name">${esc(name)}${target ? ` <span class="tk-step__target">${esc(target)}</span>` : ''}${detail ? `<span class="tk-step__detail">${esc(detail)}</span>` : ''}</span><span class="tk-step__time">${esc(time || '')}</span></li>`;
  }
  function runCard(o) {
    const { state, label, title, meta = [], progress, steps = [], summary = '', actions = '', open = true, badgeHtml } = o;
    const prog = progress != null ? `<div class="tk-progress tk-progress--agent" style="--tk-progress:${progress / 100}" role="progressbar" aria-label="진행" aria-valuenow="${progress}" aria-valuemin="0" aria-valuemax="100"></div>` : '';
    const stepsHtml = `<ol class="tk-steps">${steps.map(stepLi).join('')}</ol>`;
    const body = open ? stepsHtml : `<details class="tk-run__more"><summary>단계 ${steps.length}개</summary>${stepsHtml}</details>`;
    return `<div class="tk-run" data-state="${state}"><div class="tk-run__head"><p class="tk-run__title">${esc(title)}</p>${badgeHtml || runBadge(state, label)}</div>${meta.length ? `<p class="tk-run__meta">${meta.map((m) => `<span>${esc(m)}</span>`).join('')}</p>` : ''}${prog}${summary ? `<p class="tk-run__summary">${summary}</p>` : ''}${body}${actions ? `<div class="tk-run__actions">${actions}</div>` : ''}</div>`;
  }
  const bubble = (text, files = '') => `<div class="cw-bubble"><p>${esc(text)}</p>${files ? `<ul class="cw-files" style="margin-top:var(--tk-space-2)">${files}</ul>` : ''}</div>`;
  const fileChip = (name, state = 'ok', extra = '') => `<li class="cw-file" data-state="${state}">${ic(state === 'failed' ? 'circle-x' : state === 'uploading' ? 'upload' : state === 'unsupported' ? 'ban' : 'image')}<span class="cw-file__name">${esc(name)}</span>${extra ? `<span class="cw-muted">${esc(extra)}</span>` : ''}</li>`;

  /* ---------- 작업실 ---------- */
  function screenWork(p, c) {
    if (c.none) {
      return shell('work', c, `<div class="cw-pad">${empty('layers', '아직 크리에이티브 작업이 없어요', '이 프로젝트에서 이미지·영상·문서 작업을 말로 요청하면 여기에 작업이 생겨요. 도구는 요청에 맞는 것만 자동으로 골라요.', `<button class="tk-button tk-button--primary" data-set="s=new" data-go="work" data-t="t1">첫 작업 시작하기</button><button class="tk-button" data-go="tools">도구 상태 보기</button>`)}</div>`);
    }
    const T = c.task;
    const s = p.s || (T.id === 't2' ? 'running' : T.id === 't3' ? 'unsupported' : 'compare');
    const view = p.view || 'side';
    const showV2 = ['compare', 'approved', 'lost'].includes(s);
    const cur = Math.min(Number(p.v || (showV2 ? 2 : 1)), showV2 ? 2 : 1);
    const toolName = T.id === 't2' ? 'FilmCraft 2.3.0' : T.id === 't3' ? '연결된 도구 없음' : 'PhotoCraft 0.9.2';

    /* 상태 배지 */
    const status = s === 'approved' && cur === 2 ? role('approved') : s === 'unsupported' ? role('source') : s === 'new' || s === 'notools' ? '' : role('draft');
    const toolLine = s === 'notools' || T.id === 't3'
      ? `<p class="cw-small cw-muted cw-row">${ic('wrench')}지금 이 요청을 실행할 수 있는 도구가 없어요 · <a class="tk-link" href="${href({}, 'tools', false).replace('#tools', '#tools?s=real')}">도구 상태 보기</a></p>`
      : `<p class="cw-small cw-muted cw-row">${ic('wrench')}${toolName} <span class="cw-muted">· 요청에 맞춰 자동 선택</span> <button class="tk-link" type="button" data-dlg="whytool">왜 이 도구예요?</button> ${sampleTag()}</p>`;
    const head = `<div class="cw-stagehead"><div class="cw-stagehead__title"><h2>${esc(T.name)}</h2>${toolLine}</div><div class="cw-row">${status}</div></div>`;

    /* 버전 줄 */
    const pending = ['running', 'cancelled', 'error'].includes(s);
    const verBtn = (n, tag) => `<button class="cw-ver" type="button" aria-pressed="${cur === n && s !== 'compare' || (s === 'compare' && view === 'single' && cur === n)}" data-set="v=${n}&view=single&s=${s === 'compare' ? 'compare' : s}">v${n}${tag ? ` <span class="cw-ver__tag">${tag}</span>` : ''}</button>`;
    let verbar = '';
    if (!['new', 'notools', 'attach'].includes(s) && T.id !== 't3') {
      verbar = `<div class="cw-verbar" role="group" aria-label="버전">` + verBtn(1, s === 'approved' ? '초안' : '') +
        (showV2 ? verBtn(2, s === 'approved' ? '최종 승인' : '승인 대기') : pending ? `<span class="cw-ver" aria-disabled="true" style="cursor:not-allowed;border-style:dashed">v2 <span class="cw-ver__tag">${s === 'running' ? '만드는 중' : s === 'cancelled' ? '취소됨' : '만들지 못함'}</span></span>` : '') +
        (showV2 ? `<span class="cw-small cw-muted" style="margin-inline:var(--tk-space-1)">보기</span><div class="tk-segmented" role="radiogroup" aria-label="비교 방식"><label><input type="radio" name="view" value="side" data-view${s === 'compare' && view === 'side' ? ' checked' : ''}>${ic('columns')}나란히</label><label><input type="radio" name="view" value="overlay" data-view${s === 'compare' && view === 'overlay' ? ' checked' : ''}>겹쳐보기</label></div>` : '') + `</div>`;
    }

    /* 미리보기 */
    let canvas = '';
    if (s === 'new' || s === 'notools') {
      canvas = `<div class="cw-canvas">${empty('layers', s === 'notools' ? '실행할 수 있는 도구가 아직 없어요' : '무엇을 만들까요?', s === 'notools' ? '요청은 적어 둘 수 있지만, 연결·검증된 도구가 생기기 전에는 실행되지 않아요. 도구 탭에서 상태를 확인해 주세요.' : '말로 요청하면 따까리가 알맞은 도구를 골라 실행해요. 파일은 아래 클립으로 붙여요. 명령어는 필요 없어요.', s === 'notools' ? `<button class="tk-button" data-go="tools" data-set="s=real">도구 상태 보기</button>` : `<button class="tk-chip" type="button" data-fill="유튜브 썸네일 만들어줘. 배경은 밝게, 제목은 크게">유튜브 썸네일 만들어줘</button><button class="tk-chip" type="button" data-fill="이 영상 앞 10초만 잘라줘">영상 앞 10초 자르기</button><button class="tk-chip" type="button" data-fill="PDF에서 3쪽만 빼줘">PDF 3쪽 빼기</button>`)}</div>`;
    } else if (s === 'unsupported' || T.id === 't3') {
      canvas = `<div class="cw-pv">${ic('cad')}<p class="tk-empty__title">dwg 미리보기는 지원하지 않아요</p><p class="cw-small cw-muted">미리보기를 못 만들어도 파일은 안전해요. 원본을 받거나 원본 앱에서 열어 확인하세요.</p><div class="cw-actions"><button class="tk-button" type="button" data-mock="원본 다운로드">${ic('download')}원본 다운로드</button><button class="tk-button" type="button" data-dlg="handoff">${ic('external-link')}원본 앱에서 열기</button></div></div>`;
    } else if (s === 'running') {
      canvas = `<div class="cw-canvas"><div class="cw-pv" style="border:0;background:none"><span class="tk-spinner" aria-hidden="true"></span><p class="tk-empty__title">v2를 만드는 중이에요</p><p class="cw-small cw-muted">다 되면 여기에 나타나요. 지금은 v1을 볼 수 있어요.</p><div class="tk-progress tk-progress--agent" style="--tk-progress:0.41" role="progressbar" aria-label="렌더 진행" aria-valuenow="41" aria-valuemin="0" aria-valuemax="100"></div><p class="cw-small cw-muted">41% · 4:12 경과 · 이 화면을 닫아도 Mac Studio에서 계속돼요(확인 필요)</p></div></div>`;
    } else if (s === 'compare' && view === 'side') {
      canvas = `<div class="cw-canvas"><div class="cw-compare"><figure>${thumb(1)}<figcaption><strong>v1</strong>${role('draft')}<span class="cw-muted">12:31</span></figcaption></figure><figure>${thumb(2)}<figcaption><strong>v2</strong>${role('draft')}<span class="cw-muted">12:41 · 승인 대기</span></figcaption></figure></div></div>`;
    } else if (s === 'compare' && view === 'overlay') {
      canvas = `<div class="cw-canvas" style="flex-direction:column;align-items:stretch"><div class="cw-overlay" id="cw-ov">${thumb(1)}<div class="cw-overlay__top">${thumb(2)}</div></div><label class="cw-small cw-muted" for="cw-split">왼쪽 v1 · 오른쪽 v2 — 경계를 좌우로 움직여 보세요</label><input class="cw-overlay__range" id="cw-split" type="range" min="0" max="100" value="50" aria-label="v1과 v2 경계 위치"></div>`;
    } else {
      const v = s === 'approved' ? cur : cur === 2 && showV2 ? 2 : 1;
      canvas = `<div class="cw-canvas">${thumb(v)}</div>`;
    }

    /* 변경 요약 */
    const diff = s === 'compare' || s === 'approved' || s === 'lost'
      ? `<ul class="cw-diff" aria-label="v1에서 v2로 바뀐 것"><li><span class="cw-diff__k">배경</span>밝은 종이색 → 어두운 잉크색</li><li><span class="cw-diff__k">제목</span>크기 +57%, 글자색 반전</li><li><span class="cw-diff__k">그대로</span>구도 · 로고 위치 · 크기 1280×720</li></ul>` : '';

    /* 승인/행동 영역 */
    let act = '';
    const lostDis = s === 'lost' ? ' disabled' : '';
    if (s === 'compare' || s === 'lost') {
      act = `<section class="cw-approve" aria-labelledby="ap-t"><p id="ap-t"><strong>승인 대상: v2 · thumbnail.png</strong></p><p class="cw-small">12:41 생성 · PhotoCraft 0.9.2 · 438 KB · 승인하면 보관함에 '최종 승인'으로 저장돼요. v1은 초안으로 남아요.</p>${s === 'lost' ? `<p class="cw-small">${ic('wifi-off')} 연결이 끊겨 승인할 수 없어요. 다시 연결되면 열려요.</p>` : ''}<div class="cw-actions"><button class="tk-button tk-button--primary" type="button" data-dlg="approve"${lostDis}>${ic('check-check')}v2 승인하고 저장…</button><button class="tk-button" type="button" data-set="s=v1&v=1"${lostDis}>${ic('undo')}v1부터 다시 수정</button></div></section>`;
    } else if (s === 'v1') {
      act = `<section class="cw-approve" aria-labelledby="ap-t"><p id="ap-t"><strong>v1 · 초안</strong> — 아래에 고칠 점을 말로 적으면 v2가 만들어져요.</p><div class="cw-actions"><button class="tk-button" type="button" data-dlg="approve">이대로 v1 승인…</button></div></section>`;
    } else if (s === 'approved') {
      act = `<section class="cw-approve" data-done aria-labelledby="ap-t"><p id="ap-t">${ic('check-check')} <strong>v2가 최종 승인됐어요</strong> · 12:48 · 이 기기</p><p class="cw-small cw-muted">보관함 › 결과물에 '최종 승인'으로 저장됐어요. 더 고치려면 새 버전(v3)이 만들어지고 v2는 그대로 남아요.</p><div class="cw-actions"><button class="tk-button" type="button" data-go="library" data-set="item=l1">결과물에서 보기</button><button class="tk-button" type="button" data-set="s=v1&v=1">v2에서 이어서 수정(v3)</button><button class="tk-button tk-button--ghost" type="button" data-act="unapprove">승인 취소</button></div></section>`;
    } else if (s === 'error') {
      act = `<section class="cw-approve" style="border-color:var(--tk-color-status-danger-border);border-left-color:var(--tk-color-status-danger-icon);background:var(--tk-color-status-danger-bg);color:var(--tk-color-status-danger-text)" aria-labelledby="ap-t"><p id="ap-t"><strong>v2는 만들어지지 않았어요.</strong> v1은 그대로예요.</p><p class="cw-small">승인할 새 버전이 없어서 승인 버튼은 열리지 않아요.</p></section>`;
    } else if (s === 'cancelled') {
      act = `<section class="cw-approve" style="border-left-color:var(--tk-color-status-neutral-icon);background:var(--tk-color-status-neutral-bg);color:var(--tk-color-status-neutral-text);border-color:var(--tk-color-status-neutral-border)"><p><strong>v2 만들기를 멈췄어요.</strong> 임시 파일은 정리됐고 v1은 그대로예요.</p><div class="cw-actions"><button class="tk-button tk-button--primary" type="button" data-act="retry-run" data-focus="retry">${ic('retry')}같은 요청 다시 시도</button></div></section>`;
    }
    const stageActions = ['new', 'notools', 'attach', 'running', 'unsupported'].includes(s) && s !== 'unsupported' ? '' :
      `<div class="cw-actions"><button class="tk-button" type="button" data-dlg="handoff">${ic('external-link')}원본 앱에서 정밀 편집</button><button class="tk-button" type="button" data-mock="원본 다운로드">${ic('download')}원본 다운로드</button><span class="cw-spacer"></span>${s === 'unsupported' ? '' : `<a class="tk-link" href="${href({ run: 'r1' }, 'history', false)}">이 버전의 실행 기록</a>`}</div>`;
    const unsupApprove = s === 'unsupported' || T.id === 't3' ? `<section class="cw-approve" aria-labelledby="ap-t"><p id="ap-t"><strong>미리보기 없이는 승인하지 않는 게 안전해요.</strong></p><p class="cw-small">원본 앱으로 확인했다면 아래로 승인할 수 있어요. 승인 때 "미리보기 없이 승인"이 기록돼요.</p><div class="cw-actions"><button class="tk-button" type="button" data-dlg="approve-nopreview">미리보기 없이 승인…</button></div></section>` : '';

    const statusFirst = ['error', 'cancelled', 'lost'].includes(s);
    const stage = `<section class="cw-stage" aria-label="결과물 미리보기">${head}${statusFirst ? act : ''}${verbar}${canvas}${diff}${statusFirst ? '' : act}${unsupApprove}${stageActions}</section>`;

    /* 스레드 */
    let thread = '';
    const run1 = runCard({ state: 'succeeded', label: '완료', title: 'v1 만들기', meta: ['PhotoCraft', '31초'], open: false, steps: [['succeeded', '요청 해석', '', '2초'], ['succeeded', '도구 선택: PhotoCraft', '이미지 생성·편집', '1초'], ['succeeded', 'v1 저장·보관함 등록', 'thumbnail.png', '28초']] });
    const files1 = fileChip('스크린샷.png', 'ok', '1.1 MB');
    const u2 = bubble('썸네일의 배경을 어둡게 하고 제목을 더 크게 만들어줘');
    if (s === 'new' || s === 'notools') {
      thread = `<p class="cw-small cw-muted">아직 주고받은 말이 없어요. 아래에 요청을 적어 보세요.</p>`;
    } else if (s === 'attach') {
      thread = `<p class="cw-small cw-muted">첨부 3개 중 1개가 실패했어요. 아래에서 정리하고 보내세요.</p>`;
    } else if (s === 'unsupported' || T.id === 't3') {
      thread = bubble('거실 평면도를 수정하고 싶어', fileChip('거실-평면도.dwg', 'ok', '6.7 MB')) + runCard({ state: 'waiting', label: '도구 없음', title: '실행할 도구를 찾지 못했어요', badgeHtml: badge('warning', '실행 못함', 'warning'), summary: 'CAD 파일을 다루는 연결된 도구가 없어요. 원본 앱에서 직접 열거나, 도구가 준비되면 다시 요청하세요.', steps: [['succeeded', '요청 해석', 'CAD 수정 요청', '1초'], ['failed', '도구 선택', '', '', 'CAD용으로 검증된 도구 없음']] });
    } else {
      thread = bubble('썸네일 만들어줘', files1) + run1;
      if (s === 'v1') thread += `<p class="cw-small cw-muted">${ic('tip')} v1을 보고 "배경 어둡게, 제목 크게"처럼 말로 고칠 점을 적어 주세요.</p>`;
      if (s === 'compare' || s === 'approved') {
        thread += u2 + runCard({ state: 'succeeded', label: '완료', title: 'v2 만들기 · 배경 어둡게 · 제목 확대', meta: ['PhotoCraft 0.9.2', '48초'], open: false, steps: [['succeeded', '요청 해석', '', '2초'], ['succeeded', '도구 선택: PhotoCraft', '이미지 레이어 편집이 필요해서(자동)', '1초'], ['succeeded', '배경 어둡게', 'layer: background', '9초'], ['succeeded', '제목 확대', 'layer: title (+48%)', '11초'], ['succeeded', 'v2 저장', 'thumbnail.png v2', '3초']], actions: `<a class="tk-button tk-button--sm tk-button--ghost" href="${href({ run: 'r1' }, 'history', false)}">실행 기록</a>` });
      } else if (s === 'running') {
        thread += u2 + runCard({ state: 'running', label: '실행 중', title: '앞 10초 컷 렌더 · v2 만들기', meta: ['FilmCraft 2.3.0', '4:12'], progress: 41, steps: [['succeeded', '요청 해석', '', '2초'], ['succeeded', '도구 선택: FilmCraft', '긴 렌더(자동)', '1초'], ['running', '렌더', '41%', '4:12'], ['queued', 'v2 저장', '', '']], actions: `<button class="tk-button tk-button--sm" type="button" data-dlg="cancelrun">멈추기</button>` });
      } else if (s === 'cancelled') {
        thread += u2 + runCard({ state: 'cancelled', label: '취소됨', title: 'v2 만들기', meta: ['FilmCraft 2.3.0', '4:50'], open: false, summary: '내가 멈췄어요. v2는 만들어지지 않았어요.', steps: [['succeeded', '요청 해석', '', '2초'], ['cancelled', '렌더', '41%에서 멈춤', '4:50']] });
      } else if (s === 'error') {
        thread += u2 + runCard({ state: 'failed', label: '실패', title: 'v2 만들기', meta: ['PhotoCraft 0.9.2', '시도 1/3'], summary: '<strong>thumbnail.psd</strong>를 여는 단계에서 멈췄어요. 파일은 바뀌지 않았어요.', steps: [['succeeded', '요청 해석', '', '2초'], ['succeeded', '도구 선택: PhotoCraft', '', '1초'], ['failed', '레이어 열기', 'thumbnail.psd', '4초', '파일을 열지 못했어요 (E_OPEN_FAILED)'], ['queued', 'v2 저장', '', '']], actions: `<a class="tk-button tk-button--sm tk-button--ghost" href="${href({ run: 'r3' }, 'history', false)}">이력에서 보기</a><button class="tk-button tk-button--sm" type="button" data-dlg="handoff">원본 앱에서 열기</button><button class="tk-button tk-button--sm tk-button--primary" type="button" data-act="retry-run" data-focus="retry">${ic('retry')}다시 시도 (2/3)</button>` });
      } else if (s === 'lost') {
        thread += u2 + runCard({ state: 'running', label: '상태 확인 불가', badgeHtml: badge('warning', '상태 확인 불가', 'wifi-off'), title: 'v2 만들기', meta: ['마지막 확인 12:41:08'], summary: '연결이 끊겨 진행 상황을 못 가져오고 있어요. <strong>끝났는지 아직 몰라요.</strong> 완료로 표시하지 않아요.', steps: [['succeeded', '요청 해석', '', '2초'], ['running', '배경 어둡게', '마지막으로 본 단계', '']], actions: `<button class="tk-button tk-button--sm" type="button" data-act="reconnect">${ic('retry')}다시 연결</button>` });
      }
    }

    /* 컴포저 */
    const sendDisabled = s === 'notools' || s === 'lost';
    let tray = '';
    if (s === 'attach') {
      tray = `<ul class="cw-files" aria-label="첨부 파일">${fileChip('스크린샷.png', 'ok', '1.1 MB')}${fileChip('로고-원본.psd', 'uploading', '63%')}${fileChip('배경영상-전체.mov', 'failed', '2.2 GB > 한도 2 GB')}${fileChip('설명.flac', 'unsupported', '이 요청에 쓸 도구 없음')}</ul><p class="tk-banner tk-banner--warning" role="status">${ic('warning')}<span class="tk-banner__text">4개 중 1개만 올라갔고 1개는 올리는 중이에요. 실패 1 · 지원 안 됨 1</span></p><div class="cw-actions"><button class="tk-button tk-button--sm" type="button">실패한 것 다시 올리기</button><button class="tk-button tk-button--sm" type="button" data-set="s=v1">빼고 계속</button></div>`;
    }
    const chips = ['running', 'cancelled', 'error', 'lost', 'attach'].includes(s) ? '' : `<div class="cw-chips" aria-label="이런 말도 돼요">${s === 'new' ? '' : ['제목 색을 노랑으로', '로고 더 크게', 'v1에서 다시 해줘'].map((x) => `<button class="tk-chip" type="button" data-fill="${x}">${x}</button>`).join('')}</div>`;
    const compose = `<div class="cw-compose" id="cw-compose">${tray}${chips}<form class="tk-composer" data-act="send" aria-label="작업 지시" novalidate><div class="tk-composer__row"><button class="tk-icon-button" type="button" aria-label="파일 첨부" data-set="s=attach">${ic('paperclip')}</button><label class="tk-sr-only" for="cw-input">요청</label><textarea class="tk-composer__input" id="cw-input" rows="1" placeholder="${s === 'running' ? '실행 중에도 다음 요청을 적을 수 있어요' : '이렇게 말하면 돼요 · 예: 배경을 더 어둡게'}"${s === 'notools' ? ' aria-describedby="cw-sendwhy"' : ''}></textarea><button class="tk-icon-button tk-icon-button--primary tk-icon-button--round tk-composer__send" type="submit" aria-label="보내기"${sendDisabled ? ' aria-disabled="true"' : ''}>${ic('send')}</button></div>${sendDisabled ? `<p class="tk-composer__hint" id="cw-sendwhy"><span>${s === 'notools' ? '보낼 수 없어요: 연결·검증된 도구가 아직 없어요' : '보낼 수 없어요: 연결이 끊겨 있어요'}</span></p>` : `<p class="tk-composer__hint cw-hint"><span>명령어 없이 말로 쓰세요</span><span class="tk-composer__keys"><kbd>↵</kbd> 보내기 · <kbd>⇧</kbd><kbd>↵</kbd> 줄바꿈</span></p>`}</form></div>`;

    const banner = s === 'lost' ? `<p class="tk-banner tk-banner--danger" role="alert">${ic('wifi-off')}<span class="tk-banner__text">Mac Studio가 응답하지 않아요 (마지막 응답 12:41:08). 읽기만 가능해요.</span><button class="tk-button tk-button--sm" type="button" data-act="reconnect">다시 연결</button></p>` : '';
    return shell('work', c, `${banner}<div class="cw-work">${stage}<section class="cw-thread" aria-label="요청과 진행">${thread}</section>${compose}</div>`);
  }

  /* ---------- 결과물 라이브러리 ---------- */
  function libItems(p, c) {
    const q = (p.q || '').trim().toLowerCase();
    const type = p.type || 'all', rl = p.role || 'all', tl = p.tool || 'all';
    let items = D.LIB.filter((x) => x.proj === c.proj.id);
    if (c.task) items = items.filter((x) => x.task === c.task.name);
    if (type !== 'all') items = items.filter((x) => x.kind === type);
    if (rl !== 'all') items = items.filter((x) => x.role === rl || x.versions.some((v) => v.role === rl));
    if (tl !== 'all') items = items.filter((x) => x.tool === tl);
    if (q) items = items.filter((x) => (x.name + x.task + x.tool).toLowerCase().includes(q));
    if (p.s === 'emptyfilter') items = [];
    return items;
  }
  function libFilters(p) {
    const type = p.type || 'all';
    const chips = [['all', '전체'], ['image', '이미지'], ['video', '영상'], ['doc', '문서'], ['audio', '오디오'], ['cad', 'CAD·벡터'], ['sheet', '표']]
      .map(([k, l]) => `<button class="tk-chip" type="button" aria-pressed="${type === k}" data-set="type=${k === 'all' ? '' : k}&item=">${KIND[k] ? ic(KIND[k][1]) : ''}${l}</button>`).join('');
    const tools = ['all', 'PhotoCraft', 'FilmCraft', 'PdfCraft', 'GridCraft'].map((x) => `<option value="${x}"${(p.tool || 'all') === x ? ' selected' : ''}>${x === 'all' ? '모든 도구' : x}</option>`).join('');
    const roles = [['all', '모든 역할'], ['source', '원본'], ['draft', '초안'], ['approved', '최종 승인']].map(([k, l]) => `<option value="${k}"${(p.role || 'all') === k ? ' selected' : ''}>${l}</option>`).join('');
    return `<div class="cw-pad" style="padding-bottom:0"><div class="tk-filterbar" role="search"><div class="tk-filterbar__row"><span class="tk-input-wrap">${ic('search')}<input class="tk-input" type="search" data-filter="q" value="${esc(p.q || '')}" placeholder="이름·작업·도구로 찾기" aria-label="결과물 검색"></span></div><div class="tk-filterbar__row"><span class="tk-select"><select data-filter="role" aria-label="역할">${roles}</select></span><span class="tk-select"><select data-filter="tool" aria-label="도구">${tools}</select></span></div><div class="tk-chips" role="group" aria-label="유형 필터" style="flex-wrap:nowrap;overflow-x:auto">${chips}</div></div></div>`;
  }
  function libRow(x, sel) {
    const k = KIND[x.kind];
    const v = x.versions[x.cur - 1];
    const policy = x.state === 'restricted' ? `<span class="tk-policy" data-policy="restricted">${ic('lock')}민감</span>` : x.state === 'missing' ? badge('danger', '원본 없음', 'unlink') : x.state === 'unsupported' ? badge('', '미리보기 없음', 'eye') : x.state === 'large' ? badge('info', '미리보기 만드는 중') : '';
    return `<button class="cw-item" type="button" data-set="item=${x.id}" aria-current="${sel === x.id}" data-fk="lib-${x.id}">${glyph(x.kind)}<span class="cw-item__body"><span class="cw-item__name">${esc(x.name)}</span><span class="cw-item__meta"><span>${esc(x.task)}</span><span>${x.tool}</span><span>${x.size}</span><span>${x.when}</span>${x.retention ? `<span class="tk-retention" data-soon>${ic('clock')}${x.retention}</span>` : ''}</span><span class="cw-item__meta"><span class="cw-role" data-role="${ROLE[v.role][0]}">${ROLE[v.role][1]} v${v.n}</span>${x.versions.length > 1 ? `<span>버전 ${x.versions.length}개</span>` : ''}${x.versions.some((y) => y.pending) ? badge('warning', '승인 대기') : ''}<span class="tk-mono">${v.sha}</span></span></span><span class="cw-item__end">${policy}</span></button>`;
  }
  function libDetailHtml(x) {
    const v = x.versions[x.cur - 1];
    let pv = '';
    if (x.state === 'large') pv = `<div class="cw-pv">${ic('video')}<p class="tk-empty__title">미리보기를 만드는 중이에요</p><div class="tk-progress tk-progress--agent" style="--tk-progress:0.72" role="progressbar" aria-label="미리보기 만들기" aria-valuenow="72" aria-valuemin="0" aria-valuemax="100"></div><p class="cw-small cw-muted">72% · 2.4 GB 영상이라 오래 걸려요. 원본은 지금 받을 수 있어요.</p></div>`;
    else if (x.state === 'unsupported') pv = `<div class="cw-pv">${ic('cad')}<p class="tk-empty__title">dwg 미리보기는 지원하지 않아요</p><p class="cw-small cw-muted">원본은 그대로 받을 수 있어요.</p></div>`;
    else if (x.state === 'missing') pv = `<div class="cw-pv">${ic('unlink')}<p class="tk-empty__title">원본 파일을 찾을 수 없어요</p><p class="cw-small cw-muted">등록할 때의 SHA-256과 같은 파일이 저장 위치에 없어요. 옮겨졌거나 지워졌을 수 있어요. 기록은 남아 있어요.</p><div class="cw-actions"><button class="tk-button tk-button--sm" type="button" data-mock="위치 다시 지정">위치 다시 지정</button><button class="tk-button tk-button--sm tk-button--ghost" type="button" data-mock="기록만 남기기">기록만 남기고 정리</button></div></div>`;
    else if (x.state === 'restricted') pv = `<div class="cw-pv">${ic('lock')}<p class="tk-empty__title">민감 자료라 미리보기를 막았어요</p><p class="tk-policy-note cw-small">회사·민감 정책이 적용된 파일이에요. 원격에서는 열람·다운로드할 수 없고, Mac Studio에서만 볼 수 있어요.</p></div>`;
    else if (x.kind === 'image') pv = `<div class="cw-canvas">${thumb(Math.min(v.n, 3) === 1 ? 1 : 2)}</div>`;
    else pv = `<div class="cw-pv">${glyph(x.kind)}<p class="tk-empty__title">${KIND_LABEL[x.kind]} 미리보기</p><p class="cw-small cw-muted">${KIND_LABEL[x.kind]}은 기존 보관함 뷰어가 그대로 보여줘요.</p></div>`;
    const blocked = x.state === 'restricted';
    const dl = `<button class="tk-button" type="button" ${blocked ? 'aria-disabled="true"' : ''} data-mock="${blocked ? '' : '원본 다운로드'}">${ic('download')}원본 다운로드</button>`;
    const reedit = x.state === 'missing' ? `<button class="tk-button tk-button--primary" type="button" aria-disabled="true">${ic('pencil')}이 버전에서 재편집</button>` : `<button class="tk-button tk-button--primary" type="button" data-go="work" data-set="s=v1&v=1" data-t="${x.task === '유튜브 썸네일 제작' ? 't1' : ''}">${ic('pencil')}이 버전에서 재편집</button>`;
    const why = blocked ? `<p class="cw-small cw-muted">다운로드 막힘: 민감 정책에 따라 원격 반출이 차단돼요.</p>` : x.state === 'missing' ? `<p class="cw-small cw-muted">재편집 불가: 원본 파일이 없어요. 위치를 다시 지정하면 열려요.</p>` : '';
    const vers = x.versions.slice().reverse().map((y) => `<li data-kind="${y.role === 'approved' ? 'approved' : y.n === x.cur ? 'current' : ''}"><div class="cw-row"><strong>v${y.n}</strong>${role(y.role)}${y.pending ? badge('warning', '승인 대기') : ''}<span class="cw-small cw-muted">${y.t}</span></div><p class="cw-small" style="margin:0">${esc(y.note)} · ${y.size} · <span class="tk-mono">${y.sha}</span></p><div class="cw-actions"><button class="tk-button tk-button--xs" type="button" data-mock="미리보기">보기</button>${y.n > 1 ? `<button class="tk-button tk-button--xs" type="button" data-go="work" data-set="s=compare&view=side" data-t="t1">${ic('columns')}v${y.n - 1}과 비교</button>` : ''}<button class="tk-button tk-button--xs" type="button" data-mock="이 버전 다운로드">${ic('download')}받기</button></div></li>`).join('');
    const inputs = x.inputs.length ? x.inputs.map((i) => `<li><span class="tk-glyph tk-glyph--sm" data-type="image">${ic('image')}</span><span class="cw-caps__body"><span class="cw-wrap">${esc(i.name)} <span class="cw-muted">${i.size}</span></span><span class="cw-small ${i.changed ? '' : 'cw-muted'}">${i.changed ? `${ic('warning')} ` : ''}${esc(i.note)}</span>${i.changed ? `<span class="cw-actions"><button class="tk-button tk-button--xs" type="button" data-mock="새 원본으로 다시">새 원본으로 다시 만들기</button></span>` : ''}</span></li>`).join('') : `<li><span class="cw-small cw-muted">연결된 입력 파일이 없어요.</span></li>`;
    return `<div class="cw-detail" aria-label="${esc(x.name)} 상세">
      ${pv}
      <div class="cw-stack cw-stack--sm"><h2 class="cw-h cw-wrap">${esc(x.name)}</h2><div class="cw-row">${role(v.role)}<span class="tk-badge tk-badge--mono">${KIND_LABEL[x.kind]}</span>${x.state === 'restricted' ? `<span class="tk-policy" data-policy="restricted">${ic('lock')}민감</span>` : ''}${sampleTag()}</div><p class="cw-small cw-muted cw-wrap">${esc(x.task)} · ${x.tool}${x.retention ? ' · ' + x.retention : ''}</p></div>
      <div class="cw-actions">${reedit}${dl}</div>${why}
      <section aria-label="버전"><h3 class="cw-h cw-h--sm">버전 ${x.versions.length}개</h3><ol class="cw-timeline">${vers}</ol></section>
      <section aria-label="원본·입력 파일"><h3 class="cw-h cw-h--sm">원본·입력 파일</h3><ul class="cw-caps">${inputs}</ul></section>
      <section aria-label="정보"><h3 class="cw-h cw-h--sm">정보</h3><dl class="tk-meta"><div><dt>Artifact ID</dt><dd class="tk-mono">art_${x.id.toUpperCase()}XQ2</dd></div><div><dt>프로젝트 · 작업</dt><dd>${esc(D.PROJECTS.find((q) => q.id === x.proj).name)} › ${esc(x.task)} <button class="tk-link" type="button" data-mock="작업 옮기기">옮기기</button></dd></div><div><dt>SHA-256</dt><dd class="tk-mono">${v.sha}</dd></div><div><dt>만든 실행</dt><dd><a class="tk-link" href="${href({ run: 'r1' }, 'history', false)}">실행 기록 보기</a></dd></div><div><dt>보관</dt><dd>${x.retention || '고정 없음 · 정리 예정 없음'}</dd></div></dl></section>
    </div>`;
  }
  function screenLibrary(p, c) {
    if (c.none) return shell('library', c, `<div class="cw-pad">${empty('archive', '이 프로젝트에는 아직 결과물이 없어요', '작업실에서 요청하면 만들어진 파일이 버전과 함께 여기에 모여요.', `<button class="tk-button tk-button--primary" data-go="work">작업실로 가기</button>`)}</div>`);
    if (p.s === 'loading') return shell('library', c, `${libFilters(p)}${skeletonRows(6)}`);
    if (p.s === 'error') return shell('library', c, `<div class="cw-pad">${errorBox('결과물', 'ERR_LIST_TIMEOUT', `<button class="tk-button" data-set="s=">${ic('retry')}다시 시도</button>`)}</div>`);
    const items = libItems(p, c);
    const sel = p.item;
    const filtered = (p.q || p.type || p.role || p.tool || p.s === 'emptyfilter');
    let list;
    if (!items.length) list = `<div class="cw-pad">${empty('search', '찾는 결과물이 없어요', '다른 단어로 찾거나 필터를 풀어 보세요. 정리된 결과물은 보관 기간이 지나면 찾을 수 없어요.', filtered ? `<button class="tk-button" data-reset-filters>필터 모두 풀기</button>` : '')}</div>`;
    else {
      const groups = [...new Set(items.map((x) => x.day))];
      list = groups.map((g) => `<p class="tk-list__group cw-group">${g}</p>${items.filter((x) => x.day === g).map((x) => libRow(x, sel)).join('')}`).join('');
    }
    const x = D.LIB.find((y) => y.id === sel && items.includes(y));
    const approvedN = items.filter((y) => y.role === 'approved').length;
    const meta = `<p class="cw-small cw-muted cw-pad" style="padding-block:var(--tk-space-2)" role="status">${items.length}개 · 최종 승인 ${approvedN} · 승인 대기 ${items.filter((y) => y.versions.some((v) => v.pending)).length}${filtered ? ' · 필터 적용 중' : ''}</p>`;
    const detail = x ? libDetailHtml(x) : `<div class="cw-pad">${empty('archive', '결과물을 고르면 여기에 보여요', '버전, 원본 파일, 재편집 길이 한곳에 있어요.')}</div>`;
    return shell('library', c, `<div class="cw-split"><div class="cw-split__list">${libFilters(p)}${meta}<div class="tk-list">${list}</div></div><aside class="cw-split__detail" aria-label="결과물 상세">${detail}</aside></div>`);
  }

  /* ---------- 실행 이력 ---------- */
  function runBadgeFor(r) {
    const map = { succeeded: ['', ''], failed: ['', ''] };
    if (r.state === 'running') return runBadge('running', r.label);
    if (r.state === 'succeeded') return runBadge('succeeded', r.label);
    if (r.state === 'failed') return runBadge('failed', r.label);
    if (r.state === 'blocked') return `<span class="tk-policy" data-policy="blocked">${ic('ban')}${r.label}</span>`;
    if (r.state === 'partial') return badge('warning', r.label, 'warning');
    return badge('warning', r.label, 'help');
  }
  const runTone = (r) => (r.state === 'failed' ? 'failed' : r.state === 'running' ? 'running' : ['partial', 'review', 'blocked'].includes(r.state) ? 'warn' : '');
  function histFilters(p) {
    const st = p.st || 'all';
    const chips = [['all', '전체'], ['problem', '문제 있음'], ['running', '진행 중'], ['succeeded', '완료']].map(([k, l]) => `<button class="tk-chip" type="button" aria-pressed="${st === k}" data-set="st=${k === 'all' ? '' : k}&run=">${l}</button>`).join('');
    const tools = ['all', 'PhotoCraft', 'FilmCraft', 'PdfCraft', 'GridCraft', 'FigCraft'].map((x) => `<option value="${x}"${(p.tool || 'all') === x ? ' selected' : ''}>${x === 'all' ? '모든 도구' : x}</option>`).join('');
    return `<div class="cw-pad" style="padding-bottom:0"><div class="tk-filterbar" role="search"><div class="tk-filterbar__row"><span class="tk-input-wrap">${ic('search')}<input class="tk-input" type="search" data-filter="q" value="${esc(p.q || '')}" placeholder="작업·파일·오류 코드로 찾기" aria-label="이력 검색"></span><span class="tk-select"><select data-filter="tool" aria-label="도구">${tools}</select></span></div><div class="tk-chips" role="group" aria-label="상태 필터">${chips}</div></div></div>`;
  }
  function runRow(r, sel) {
    return `<button class="cw-run" type="button" data-tone="${runTone(r)}" data-set="run=${r.id}" aria-current="${sel === r.id}" data-fk="run-${r.id}"><span class="cw-item__body"><span class="cw-row" style="justify-content:space-between;flex-wrap:nowrap"><span class="cw-item__name cw-trunc" style="font-size:var(--tk-font-size-sm)">${esc(r.title)}</span>${runBadgeFor(r)}</span><span class="cw-item__meta"><span>${esc(r.task)}</span><span>시도 ${r.attempt}${r.of ? '/' + r.of : ''}</span><span>${r.tool}</span><span class="tk-mono">${r.dur}</span><span>${r.t}</span></span></span></button>`;
  }
  function maskLog(msg) { return esc(msg).replace(/(•{3,}[^\s)]*)/g, '<span class="cw-mask">$1</span>'); }
  function runDetail(r) {
    const hasErr = ['failed', 'blocked', 'partial', 'review'].includes(r.state);
    const toneCls = r.state === 'failed' ? 'danger' : 'warning';
    const where = hasErr ? `<aside class="tk-callout tk-callout--${r.state === 'failed' ? 'caution' : 'warning'}" role="note">${ic(r.state === 'failed' ? 'caution' : 'warning', 'tk-callout__icon')}<div class="tk-callout__body"><strong class="tk-callout__title">${r.state === 'failed' ? '어디서 멈췄나' : r.state === 'blocked' ? '왜 막혔나' : '무엇을 확인해야 하나'}</strong><p>${esc(r.why)}</p><dl class="tk-meta"><div><dt>파일</dt><dd class="cw-wrap">${esc(r.where.file)}</dd></div><div><dt>단계</dt><dd>${esc(r.where.step)}</dd></div><div><dt>시도</dt><dd>${esc(r.where.attempt)}</dd></div></dl></div></aside>` : '';
    const out = r.outputs && r.outputs.length ? `<ul class="cw-caps">${r.outputs.map((o) => `<li>${ic('file-archive')}<span class="cw-caps__body"><a class="tk-link" href="${href({ item: o.lib }, 'library', false)}">${esc(o.n)}</a><span class="cw-small cw-muted">결과물에서 열기</span></span></li>`).join('')}</ul>` : `<p class="cw-small cw-muted">${r.state === 'succeeded' ? '연결된 결과물이 없어요.' : '이 시도에서 보관함에 등록된 결과물이 없어요.'}</p>`;
    const retry = r.retry ? `<section aria-label="다시 시도"><h3 class="cw-h cw-h--sm">${r.retry.safe ? '안전하게 다시 시도' : '다시 시도'}</h3><p class="cw-small" style="margin:0">${esc(r.retry.note)}</p><div class="cw-actions" style="margin-top:var(--tk-space-2)">${(r.retry.opts || [r.state === 'succeeded' ? '같은 요청 다시 실행' : '다시 시도']).map((o, i) => `<button class="tk-button${i === 0 && hasErr ? ' tk-button--primary' : ''}" type="button" data-act="retry-hist" data-fk="retry-${r.id}">${ic('retry')}${esc(o)}</button>`).join('')}</div><p class="cw-small cw-muted" style="margin:var(--tk-space-2) 0 0">재시도는 새 시도로 기록돼요. 이전 시도와 로그는 지워지지 않아요.</p></section>` : r.state === 'running' ? `<section><div class="cw-actions"><button class="tk-button" type="button" data-dlg="cancelrun">멈추기</button><a class="tk-button tk-button--ghost" href="${href({ s: 'running', t: 't2' }, 'work', false)}">작업실에서 보기</a></div></section>` : '';
    const logLines = (r.log || []).map((l) => `<li data-level="${l[1]}"><span class="tk-log__time">${l[0]}</span><span class="tk-log__level">${l[1]}</span><span class="tk-log__msg">${maskLog(l[2])}</span></li>`).join('');
    const logTotal = r.logTotal ? `<button class="tk-log__more" type="button" data-mock="전체 로그">전체 로그 ${r.logTotal.toLocaleString('ko-KR')}줄 보기</button>` : '';
    return `<div class="cw-detail" aria-label="실행 상세">
      <div class="cw-stack cw-stack--sm"><div class="cw-row" style="justify-content:space-between;flex-wrap:nowrap"><h2 class="cw-h cw-wrap">${esc(r.title)}</h2>${runBadgeFor(r)}</div><p class="cw-small cw-muted cw-wrap">${esc(r.task)} · 시도 ${r.attempt}${r.of ? '/' + r.of : ''} · ${r.t} ${sampleTag()}</p></div>
      ${where}
      <section aria-label="사용한 도구와 에이전트"><dl class="tk-meta"><div><dt>도구</dt><dd>${r.tool}</dd></div><div><dt>에이전트</dt><dd>${r.agent}</dd></div><div><dt>걸린 시간</dt><dd class="tk-mono">${r.dur}</dd></div></dl></section>
      <section aria-label="단계"><h3 class="cw-h cw-h--sm">단계</h3><ol class="tk-steps">${r.steps.map(stepLi).join('')}</ol></section>
      <section aria-label="자원 사용"><h3 class="cw-h cw-h--sm">자원 사용 ${sampleTag('수집 가능 여부 확인 필요')}</h3><dl class="cw-usage">${Object.entries(r.res).map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl></section>
      <section aria-label="결과물"><h3 class="cw-h cw-h--sm">결과물</h3>${out}</section>
      ${retry}
      ${logLines ? `<section aria-label="로그"><h3 class="cw-h cw-h--sm">로그 <span class="cw-small cw-muted">· 경로와 키는 가려서 보여요</span></h3><div class="tk-log"><div class="tk-log__header"><span class="tk-log__title">실행 로그</span></div><div class="tk-log__body" tabindex="0" role="region" aria-label="실행 로그"><ol class="tk-log__lines">${logLines}</ol></div>${logTotal}</div></section>` : ''}
    </div>`;
  }
  function screenHistory(p, c) {
    if (p.s === 'loading') return shell('history', c, `${histFilters(p)}${skeletonRows(6)}`);
    if (p.s === 'error') return shell('history', c, `<div class="cw-pad">${errorBox('실행 이력', 'ERR_RUNS_TIMEOUT', `<button class="tk-button" data-set="s=">${ic('retry')}다시 시도</button>`)}</div>`);
    if (p.s === 'empty' || c.none) return shell('history', c, `<div class="cw-pad">${empty('history', '아직 실행 기록이 없어요', '작업실에서 요청하면 어떤 도구가 어떤 단계를 거쳤는지 여기에 남아요.', `<button class="tk-button tk-button--primary" data-go="work">작업실로 가기</button>`)}</div>`);
    let runs = D.RUNS.slice();
    if (c.task) runs = runs.filter((r) => r.task === c.task.name);
    const st = p.st, q = (p.q || '').toLowerCase();
    if (st === 'problem') runs = runs.filter((r) => ['failed', 'blocked', 'partial', 'review'].includes(r.state));
    else if (st === 'running') runs = runs.filter((r) => r.state === 'running');
    else if (st === 'succeeded') runs = runs.filter((r) => r.state === 'succeeded');
    if (p.tool && p.tool !== 'all') runs = runs.filter((r) => r.tool.startsWith(p.tool));
    if (q) runs = runs.filter((r) => (r.title + r.task + (r.why || '') + r.tool).toLowerCase().includes(q));
    const sel = p.run; const cur = D.RUNS.find((r) => r.id === sel && runs.includes(r));
    const problems = runs.filter((r) => ['failed', 'blocked', 'partial', 'review'].includes(r.state)).length;
    let list;
    if (!runs.length) list = `<div class="cw-pad">${empty('search', '조건에 맞는 실행이 없어요', '필터를 풀면 전체 기록이 보여요.', `<button class="tk-button" data-reset-filters>필터 모두 풀기</button>`)}</div>`;
    else { const days = [...new Set(runs.map((r) => r.day))]; list = days.map((d) => `<p class="tk-list__group cw-group">${d}</p>${runs.filter((r) => r.day === d).map((r) => runRow(r, sel)).join('')}`).join(''); }
    const meta = `<p class="cw-small cw-muted cw-pad" style="padding-block:var(--tk-space-2)" role="status">${runs.length}건 · 문제 있음 ${problems} <span class="cw-muted">(실패·부분 성공·정책 차단·확인 필요는 완료로 세지 않아요)</span></p>`;
    const detail = cur ? runDetail(cur) : `<div class="cw-pad">${empty('history', '실행을 고르면 여기에 보여요', '어떤 파일의 어느 단계에서 어떤 도구가 무엇을 했는지 볼 수 있어요.')}</div>`;
    return shell('history', c, `<div class="cw-split"><div class="cw-split__list">${histFilters(p)}${meta}<div>${list}</div></div><aside class="cw-split__detail" aria-label="실행 상세">${detail}</aside></div>`);
  }

  /* ---------- 도구 관리 ---------- */
  function toolState(id, sample) { return sample && D.SAMPLE[id] ? D.SAMPLE[id] : D.REAL; }
  function toolRow(t, st, sel) {
    const ax = [st.install, st.connect, st.support, st.active, st.verify];
    const sum = st.summary;
    const tone = sum[1];
    return `<button class="cw-tool" type="button" data-set="tool=${t.id}" aria-current="${sel === t.id}" data-fk="tool-${t.id}"><span class="tk-glyph" data-type="${t.id === 'photo' ? 'image' : t.id === 'film' ? 'slide' : t.id === 'pdf' ? 'pdf' : t.id === 'grid' ? 'sheet' : t.id === 'vector' || t.id === 'fig' || t.id === 'design' ? 'code' : 'doc'}">${ic(t.id === 'photo' ? 'image' : t.id === 'film' ? 'video' : t.id === 'pdf' ? 'file-pdf' : t.id === 'grid' ? 'sheet' : t.id === 'vector' ? 'vector' : t.id === 'fig' || t.id === 'design' ? 'box' : 'help')}</span><span class="cw-tool__body"><span class="cw-row" style="justify-content:space-between;flex-wrap:nowrap"><span class="cw-tool__name">${esc(t.name)} <span class="cw-small cw-muted">${esc(t.use)}</span></span><span class="cw-axis" data-tone="${tone}" style="flex:none">${esc(sum[0])}</span></span><ul class="cw-axes" aria-label="${esc(t.name)} 상태 요약">${ax.map(axis).join('')}</ul></span></button>`;
  }
  function toolDetail(t, st, sample, offline) {
    const caps = (sample && D.CAPS[t.id]) || null;
    const tests = (sample && D.TESTS[t.id]) || [];
    const ax = [st.install, st.connect, st.support, st.active, st.verify];
    const canEnable = sample && st.connect[2] === 'ok' && st.support[2] !== 'bad' && st.support[2] !== 'unk' && st.install[2] === 'ok' && !offline;
    const reason = !sample ? '연결·검증된 기능이 아직 없어서 켤 수 없어요. 켜려면 ① 설치 확인 ② 연결 성공 ③ 검증된 기능 1개 이상이 필요해요.' : canEnable ? '켜면 이 도구가 요청에 맞을 때만 에이전트에게 보여요(상시 노출 아님).' : `켤 수 없어요: ${st.install[2] !== 'ok' ? '설치/버전 문제' : st.connect[2] !== 'ok' ? '연결 안 됨' : '검증된 기능 없음'}.`;
    const isOn = sample && st.active[1].startsWith('켜짐');
    const capHtml = caps ? caps.map((k) => `<li>${ic(k.st === 'ok' ? 'circle-check' : k.st === 'no' ? 'ban' : 'dashed')}<span class="cw-caps__body"><span>${esc(k.n)}</span><span class="cw-small ${k.st === 'ok' ? '' : 'cw-muted'}">${k.st === 'ok' ? '검증됨' : k.st === 'no' ? '지원 불가' : '미검증'} · ${esc(k.note)}</span>${k.cons ? `<span class="cw-small cw-muted">제약: ${esc(k.cons)}</span>` : ''}</span></li>`).join('') : `<li>${ic('dashed')}<span class="cw-caps__body"><span>확인된 기능 없음</span><span class="cw-small cw-muted">기능 목록은 앱별 감사(실제 CLI·MCP·JSON 호출 확인) 뒤에 채워져요. 공식 사이트의 소개 문구는 기능으로 세지 않아요.</span></span></li>`;
    const testHtml = tests.length ? tests.map((x) => `<li>${ic(x.r === 'ok' ? 'circle-check' : x.r === 'bad' ? 'circle-x' : 'warning')}<span class="cw-caps__body"><span>${esc(x.k)} <span class="cw-muted cw-small">${x.t}</span></span><span class="cw-small cw-muted">${esc(x.d)}</span></span></li>`).join('') : `<li><span class="cw-small cw-muted">테스트 기록이 없어요.</span></li>`;
    const testDisabled = !sample || offline;
    const testWhy = !sample ? '테스트할 수 없어요: 이 도구와 이어 줄 어댑터가 아직 없어요(앱별 감사·어댑터 개발 이후).' : offline ? '테스트할 수 없어요: Mac Studio와 연결이 끊겨 있어요.' : '';
    const stale = sample && (st.verify[2] === 'warn');
    return `<div class="cw-detail" aria-label="${esc(t.name)} 상세">
      <div class="cw-stack cw-stack--sm"><div class="cw-row"><h2 class="cw-h">${esc(t.name)}</h2>${sample ? sampleTag() : badge('', '후보 · 미검증')}</div><p class="cw-small cw-muted" style="margin:0">${esc(t.use)} · 근거: ${esc(t.src)}</p></div>
      ${stale ? `<p class="tk-banner tk-banner--warning" role="status">${ic('clock')}<span class="tk-banner__text">${esc(st.verify[1])}. 지금도 그런지는 몰라요. 다시 테스트하면 확인돼요.</span></p>` : ''}
      <section aria-label="상태"><h3 class="cw-h cw-h--sm">상태 · 다섯 가지를 따로 봐요</h3><ul class="cw-caps">${ax.map((a, i) => `<li>${ic(a[3])}<span class="cw-caps__body"><span><strong>${a[0]}</strong> · ${esc(a[1])}</span><span class="cw-small cw-muted">${['앱이 이 컴퓨터(Mac Studio)에 있는지, 버전이 맞는지', '따까리가 앱과 실제로 통신하는지', '실제로 확인한 기능이 있는지(공식 소개 문구는 세지 않음)', '에이전트에게 보여 줄지 사용자가 정한 값', '마지막으로 직접 테스트한 시각과 결과'][i]}</span></span></li>`).join('')}</ul></section>
      <section aria-label="활성화"><label class="tk-switch tk-switch--row"><input type="checkbox" role="switch"${isOn ? ' checked' : ''}${canEnable ? '' : ' disabled'} aria-describedby="en-why"> 에이전트가 이 도구를 쓰게 하기</label><p class="cw-small cw-muted" id="en-why" style="margin:var(--tk-space-1) 0 0">${reason}</p></section>
      <section aria-label="제공 기능"><h3 class="cw-h cw-h--sm">제공 기능과 제약</h3><ul class="cw-caps">${capHtml}</ul></section>
      <section aria-label="테스트"><h3 class="cw-h cw-h--sm">테스트</h3><div class="cw-actions"><button class="tk-button" type="button" data-act="test"${testDisabled ? ' aria-disabled="true"' : ''}>${ic('activity')}연결 테스트</button><button class="tk-button" type="button" data-act="test"${testDisabled ? ' aria-disabled="true"' : ''}>${ic('check-check')}기능 테스트</button></div>${testWhy ? `<p class="cw-small cw-muted" style="margin:var(--tk-space-1) 0 0">${testWhy}</p>` : ''}<ul class="cw-caps" id="cw-testlog">${testHtml}</ul></section>
      <section aria-label="설치와 업데이트"><h3 class="cw-h cw-h--sm">설치·업데이트 안내</h3>${sample && t.id === 'vector' ? `<ol class="tk-steps"><li data-state="succeeded">${ic('circle-check')}<span class="tk-step__name">현재 0.4.1 설치됨</span><span class="tk-step__time"></span></li><li data-state="waiting">${ic('hand')}<span class="tk-step__name">0.5.x로 업데이트<span class="tk-step__detail">검증은 0.5.x 기준이에요. 업데이트하면 자동으로 다시 테스트해요.</span></span><span class="tk-step__time"></span></li></ol>` : sample && t.id === 'film' ? `<ol class="tk-steps"><li data-state="waiting">${ic('hand')}<span class="tk-step__name">Mac Studio에 설치<span class="tk-step__detail">공식 릴리스 링크·라이선스 확인 뒤 안내가 채워져요</span></span><span class="tk-step__time"></span></li><li data-state="queued"><span class="tk-step__mark" aria-hidden="true"></span><span class="tk-step__name">버전 확인 → 연결 테스트</span><span class="tk-step__time"></span></li></ol>` : `<p class="cw-small cw-muted" style="margin:0">설치 방법은 공식 릴리스·라이선스·실행 호스트를 확인한 뒤에 채워져요. 지금은 안내할 수 있는 검증된 절차가 없어요.</p>`}<p class="cw-small cw-muted" style="margin:var(--tk-space-2) 0 0">설치 위치: <strong>Mac Studio(실행 호스트)</strong>와 <strong>이 기기</strong>는 서로 달라요. 앱이 어느 쪽에 있는지 항상 함께 보여요.</p></section>
      <section aria-label="오류 복구"><details class="tk-run__more"><summary>오류별 복구 안내 (시안)</summary><ul class="cw-caps">${[['E_CLI_EXIT_2', '앱이 오류로 끝났어요', '로그 보기 → 같은 입력으로 다시 테스트 → 계속되면 이 도구 끄기'], ['권한 거절', 'macOS가 앱 접근을 막았어요', '시스템 설정 › 개인정보 보호에서 허용한 뒤 다시 테스트 (이 앱이 대신 바꾸지 않아요)'], ['버전 불일치', '검증한 버전과 달라요', '업데이트 안내 따르기 → 자동 재테스트'], ['연결 끊김', 'Mac Studio가 응답하지 않아요', '연결이 돌아오면 자동으로 다시 확인해요. 그동안 마지막 상태는 "오래됨"으로 표시돼요']].map((r) => `<li>${ic('info')}<span class="cw-caps__body"><span><code>${r[0]}</code> ${r[1]}</span><span class="cw-small cw-muted">${r[2]}</span></span></li>`).join('')}</ul></details></section>
      <section aria-label="원본 편집기 열기 방식"><h3 class="cw-h cw-h--sm">원본 편집기 열기 방식 ${sampleTag('판정 대기')}</h3><ul class="cw-caps"><li>${ic('dashed')}<span class="cw-caps__body"><span>앱 안에 임베드</span><span class="cw-small cw-muted">미검증 · 웹 빌드, CSP/COOP/COEP, 인증, 파일 왕복을 실제로 시험한 뒤에만 채택해요</span></span></li><li>${ic('dashed')}<span class="cw-caps__body"><span>별도 실행 · 이 기기</span><span class="cw-small cw-muted">미검증</span></span></li><li>${ic('dashed')}<span class="cw-caps__body"><span>별도 실행 · Mac Studio 화면(원격 GUI)</span><span class="cw-small cw-muted">미검증</span></span></li></ul><div class="cw-actions"><button class="tk-button" type="button" data-dlg="handoff">${ic('external-link')}돌아오기·동기화 흐름 시안 보기</button></div></section>
    </div>`;
  }
  function screenTools(p, c) {
    const s = p.s || 'real';
    if (s === 'loading') return shell('tools', c, skeletonRows(7));
    if (s === 'error') return shell('tools', c, `<div class="cw-pad">${errorBox('도구 목록', 'ERR_REGISTRY_UNREACHABLE', `<button class="tk-button" data-set="s=real">${ic('retry')}다시 시도</button>`)}</div>`);
    const sample = s === 'sample' || s === 'offline';
    const offline = s === 'offline';
    const sel = p.tool || 'photo';
    const rows = D.TOOLS.map((t) => { let st = toolState(t.id, sample); if (offline && D.SAMPLE[t.id]) { st = { ...st, connect: ['연결', 'Mac Studio 끊김 · 마지막 확인 12:20', 'bad', 'wifi-off'], summary: ['연결 끊김 · 마지막 상태', 'bad'] }; } return toolRow(t, st, sel); }).join('');
    const tool = D.TOOLS.find((t) => t.id === sel) || D.TOOLS[0];
    let st = toolState(tool.id, sample);
    if (offline && D.SAMPLE[tool.id]) st = { ...st, connect: ['연결', 'Mac Studio 끊김 · 마지막 확인 12:20', 'bad', 'wifi-off'] };
    const top = !sample ? `<aside class="tk-callout tk-callout--note cw-pad" style="margin:var(--tk-space-3) var(--tk-size-layout-page-gutter)" role="note">${ic('info', 'tk-callout__icon')}<div class="tk-callout__body"><strong class="tk-callout__title">13개 모두 후보 · 미검증이에요</strong><p>아직 어느 앱도 설치·호출을 확인하지 못했어요. 사이트에 올라 있거나 등록돼 있다는 사실을 연결 성공처럼 보여 주지 않고, 실행할 수 있는 동작만 켜져요. 이름이 확정된 앱은 7개이고 나머지 6개는 감사 뒤에 채워져요.</p></div></aside>` : '';
    const offBanner = offline ? `<p class="tk-banner tk-banner--danger" role="alert">${ic('wifi-off')}<span class="tk-banner__text">Mac Studio가 응답하지 않아요 (마지막 응답 12:20). 아래 상태는 그때 기준이에요. 테스트·활성화는 잠겨요.</span></p>` : '';
    const sum = sample ? `<p class="cw-small cw-muted cw-pad" style="padding-block:var(--tk-space-2)" role="status">13개 · 사용 가능 0 · 일부 기능만 1 · 점검 필요 3 · 검증 실패 1 · 미설치 1 · 후보 6 ${sampleTag()}</p>` : `<p class="cw-small cw-muted cw-pad" style="padding-block:var(--tk-space-2)" role="status">13개 · 사용 가능 0 · 후보(미검증) 13</p>`;
    return shell('tools', c, `${offBanner}<div class="cw-split"><div class="cw-split__list">${top}${sum}<div>${rows}</div></div><aside class="cw-split__detail" aria-label="도구 상세">${toolDetail(tool, st, sample, offline)}</aside></div>`);
  }

  /* ---------- 렌더/모바일 상세 ---------- */
  function render() {
    const { scr, p } = parse();
    const c = ctx(p, scr);
    const th = p.theme; if (th === 'light' || th === 'dark') document.documentElement.dataset.theme = th;
    syncThemeIcon();
    const fk = document.activeElement && document.activeElement.dataset && document.activeElement.dataset.fk;
    const html = scr === 'work' ? screenWork(p, c) : scr === 'library' ? screenLibrary(p, c) : scr === 'history' ? screenHistory(p, c) : screenTools(p, c);
    $('#cw-screen').innerHTML = html;
    document.title = `${TAB_LABEL[scr]} · 크리에이티브 시안`;
    if (fk) { const el = document.querySelector(`[data-fk="${fk}"]`); if (el) el.focus({ preventScroll: true }); }
    const ov = $('#cw-split'); if (ov) { const set = () => $('#cw-ov .cw-overlay__top').style.setProperty('--cw-split', ov.value + '%'); ov.addEventListener('input', set); set(); }
    // 모바일에서 항목을 고르면 전체 화면 상세(뒤로가기로 닫힘)
    if (matchMedia('(max-width: 1023px)').matches && ((scr === 'library' && p.item) || (scr === 'history' && p.run) || (scr === 'tools' && p.tool && p.open))) openMobileDetail(scr, p, c);
  }
  function openMobileDetail(scr, p, c) {
    closeDlg();
    let body = '', title = '';
    if (scr === 'library') { const x = D.LIB.find((y) => y.id === p.item); if (!x) return; body = libDetailHtml(x); title = x.name; }
    if (scr === 'history') { const r = D.RUNS.find((y) => y.id === p.run); if (!r) return; body = runDetail(r); title = r.title; }
    if (scr === 'tools') { const t = D.TOOLS.find((y) => y.id === p.tool); const sample = (p.s === 'sample' || p.s === 'offline'); body = toolDetail(t, toolState(t.id, sample), sample, p.s === 'offline'); title = t.name; }
    const d = document.createElement('dialog');
    d.className = 'tk-dialog cw-full'; d.setAttribute('aria-label', title);
    d.innerHTML = `<div class="tk-viewer__toolbar"><button class="tk-icon-button" type="button" data-back aria-label="목록으로">${ic('back')}</button><div class="tk-viewer__title"><h2 class="tk-viewer__name cw-trunc">${esc(title)}</h2></div></div><div class="cw-fullbody">${body}</div>`;
    d.addEventListener('cancel', (e) => { e.preventDefault(); history.back(); });
    $('body').appendChild(d); d.showModal(); d.dataset.mobile = '1';
  }
  function closeDlg() { $$('dialog[open]').forEach((d) => { d.close(); d.remove(); }); $$('dialog:not([open])').forEach((d) => d.remove()); }

  /* ---------- 다이얼로그 ---------- */
  const DLG = {
    approve: () => ({ title: 'v2를 최종 승인할까요?', body: `<dl class="tk-meta"><div><dt>승인 대상</dt><dd><strong>thumbnail.png · v2</strong></dd></div><div><dt>만든 시각 · 도구</dt><dd>오늘 12:41 · PhotoCraft 0.9.2</dd></div><div><dt>크기 · SHA-256</dt><dd class="tk-mono">438 KB · 5be0…c3d9</dd></div><div><dt>변경 요약</dt><dd>배경 어둡게 · 제목 +57%</dd></div></dl><ul class="tk-checks" aria-label="승인하면 일어나는 일"><li data-result="pass">${ic('circle-check')}<span>보관함에 <strong>최종 승인</strong>으로 저장돼요</span></li><li data-result="pass">${ic('circle-check')}<span>v1은 <strong>초안</strong>으로 그대로 남아요</span></li><li data-result="pass">${ic('circle-check')}<span>승인 뒤에도 새 버전을 만들 수 있고, <strong>승인은 취소</strong>할 수 있어요</span></li></ul>`, foot: `<button class="tk-button" data-close>취소</button><button class="tk-button tk-button--primary" data-act="approve-do" data-busyable>v2 승인하고 저장</button>`, modal: true }),
    'approve-nopreview': () => ({ title: '미리보기 없이 승인할까요?', body: `<p>이 파일(<strong>거실-평면도.dwg</strong>)은 미리보기를 만들지 못했어요. 내용을 직접 확인하지 못한 상태로 승인돼요.</p><label class="tk-checkbox"><input type="checkbox" id="np-ok"> 원본 앱에서 확인했거나, 확인 없이 승인함을 이해했어요</label>`, foot: `<button class="tk-button" data-close>취소</button><button class="tk-button tk-button--primary" data-act="approve-np">미리보기 없이 승인</button>`, modal: true }),
    cancelrun: () => ({ title: '이 실행을 멈출까요?', body: `<p>멈추면 <strong>v2는 만들어지지 않아요</strong>. v1은 그대로 남고 임시 파일은 정리돼요. 렌더는 41%까지 진행됐어요.</p>`, foot: `<button class="tk-button" data-close>계속 두기</button><button class="tk-button tk-button--danger" data-act="cancel-do">멈추기</button>`, modal: true }),
    whytool: () => ({ title: '왜 PhotoCraft예요?', body: `<p>요청이 <strong>"배경 어둡게, 제목 크게"</strong>라서 이미지 레이어를 고칠 수 있는 도구가 필요했고, 켜져 있고 검증된 도구 중 PhotoCraft만 해당돼요.</p><dl class="tk-meta"><div><dt>후보</dt><dd>PhotoCraft(이미지 편집) · DesignCraft(용도 미확인)</dd></div><div><dt>제외 이유</dt><dd>DesignCraft: 검증된 이미지 기능 없음</dd></div><div><dt>에이전트에게 보인 도구</dt><dd>이 요청에 필요한 1개뿐 (13개 상시 노출 아님)</dd></div></dl><p class="cw-small cw-muted">${sampleTag()} 도구 바꾸기: 켜져 있고 검증된 다른 도구가 있을 때만 고를 수 있어요.</p>`, foot: `<button class="tk-button" data-close>닫기</button>` }),
    handoff: () => ({ title: '원본 앱에서 정밀 편집', wide: true, body: `<p>따까리 화면에서는 큰 그림만 고쳐요. 세밀한 편집은 원본 앱에서 하고, 저장하면 새 버전으로 돌아와요. <span class="cw-sample-tag">시안 · 실제 왕복 미검증</span></p>
      <fieldset class="cw-card"><legend class="cw-h cw-h--sm">어디서 열까요?</legend><div class="tk-choices"><label class="tk-radio"><input type="radio" name="host" checked> Mac Studio 화면에서 <span class="cw-small cw-muted">· 원격 GUI. 파일이 옮겨지지 않아요. 폰에서는 작게 보일 수 있어요</span></label><label class="tk-radio"><input type="radio" name="host" disabled> 이 기기에서 <span class="cw-small cw-muted">· 이 기기에 앱이 없어요(확인 안 됨)</span></label><label class="tk-radio"><input type="radio" name="host" disabled> 이 화면 안에서(임베드) <span class="cw-small cw-muted">· 지원 여부 미검증이라 선택할 수 없어요</span></label></div></fieldset>
      <ol class="tk-steps" aria-label="돌아오는 흐름"><li data-state="succeeded">${ic('circle-check')}<span class="tk-step__name">복사본을 열어요<span class="tk-step__detail">원본 v2는 그대로 둬요</span></span><span class="tk-step__time"></span></li><li data-state="running"><span class="tk-spinner tk-spinner--sm" aria-hidden="true"></span><span class="tk-step__name">편집 중<span class="tk-step__detail">앱에서 저장하면 알려드려요</span></span><span class="tk-step__time"></span></li><li data-state="queued"><span class="tk-step__mark" aria-hidden="true"></span><span class="tk-step__name">저장을 감지해 v3로 가져와요</span><span class="tk-step__time"></span></li><li data-state="queued"><span class="tk-step__mark" aria-hidden="true"></span><span class="tk-step__name">작업실로 돌아와 v2↔v3 비교</span><span class="tk-step__time"></span></li></ol>
      <ul class="tk-checks" aria-label="실패할 때"><li data-result="warn">${ic('warning')}<span><strong>저장했는데 안 가져와졌어요</strong> → 다시 가져오기 · 파일 직접 올리기</span></li><li data-result="warn">${ic('warning')}<span><strong>세션이 끊겼어요</strong> → 편집 내용은 Mac Studio에 남아 있을 수 있어요. 돌아와서 확인하기</span></li></ul>`, foot: `<button class="tk-button" data-close>닫기</button><button class="tk-button tk-button--primary" aria-disabled="true">Mac Studio에서 열기 (준비 안 됨)</button>` }),
    review: () => {
      const { scr, p } = parse();
      const sc = (SCENARIOS[scr] || []).map(([k, l]) => `<button class="tk-chip" type="button" aria-pressed="${(p.s || SCENARIOS[scr][0][0]) === k}" data-set="s=${k}&item=&run=&view=">${l}</button>`).join('');
      const scr2 = SCREENS.map((s) => `<button class="tk-chip" type="button" aria-pressed="${s === scr}" data-go="${s}">${TAB_LABEL[s]}</button>`).join('');
      return { title: '시안 보기 설정', body: `<div class="cw-review"><fieldset><legend>화면</legend><div class="tk-chips">${scr2}</div></fieldset><fieldset><legend>상태</legend><div class="tk-chips">${sc}</div></fieldset><fieldset><legend>테마</legend><div class="tk-chips"><button class="tk-chip" type="button" data-theme="light">라이트</button><button class="tk-chip" type="button" data-theme="dark">다크</button></div></fieldset><p class="cw-small cw-muted">주소(해시)에 상태가 담겨요. 새로고침·뒤로가기로 같은 화면이 복원돼요.</p></div>`, foot: `<button class="tk-button" data-close>닫기</button>` };
    },
  };
  function openDlg(id) {
    const o = DLG[id]; if (!o) return;
    const conf = o();
    closeDlg();
    const d = document.createElement('dialog');
    d.className = `tk-dialog cw-dlg${conf.wide ? ' tk-dialog--lg' : ' tk-dialog--sm'}`; d.setAttribute('aria-labelledby', 'dlg-t'); if (conf.modal) d.dataset.tkModal = '';
    d.innerHTML = `<div class="tk-dialog__header"><h2 class="tk-dialog__title" id="dlg-t">${conf.title}</h2><button class="tk-icon-button tk-icon-button--sm" type="button" data-close aria-label="닫기">${ic('x')}</button></div><div class="tk-dialog__body cw-stack">${conf.body}</div><div class="tk-dialog__footer">${conf.foot}</div>`;
    d.addEventListener('click', (e) => { if (e.target === d && !conf.modal) { d.close(); d.remove(); } });
    document.body.appendChild(d); d.showModal(); d.dataset.dlgId = id;
    const first = d.querySelector('[data-close]:not(.tk-icon-button)') || d.querySelector('button'); if (first && conf.modal) first.focus();
  }

  /* ---------- 이벤트 ---------- */
  const theme = (t) => { document.documentElement.dataset.theme = t; try { localStorage.setItem('tk-theme', t); } catch (e) { /* 저장 불가 */ } syncThemeIcon(); };
  function syncThemeIcon() { const b = $('[data-act="theme"]'); if (!b) return; const dark = document.documentElement.dataset.theme === 'dark' || (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches); b.innerHTML = ic(dark ? 'sun' : 'moon'); b.setAttribute('aria-label', dark ? '라이트 모드 전환' : '다크 모드 전환'); }
  const busyBtn = (b, ms, then) => { if (b.getAttribute('aria-busy')) return; b.setAttribute('aria-busy', 'true'); b.disabled = true; setTimeout(then, ms); };

  document.addEventListener('click', (e) => {
    const t = e.target.closest('[data-go],[data-set],[data-dlg],[data-act],[data-close],[data-fill],[data-mock],[data-theme],[data-reset-filters],[data-back]');
    if (!t) return;
    if (t.getAttribute('aria-disabled') === 'true') { e.preventDefault(); announce('지금은 쓸 수 없어요. 이유는 옆 설명을 확인하세요.'); return; }
    if (t.matches('a[data-mock]')) e.preventDefault();
    if (t.dataset.mock !== undefined) { if (t.dataset.mock) announce(`시안: "${t.dataset.mock}"은 실제 동작이 연결되지 않았어요.`); return; }
    if (t.dataset.back !== undefined) { history.back(); return; }
    if (t.dataset.close !== undefined) { closeDlg(); return; }
    if (t.dataset.theme) { theme(t.dataset.theme); return; }
    if (t.dataset.fill) { const i = $('#cw-input'); if (i) { i.value = t.dataset.fill; i.focus(); } return; }
    if (t.dataset.resetFilters !== undefined) { go({ q: '', type: '', role: '', tool: '', st: '', s: '', item: '', run: '' }); return; }
    if (t.dataset.dlg) { openDlg(t.dataset.dlg); return; }
    if (t.dataset.act) { act(t); return; }
    const patch = t.dataset.set ? setQ(t.dataset.set) : {};
    if (t.dataset.t) patch.t = t.dataset.t;
    if (t.dataset.go) {
      const dlg = $('dialog[open]'); if (dlg) { dlg.close(); dlg.remove(); }
      const target = t.dataset.go; const cur = parse();
      const base = target === cur.scr ? cur.p : Object.fromEntries(Object.entries(cur.p).filter(([k]) => ['p', 't', 'theme'].includes(k)));
      const p2 = { ...base, ...patch }; Object.keys(p2).forEach((k) => (p2[k] === '' || p2[k] == null) && delete p2[k]);
      const qs = new URLSearchParams(p2).toString(); const h = '#' + target + (qs ? '?' + qs : '');
      if (h === location.hash) render(); else location.hash = h;
      return;
    }
    if (t.dataset.set) { const dlg = $('dialog[open]:not([data-mobile])'); if (dlg) { dlg.close(); dlg.remove(); } const cur = parse(); if (cur.scr === 'tools' && patch.tool) patch.open = '1'; go(patch); }
  });
  function act(t) {
    const a = t.dataset.act;
    if (a === 'theme') { const dark = document.documentElement.dataset.theme === 'dark' || (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches); theme(dark ? 'light' : 'dark'); }
    else if (a === 'approve-do') busyBtn(t, 900, () => { closeDlg(); announce('v2를 최종 승인하고 저장했어요'); go({ s: 'approved', v: '2' }); });
    else if (a === 'approve-np') { if (!$('#np-ok').checked) { announce('확인 상자를 먼저 체크해 주세요'); $('#np-ok').focus(); return; } busyBtn(t, 600, () => { closeDlg(); announce('미리보기 없이 승인했어요 (기록됨)'); }); }
    else if (a === 'cancel-do') busyBtn(t, 700, () => { closeDlg(); announce('실행을 멈췄어요'); go({ s: 'cancelled' }); });
    else if (a === 'retry-run') busyBtn(t, 1200, () => { announce('다시 실행을 시작했어요'); go({ s: 'running' }); });
    else if (a === 'retry-hist') busyBtn(t, 1000, () => { announce('새 시도로 기록하고 다시 실행해요 (시안)'); t.disabled = false; t.removeAttribute('aria-busy'); });
    else if (a === 'unapprove') { announce('승인을 취소했어요. v2는 초안으로 돌아갔어요'); go({ s: 'compare', view: 'side' }); }
    else if (a === 'reconnect') busyBtn(t, 1000, () => { announce('다시 연결했어요'); go({ s: 'compare' }); });
    else if (a === 'test') { if (t.getAttribute('aria-disabled') === 'true') return; busyBtn(t, 1200, () => { t.disabled = false; t.removeAttribute('aria-busy'); const ul = $('#cw-testlog'); if (ul) ul.insertAdjacentHTML('afterbegin', `<li>${ic('circle-check')}<span class="cw-caps__body"><span>방금 테스트 <span class="cw-small cw-muted">(가상 결과)</span></span><span class="cw-small cw-muted">통과 · 응답 80ms</span></span></li>`); announce('테스트를 마쳤어요 (가상 결과)'); }); }
  }
  document.addEventListener('submit', (e) => {
    const f = e.target.closest('form[data-act="send"]'); if (!f) return; e.preventDefault();
    const btn = f.querySelector('.tk-composer__send'); const input = f.querySelector('textarea');
    if (btn.getAttribute('aria-disabled') === 'true') { announce('지금은 보낼 수 없어요. 이유는 입력창 아래에 있어요.'); return; }
    if (!input.value.trim()) { announce('요청을 먼저 적어 주세요'); input.focus(); return; }
    if (btn.getAttribute('aria-busy')) return;
    btn.setAttribute('aria-busy', 'true'); announce('요청을 보냈어요');
    setTimeout(() => go({ s: 'running' }), 400);
  });
  document.addEventListener('keydown', (e) => {
    const ta = e.target.closest && e.target.closest('.tk-composer__input');
    if (ta && e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229 && !matchMedia('(pointer: coarse)').matches) { e.preventDefault(); ta.form.requestSubmit(); }
    const tab = e.target.closest && e.target.closest('[role="tab"]');
    if (tab && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) {
      const tabs = $$('[role="tab"]', tab.parentElement); let i = tabs.indexOf(tab);
      i = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : (i + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      e.preventDefault(); tabs[i].focus(); tabs[i].click(); setTimeout(() => { const nt = $(`#tab-${tabs[i].dataset.go}`); if (nt) nt.focus(); }, 0);
    }
  });
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (el.matches('[data-ctx]')) { const k = el.dataset.ctx; const patch = { [k]: el.value }; if (k === 'p') { patch.t = ''; patch.item = ''; patch.run = ''; } go(patch); }
    else if (el.matches('select[data-filter]')) go({ [el.dataset.filter]: el.value === 'all' ? '' : el.value, item: '', run: '' });
    else if (el.matches('input[data-view]')) go({ view: el.value, s: 'compare' });
  });
  let qTimer;
  document.addEventListener('input', (e) => {
    const el = e.target; if (!el.matches('input[data-filter="q"]')) return;
    clearTimeout(qTimer); qTimer = setTimeout(() => { const pos = el.selectionStart; history.replaceState(null, '', href({ q: el.value })); render(); const n = $('input[data-filter="q"]'); if (n) { n.focus(); n.setSelectionRange(pos, pos); } }, 250);
  });
  window.addEventListener('hashchange', () => { const open = $('dialog[open][data-mobile]'); if (open) { open.close(); open.remove(); } render(); });
  render();
})();
