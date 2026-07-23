# ProjectL Vision — Forge Playbook Storefront

## Mission
Ship a production-grade, membership-ready storefront for the Forge Playbook:
page, checkout wiring, gated delivery, sales ledger, deploy runbook. Built
autonomously by the Forge daemon; deployed to production by the human
operator afterwards.

## Product decisions (operator-fixed, 2026-07-23 — do not revisit)
- Book price: **$79** (one-time). Never use token-cost framing anywhere.
- Delivery is **membership-ready**: a generic verify-entitlement-then-serve
  gate. Entitlement #1 = book purchase; member/community content must be
  addable later without reworking the gate.
- Checkout = hosted Stripe Payment Link. The link URL and all Stripe keys
  are OPERATOR-SUPPLIED deploy-time configuration — a placeholder config
  key, never a committed value.
- **The paid book artifacts are never committed to this repo** (it is
  public). Only `assets/forge-playbook-sample.*` may be referenced. The
  Worker serves the full book from private storage configured at deploy.
- Design language (from forgeframework.dev): dark charred background
  #0b0705, ember orange #ff6a1a / deep ember #c2440a, amber #ffb347,
  paper #faf7f4; display font Anton, body Hanken Grotesk, mono JetBrains
  Mono. Bold industrial look, generous whitespace, no stock-template feel.

## Machine-Readable Goals

### Period: ProjectL 2026 [status: active]

| Goal | Description | Success metric | Owner |
|------|-------------|----------------|-------|
| G1: Storefront page | Static product page for the Forge Playbook: cover, what-you-get, $79 buy button, free sample downloads in three formats from assets/ | site/index.html exists and renders: price $79 visible, buy button href reads from config placeholder, three working sample links | frontend-developer |
| G2: Entitlement gate | Cloudflare Worker implementing verify-entitlement-then-serve with entitlement types as an extensible enum (purchase now, membership later) | worker/ passes its test suite: valid entitlement serves a time-limited signed URL, invalid/absent entitlement gets 403, no route serves paid content unauthenticated | frontend-developer |
| G3: Purchase verification | Entitlement #1: verify a Stripe checkout session id server-side (paid=true) before granting the book entitlement | worker tests cover: paid session grants, unpaid/unknown session denied, replayed session id rate-limited; Stripe key read from env only | qa-engineer |
| G4: Sales ledger | Every verified sale appends one JSONL record (ts, session id hash, product, amount) the operator's revenue assessor can consume | ledger write covered by worker tests; docs/LEDGER.md documents the record schema and where it lands in production | qa-engineer |
| G5: Deploy runbook | Operator-facing runbook: promote page+worker into the production website repo, configure secrets/storage, end-to-end test purchase | docs/DEPLOY.md exists with numbered steps incl. secret configuration and a test-purchase checklist; a fresh reader could execute it | technical-writer |
