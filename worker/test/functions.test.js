// Verifies the Cloudflare Pages Function adapters (functions/api/*.js) wire the
// Pages onRequest signature to the worker's gate handlers and preserve gating.
import test from 'node:test';
import assert from 'node:assert/strict';
import { onRequestGet as grant } from '../../functions/api/grant.js';
import { onRequestGet as download } from '../../functions/api/download.js';
import { createTestEnv, paidSession } from './helpers.js';

test('Pages Function /api/grant grants a paid session and returns a download URL', async () => {
  const env = createTestEnv({ sessions: { cs_test_paid_1: paidSession() } });
  const res = await grant({
    request: new Request('https://example.com/api/grant?session_id=cs_test_paid_1'),
    env,
  });
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.ok(body.url.includes('/api/download'), 'grant should hand back a /api/download URL');
});

test('Pages Function /api/grant denies an unknown session (403)', async () => {
  const env = createTestEnv({ sessions: {} });
  const res = await grant({
    request: new Request('https://example.com/api/grant?session_id=cs_test_unknown'),
    env,
  });
  assert.equal(res.status, 403);
});

test('Pages Function /api/download rejects a request with no valid token (403)', async () => {
  const env = createTestEnv({ sessions: {} });
  const res = await download({
    request: new Request('https://example.com/api/download?product=forge-playbook&order=deadbeef&exp=1&sig=bad'),
    env,
  });
  assert.equal(res.status, 403);
});
