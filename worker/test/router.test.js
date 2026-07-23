import { test } from 'node:test';
import assert from 'node:assert/strict';
import { handleRequest } from '../src/router.js';
import { EntitlementType } from '../src/entitlements.js';
import { signPath } from '../src/signing.js';
import { fakeKV } from './fake-kv.js';

const SECRET = 'test-signing-secret';

function makeEnv(kvSeed = {}) {
  const env = { SIGNING_SECRET: SECRET, ENTITLEMENTS: fakeKV() };
  for (const [token, record] of Object.entries(kvSeed)) {
    env.ENTITLEMENTS._set(token, record);
  }
  return env;
}

test('GET /entitlement/:id without a token is 403', async () => {
  const env = makeEnv();
  const res = await handleRequest(new Request('https://worker.test/entitlement/forge-playbook'), env);
  assert.equal(res.status, 403);
});

test('GET /entitlement/:id with an invalid token is 403', async () => {
  const env = makeEnv();
  const res = await handleRequest(
    new Request('https://worker.test/entitlement/forge-playbook', {
      headers: { Authorization: 'Bearer not-a-real-token' },
    }),
    env,
  );
  assert.equal(res.status, 403);
});

test('GET /entitlement/:id with a valid entitlement returns a time-limited signed URL', async () => {
  const env = makeEnv({
    'good-token': { type: EntitlementType.PURCHASE, productId: 'forge-playbook', expiresAt: null },
  });
  const res = await handleRequest(
    new Request('https://worker.test/entitlement/forge-playbook', {
      headers: { Authorization: 'Bearer good-token' },
    }),
    env,
  );
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.match(body.url, /^\/content\/forge-playbook\?exp=\d+&sig=[0-9a-f]+$/);
  assert.ok(body.expiresAt > Date.now());
});

test('GET /content/:id with no signature is 403 - no unauthenticated access', async () => {
  const env = makeEnv();
  const res = await handleRequest(new Request('https://worker.test/content/forge-playbook'), env);
  assert.equal(res.status, 403);
});

test('GET /content/:id with a tampered signature is 403', async () => {
  const env = makeEnv();
  const { exp, sig } = await signPath(SECRET, '/content/forge-playbook', 300);
  const tampered = sig.slice(0, -1) + (sig.at(-1) === '0' ? '1' : '0');
  const res = await handleRequest(
    new Request(`https://worker.test/content/forge-playbook?exp=${exp}&sig=${tampered}`),
    env,
  );
  assert.equal(res.status, 403);
});

test('GET /content/:id with an expired signature is 403', async () => {
  const env = makeEnv();
  const { exp, sig } = await signPath(SECRET, '/content/forge-playbook', -5);
  const res = await handleRequest(
    new Request(`https://worker.test/content/forge-playbook?exp=${exp}&sig=${sig}`),
    env,
  );
  assert.equal(res.status, 403);
});

test('GET /content/:id with a valid signature but no storage binding is 501, never a bare 200 of paid bytes', async () => {
  const env = makeEnv();
  const { exp, sig } = await signPath(SECRET, '/content/forge-playbook', 300);
  const res = await handleRequest(
    new Request(`https://worker.test/content/forge-playbook?exp=${exp}&sig=${sig}`),
    env,
  );
  assert.equal(res.status, 501);
});

test('GET /content/:id with a valid signature and a storage binding serves the object', async () => {
  const env = makeEnv();
  env.CONTENT_STORE = {
    async get(key) {
      assert.equal(key, 'forge-playbook');
      return { body: 'the whole book' };
    },
  };
  const { exp, sig } = await signPath(SECRET, '/content/forge-playbook', 300);
  const res = await handleRequest(
    new Request(`https://worker.test/content/forge-playbook?exp=${exp}&sig=${sig}`),
    env,
  );
  assert.equal(res.status, 200);
  assert.equal(await res.text(), 'the whole book');
});

test('end-to-end: entitlement check mints a URL that content route then accepts', async () => {
  const env = makeEnv({
    'good-token': { type: EntitlementType.PURCHASE, productId: 'forge-playbook', expiresAt: null },
  });
  env.CONTENT_STORE = { async get() { return { body: 'the whole book' }; } };

  const entitlementRes = await handleRequest(
    new Request('https://worker.test/entitlement/forge-playbook', {
      headers: { Authorization: 'Bearer good-token' },
    }),
    env,
  );
  const { url } = await entitlementRes.json();

  const contentRes = await handleRequest(new Request(`https://worker.test${url}`), env);
  assert.equal(contentRes.status, 200);
  assert.equal(await contentRes.text(), 'the whole book');
});

test('unknown route is 404', async () => {
  const env = makeEnv();
  const res = await handleRequest(new Request('https://worker.test/nope'), env);
  assert.equal(res.status, 404);
});

test('non-GET method is rejected', async () => {
  const env = makeEnv();
  const res = await handleRequest(
    new Request('https://worker.test/content/forge-playbook', { method: 'POST' }),
    env,
  );
  assert.equal(res.status, 405);
});
