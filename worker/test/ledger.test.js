import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { appendSale } from '../src/ledger.js';
import { createFakeR2 } from './helpers/fakeR2.js';

function sha256Hex(value) {
  return createHash('sha256').update(value).digest('hex');
}

function testEnv() {
  return { LEDGER_BUCKET: createFakeR2() };
}

test('appendSale writes exactly one JSONL record with the expected schema', async () => {
  const env = testEnv();
  const sessionIdSha256 = sha256Hex('cs_test_paid_1');

  const result = await appendSale(env, {
    sessionIdSha256,
    product: 'forge-playbook',
    amount: 7900,
    timestamp: Date.parse('2026-07-23T20:15:03.512Z'),
  });

  assert.equal(result.written, true);
  assert.equal(env.LEDGER_BUCKET.store.size, 1);

  const stored = env.LEDGER_BUCKET.store.get(result.key);
  assert.ok(stored, 'expected an object at the returned key');
  assert.equal(stored.httpMetadata.contentType, 'application/x-ndjson');

  const lines = stored.body.split('\n').filter(Boolean);
  assert.equal(lines.length, 1, 'ledger object must contain exactly one JSONL line');

  const record = JSON.parse(lines[0]);
  assert.equal(record.ts, '2026-07-23T20:15:03.512Z');
  assert.equal(record.session_id_sha256, sessionIdSha256);
  assert.equal(record.product, 'forge-playbook');
  assert.equal(record.amount, 7900);
});

test('the ledger key never embeds the raw session id, only its sha256', async () => {
  const env = testEnv();
  const rawSessionId = 'cs_test_paid_1';
  const sessionIdSha256 = sha256Hex(rawSessionId);

  const { key } = await appendSale(env, { sessionIdSha256, product: 'forge-playbook', amount: 7900 });

  assert.ok(!key.includes(rawSessionId));
  assert.ok(key.startsWith(`ledger/${sessionIdSha256.slice(0, 2)}/`));
  assert.ok(key.endsWith(`${sessionIdSha256}.jsonl`));
});

test('a repeated call for the same session is a no-op, not a duplicate sale', async () => {
  const env = testEnv();
  const sessionIdSha256 = sha256Hex('cs_test_paid_1');

  const first = await appendSale(env, { sessionIdSha256, product: 'forge-playbook', amount: 7900 });
  const second = await appendSale(env, { sessionIdSha256, product: 'forge-playbook', amount: 7900 });
  const third = await appendSale(env, { sessionIdSha256, product: 'forge-playbook', amount: 7900 });

  assert.equal(first.written, true);
  assert.equal(second.written, false);
  assert.equal(third.written, false);
  assert.equal(env.LEDGER_BUCKET.store.size, 1, 'a retried/replayed grant must not double-count revenue');
});

test('two distinct sessions produce two distinct ledger records', async () => {
  const env = testEnv();
  const a = await appendSale(env, { sessionIdSha256: sha256Hex('cs_test_paid_1'), product: 'forge-playbook', amount: 7900 });
  const b = await appendSale(env, { sessionIdSha256: sha256Hex('cs_test_paid_2'), product: 'forge-playbook', amount: 7900 });

  assert.notEqual(a.key, b.key);
  assert.equal(env.LEDGER_BUCKET.store.size, 2);
});

test('rejects a sessionIdSha256 that is not a 64-char lowercase hex digest', async () => {
  const env = testEnv();
  await assert.rejects(
    () => appendSale(env, { sessionIdSha256: 'not-a-hash', product: 'forge-playbook', amount: 7900 }),
    /sha256 digest/
  );
  await assert.rejects(
    () => appendSale(env, { sessionIdSha256: 'cs_test_paid_1', product: 'forge-playbook', amount: 7900 }),
    /sha256 digest/,
    'a raw (unhashed) session id must be rejected, not silently accepted'
  );
});

test('fails closed when LEDGER_BUCKET is not configured, rather than falling back silently', async () => {
  await assert.rejects(
    () => appendSale({}, { sessionIdSha256: sha256Hex('cs_test_paid_1'), product: 'forge-playbook', amount: 7900 }),
    /LEDGER_BUCKET/
  );
});

test('a losing conditional put (object appears between head() and put()) is reported as not-written, not silently true', async () => {
  const inner = createFakeR2();
  const sessionIdSha256 = sha256Hex('cs_test_paid_1');
  const key = `ledger/${sessionIdSha256.slice(0, 2)}/${sessionIdSha256}.jsonl`;

  // Simulate a concurrent writer landing its object at this key after our
  // head() ran (so head() still reports "nothing here") but before our
  // put() lands — the exact race the conditional put guards against.
  await inner.put(key, 'existing-record\n', {});
  const racyBucket = {
    ...inner,
    async head() {
      return null;
    },
  };

  const result = await appendSale(
    { LEDGER_BUCKET: racyBucket },
    { sessionIdSha256, product: 'forge-playbook', amount: 7900 }
  );

  assert.equal(result.written, false, 'a losing conditional put must not be reported as written');
  assert.equal(
    inner.store.get(key).body,
    'existing-record\n',
    'the other writer\'s record must not be overwritten'
  );
});
