import test from 'node:test';
import assert from 'node:assert/strict';
import { checkAndRecordGrant } from '../src/rateLimit.js';
import { sha256Hex } from '../src/hash.js';
import { createMockKV } from './helpers.js';

// Unit-level coverage for the KV-backed grant counter itself, complementing
// the HTTP-level "replay beyond N grants is denied" test in grant.test.js.

test('grants are allowed up to MAX_GRANTS_PER_SESSION, then denied', async () => {
  const env = { GRANTS_KV: createMockKV(), MAX_GRANTS_PER_SESSION: '3' };

  const first = await checkAndRecordGrant(env, 'purchase', 'cs_test_replay');
  assert.deepEqual([first.allowed, first.isFirstGrant, first.count], [true, true, 1]);

  const second = await checkAndRecordGrant(env, 'purchase', 'cs_test_replay');
  assert.deepEqual([second.allowed, second.isFirstGrant, second.count], [true, false, 2]);

  const third = await checkAndRecordGrant(env, 'purchase', 'cs_test_replay');
  assert.deepEqual([third.allowed, third.isFirstGrant, third.count], [true, false, 3]);

  const fourth = await checkAndRecordGrant(env, 'purchase', 'cs_test_replay');
  assert.deepEqual([fourth.allowed, fourth.isFirstGrant, fourth.count], [false, false, 3]);
});

test('falls back to a default max of 5 when MAX_GRANTS_PER_SESSION is unset', async () => {
  const env = { GRANTS_KV: createMockKV() };

  for (let i = 0; i < 5; i++) {
    const result = await checkAndRecordGrant(env, 'purchase', 'cs_test_default');
    assert.equal(result.allowed, true, `grant ${i + 1} of 5 should be allowed`);
  }
  const sixth = await checkAndRecordGrant(env, 'purchase', 'cs_test_default');
  assert.equal(sixth.allowed, false);
});

test('falls back to the default max when MAX_GRANTS_PER_SESSION is not a valid number', async () => {
  const env = { GRANTS_KV: createMockKV(), MAX_GRANTS_PER_SESSION: 'not-a-number' };

  for (let i = 0; i < 5; i++) {
    const result = await checkAndRecordGrant(env, 'purchase', 'cs_test_invalid_env');
    assert.equal(result.allowed, true);
  }
  const sixth = await checkAndRecordGrant(env, 'purchase', 'cs_test_invalid_env');
  assert.equal(sixth.allowed, false);
});

test('the same grantKey under different entitlement types gets independent counters', async () => {
  const env = { GRANTS_KV: createMockKV(), MAX_GRANTS_PER_SESSION: '1' };

  const purchase = await checkAndRecordGrant(env, 'purchase', 'shared-key');
  assert.deepEqual([purchase.allowed, purchase.isFirstGrant], [true, true]);

  // Same raw grantKey, different type — must not share the purchase type's
  // exhausted counter (the type is part of what gets hashed into the KV key).
  const membership = await checkAndRecordGrant(env, 'membership', 'shared-key');
  assert.deepEqual([membership.allowed, membership.isFirstGrant], [true, true]);

  const purchaseReplay = await checkAndRecordGrant(env, 'purchase', 'shared-key');
  assert.equal(purchaseReplay.allowed, false, 'purchase type should already be exhausted');
});

test('the KV key hash is the sha256 of "type:grantKey", never the raw grant key', async () => {
  const env = { GRANTS_KV: createMockKV(), MAX_GRANTS_PER_SESSION: '5' };

  const result = await checkAndRecordGrant(env, 'purchase', 'cs_test_hash_check');
  const expectedHash = await sha256Hex('purchase:cs_test_hash_check');
  assert.equal(result.hash, expectedHash);

  const storedKeys = [...env.GRANTS_KV.store.keys()];
  assert.deepEqual(storedKeys, [`grant:${expectedHash}`]);
  assert.ok(!storedKeys.some((k) => k.includes('cs_test_hash_check')));
});
