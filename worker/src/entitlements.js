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
 *     buyerEmail?: string|null, purchasedAt?: string|null }
 *     - grantKey: a stable identifier used for rate-limiting/replay and for
 *       the ledger's sha256'd session identifier. For "purchase" this is the
 *       Stripe checkout session id. Never the raw value from an untrusted
 *       field the caller can shape arbitrarily.
 *     - product: the product slug this grant is for (server-derived/trusted,
 *       not blindly echoed from client input beyond selecting a configured
 *       entry — see verifyPurchase in stripe.js).
 *     - amount: integer minor-currency-unit amount (e.g. cents) for the
 *       ledger, or null if not applicable.
 *     - buyerEmail: optional, used to personalize the delivery watermark
 *       (see watermark.js). Omit or return null when there's no buyer
 *       identity to attach (e.g. a future membership entitlement) — the
 *       watermark then falls back to the order id alone.
 *     - purchasedAt: optional, YYYY-MM-DD watermark purchase date. Omit or
 *       return null when not applicable.
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
