import { EntitlementType } from "./types.js";

const STRIPE_API_BASE = "https://api.stripe.com/v1";
const SESSION_ID_PATTERN = /^cs_[a-zA-Z0-9_]+$/;
const REPLAY_TTL_SECONDS = 300;

export class PurchaseVerificationError extends Error {
  constructor(message, code) {
    super(message);
    this.name = "PurchaseVerificationError";
    this.code = code;
  }
}

function replayKeyFor(sessionId) {
  return `purchase_session_seen:${sessionId}`;
}

// Verifies a Stripe Checkout Session server-side before granting the book
// entitlement. `env` is the Worker's bound environment: STRIPE_SECRET_KEY
// (secret, never hardcoded) and SESSIONS (a KV namespace used to block
// replays of the same session id). `fetchImpl` is injectable for tests.
export async function verifyPurchaseSession(sessionId, env, { fetchImpl = fetch } = {}) {
  if (typeof sessionId !== "string" || !SESSION_ID_PATTERN.test(sessionId)) {
    return { granted: false, reason: "invalid_session_id" };
  }

  const secretKey = env && env.STRIPE_SECRET_KEY;
  if (!secretKey) {
    throw new PurchaseVerificationError(
      "STRIPE_SECRET_KEY is not configured",
      "missing_stripe_key",
    );
  }
  if (!env.SESSIONS) {
    throw new PurchaseVerificationError(
      "SESSIONS KV namespace is not bound",
      "missing_sessions_kv",
    );
  }

  const key = replayKeyFor(sessionId);
  const alreadySeen = await env.SESSIONS.get(key);
  if (alreadySeen) {
    return { granted: false, reason: "rate_limited" };
  }

  // Mark the session id as seen before calling Stripe so a second request
  // fired while the first is in flight (or after it fails) is still treated
  // as a replay - a Stripe checkout session id is single-use by design.
  await env.SESSIONS.put(key, "1", { expirationTtl: REPLAY_TTL_SECONDS });

  const response = await fetchImpl(
    `${STRIPE_API_BASE}/checkout/sessions/${encodeURIComponent(sessionId)}`,
    {
      method: "GET",
      headers: { Authorization: `Bearer ${secretKey}` },
    },
  );

  if (!response.ok) {
    return { granted: false, reason: "unknown_session" };
  }

  const session = await response.json();

  if (session.payment_status !== "paid") {
    return { granted: false, reason: "unpaid" };
  }

  return {
    granted: true,
    entitlementType: EntitlementType.PURCHASE,
    sessionId,
    customerEmail: session.customer_details?.email ?? session.customer_email ?? null,
    amountTotal: session.amount_total ?? null,
  };
}
