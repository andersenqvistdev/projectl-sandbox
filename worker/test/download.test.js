import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/index.js';
import { createTestEnv, paidSession, readSampleAsset } from './helpers.js';
import { signDownloadToken } from '../src/signing.js';

test('unauthenticated direct artifact route is denied with 403 (no token)', async () => {
  const env = createTestEnv();
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.epub', 'FAKE ARTIFACT BYTES');

  const res = await worker.fetch(
    new Request('https://example.com/api/download?product=forge-playbook'),
    env
  );
  assert.equal(res.status, 403);
});

test('direct artifact route with a forged signature is denied with 403', async () => {
  const env = createTestEnv();
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.epub', 'FAKE ARTIFACT BYTES');

  const exp = Math.floor(Date.now() / 1000) + 3600;
  const res = await worker.fetch(
    new Request(
      `https://example.com/api/download?product=forge-playbook&exp=${exp}&sig=not-a-real-signature`
    ),
    env
  );
  assert.equal(res.status, 403);
});

test('expired signed token is denied with 403', async () => {
  const env = createTestEnv();
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.epub', 'FAKE ARTIFACT BYTES');

  const token = await signDownloadToken('forge-playbook', env, -10); // already expired
  const res = await worker.fetch(
    new Request(
      `https://example.com/api/download?product=${token.product}&exp=${token.exp}&sig=${token.sig}`
    ),
    env
  );
  assert.equal(res.status, 403);
});

test('a validly signed token (as issued by /api/grant) downloads a watermarked copy, never the raw artifact', async () => {
  const env = createTestEnv({ sessions: { cs_test_paid_1: paidSession() } });
  const original = readSampleAsset('forge-playbook-sample.epub');
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.epub', original, {
    httpMetadata: { contentType: 'application/epub+zip' },
  });

  const grantRes = await worker.fetch(
    new Request('https://example.com/api/grant?session_id=cs_test_paid_1'),
    env
  );
  const { url } = await grantRes.json();

  const downloadRes = await worker.fetch(new Request(url), env);
  assert.equal(downloadRes.status, 200);
  const bytes = new Uint8Array(await downloadRes.arrayBuffer());

  // The raw un-watermarked artifact must never be what comes back.
  assert.notEqual(bytes.length, original.length);
});

test('token signed with a different signing secret is rejected', async () => {
  const env = createTestEnv();
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.epub', 'FAKE ARTIFACT BYTES');

  const foreignEnv = { ...env, SIGNING_SECRET: 'a-different-secret' };
  const token = await signDownloadToken('forge-playbook', foreignEnv);

  const res = await worker.fetch(
    new Request(
      `https://example.com/api/download?product=${token.product}&exp=${token.exp}&sig=${token.sig}`
    ),
    env
  );
  assert.equal(res.status, 403);
});
