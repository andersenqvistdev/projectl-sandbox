import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/index.js';
import { createTestEnv, paidSession } from './helpers.js';

/**
 * no-secrets.test.js proves no literal key is committed to src/. This file
 * proves the complementary runtime half of "Stripe key read from env only":
 * the actual outbound request to Stripe carries whatever value is on
 * env.STRIPE_SECRET_KEY for *that* call, not a cached or hardcoded value.
 */

function createCapturingStripeFetch(sessions) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url: String(url), authorization: init?.headers?.Authorization });
    const match = String(url).match(/\/checkout\/sessions\/([^/?]+)/);
    const sessionId = decodeURIComponent(match[1]);
    const session = sessions[sessionId];
    if (!session) {
      return new Response(JSON.stringify({ error: { message: 'No such checkout session' } }), {
        status: 404,
      });
    }
    return new Response(JSON.stringify(session), { status: 200 });
  };
  return { calls, fetchImpl };
}

test('Stripe request Authorization header carries env.STRIPE_SECRET_KEY', async () => {
  const { calls, fetchImpl } = createCapturingStripeFetch({ cs_test_paid_1: paidSession() });
  const env = createTestEnv({
    sessions: {},
    STRIPE_SECRET_KEY: 'sk_test_this_is_the_env_value',
    fetchImpl,
  });

  const res = await worker.fetch(
    new Request('https://example.com/api/grant?session_id=cs_test_paid_1'),
    env
  );

  assert.equal(res.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].authorization, 'Bearer sk_test_this_is_the_env_value');
});

test('changing env.STRIPE_SECRET_KEY changes the header sent, proving it is read live from env', async () => {
  const { calls, fetchImpl } = createCapturingStripeFetch({ cs_test_paid_1: paidSession() });
  const env = createTestEnv({
    sessions: {},
    STRIPE_SECRET_KEY: 'sk_test_a_completely_different_value',
    fetchImpl,
  });

  await worker.fetch(new Request('https://example.com/api/grant?session_id=cs_test_paid_1'), env);

  assert.equal(calls[0].authorization, 'Bearer sk_test_a_completely_different_value');
});
