# worker/

Cloudflare Worker for the Forge Playbook's verify-entitlement-then-serve
gate (see `.company/vision.md` G2-G4).

## G3: Purchase verification

`src/entitlements/purchase.js` exports `verifyPurchaseSession(sessionId, env, opts)`,
which grants the `purchase` entitlement (`src/entitlements/types.js`) only
after confirming server-side with Stripe that a Checkout Session id was
actually paid:

- **Paid session** -> `{ granted: true, entitlementType: "purchase", ... }`
- **Unpaid or unknown session id** -> `{ granted: false, reason: "unpaid" | "unknown_session" }`
- **Replayed session id** -> the first lookup marks the session id as seen
  in the `SESSIONS` KV binding (5 minute TTL); any further verification
  attempt for that same id is rejected with `{ granted: false, reason: "rate_limited" }`
  without a second call to Stripe. Checkout session ids are single-use by
  design, so this also blocks naive brute-force/replay attempts.
- **Stripe key** is read exclusively from `env.STRIPE_SECRET_KEY` (a Worker
  secret bound at deploy time) and sent as a bearer token on the
  server-to-server call to the Stripe API. It is never hardcoded in source;
  a test asserts the source file contains no literal `sk_live_*`/`sk_test_*`
  value.

Run the suite:

```
cd worker && npm test
```

## Not yet implemented

The HTTP routing layer (`src/index.js`), the signed time-limited download
URL, and the sales ledger are tracked separately under G2 and G4.
