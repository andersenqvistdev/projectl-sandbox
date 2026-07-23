/**
 * Entitlement type registry.
 *
 * The gate route (index.js) never branches on entitlement type by name — it
 * looks up `type` in ENTITLEMENT_VERIFIERS and dispatches. Adding a new
 * entitlement type (e.g. "membership") means adding an entry here; index.js
 * does not change.
 *
 * A verifier is: async (request, env) => VerifierResult
 *
 * VerifierResult (granted):
 *   { granted: true, grantKey: string, product: string, amount: number|null,
 *     buyerEmail?: string|null, purchaseDate?: string|null }
 *     - grantKey: a stable identifier used for rate-limiting/replay, for the
 *       ledger's sha256'd session identifier, AND (sha256'd + truncated —
 *       see orderId.js) for the per-buyer watermark's order id. For
 *       "purchase" this is the Stripe checkout session id. Never the raw
 *       value from an untrusted field the caller can shape arbitrarily.
 *     - product: the product slug this grant is for (server-derived/trusted,
 *       not blindly echoed from client input beyond selecting a configured
 *       entry — see verifyPurchase in stripe.js).
 *     - amount: integer minor-currency-unit amount (e.g. cents) for the
 *       ledger, or null if not applicable.
 *     - buyerEmail: optional, for stamping "Licensed to <email>" on the
 *       delivered copy (see watermark.js). Omit or return null if the
 *       entitlement type has no notion of a buyer email (e.g. a future
 *       membership type) — the watermark then falls back to the order id
 *       alone. Never fabricate a value.
 *     - purchaseDate: optional, YYYY-MM-DD, for the watermark's "purchased
 *       <date>" clause. Omit or return null to fall back to the grant date
 *       — reasonable for non-revenue entitlement types with no purchase to
 *       date.
 *
 * VerifierResult (denied):
 *   { granted: false, reason: string, status?: number }
 */

export const EntitlementType = Object.freeze({
  PURCHASE: 'purchase',
  // MEMBERSHIP: 'membership' — added later by registering a verifier below;
  // no change required to index.js's gate route.
});

export const ENTITLEMENT_VERIFIERS = {};

export function registerEntitlementVerifier(type, verifier) {
  ENTITLEMENT_VERIFIERS[type] = verifier;
}

export function getEntitlementVerifier(type) {
  return ENTITLEMENT_VERIFIERS[type];
}
