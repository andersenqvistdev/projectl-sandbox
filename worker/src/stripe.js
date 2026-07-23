/**
 * Stripe checkout session verification.
 *
 * env.STRIPE_SECRET_KEY is a Worker secret (`wrangler secret put
 * STRIPE_SECRET_KEY`) — never committed, never a literal in source.
 *
 * env.fetchImpl is an optional injection point so tests never hit the
 * network; production leaves it unset and the real global `fetch` is used.
 */

const STRIPE_API_BASE = 'https://api.stripe.com/v1';

export async function fetchStripeCheckoutSession(sessionId, env) {
  const fetchImpl = env.fetchImpl || fetch;
  const response = await fetchImpl(
    `${STRIPE_API_BASE}/checkout/sessions/${encodeURIComponent(sessionId)}`,
    {
      headers: {
        Authorization: `Bearer ${env.STRIPE_SECRET_KEY}`,
      },
    }
  );

  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`Stripe API error: ${response.status}`);
  }
  return response.json();
}

/**
 * Purchase entitlement verifier — see entitlements.js for the VerifierResult
 * contract. Registered against EntitlementType.PURCHASE in index.js.
 *
 * Product resolution: this storefront currently sells exactly one product
 * (env.DEFAULT_PRODUCT), so a verified-paid session always grants that
 * product — there is no per-line-item binding to get wrong. A client-
 * supplied `product` query param is accepted only as a sanity check (it
 * must match DEFAULT_PRODUCT) and never by itself selects what gets served.
 *
 * IMPORTANT — before adding a second paid product: a paid session must be
 * bound server-side to the specific product it paid for (e.g. by requesting
 * `expand[]=line_items` from Stripe and mapping each line item's price id
 * to a product slug, rejecting sessions with more than one line item or
 * items that don't resolve). Do not just add a second entry to
 * ARTIFACT_KEYS — without that binding, a session paid for product A could
 * be used to fetch product B.
 */
export async function verifyPurchase(request, env) {
  const url = new URL(request.url);
  const sessionId = url.searchParams.get('session_id');
  const requestedProduct = url.searchParams.get('product');

  if (!sessionId) {
    return { granted: false, reason: 'missing session_id', status: 403 };
  }

  const session = await fetchStripeCheckoutSession(sessionId, env);
  if (!session) {
    return { granted: false, reason: 'unknown session', status: 403 };
  }
  if (session.payment_status !== 'paid') {
    return { granted: false, reason: 'unpaid session', status: 403 };
  }

  const product = env.DEFAULT_PRODUCT || null;
  if (!product) {
    return { granted: false, reason: 'no product configured', status: 403 };
  }
  if (requestedProduct && requestedProduct !== product) {
    return { granted: false, reason: 'product mismatch', status: 403 };
  }

  return {
    granted: true,
    grantKey: sessionId,
    product,
    amount: typeof session.amount_total === 'number' ? session.amount_total : null,
  };
}
