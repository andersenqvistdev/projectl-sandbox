/**
 * Per-order watermark metadata (buyer email, purchase date), keyed by the
 * short order id and stored in the same GRANTS_KV namespace already bound
 * for rate-limiting — no new binding needed.
 *
 * This exists so the buyer's email never has to travel in the download
 * URL: the signed token only carries the order id (see signing.js), which
 * is not sensitive on its own, and the download route looks up the buyer
 * details server-side. The alternative — encoding email directly into the
 * signed URL — would put PII into edge/access logs and browser history for
 * the token's whole lifetime; this avoids that for the cost of one KV
 * read, using infrastructure the Worker already depends on.
 *
 * If the record is missing at download time (e.g. it expired, or KV's
 * eventual consistency hasn't caught up), the download route degrades to
 * stamping with the order id alone rather than failing the request — the
 * order id came from the verified signature, not this store, so
 * traceability is never fully lost even on a cache miss here.
 */

import { MAX_TTL_SECONDS } from './signing.js';

// Workers KV rejects expirationTtl below this, so a DOWNLOAD_TTL_SECONDS
// configured under 60s means this record can slightly outlive its download
// token (up to ~60s of extra exposure) — accepted, since that's an unusual
// deploy-time config choice, not the documented default.
const MIN_KV_TTL_SECONDS = 60;

function watermarkKey(orderId) {
  return `watermark:${orderId}`;
}

export async function storeWatermarkRecord(env, orderId, record, ttlSeconds = MAX_TTL_SECONDS) {
  const expirationTtl = Math.min(Math.max(ttlSeconds, MIN_KV_TTL_SECONDS), MAX_TTL_SECONDS);
  await env.GRANTS_KV.put(watermarkKey(orderId), JSON.stringify(record), { expirationTtl });
}

export async function getWatermarkRecord(env, orderId) {
  const raw = await env.GRANTS_KV.get(watermarkKey(orderId));
  return raw ? JSON.parse(raw) : null;
}
