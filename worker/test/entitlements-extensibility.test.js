import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/index.js';
import { registerEntitlementVerifier } from '../src/entitlements.js';
import { createTestEnv } from './helpers.js';

// Proves the "membership added later without reworking the gate" requirement:
// the gate route (src/index.js) is never imported or modified by this test —
// only entitlements.js's registry is extended, exactly as a future
// membership feature would do.
test('adding a stub membership entitlement type requires no change to the gate route', async () => {
  registerEntitlementVerifier('membership', async (request) => {
    const url = new URL(request.url);
    const memberToken = url.searchParams.get('member_token');
    if (memberToken !== 'valid-member-token') {
      return { granted: false, reason: 'invalid member token', status: 403 };
    }
    return { granted: true, grantKey: memberToken, product: 'forge-playbook', amount: null };
  });

  const env = createTestEnv();
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.epub', 'FAKE ARTIFACT BYTES');

  const denied = await worker.fetch(
    new Request('https://example.com/api/grant?type=membership&member_token=wrong'),
    env
  );
  assert.equal(denied.status, 403);

  const granted = await worker.fetch(
    new Request('https://example.com/api/grant?type=membership&member_token=valid-member-token'),
    env
  );
  assert.equal(granted.status, 200);
  const body = await granted.json();
  assert.ok(body.url.includes('/api/download'));

  // A membership grant (amount: null, no Stripe session involved) must not
  // pollute the *sales* ledger — it's a non-revenue entitlement type.
  const ledgerKeys = [...env.PLAYBOOK_BUCKET.store.keys()].filter((k) => k.startsWith('ledger/'));
  assert.equal(ledgerKeys.length, 0, 'non-revenue entitlement grants must not write a sales ledger record');
});
