# ProjectL — Storefront for the Forge Playbook

An autonomy experiment: the [Forge framework](https://forgeframework.dev)'s
daemon builds, tests, and ships a **membership-ready storefront** for the
Forge Playbook (the paid book about running an autonomous AI development
company) — autonomously, under Forge's deterministic safety gates.

This is the successor to ProjectK (csv2md): same protocol — fresh Forge
install at HEAD, vision in, gate canaries, ~24h autonomous run — but this
time the product is commercial infrastructure the operator will actually
deploy.

## What gets built here

1. **Storefront page** — product page for the Forge Playbook at $79
   (free sample downloads from `assets/`), design consistent with
   forgeframework.dev.
2. **Membership-ready delivery** — a Cloudflare Worker implementing
   *verify entitlement, then serve*: entitlement #1 is a book purchase
   (Stripe checkout session verification); the same gate later serves
   member/community content without rework.
3. **Sales ledger** — every verified sale appended to a ledger the
   operator's G13 (first revenue) assessor can read.
4. **Deploy runbook** — how the result promotes into the production
   website repo (human-gated, one reviewed PR).

## Hard rules

- The **paid book artifacts are never committed here** — this repo is
  public. Only the free sample files live in `assets/`. The full book is
  served by the Worker from private storage, configured at deploy time.
- No payment-provider secrets in the repo, ever. The Stripe payment link
  and API keys are operator-supplied deploy-time configuration.
- All work ships via branch + PR through Forge's gate chain. Scout-class
  and external PRs never auto-merge.

## Status

Launched 2026-07-23. Run log and results will be written up as
PROJECTL-REPORT in the operator's fieldnotes when the run concludes.
