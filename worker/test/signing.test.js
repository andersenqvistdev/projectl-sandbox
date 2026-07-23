import { test } from 'node:test';
import assert from 'node:assert/strict';
import { signPath, verifySignedPath } from '../src/signing.js';

const SECRET = 'test-signing-secret';

test('a freshly signed path verifies successfully', async () => {
  const { exp, sig } = await signPath(SECRET, '/content/forge-playbook', 300);
  const ok = await verifySignedPath(SECRET, '/content/forge-playbook', exp, sig);
  assert.equal(ok, true);
});

test('expiry is set ttlSeconds in the future', async () => {
  const before = Date.now();
  const { exp } = await signPath(SECRET, '/content/forge-playbook', 60);
  const after = Date.now();
  assert.ok(exp >= before + 60_000);
  assert.ok(exp <= after + 60_000);
});

test('a tampered path fails verification', async () => {
  const { exp, sig } = await signPath(SECRET, '/content/forge-playbook', 300);
  const ok = await verifySignedPath(SECRET, '/content/some-other-book', exp, sig);
  assert.equal(ok, false);
});

test('a tampered signature fails verification', async () => {
  const { exp, sig } = await signPath(SECRET, '/content/forge-playbook', 300);
  const tampered = sig.slice(0, -2) + (sig.slice(-2) === '00' ? '11' : '00');
  const ok = await verifySignedPath(SECRET, '/content/forge-playbook', exp, tampered);
  assert.equal(ok, false);
});

test('an expired signature fails verification even if the hash matches', async () => {
  // A negative TTL produces an exp already in the past, with a signature
  // that legitimately matches it.
  const { exp, sig } = await signPath(SECRET, '/content/forge-playbook', -10);
  const ok = await verifySignedPath(SECRET, '/content/forge-playbook', exp, sig);
  assert.equal(ok, false);
});

test('missing exp or sig is rejected', async () => {
  assert.equal(await verifySignedPath(SECRET, '/content/forge-playbook', null, 'x'), false);
  assert.equal(await verifySignedPath(SECRET, '/content/forge-playbook', 123, null), false);
});

test('signature produced with a different secret does not verify', async () => {
  const { exp, sig } = await signPath(SECRET, '/content/forge-playbook', 300);
  const ok = await verifySignedPath('a-different-secret', '/content/forge-playbook', exp, sig);
  assert.equal(ok, false);
});
