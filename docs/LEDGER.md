# Sales Ledger

The entitlement gate Worker (`worker/`) records one entry per completed sale
so the operator's revenue assessor has a durable, append-only record it can
consume independently of Stripe's dashboard.

## Record schema

Each ledger entry is a single line of JSON (JSONL — one JSON object per
line, newline-terminated):

```json
{"ts":"2026-07-23T20:15:03.512Z","session_id_sha256":"3b1c...e4a2","product":"forge-playbook","amount":7900}
```

| Field                | Type            | Description                                                                 |
|-----------------------|-----------------|-------------------------------------------------------------------------------|
| `ts`                  | string (ISO8601)| UTC timestamp of the first successful verification.                          |
| `session_id_sha256`   | string (hex-64) | SHA-256 of the Stripe checkout session id. The raw session id is never stored — the hash is enough to de-duplicate and cross-reference without retaining a value that could be replayed against Stripe. |
| `product`             | string          | Product slug (e.g. `forge-playbook`).                                        |
| `amount`              | integer         | `amount_total` from the verified Stripe session, in the currency's minor unit (cents for USD). |

Only entitlement grants tied to a real transaction (`amount !== null`) are
recorded — see `worker/src/index.js`. A future non-revenue entitlement type
(e.g. membership) does not write to this ledger.

## Where it lands: R2, one object per sale

The ledger is **not** a single growing object. Each sale is written as its
own R2 object under a `ledger/` prefix, containing exactly one JSONL line:

```
ledger/<year>/<month>/<day>/<unix-ms-timestamp>-<first-16-hex-chars-of-sha256>.jsonl
```

Example: `ledger/2026/07/23/1753301703512-3b1ce4a2f9107bde.jsonl`

**Why one object per sale instead of appending to a single growing file:**
Cloudflare R2 (like S3) has no atomic append operation. A
read-modify-write pattern against one shared object races under concurrent
purchases — two near-simultaneous sales can both read the same base
content, and the second write silently clobbers the first. Writing a new,
uniquely-keyed object per sale sidesteps that entirely: every write is
independent and nothing can be lost to a lost update.

**Reconstituting the full ledger:** the R2 object keys are zero-padded and
timestamp-prefixed, so listing everything under `ledger/` and concatenating
the objects in key order yields a valid, chronologically-ordered JSONL file.
The bucket binding is `PLAYBOOK_BUCKET` (or `LEDGER_BUCKET` if the operator
configures a separate bucket for the ledger — see `worker/wrangler.toml`);
in production the revenue assessor (or a scheduled job) lists that prefix
via the R2 API/dashboard and streams the concatenated objects.

## Known limitation: grant counting is not atomic

The grant counter that gates both the ledger write and `MAX_GRANTS_PER_SESSION`
is backed by Workers KV (`GRANTS_KV`), which has no atomic compare-and-swap
and can take up to ~60s to propagate a write globally. Two `/api/grant`
requests for the same session id, routed to different edge locations within
that window, can each read a stale count and each succeed. Concretely this
means:

- **Ledger:** both requests could observe "not yet granted" and both write a
  ledger entry, double-counting one sale.
- **Rate limit:** the same race can let a session exceed
  `MAX_GRANTS_PER_SESSION` during that window — the limit is a deterrent
  against casual link-sharing, not a hard distributed guarantee.

This is accepted for the current scope: a checkout session's success
redirect is normally hit once by one browser tab, so the realistic window
for a true cross-PoP race is small, and the operator can reconcile against
Stripe's own records (keyed by the same session id, pre-hash) if a
discrepancy is ever suspected. If concurrent-replay volume becomes a real
concern, the documented upgrade path is to move the grant counter into a
Durable Object, which serializes access per session id and gives true
exactly-once semantics for both the ledger and the rate limit.

## Ledger write failures don't block the download

`appendSale` runs after the sale is already decided (Stripe verified, rate
limit passed) and is wrapped in its own try/catch in `worker/src/index.js`.
A transient R2 outage on the `bucket.put` is logged (`console.error`) and
swallowed rather than failing the whole `/api/grant` request — a customer
who already paid and cleared verification always gets their download link,
even if the ledger write itself fails.

The trade-off: the grant counter (Workers KV, see above) is incremented
*before* `appendSale` runs, so a client retry after a failed ledger write
sees `isFirstGrant: false` and the write is not retried — that sale is then
permanently absent from the ledger, with only the Worker log line as a
record. This is accepted for the same reason as the KV race above: R2
write failures are rare and transient, and the operator can reconcile
against Stripe's own record of the session (by re-hashing a known session
id) if a gap is ever suspected.

## Rate limiting (separate from the ledger)

Every grant request — first or repeat — increments a KV counter keyed by
`sha256(entitlement_type + ":" + grant_key)`. Once a session has been
granted `MAX_GRANTS_PER_SESSION` (default 5, `worker/wrangler.toml`) fresh
download links, further requests for that session are denied with 403. Only
the *first* grant within that limit writes a ledger record; repeat grants
reuse the same sale record.
