# Sales Ledger

The entitlement gate Worker (`worker/`) records one entry per completed sale
so the operator's revenue assessor has a durable, append-only record it can
consume independently of Stripe's dashboard. The write path lives in
`worker/src/ledger.js` (`appendSale`), covered by `worker/test/ledger.test.js`.

## Record schema

Each ledger entry is a single line of JSON (JSONL — one JSON object per
line, newline-terminated):

```json
{"ts":"2026-07-23T20:15:03.512Z","session_id_sha256":"e2f...9a1c","product":"forge-playbook","amount":7900}
```

| Field                | Type            | Description                                                                 |
|-----------------------|-----------------|-------------------------------------------------------------------------------|
| `ts`                  | string (ISO8601)| UTC timestamp of the sale, as recorded at write time.                        |
| `session_id_sha256`   | string (hex-64) | SHA-256 of the Stripe checkout session id. The raw session id is never passed to `appendSale` or stored — only its hash, which is enough to de-duplicate and cross-reference without retaining a value that could be replayed against Stripe. |
| `product`             | string          | Product slug (e.g. `forge-playbook`).                                        |
| `amount`              | integer         | `amount_total` from the verified Stripe session, in the currency's minor unit (cents for USD). |

Only entitlement grants tied to a real transaction are expected to call
`appendSale` — a future non-revenue entitlement type (e.g. membership) simply
never calls it. `appendSale` itself has no opinion on *why* a sale
happened, only that it did.

## Where it lands: R2, one object per sale

The ledger is **not** a single growing object. Each sale is written as its
own object in the bucket bound as `env.LEDGER_BUCKET`, keyed deterministically
from the session hash — **not** from a timestamp:

```
ledger/<first-2-hex-chars-of-session_id_sha256>/<session_id_sha256>.jsonl
```

Example: `ledger/e2/e2f4a9c1...9a1c.jsonl`

**Why keyed by session hash instead of by date/timestamp:** the key doubles
as the de-duplication mechanism (see "Idempotency" below) — if a grant
request is retried or replayed for a session that already has a ledger
entry, the key resolves to the same object and `appendSale` is a no-op
instead of writing a second sale record. A timestamp-based key would let
every retry mint a fresh, distinct object and silently double-count
revenue. The leading 2-hex-char shard exists only to keep any single R2
"directory" from holding an unbounded number of objects as sales accumulate;
it carries no chronological meaning.

**Reconstituting the full ledger:** because the key is not
chronologically ordered, listing `ledger/` and concatenating in key order
does **not** yield a time-ordered file. Instead: list every object under
`ledger/`, read each one (one JSONL line), and sort by the `ts` field in the
record itself. In production the revenue assessor (or a scheduled job) does
this via the R2 API/dashboard.

**Bucket binding:** `LEDGER_BUCKET`. `appendSale` fails closed (throws) if
this binding is missing rather than silently falling back to a
differently-purposed bucket — a sales ledger should never land somewhere it
wasn't explicitly provisioned for. The binding is configured in the
Worker's `wrangler.toml` when the entitlement-gate route that calls
`appendSale` is wired up.

## Idempotency

`appendSale` checks for an existing object at the session's key
(`bucket.head`) before writing, and performs the actual write as a
conditional put (`onlyIf: { etagDoesNotMatch: '*' }`) so a second writer
racing the same key does not overwrite the first. Calling it twice for the
same `sessionIdSha256` — e.g. because the download link was reloaded, or a
grant request was retried — writes exactly one ledger record; the second
(and any subsequent) call returns `{ written: false }`. Both the `head`
no-op path and a losing conditional `put` are treated identically by the
caller: neither is an error, and neither results in a duplicate record.

## Known limitation: the head-then-put check is not a hard atomicity
## guarantee against every possible race

The `head` check and the conditional `put` narrow the race window
considerably compared to a plain unconditional write, but two calls for the
same session id landing on different edge locations within the same
instant could both observe "no existing object" before either write lands.
The deterministic (non-timestamped) key guarantees the *result* is still
correct either way — only one object ever ends up at that key, so there is
never a duplicate sale record — but the losing call's conditional `put`
**resolves to `null` rather than throwing** (this is real R2 behavior, not
a simplification), so `appendSale` reports `written: false` for it rather
than surfacing an error. This is accepted for the current scope: a
checkout session's success redirect is normally hit once by one browser
tab, so the realistic window for a true concurrent race is small, and the
failure mode that matters — a double-counted sale — cannot happen even
under the race, it just isn't loudly flagged when it's avoided. If this
ever needs to surface as an event (e.g. for alerting on unexpectedly
frequent races), the caller can act on `written: false` itself. If
exactly-once semantics need to become a hard, non-probabilistic guarantee,
the documented upgrade path is a Durable Object keyed on the session hash,
which serializes access entirely.
