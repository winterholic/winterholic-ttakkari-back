// Optional observers for the coreBundle loaded by the actual CLI. Never loads core.
'use strict';
const Module = require('node:module');
const { createRequire } = Module;
const wrappedCores = new WeakSet();
const wrappedMethods = new WeakMap();

function installPrepageTrace({ cli, emit, isActive, rpcId }) {
  // Only fixed phase names and outcomes enter the trace. Do not log arguments,
  // endpoints, context options, return values, module paths, or error messages.
  const record = row => { try { emit(row); } catch { /* Observer must not break MCP. */ } };
  const safely = action => {
    try { action(); }
    catch { record({ event: 'prepage_coverage', target: 'observer', phases: [], missing: ['observer_install'] }); }
  };
  const now = () => process.hrtime.bigint();

  function wrap(target, method, phase, onResult) {
    if (!target || typeof target[method] !== 'function') return false;
    let methods = wrappedMethods.get(target);
    if (!methods) wrappedMethods.set(target, methods = new Set());
    if (methods.has(method)) return true;
    const original = target[method];
    const observed = function(...args) {
      if (!isActive()) return Reflect.apply(original, this, args);
      const rpc_id = rpcId();
      const start = now();
      record({ event: 'phase_start', phase, rpc_id });
      const end = outcome => record({ event: 'phase_end', phase, rpc_id, outcome,
        duration_ms: Number(now() - start) / 1e6 });
      let result;
      try { result = Reflect.apply(original, this, args); }
      catch (error) { end('error'); throw error; }
      const success = value => {
        end('ok');
        if (onResult) safely(() => onResult(value));
      };
      // Keep the original Promise rather than replacing it with an async wrapper.
      // Both observers settle normally; original rejection/error identity survives.
      if (result instanceof Promise) result.then(success, () => end('error'));
      else success(result);
      return result;
    };
    try {
      const descriptor = Object.getOwnPropertyDescriptor(target, method);
      Object.defineProperty(target, method, {
        configurable: descriptor?.configurable ?? true,
        enumerable: descriptor?.enumerable ?? false,
        writable: descriptor?.writable ?? true,
        value: observed,
      });
      methods.add(method);
      return true;
    } catch { return false; }
  }

  function observeMethods(target, label, definitions) {
    const phases = [], missing = [];
    for (const [method, phase, onResult] of definitions) {
      if (wrap(target, method, phase, onResult)) phases.push(phase);
      else missing.push(phase);
    }
    record({ event: 'prepage_coverage', target: label, phases, missing });
  }

  const contextReady = context => observeMethods(context, 'context', [['newPage', 'browser_page']]);
  const browserReady = browser => observeMethods(browser, 'browser', [
    ['bind', 'browser_bind'], ['newContext', 'browser_context', contextReady],
    ['newPage', 'browser_page'],
  ]);

  function observeCore(core, loadState) {
    if (!core || typeof core !== 'object' || wrappedCores.has(core)) return;
    wrappedCores.add(core);
    record({ event: 'prepage_core', load_state: loadState });
    observeMethods(core.inprocess?.playwright?.chromium, 'chromium', [
      ['launch', 'browser_launch', browserReady],
      ['launchPersistentContext', 'browser_launch_persistent_context', contextReady],
      ['connect', 'browser_connect', browserReady],
      ['connectOverCDP', 'browser_connect_cdp', browserReady],
    ]);
    observeMethods(core.tools?.BrowserBackend?.prototype, 'backend', [['initialize', 'backend_initialize']]);
    observeMethods(core.tools?.Tab?.prototype, 'tab', [['_initialize', 'tab_initialize']]);
  }

  let corePath;
  try { corePath = createRequire(cli).resolve('playwright-core/lib/coreBundle'); }
  catch {
    record({ event: 'prepage_coverage', target: 'core', phases: [], missing: ['core_bundle'] });
    return;
  }
  // Activation can happen after MCP initialization but before its first browser
  // call. Cache inspection must never reload core or move startup out of the RPC.
  if (Module._cache[corePath]?.loaded) {
    safely(() => observeCore(Module._cache[corePath].exports, 'cached'));
    return;
  }
  const originalLoad = Module._load;
  function observeLoad(...args) {
    const result = Reflect.apply(originalLoad, this, args);
    const loaded = Module._cache[corePath];
    if (loaded?.loaded && loaded.exports === result) {
      // One natural load is enough. Do not overwrite a later third-party hook.
      if (Module._load === observeLoad) Module._load = originalLoad;
      if (isActive()) safely(() => observeCore(result, 'natural'));
    }
    return result;
  }
  Module._load = observeLoad;
}

module.exports = { installPrepageTrace };
