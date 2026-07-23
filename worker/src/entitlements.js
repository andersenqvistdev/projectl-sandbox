// Entitlement types are an extensible enum: entitlement #1 is a book
// purchase (G3); membership content reuses the same gate later without
// rework — just another type added to this object.
export const EntitlementType = Object.freeze({
  PURCHASE: 'purchase',
  MEMBERSHIP: 'membership',
});

const VALID_TYPES = new Set(Object.values(EntitlementType));

/**
 * Look up and validate an entitlement token against the KV-backed store.
 *
 * @param {{get: (key: string, type?: string) => Promise<any>}} store - KV-like
 *   binding (env.ENTITLEMENTS). Record shape: { type, productId, expiresAt, revoked }.
 * @param {string|null|undefined} token - bearer token presented by the caller.
 * @param {string} productId - product being requested.
 * @returns {Promise<{valid: boolean, reason?: string, type?: string}>}
 */
export async function checkEntitlement(store, token, productId) {
  if (!token) {
    return { valid: false, reason: 'missing_token' };
  }

  const record = await store.get(token, 'json');
  if (!record) {
    return { valid: false, reason: 'unknown_token' };
  }

  if (!VALID_TYPES.has(record.type)) {
    return { valid: false, reason: 'unknown_entitlement_type' };
  }

  if (record.revoked) {
    return { valid: false, reason: 'revoked' };
  }

  if (record.productId !== productId) {
    return { valid: false, reason: 'wrong_product' };
  }

  if (typeof record.expiresAt === 'number' && Date.now() > record.expiresAt) {
    return { valid: false, reason: 'expired' };
  }

  return { valid: true, type: record.type };
}
