# Deploy Runbook

How the entitlement gate Worker (`worker/`) goes from this repo to a live,
paid, watermarked download at forgeframework.dev.

## 1. Prerequisites (operator-supplied, never committed)

- A Cloudflare account with Workers, R2, and KV enabled.
- A Stripe account with a checkout link/product configured for the book.
- `wrangler` installed and authenticated (`wrangler login`).

## 2. Create the R2 bucket and KV namespace

```
wrangler r2 bucket create <bucket-name>
wrangler kv namespace create GRANTS_KV
```

Put the resulting bucket name and KV namespace id into `worker/wrangler.toml`
in place of the `REPLACE_WITH_*` placeholders.

## 3. Upload the paid book — unwatermarked, private

The full book is **never committed to this repo** — only the free samples
under `assets/` are. Upload the real artifacts directly to the R2 bucket
under the `artifacts/` prefix, matching the keys configured in
`ARTIFACT_KEYS` (`worker/wrangler.toml`), e.g.:

```
wrangler r2 object put <bucket-name>/artifacts/forge-playbook.epub --file ./forge-playbook.epub
wrangler r2 object put <bucket-name>/artifacts/forge-playbook.pdf  --file ./forge-playbook.pdf
wrangler r2 object put <bucket-name>/artifacts/forge-playbook.html --file ./forge-playbook.html
```

**Upload the plain, un-watermarked file.** Watermarking happens per-buyer at
delivery time (see §5) — R2 only ever holds one unstamped master copy per
format, regardless of how many copies get sold. There is no "watermarked
master" to generate or store.

## 4. Configure secrets and vars

Secrets (never in `wrangler.toml`, never committed):

```
wrangler secret put STRIPE_SECRET_KEY
wrangler secret put SIGNING_SECRET   # any high-entropy random string
```

Vars (`worker/wrangler.toml` `[vars]`): `MAX_GRANTS_PER_SESSION`,
`DOWNLOAD_TTL_SECONDS`, `DEFAULT_PRODUCT`, `ARTIFACT_KEYS` — see the inline
comments in `wrangler.toml` for what each controls.

## 5. Deploy

```
cd worker
npm install
npm test
wrangler deploy
```

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

## Promoting into the production site repo

The storefront page (`site/`) and this Worker are developed here, then
promoted into the production website repo via a single reviewed PR — no
direct pushes, per this project's hard rules (see `README.md`).
