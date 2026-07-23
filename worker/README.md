# Entitlement Gate Worker

Cloudflare Worker implementing **verify-entitlement-then-serve** for paid
Forge Playbook content. This is the generic gate (G2) — issuing real
entitlements from a verified Stripe purchase is G3; a private storage
binding for the actual book bytes is deploy-time config (G5).

## Routes

- `GET /entitlement/:productId` — requires `Authorization: Bearer <token>`.
  Looks the token up in the `ENTITLEMENTS` KV binding. Valid, unexpired,
  non-revoked, product-matching entitlement → `200` with a time-limited
  signed URL (`{ url, expiresAt }`). Otherwise → `403`.
- `GET /content/:productId?exp=&sig=` — only ever serves content given a
  signature minted by the entitlement route above; missing, tampered, or
  expired signature → `403`. No route serves paid content unauthenticated.
  With a valid signature but no `CONTENT_STORE` binding configured →
  `501` (storage not wired yet, not a leak of bytes).

## Entitlement types

`src/entitlements.js` exports `EntitlementType` as an extensible enum:
`PURCHASE` (book purchase, G3) and `MEMBERSHIP` (community content,
future) — both flow through the same gate without rework.

## Running the tests

```sh
cd worker
npm test
```

Uses Node's built-in test runner (`node --test`) against plain ES modules
and Web Crypto — no `wrangler`/Miniflare install required to run the unit
and router-integration tests. Deploy-time wiring lives in `wrangler.toml`
and `docs/DEPLOY.md` (G5).
