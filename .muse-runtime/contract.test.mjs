import test from 'node:test';
import assert from 'node:assert/strict';
import { assertReady, verifySession, sessionParams } from './contract.mjs';

const ready = { schema: {version: 1}, sandboxAvailability: 'available', sessionDurability: 'durable', grantedCapabilities: ['sessionMcp'] };
test('API credentials never qualify as subscription login', () => {
  for (const state of ['envKey','apiKey','loggedOut','futureLane']) {
    assert.throws(() => assertReady(ready, {state}, true), /subscription/);
  }
  assert.doesNotThrow(() => assertReady(ready, {state:'accountLogin'}, true));
});
test('missing sandbox observation fails closed', () => {
  assert.throws(() => assertReady({...ready, sandboxAvailability:undefined}, {state:'accountLogin'}, false), /sandbox/);
  assert.throws(() => assertReady({...ready, sandboxAvailability:'unavailable'}, {state:'accountLogin'}, false), /sandbox/);
});
test('MCP grant is required before supplying MCP credentials', () => {
  assert.throws(() => assertReady({...ready, grantedCapabilities:[]}, {state:'accountLogin'}, true), /sessionMcp/);
});
test('resume validates session identity and canonical workspace', () => {
  const spec={cwd:'/tmp/ws',engine_session_id:'saved',model:'muse-wonder-1.0'};
  assert.throws(() => verifySession({sessionId:'different',workspaceRoot:'/tmp/ws',modelId:spec.model},spec), /identity/);
  assert.throws(() => verifySession({sessionId:'saved',workspaceRoot:'/else',modelId:spec.model},spec), /workspace/);
  assert.throws(() => verifySession({sessionId:'saved',workspaceRoot:'/tmp/ws',modelId:'other'},spec), /model/);
});
test('extra roots are explicit and MCP uses MSP transport spelling', () => {
  const p=sessionParams({cwd:'/tmp/ws',write_roots:['/tmp/ws','/tmp/out'],mcp_servers:{test:{command:'python',args:['server.py'],env:{TOKEN:'fixture'}}},model:'muse-wonder-1.0'});
  assert.deepEqual(p.workspaceRoots,['/tmp/ws','/tmp/out']);
  assert.equal(p.config.mcpServers.test.transport,'stdio');
  assert.equal(p.config.mcpServers.test.mode,'required');
});
