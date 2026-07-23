# Production Deploy Runbook — Storefront + Entitlement Worker

Operator runbook for promoting the sandbox storefront (`site/`) and
entitlement-gate Worker (`worker/`) into your production website repo and a
live Cloudflare account. This documents what exists in this repo today, not
a future plan — every path below is real, and every command is meant to be
copy-pasted as-is (substitute the bracketed placeholders).

**What you're deploying:**
- A static product page (`site/index.html` + `site/config.js`) that links to
  a Stripe Payment Link.
- A Cloudflare Worker (`worker/`) that verifies a completed Stripe payment,
  hands back a short-lived signed download URL, and records the sale to an
  append-only ledger in R2. See `docs/LEDGER.md` for the ledger format.

**What is NOT in this repo, by design:** the paid book artifact itself. The
public repo only ships free samples (`assets/forge-playbook-sample.*`). You
supply the real, sellable file (e.g. from your own content pipeline's build
output) directly to R2 in step 3 — it never touches source control.

## Prerequisites

- A Cloudflare account with Workers and R2 enabled, and `wrangler` available
  via `npx` (no version is pinned in `worker/package.json`; `npx wrangler`
  pulls current stable). Log in once per machine: `npx wrangler login`.
- A Stripe account, live mode, with payouts enabled.
- Write access to your production website repo (the operator repo this
  sandbox's output gets promoted into — referred to below as
  `<production-repo>`).
- `worker/` passes its own test suite before you deploy anything:
  ```
  cd worker && npm install --no-audit --no-fund && npm test
  ```

## 1. Copy the storefront into the production website repo

The storefront lives entirely in two files plus four static assets. Copy
these paths, preserving their relative structure (the page references
assets via `../assets/...` and the config via a same-directory `<script>`
tag):

```
site/index.html
site/config.js
assets/cover.png
assets/forge-playbook-sample.pdf
assets/forge-playbook-sample.epub
assets/forge-playbook-sample.html
```

Land them at the equivalent relative layout in `<production-repo>` (e.g.
`<production-repo>/site/index.html` and `<production-repo>/assets/...`, or
adjust the `../assets/` and `config.js` references in `index.html` to match
wherever `<production-repo>` actually serves assets from — check the copied
page renders and all four asset links resolve before merging).

Open this as **one PR against `<production-repo>`, reviewed and merged by a
human**. Never auto-merge into production — this repo's daemon auto-merge
config applies only to this sandbox, not to `<production-repo>`. It's safe
for this PR to ship with `site/config.js` still holding the sandbox's
`TODO-OPERATOR` placeholder — the buy button is a dead link until step 2 is
finished, not a broken/dangerous one.

## 2. Create the Stripe Payment Link and wire the config placeholder

1. In the Stripe Dashboard (live mode): **Product catalog → Add product**.
   Name it (e.g. "Forge Playbook"), set pricing to **One time**, **$79.00
   USD**, matching the price advertised on the storefront page
   (`site/index.html` shows `$79`). **Note:** the Worker does not validate
   this amount server-side — `worker/src/stripe.js:verifyPurchase` only
   checks that the Stripe session is `paid` and passes whatever
   `amount_total` Stripe reports straight through to the ledger. Keeping the
   Payment Link's price matched to the storefront's advertised price is on
   you; if they ever drift, the Worker will still grant access at whatever
   the customer actually paid.
2. **Payment links → Create payment link**, select the product from step 1.
3. Under **After payment**, choose **Redirect customers to your website**
   and set the URL to the Worker's grant route with Stripe's session-id
   placeholder appended — see step 4, which must happen first or you'll be
   editing this URL twice. It's fine to save the link now with a placeholder
   and come back to fix the redirect URL after step 4.
4. Copy the generated payment link URL (`https://buy.stripe.com/...`).
5. **Do not wire this into the live production `config.js` yet.** Hold the
   real link until the Worker is deployed and the redirect is wired (step
   4) — if a customer completes checkout while `config.js` still points at
   `TODO-OPERATOR` that's a harmless dead button (nothing charges), but if
   it points at a *real, live* Payment Link before the Worker exists, a
   customer can pay with nowhere working to redeem the purchase. Draft the
   `site/config.js` change now, land it in the same PR or a follow-up, but
   don't let it reach production until you've completed step 4:
   ```js
   window.FORGE_CONFIG = {
     paymentLinkUrl: "https://buy.stripe.com/REPLACE_WITH_YOUR_LINK"
   };
   ```
   `site/index.html` reads this at load and wires it onto the `#buy-button`
   element — no other file needs to change for this step. (If a real charge
   ever does land before the Worker is live, it's recoverable: `verifyPurchase`
   only checks the session's current Stripe-side state, not a time window, so
   once the Worker is deployed you can manually hit
   `https://<worker-url>/api/grant?session_id=<their session id>` — from
   Stripe's Payments list — to issue their download link after the fact.)

## 3. Provision R2 storage, upload the paid artifact, set secrets

The Worker binds a single R2 bucket (`PLAYBOOK_BUCKET`) for **both** the
private artifact and the sales ledger — see `worker/wrangler.toml`. There is
no second bucket to create unless you choose to split them.

```bash
cd worker

# 1. Create the bucket.
npx wrangler r2 bucket create <YOUR_BUCKET_NAME>

# 2. Upload the paid artifact. The key MUST match ARTIFACT_KEYS in
#    wrangler.toml, which currently maps "forge-playbook" ->
#    "artifacts/forge-playbook.epub". This file is not in this repo -
#    supply it yourself (e.g. the finished export from your own content
#    pipeline).
npx wrangler r2 object put <YOUR_BUCKET_NAME>/artifacts/forge-playbook.epub \
  --file=/path/to/your/forge-playbook.epub

# 3. Create the KV namespace used for the replay/rate-limit grant counter.
npx wrangler kv namespace create GRANTS_KV
# Prints an "id" - copy it into wrangler.toml in the next step.
```

Edit `worker/wrangler.toml` (in your deploy checkout) and replace the two
placeholders with real values from above:

```toml
[[r2_buckets]]
binding = "PLAYBOOK_BUCKET"
bucket_name = "<YOUR_BUCKET_NAME>"

[[kv_namespaces]]
binding = "GRANTS_KV"
id = "<ID_FROM_KV_NAMESPACE_CREATE>"
```

Double-check both placeholders are actually replaced before you deploy —
`wrangler deploy` will fail outright if `bucket_name` or the KV `id` are
still the literal `REPLACE_WITH_...` placeholders.

Set the two Worker secrets — never put these in `wrangler.toml` or any
committed file (`worker/test/no-secrets.test.js` enforces there's no literal
key material in `worker/src`):

```bash
# Your Stripe secret key (Dashboard -> Developers -> API keys). Use the
# live key (sk_live_...) for production; you'll temporarily swap in a
# sk_test_... key for the rehearsal in step 5, then swap back.
npx wrangler secret put STRIPE_SECRET_KEY

# A random high-entropy string used to HMAC-sign download tokens. Generate
# one - do not reuse a secret from anywhere else:
openssl rand -hex 32
npx wrangler secret put SIGNING_SECRET
```

## 4. Deploy the Worker and wire the payment-link redirect

```bash
cd worker
npx wrangler deploy
```

This publishes to `https://forge-playbook-gate.<your-account-subdomain>.workers.dev`
(the `name` in `worker/wrangler.toml`) unless you've configured a custom
route. Note the deployed base URL — you need it for both remaining
sub-steps.

**Wire the redirect (finishing step 2.3):** in the Stripe Payment Link's
**After payment → Redirect customers to your website** field, set the URL
to the Worker's `/api/grant` route with Stripe's session-id template
variable:

```
https://forge-playbook-gate.<your-account-subdomain>.workers.dev/api/grant?session_id={CHECKOUT_SESSION_ID}
```

Stripe substitutes `{CHECKOUT_SESSION_ID}` with the real completed session
id at redirect time — `worker/src/stripe.js` reads it from the
`session_id` query param and verifies it against the Stripe API server-side
before granting anything.

**Known gap to document, not paper over:** `/api/grant` is a JSON API, not
an HTML receipt page — see `worker/src/index.js:handleGrant`. On success a
customer's browser currently lands on a raw JSON response,
`{"url": "<signed /api/download link>", "expires_at": "..."}`. There is no
"click here to download" page in this repo yet. Until one is built, the
customer must locate the `url` field to get their file. Plan for this in
your support expectations, or budget a follow-up task to add a thin HTML
success page that fetches this same JSON and auto-redirects.

At this point the Worker is live but the production storefront still has no
real Payment Link wired in (step 2.5) — do step 5 now, before finishing
step 2.5, so the rehearsal happens with no real customer traffic possible
yet.

## 5. End-to-end test purchase checklist

Do this against the freshly deployed Worker before wiring the real Payment
Link into the live production `config.js` (step 2.5) and before telling
anyone the store is live. Prefer Stripe **test mode** end to end: create a
second, test-mode Payment Link for the same product, and temporarily point
`STRIPE_SECRET_KEY` at a test key:

```bash
cd worker
npx wrangler secret put STRIPE_SECRET_KEY   # paste an sk_test_... key
```

**Because `worker/wrangler.toml` has no separate staging environment, this
key change applies to the one live Worker and its one live `PLAYBOOK_BUCKET`
/ `GRANTS_KV`.** Run this rehearsal in a single sitting, note the test
session's ledger object key (step 5.5 below) so you can identify and delete
it afterward, and switch back to the `sk_live_...` key
(`npx wrangler secret put STRIPE_SECRET_KEY`) as your last action before any
real customer could reach the store.

1. Open the storefront page (with the test-mode Payment Link URL, e.g. by
   temporarily editing a local/preview copy of `config.js` — don't merge a
   test link into production) and click the buy button; confirm it lands on
   the Stripe Payment Link.
2. Complete checkout using Stripe's test card `4242 4242 4242 4242`, any
   future expiry, any CVC, any postal code.
3. Confirm the post-payment redirect lands on
   `.../api/grant?session_id=cs_...` and returns **HTTP 200** with a JSON
   body containing a `url` and `expires_at`.
4. Open the `url` from step 3 directly. Confirm the file downloads
   (`Content-Disposition: attachment`) and its bytes match what you
   uploaded in step 3 of the deploy (not a stub/placeholder).
5. **Verify the ledger record was written:** in the Cloudflare dashboard,
   open the R2 bucket from step 3 → browse the `ledger/` prefix → confirm a
   new object exists for this purchase (path shape:
   `ledger/<yyyy>/<mm>/<dd>/<unix-ms>-<hash prefix>.jsonl`, see
   `docs/LEDGER.md`). Open it and confirm `amount` is `7900` and
   `session_id_sha256` is present — the raw Stripe session id must **not**
   appear anywhere in the object. Note this object's key now; delete it
   after the rehearsal so it doesn't linger in the real sales ledger.
6. **Verify unauthenticated access is denied:** request
   `https://<worker-url>/api/download?product=forge-playbook` with no `exp`
   or `sig` query params — confirm **HTTP 403**.
7. **Verify the replay/rate limit:** the counter is only touched by
   `/api/grant` (`worker/src/rateLimit.js`, called from
   `worker/src/index.js:handleGrant`) — replaying the *download* URL from
   step 4 never trips it. Instead, re-request the **grant** URL from step 3
   (`.../api/grant?session_id=cs_...`, same session id) more than
   `MAX_GRANTS_PER_SESSION` times (default 5, `worker/wrangler.toml`).
   Confirm the requests beyond the limit come back **HTTP 403**, and that
   the ledger still shows exactly one entry for this session (only the
   first grant writes a ledger record).
8. **Refund the test purchase:** in the Stripe Dashboard, open the payment
   under **Payments**, click **Refund**, refund the full amount. (Test-mode
   payments aren't real money, but refunding confirms your refund flow
   works before you need it for a real customer.)
9. Delete the test ledger object you noted in step 5, then switch
   `STRIPE_SECRET_KEY` back to the live key as described above.

Only after this checklist passes should you finish step 2.5 (wire the real
Payment Link into production `config.js` and merge/deploy).

## 6. Where the ledger lands, and pointing revenue tracking at it

Every completed sale writes one JSONL object to the `PLAYBOOK_BUCKET` R2
bucket under the `ledger/` prefix — full schema, key format, and the
known KV-race caveat are documented in `docs/LEDGER.md`. There is no
separate ledger database; R2 **is** the ledger.

To point your revenue tracking at it:

- **Manual / dashboard:** Cloudflare dashboard → R2 → `<YOUR_BUCKET_NAME>` →
  browse `ledger/`. Objects are timestamp-prefixed, so listing the prefix in
  key order and concatenating the objects yields a valid chronological JSONL
  file (`docs/LEDGER.md` explains why it's one object per sale instead of
  one growing file).
- **Programmatic:** R2 exposes an S3-compatible API. Configure any
  S3-compatible client (`aws s3` CLI, `rclone`, or your revenue assessor's
  own ingestion job) with an R2 API token (Cloudflare dashboard → R2 → Manage
  API tokens) and point it at the bucket's S3 endpoint
  (`https://<account-id>.r2.cloudflarestorage.com`), listing/syncing the
  `ledger/` prefix on whatever cadence you need.

## Rollback: take the buy button offline in one step

**Deactivate the Stripe Payment Link** — Stripe Dashboard → Payment Links →
select the link from step 2 → **Deactivate**. This takes effect immediately:
anyone who already has the URL (including the one wired into
`site/config.js`) hits Stripe's own "this link is no longer active" page
instead of a checkout form. No redeploy of the storefront or the Worker is
needed, and no code changes are required — this is genuinely the fastest
path to stop new sales. (It only stops *new* checkouts — any signed download
URL already issued to a past customer stays valid until it naturally
expires, per `DOWNLOAD_TTL_SECONDS`; that's expected and not a rollback gap.)

Reactivating is the same toggle in reverse, once you're ready to resume.

(A slower, code-level alternative — reverting `paymentLinkUrl` in
`site/config.js` to a placeholder and redeploying the storefront page —
also works, but requires a PR + merge + redeploy cycle and doesn't stop
anyone who already loaded the old page with the live link cached. Prefer
the Stripe-side toggle.)

## What happens at delivery time (watermarking)

`/api/grant` verifies the Stripe checkout session, then hands back a
short-lived signed URL to `/api/download` — this part is unchanged from the
original gate design. What changed: **`/api/download` never streams the raw
R2 object.** On every request it:

1. Fetches the raw artifact bytes from R2 (the plain file uploaded in §3).
2. Builds a watermark sentence: the buyer's email domain (e.g. "buyer at
   example.com" — only the domain, not the full address, appears in the
   document itself), the order id (first 8 hex chars of the same
   sha256(session id) written to the sales ledger, see `docs/LEDGER.md`),
   the Stripe purchase date, and the delivery date. If the buyer's email is
   unavailable, the watermark falls back to the order id alone.
3. Stamps that sentence into an **in-memory copy** of the artifact — a
   low-opacity footer on every PDF page (`pdf-lib`), a footer banner
   injected into the HTML (`worker/src/watermark/html.js`), or a stamped
   colophon on the EPUB's first content document (`worker/src/watermark/
   epub.js` — see that file's header comment for why a full spine-edit
   colophon was not used).
4. Streams the stamped bytes back as the response. The unstamped bytes
   never leave the Worker.

Every buyer therefore gets their own distinct copy, generated on the fly —
there's no watermarked master to keep in sync, and no way to reach the raw
file directly: `/api/download` fails closed (500) rather than falling back
to raw bytes if the artifact's format isn't recognized or stamping fails.

This is a deterrence measure, not DRM: a buyer can still read, print, or
copy their book freely. What they can't do unnoticed is redistribute it
without a marker tying that specific copy back to their purchase.
