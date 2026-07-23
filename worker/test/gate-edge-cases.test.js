import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/index.js';
import { registerEntitlementVerifier } from '../src/entitlements.js';
import { createTestEnv, paidSession } from './helpers.js';

// Covers gate behavior that grant.test.js / download.test.js don't exercise:
// non-GET methods, unknown routes, a verifier that throws, a missing R2
// object, and a requested product that doesn't match the paid product.

test('non-GET request to the grant route is denied with 405', async () => {
  const env = createTestEnv({ sessions: { cs_test_paid_1: paidSession() } });
  const res = await worker.fetch(
    new Request('https://example.com/api/grant?session_id=cs_test_paid_1', { method: 'POST' }),
    env
  );
  assert.equal(res.status, 405);
});

test('non-GET request to the download route is denied with 405', async () => {
  const env = createTestEnv();
  const res = await worker.fetch(
    new Request('https://example.com/api/download?product=forge-playbook', { method: 'POST' }),
    env
  );
  assert.equal(res.status, 405);
});

test('unknown route is denied with 404', async () => {
  const env = createTestEnv();
  const res = await worker.fetch(new Request('https://example.com/api/nope'), env);
  assert.equal(res.status, 404);
});

test('a verifier that throws is denied with 502, not treated as granted', async () => {
  registerEntitlementVerifier('broken', async () => {
    throw new Error('upstream exploded');
  });
  const env = createTestEnv();
  const res = await worker.fetch(
    new Request('https://example.com/api/grant?type=broken'),
    env
  );
  assert.equal(res.status, 502);
});

test('a valid token for an artifact missing from R2 is denied with 404, not served', async () => {
  const env = createTestEnv({ sessions: { cs_test_paid_1: paidSession() } });
  // Note: no PLAYBOOK_BUCKET.put — the artifact was never uploaded.

  const grantRes = await worker.fetch(
    new Request('https://example.com/api/grant?session_id=cs_test_paid_1'),
    env
  );
  const { url } = await grantRes.json();

  const downloadRes = await worker.fetch(new Request(url), env);
  assert.equal(downloadRes.status, 404);
});

test('a requested product that does not match the paid product is denied with 403', async () => {
  const env = createTestEnv({ sessions: { cs_test_paid_1: paidSession() } });
  const res = await worker.fetch(
    new Request(
      'https://example.com/api/grant?session_id=cs_test_paid_1&product=some-other-product'
    ),
    env
  );
  assert.equal(res.status, 403);
});
