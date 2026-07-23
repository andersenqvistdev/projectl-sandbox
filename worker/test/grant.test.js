import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/index.js';
import { createTestEnv, paidSession, unpaidSession } from './helpers.js';

test('paid session grants and returns a time-limited download URL', async () => {
  const env = createTestEnv({ sessions: { cs_test_paid_1: paidSession() } });
  const res = await worker.fetch(
    new Request('https://example.com/api/grant?session_id=cs_test_paid_1'),
    env
  );
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.ok(body.url.startsWith('https://example.com/api/download?'));
  assert.ok(body.expires_at);
  const exp = new URL(body.url).searchParams.get('exp');
  const ttl = Number(exp) - Math.floor(Date.now() / 1000);
  // Short-lived by default (env.DOWNLOAD_TTL_SECONDS), never the 24h hard cap.
  assert.ok(ttl > 0 && ttl <= 15 * 60, `ttl ${ttl} should be a short default, not the 24h max`);
});

test('unpaid session is denied with 403', async () => {
  const env = createTestEnv({ sessions: { cs_test_unpaid_1: unpaidSession() } });
  const res = await worker.fetch(
    new Request('https://example.com/api/grant?session_id=cs_test_unpaid_1'),
    env
  );
  assert.equal(res.status, 403);
});

test('unknown session is denied with 403', async () => {
  const env = createTestEnv({ sessions: {} });
  const res = await worker.fetch(
    new Request('https://example.com/api/grant?session_id=cs_test_does_not_exist'),
    env
  );
  assert.equal(res.status, 403);
});

test('missing session_id is denied with 403', async () => {
  const env = createTestEnv({ sessions: {} });
  const res = await worker.fetch(new Request('https://example.com/api/grant'), env);
  assert.equal(res.status, 403);
});

test('replay beyond N grants is denied', async () => {
  const env = createTestEnv({
    sessions: { cs_test_paid_1: paidSession() },
    MAX_GRANTS_PER_SESSION: '2',
  });
  const req = () =>
    worker.fetch(new Request('https://example.com/api/grant?session_id=cs_test_paid_1'), env);

  assert.equal((await req()).status, 200);
  assert.equal((await req()).status, 200);
  const third = await req();
  assert.equal(third.status, 403);
});

test('ledger is written exactly once per session across repeated grants', async () => {
  const env = createTestEnv({
    sessions: { cs_test_paid_1: paidSession() },
    MAX_GRANTS_PER_SESSION: '5',
  });
  const req = () =>
    worker.fetch(new Request('https://example.com/api/grant?session_id=cs_test_paid_1'), env);

  await req();
  await req();
  await req();

  const ledgerKeys = [...env.PLAYBOOK_BUCKET.store.keys()].filter((k) => k.startsWith('ledger/'));
  assert.equal(ledgerKeys.length, 1, 'expected exactly one ledger object for the session');

  const entry = env.PLAYBOOK_BUCKET.store.get(ledgerKeys[0]);
  const record = JSON.parse(entry.body.trim());
  assert.equal(record.product, 'forge-playbook');
  assert.equal(record.amount, 7900);
  assert.equal(typeof record.session_id_sha256, 'string');
  assert.equal(record.session_id_sha256.length, 64);
  assert.ok(record.ts);
});

test('ledger never stores the raw session id, only its sha256', async () => {
  const env = createTestEnv({ sessions: { cs_test_paid_1: paidSession() } });
  await worker.fetch(new Request('https://example.com/api/grant?session_id=cs_test_paid_1'), env);

  const ledgerKeys = [...env.PLAYBOOK_BUCKET.store.keys()].filter((k) => k.startsWith('ledger/'));
  const entry = env.PLAYBOOK_BUCKET.store.get(ledgerKeys[0]);
  assert.ok(!entry.body.includes('cs_test_paid_1'));
});

test('unknown entitlement type is denied with 403', async () => {
  const env = createTestEnv({ sessions: {} });
  const res = await worker.fetch(
    new Request('https://example.com/api/grant?type=does-not-exist&session_id=x'),
    env
  );
  assert.equal(res.status, 403);
});
