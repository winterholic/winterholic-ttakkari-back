const assert = require('node:assert/strict');
const { test } = require('node:test');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const modulePath = path.resolve(__dirname, '../src/ttakkari/agents/browser_phase_trace.cjs');

function run(code, input = '', traceTarget, enabled = true) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'browser-spans-'));
  const trace = traceTarget === 'directory' ? path.join(dir, 'directory')
    : traceTarget || path.join(dir, 'browser', 'phase-spans.jsonl');
  try {
    // Activation lives in this Run's writable outbox, never a shared setting.
    if (enabled) {
      fs.mkdirSync(path.dirname(trace), {recursive: true});
      fs.writeFileSync(trace + '.request', '');
      if (traceTarget === 'directory') fs.mkdirSync(trace);
    }
    const result = spawnSync(process.execPath, ['--require', modulePath, '-e', code], {
      input, encoding: 'utf8', env: { ...process.env, TTAKKARI_BROWSER_TRACE_FILE: trace },
    });
    const events = fs.existsSync(trace) && fs.statSync(trace).isFile()
      ? fs.readFileSync(trace, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse) : [];
    return { ...result, events };
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

test('records page phases and RPC boundaries without changing responses or storing content', () => {
  assert.ok(fs.existsSync(modulePath), 'browser phase instrumentation must exist');
  const code = `
    const assert = require('node:assert/strict');
    const { default: initPage } = require(${JSON.stringify(modulePath)});
    assert.equal(process.stdin.readableFlowing, null);
    process.stdin.resume();
    process.stdin.on('end', async () => {
      const marker = new Error('private error detail');
      const page = {
        async goto(url, opts) { assert.equal(this, page); assert.equal(opts.timeout, 3000); return 'same result'; },
        async waitForLoadState() { throw marker; },
        async ariaSnapshot() { return 'private snapshot'; },
        async ariaSnapshotJSON() { return { private: true }; },
      };
      await initPage({ page });
      await initPage({ page }); // no duplicate wrappers
      assert.equal(await page.goto('https://private.example', {timeout: 3000}), 'same result');
      await assert.rejects(page.waitForLoadState('load'), error => error === marker);
      assert.equal(await page.ariaSnapshot(), 'private snapshot');
      assert.deepEqual(await page.ariaSnapshotJSON(), {private: true});
      process.stdout.write('{"jsonrpc":"2.0","id":17,"result":{"secret":"untouched"}}\\n');
    });
  `;
  const input = JSON.stringify({ jsonrpc: '2.0', id: 17, method: 'tools/call',
    params: { name: 'browser_navigate', arguments: { url: 'https://private.example', password: 'private password' } } }) + '\n';
  const result = run(code, input);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, '{"jsonrpc":"2.0","id":17,"result":{"secret":"untouched"}}\n');
  const phases = result.events.filter(event => event.event === 'phase_end');
  assert.deepEqual(phases.map(event => event.phase), ['goto', 'load_state', 'snapshot', 'snapshot_json']);
  assert.equal(phases.filter(event => event.outcome === 'error').length, 1);
  for (const phase of phases) {
    assert.equal(phase.rpc_id, 17);
    assert.ok(phase.duration_ms >= 0);
  }
  assert.equal(result.events.filter(event => event.event === 'rpc_start').length, 1);
  assert.equal(result.events.filter(event => event.event === 'rpc_end').length, 1);
  const text = JSON.stringify(result.events);
  for (const sensitive of ['private.example', 'private password', 'private error detail', 'private snapshot', 'untouched'])
    assert.ok(!text.includes(sensitive), sensitive);
  const runtime = result.events.find(event => event.event === 'runtime');
  assert.equal(runtime.node, process.version);
  assert.match(runtime.instrumentation_sha256, /^[a-f0-9]{64}$/);
});

test('ambiguous concurrent requests are not attributed to one RPC', () => {
  assert.ok(fs.existsSync(modulePath), 'browser phase instrumentation must exist');
  const result = run(`
    process.stdin.resume();
    process.stdin.on('end', async () => {
      const page = { async goto() {} };
      await require(${JSON.stringify(modulePath)}).default({page});
      await page.goto();
      process.stdout.write('{"jsonrpc":"2.0","id":"b","result":{}}\\n');
      process.stdout.write('{"jsonrpc":"2.0","id":"a","error":{"code":-1,"message":"private"}}\\n');
    });
  `, ['a', 'b'].map(id => JSON.stringify({jsonrpc:'2.0',id,method:'tools/call',params:{name:'browser_navigate'}})).join('\n') + '\n');
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.events.find(event => event.event === 'phase_end').rpc_id, null);
  assert.deepEqual(result.events.filter(event => event.event === 'rpc_end').map(event => event.rpc_id), ['b', 'a']);
});

test('trace write failure leaves the MCP response intact and reports one diagnostic', () => {
  assert.ok(fs.existsSync(modulePath), 'browser phase instrumentation must exist');
  const result = run(`process.stdout.write('{"jsonrpc":"2.0","id":1,"result":{}}\\n');`, '', 'directory');
  assert.equal(result.status, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).id, 1);
  assert.equal(result.stderr.split('browser phase trace disabled').length - 1, 1);
});

test('ordinary browser sessions do not record traces without a per-Run marker', () => {
  const result = run(`process.stdout.write('{"jsonrpc":"2.0","id":1,"result":{}}\\n');`, '', undefined, false);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.events.length, 0);
  assert.equal(result.stderr, '');
});
