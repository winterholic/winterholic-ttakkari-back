const assert = require('node:assert/strict');
const { test } = require('node:test');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');

const preload = path.resolve(__dirname, '../src/ttakkari/agents/browser_phase_trace.cjs');
const fakeCore = `
  globalThis.coreLoads = (globalThis.coreLoads || 0) + 1;
  const marker = new Error('private failure');
  const context = {
    newPage() { assert.equal(this, context); return Promise.resolve({privatePage: true}); }
  };
  const browser = {
    bind(options) { assert.equal(this, browser); assert.equal(options.private, true); return Promise.resolve('private endpoint'); },
    newContext() { assert.equal(this, browser); return Promise.resolve(context); },
    newPage() { assert.equal(this, browser); return Promise.resolve({privatePage: true}); }
  };
  const chromium = {
    launch(options) {
      assert.equal(this, chromium); assert.equal(options.private, true);
      const promise = Promise.resolve(browser); globalThis.launchPromise = promise; return promise;
    },
    connect() { return Promise.reject(marker); },
    connectOverCDP() { throw marker; },
    launchPersistentContext() { return Promise.resolve(context); }
  };
  class Backend {
    initialize(options) { assert.equal(this.private, true); assert.equal(options.private, true); return Promise.resolve('private initialized'); }
  }
  class Tab {
    _initialize() { assert.equal(this.private, true); return Promise.resolve('private tab'); }
  }
  module.exports = {inprocess: {playwright: {chromium}}, tools: {BrowserBackend: Backend, Tab}, marker};
`;

// Only external I/O is doubled: Node loads a local package naturally, and its methods
// return ordinary promises/objects/errors. No real browser, model, or network starts.
function run(body, { marker = true, core = fakeCore } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'browser-prepage-'));
  const trace = path.join(dir, 'outbox', 'browser', 'phase-spans.jsonl');
  const cli = path.join(dir, 'cli.cjs');
  const pkg = path.join(dir, 'node_modules', 'playwright-core');
  const corePath = path.join(pkg, 'lib', 'coreBundle.js');
  try {
    fs.mkdirSync(path.dirname(trace), { recursive: true });
    if (marker) fs.writeFileSync(trace + '.request', '');
    fs.mkdirSync(path.dirname(corePath), { recursive: true });
    fs.writeFileSync(path.join(pkg, 'package.json'), JSON.stringify({
      name: 'playwright-core', version: 'test', exports: { './lib/coreBundle': './lib/coreBundle.js' }
    }));
    fs.writeFileSync(corePath, "const assert = require('node:assert/strict');\n" + core);
    fs.writeFileSync(cli, `
      const assert = require('node:assert/strict');
      const fs = require('node:fs');
      const Module = require('node:module');
      const corePath = ${JSON.stringify(corePath)};
      const trace = ${JSON.stringify(trace)};
      const rpc = id => process.stdin.emit('data', JSON.stringify({jsonrpc:'2.0', id, method:'tools/call', params:{name:'browser_navigate'}}) + '\\n');
      const finish = id => process.stdout.write(JSON.stringify({jsonrpc:'2.0', id, result:{private:'unchanged'}}) + '\\n');
      (async () => { ${body} })().catch(e => { console.error(e); process.exitCode = 1; });
    `);
    const result = spawnSync(process.execPath, ['--require', preload, cli], {
      encoding: 'utf8', env: { ...process.env, TTAKKARI_BROWSER_TRACE_FILE: trace },
    });
    const events = fs.existsSync(trace)
      ? fs.readFileSync(trace, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse) : [];
    assert.equal(result.status, 0, result.stderr);
    return { ...result, events };
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

test('pre-page spans start on natural core loading and preserve values, promises, errors, and this', () => {
  const result = run(`
    assert.equal(require.cache[corePath], undefined, 'preload must not eagerly load core');
    rpc(77);
    const core = require('playwright-core/lib/coreBundle');
    assert.equal(globalThis.coreLoads, 1);
    const chromium = core.inprocess.playwright.chromium;
    const promise = chromium.launch({private:true});
    assert.equal(promise, globalThis.launchPromise, 'original promise identity');
    const browser = await promise;
    assert.equal(await browser.bind({private:true}), 'private endpoint');
    const context = await browser.newContext();
    assert.deepEqual(await context.newPage(), {privatePage:true});
    assert.deepEqual(await browser.newPage(), {privatePage:true});
    const persistent = await chromium.launchPersistentContext();
    assert.equal(persistent, context);
    await assert.rejects(chromium.connect(), error => error === core.marker);
    assert.throws(() => chromium.connectOverCDP(), error => error === core.marker);
    const backend = new core.tools.BrowserBackend(); backend.private = true;
    assert.equal(await backend.initialize({private:true}), 'private initialized');
    const tab = new core.tools.Tab(); tab.private = true;
    assert.equal(await tab._initialize(), 'private tab');
    finish(77);
  `);
  const ends = result.events.filter(e => e.event === 'phase_end');
  assert.deepEqual(ends.map(e => e.phase).sort(), [
    'browser_launch', 'browser_bind', 'browser_context', 'browser_page', 'browser_page',
    'browser_launch_persistent_context', 'browser_connect', 'browser_connect_cdp',
    'backend_initialize', 'tab_initialize'
  ].sort());
  assert.equal(ends.filter(e => e.outcome === 'error').length, 2);
  for (const e of ends) { assert.equal(e.rpc_id, 77); assert.ok(e.duration_ms >= 0); }
  for (const secret of ['private endpoint', 'private failure', 'private initialized', 'private tab'])
    assert.ok(!JSON.stringify(result.events).includes(secret));
  assert.equal(JSON.parse(result.stdout).result.private, 'unchanged');
  const runtime = result.events.find(e => e.event === 'runtime');
  assert.equal(runtime.prepage_instrumentation_sha256,
    crypto.createHash('sha256').update(fs.readFileSync(path.resolve(path.dirname(preload), 'browser_prepage_trace.cjs'))).digest('hex'));
});

test('a marker added after core loading patches cached exports without reloading or double wrapping', () => {
  const result = run(`
    const core = require('playwright-core/lib/coreBundle');
    const original = core.inprocess.playwright.chromium.launch;
    const loader = Module._load;
    fs.writeFileSync(trace + '.request', '');
    rpc(81);
    assert.notEqual(core.inprocess.playwright.chromium.launch, original);
    assert.equal(Module._load, loader, 'cached core does not need a loader hook');
    const wrapped = core.inprocess.playwright.chromium.launch;
    assert.equal(require('playwright-core/lib/coreBundle'), core);
    assert.equal(core.inprocess.playwright.chromium.launch, wrapped);
    await core.inprocess.playwright.chromium.launch({private:true});
    finish(81);
    rpc(82);
    await core.inprocess.playwright.chromium.launch({private:true});
    finish(82);
    assert.equal(globalThis.coreLoads, 1);
  `, { marker: false });
  assert.deepEqual(result.events.filter(e => e.event === 'phase_end').map(e => e.rpc_id), [81, 82]);
});

test('without a marker ordinary sessions leave core methods and module loading alone', () => {
  const result = run(`
    const loader = Module._load;
    assert.equal(require.cache[corePath], undefined);
    rpc(3);
    const core = require('playwright-core/lib/coreBundle');
    const chromium = core.inprocess.playwright.chromium;
    assert.equal(chromium.launch.name, 'launch');
    await chromium.launch({private:true});
    assert.equal(Module._load, loader);
    finish(3);
  `, { marker: false });
  assert.equal(result.events.length, 0);
  assert.equal(result.stderr, '');
});

test('pre-page spans keep ambiguous RPC attribution null', () => {
  const result = run(`
    rpc(4); rpc(5);
    await require('playwright-core/lib/coreBundle').inprocess.playwright.chromium.launch({private:true});
    finish(4); finish(5);
  `);
  const ends = result.events.filter(e => e.event === 'phase_end');
  assert.equal(ends.length, 1);
  assert.equal(ends[0].rpc_id, null);
});

test('unsupported core exports produce an explicit coverage gap without changing module exports', () => {
  const result = run(`
    rpc(9);
    assert.deepEqual(require('playwright-core/lib/coreBundle'), {unsupported:true});
    finish(9);
  `, { core: 'module.exports = {unsupported:true};' });
  assert.ok(result.events.some(e => e.event === 'prepage_coverage' && e.missing.length > 0));
  assert.equal(result.events.filter(e => e.event === 'phase_end').length, 0);
});
