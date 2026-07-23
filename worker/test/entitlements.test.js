import { test } from 'node:test';
import assert from 'node:assert/strict';
import { checkEntitlement, EntitlementType } from '../src/entitlements.js';
import { fakeKV } from './fake-kv.js';

test('EntitlementType enum exposes purchase and membership', () => {
  assert.equal(EntitlementType.PURCHASE, 'purchase');
  assert.equal(EntitlementType.MEMBERSHIP, 'membership');
});

test('valid, unexpired token for the right product is accepted', async () => {
  const kv = fakeKV();
  kv._set('good-token', {
    type: EntitlementType.PURCHASE,
    productId: 'forge-playbook',
    expiresAt: null,
  });

  const result = await checkEntitlement(kv, 'good-token', 'forge-playbook');
  assert.equal(result.valid, true);
  assert.equal(result.type, EntitlementType.PURCHASE);
});

test('membership entitlement type is accepted the same way', async () => {
  const kv = fakeKV();
  kv._set('member-token', {
    type: EntitlementType.MEMBERSHIP,
    productId: 'community-area',
    expiresAt: null,
  });

  const result = await checkEntitlement(kv, 'member-token', 'community-area');
  assert.equal(result.valid, true);
  assert.equal(result.type, EntitlementType.MEMBERSHIP);
});

test('missing token is rejected', async () => {
  const kv = fakeKV();
  const result = await checkEntitlement(kv, null, 'forge-playbook');
  assert.equal(result.valid, false);
  assert.equal(result.reason, 'missing_token');
});

test('unknown token is rejected', async () => {
  const kv = fakeKV();
  const result = await checkEntitlement(kv, 'never-issued', 'forge-playbook');
  assert.equal(result.valid, false);
  assert.equal(result.reason, 'unknown_token');
});

test('token for a different product is rejected', async () => {
  const kv = fakeKV();
  kv._set('good-token', {
    type: EntitlementType.PURCHASE,
    productId: 'some-other-product',
    expiresAt: null,
  });

  const result = await checkEntitlement(kv, 'good-token', 'forge-playbook');
  assert.equal(result.valid, false);
  assert.equal(result.reason, 'wrong_product');
});

test('expired token is rejected', async () => {
  const kv = fakeKV();
  kv._set('stale-token', {
    type: EntitlementType.PURCHASE,
    productId: 'forge-playbook',
    expiresAt: Date.now() - 1000,
  });

  const result = await checkEntitlement(kv, 'stale-token', 'forge-playbook');
  assert.equal(result.valid, false);
  assert.equal(result.reason, 'expired');
});

test('revoked token is rejected even if otherwise valid', async () => {
  const kv = fakeKV();
  kv._set('revoked-token', {
    type: EntitlementType.PURCHASE,
    productId: 'forge-playbook',
    expiresAt: null,
    revoked: true,
  });

  const result = await checkEntitlement(kv, 'revoked-token', 'forge-playbook');
  assert.equal(result.valid, false);
  assert.equal(result.reason, 'revoked');
});

test('unrecognized entitlement type is rejected', async () => {
  const kv = fakeKV();
  kv._set('weird-token', {
    type: 'not-a-real-type',
    productId: 'forge-playbook',
    expiresAt: null,
  });

  const result = await checkEntitlement(kv, 'weird-token', 'forge-playbook');
  assert.equal(result.valid, false);
  assert.equal(result.reason, 'unknown_entitlement_type');
});
