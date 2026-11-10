// Loaded by Node --require and Playwright MCP --init-page. No page content is logged.
// Timings describe this instrumented process, not a historical browser invocation.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { createRequire } = require('node:module');
const { StringDecoder } = require('node:string_decoder');

const traceFile = process.env.TTAKKARI_BROWSER_TRACE_FILE;
const session = crypto.randomUUID();
const inFlight = new Map();
const wrappedPages = new WeakSet();
let disabled = !traceFile;
let active = false;
let observerNs = 0n;
const now = () => process.hrtime.bigint();
const ms = ns => Number(ns) / 1e6;
const hash = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');

function emit(event) {
  if (disabled || !active) return;
  const start = now();
  try {
    fs.appendFileSync(traceFile, JSON.stringify({ session, unix_ms: Date.now(),
      monotonic_ms: ms(start), trace_write_ms_before: ms(observerNs), ...event }) + '\n', { mode: 0o600 });
  } catch (error) {
    disabled = true;
    active = false;
    process.stderr.write(`browser phase trace disabled: ${error.code || 'write_error'}\n`);
  } finally {
    observerNs += now() - start;
  }
}

function runtimeIdentity() {
  const identity = { event: 'runtime', node: process.version, instrumentation_sha256: hash(__filename),
    prepage_instrumentation_sha256: hash(path.join(__dirname, 'browser_prepage_trace.cjs')),
    backend_revision: process.env.TTAKKARI_BROWSER_BACKEND_REVISION || null,
    mcp_version: null, core_version: null, cli_sha256: null, core_sha256: null };
  try {
    // Resolve from the CLI actually launched, never from a different checkout.
    const cli = path.resolve(process.argv[1]);
    const releaseName = path.basename(path.resolve(path.dirname(cli), '../../../..'));
    if (/^[a-f0-9]{40}$/.test(releaseName)) identity.backend_revision = releaseName;
    const requireCli = createRequire(cli);
    identity.cli_sha256 = hash(cli);
    identity.mcp_version = requireCli('@playwright/mcp/package.json').version;
    identity.core_version = requireCli('playwright-core/package.json').version;
    identity.core_sha256 = hash(requireCli.resolve('playwright-core/lib/coreBundle'));
  } catch {
    // Missing fields remain null; a package pin is not an observed runtime identity.
  }
  return identity;
}

function observeMessage(message, direction) {
  if (!message || message.jsonrpc !== '2.0' || !Object.hasOwn(message, 'id')) return;
  const id = message.id;
  if (!['number', 'string'].includes(typeof id)) return;
  const key = JSON.stringify(id);
  if (direction === 'input' && message.method === 'tools/call') {
    const tool = message.params?.name;
    if (typeof tool !== 'string' || !/^browser_[a-z_]+$/.test(tool)) return;
    const started = now();
    inFlight.set(key, { id, tool, started });
    emit({ event: 'rpc_start', rpc_id: id, tool, boundary: 'stdin_data' });
  } else if (direction === 'output' && inFlight.has(key)) {
    const request = inFlight.get(key);
    inFlight.delete(key);
    emit({ event: 'rpc_end', rpc_id: id, tool: request.tool,
      duration_ms: ms(now() - request.started), boundary: 'stdout_write_entry',
      outcome: message.error || message.result?.isError ? 'error' : 'ok' });
  }
}

function lineObserver(direction) {
  const decoder = new StringDecoder('utf8');
  let pending = '';
  let discarding = false;
  // Bounded observational copies; an oversized frame still passes through untouched.
  const limit = 8 * 1024 * 1024;
  return chunk => {
    if (direction === 'input') tryActivate();
    if (disabled || !active) return;
    const text = typeof chunk === 'string' ? chunk : decoder.write(chunk);
    for (const part of text.split(/(?<=\n)/)) {
      const complete = part.endsWith('\n');
      if (!discarding) {
        if (pending.length + part.length > limit) {
          pending = '';
          discarding = true;
          emit({ event: 'observation_gap', direction, reason: 'frame_size_limit' });
        } else pending += part;
      }
      if (complete) {
        if (!discarding) {
          try { observeMessage(JSON.parse(pending), direction); } catch { /* Non-RPC output. */ }
        }
        pending = '';
        discarding = false;
      }
    }
  };
}

function tryActivate() {
  if (disabled || active || !fs.existsSync(traceFile + '.request')) return;
  try { fs.mkdirSync(path.dirname(traceFile), { recursive: true }); active = true; }
  catch (error) {
    disabled = true;
    process.stderr.write(`browser phase trace disabled: ${error.code || 'mkdir_error'}\n`);
  }
  if (active) {
    emit(runtimeIdentity());
    require('./browser_prepage_trace.cjs').installPrepageTrace({
      cli: process.argv[1], emit, isActive: () => active && !disabled,
      rpcId: () => inFlight.size === 1 ? [...inFlight.values()][0].id : null,
    });
  }
}

if (!disabled) {
  tryActivate();
  const input = lineObserver('input');
  const output = lineObserver('output');
  // Observe existing data emission without adding a listener that starts stdin flowing.
  const originalEmit = process.stdin.emit;
  process.stdin.emit = function(event, ...args) {
    if (event === 'data') input(args[0]);
    return Reflect.apply(originalEmit, this, [event, ...args]);
  };
  const originalWrite = process.stdout.write;
  process.stdout.write = function(chunk, ...args) {
    output(chunk);
    return Reflect.apply(originalWrite, this, [chunk, ...args]);
  };
}

async function initPage({ page }) {
  // Create the .request marker before the first browser call of a fresh Run.
  if (disabled || !active || wrappedPages.has(page)) return;
  wrappedPages.add(page);
  const phases = { goto: 'goto', waitForLoadState: 'load_state',
    ariaSnapshot: 'snapshot', ariaSnapshotJSON: 'snapshot_json' };
  const observed = [];
  for (const [method, phase] of Object.entries(phases)) {
    if (typeof page[method] !== 'function') continue;
    const original = page[method];
    page[method] = async function(...args) {
      const request = inFlight.size === 1 ? [...inFlight.values()][0] : null;
      const rpc_id = request?.id ?? null;
      const start = now();
      emit({ event: 'phase_start', phase, rpc_id });
      let outcome = 'ok';
      try { return await Reflect.apply(original, this, args); }
      catch (error) { outcome = 'error'; throw error; }
      finally { emit({ event: 'phase_end', phase, rpc_id, outcome, duration_ms: ms(now() - start) }); }
    };
    observed.push(phase);
  }
  emit({ event: 'page_instrumented', phases: observed,
    unobserved: ['internal_settle', 'artifact_write', 'client_receipt'],
    prepage_coverage: 'see prepage_coverage events; missing phases remain unmeasured' });
}

module.exports = { default: initPage };
