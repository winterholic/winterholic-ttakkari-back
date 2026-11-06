import test from 'node:test';
import assert from 'node:assert/strict';
import { launchStudioBrowser } from '../../src/ttakkari/games/studio_kit/ttakkari-game-studio/lib/play.mjs';

test('blocked preflight never calls the browser launcher', async () => {
  let calls = 0;
  await assert.rejects(launchStudioBrowser({ launch: async () => { calls++; } }, () => ({ status: 'blocked' })), /preflight/);
  assert.equal(calls, 0);
});

test('unknown preflight never calls the browser launcher', async () => {
  let calls = 0;
  await assert.rejects(launchStudioBrowser({ launch: async () => { calls++; } }, () => ({ status: 'unknown' })), /preflight/);
  assert.equal(calls, 0);
});

test('browser launch failure is reported after one attempt', async () => {
  let calls = 0;
  await assert.rejects(launchStudioBrowser({ launch: async () => { calls++; throw new Error('permission denied'); } }, () => ({ status: 'permissions_allowed' })), /permission denied/);
  assert.equal(calls, 1);
});
