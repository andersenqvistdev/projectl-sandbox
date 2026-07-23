import { EntitlementType, registerEntitlementVerifier, getEntitlementVerifier } from './entitlements.js';
import { verifyPurchase } from './stripe.js';
import { checkAndRecordGrant } from './rateLimit.js';
import { appendSale } from './ledger.js';
import { signDownloadToken, verifyDownloadToken } from './signing.js';
import { sha256Hex } from './hash.js';
import { shortOrderId } from './orderId.js';
import { formatDateUTC } from './dates.js';
import { storeWatermarkRecord, getWatermarkRecord } from './watermarkStore.js';
import { detectFormat, contentTypeForFormat, buildWatermarkText, watermarkArtifact } from './watermark.js';

registerEntitlementVerifier(EntitlementType.PURCHASE, verifyPurchase);

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function resolveArtifactKey(product, env) {
  let map = {};
  try {
    map = env.ARTIFACT_KEYS ? JSON.parse(env.ARTIFACT_KEYS) : {};
  } catch {
    map = {};
  }
  return map[product] || `artifacts/${product}`;
}

/**
 * The gate route: check entitlement first, only ever hand back a
 * short-lived signed URL on success. It dispatches purely through the
 * ENTITLEMENT_VERIFIERS registry — adding a new entitlement type never
 * requires touching this function.
 */
async function handleGrant(request, env) {
  const url = new URL(request.url);
  const type = url.searchParams.get('type') || EntitlementType.PURCHASE;

  const verifier = getEntitlementVerifier(type);
  if (!verifier) {
    return json({ error: 'unknown entitlement type' }, 403);
  }

  let result;
  try {
    result = await verifier(request, env);
  } catch (err) {
    return json({ error: 'entitlement verification failed' }, 502);
  }

  if (!result.granted) {
    return json({ error: result.reason || 'not entitled' }, result.status || 403);
  }

  const rl = await checkAndRecordGrant(env, type, result.grantKey);
  if (!rl.allowed) {
    return json({ error: 'grant limit exceeded for this session' }, 403);
  }

  // Every grant needs an order id for the watermark (not just the first,
  // revenue-bearing one — a repeat download re-derives the same id from the
  // same grantKey), so this hash is now computed unconditionally rather
  // than only inside the ledger-write branch below.
  const sessionIdSha256 = await sha256Hex(result.grantKey);
  const orderId = shortOrderId(sessionIdSha256);

  // The ledger is a *sales* ledger — only entitlements tied to a real
  // transaction (amount !== null) are recorded. A future non-revenue
  // entitlement type (e.g. membership) can return amount: null and simply
  // won't appear here; no change to this route is needed either way.
  if (rl.isFirstGrant && result.amount !== null) {
    await appendSale(env, {
      sessionIdSha256,
      product: result.product,
      amount: result.amount,
    });
  }

  const ttlSeconds = Number.parseInt(env.DOWNLOAD_TTL_SECONDS, 10) || undefined;

  // Buyer email/purchase date travel via KV, keyed by orderId, rather than
  // in the URL — see watermarkStore.js for why. The signed token binds
  // orderId itself so a tampered value invalidates the signature.
  await storeWatermarkRecord(
    env,
    orderId,
    {
      buyerEmail: result.buyerEmail || null,
      purchaseDate: result.purchaseDate || formatDateUTC(Math.floor(Date.now() / 1000)),
    },
    ttlSeconds
  );

  const token = await signDownloadToken(result.product, env, ttlSeconds, orderId);
  const downloadUrl = new URL('/api/download', url.origin);
  downloadUrl.searchParams.set('product', token.product);
  downloadUrl.searchParams.set('exp', String(token.exp));
  downloadUrl.searchParams.set('sig', token.sig);
  downloadUrl.searchParams.set('order', token.orderId);

  return json({
    url: downloadUrl.toString(),
    expires_at: new Date(token.exp * 1000).toISOString(),
  });
}

/**
 * The only route that reads the R2 bucket. It never trusts the `product`
 * query param on its own — it requires a signature minted by handleGrant
 * (i.e. only ever issued after a successful entitlement check) and re-
 * validates the expiry against the current time on every request.
 *
 * It never streams the R2 object straight through, either: the artifact is
 * always watermarked into a fresh per-buyer copy first. There is no code
 * path here that returns the fetched bytes unmodified — if the format
 * can't be resolved or stamping fails, the request fails closed instead of
 * falling back to the original bytes.
 *
 * Known accepted tradeoff: unlike /api/grant, this route has no replay
 * limit of its own — a given signed URL can be re-downloaded any number of
 * times before it expires, and each hit now does real CPU work (pdf-lib /
 * fflate) instead of the cheap passthrough this route used to do. A single
 * request's cost is still bounded by the Worker's per-request CPU limit, so
 * this is a cost/latency concern under heavy replay, not an unbounded one.
 * If that becomes a real problem, cache the stamped bytes per order id
 * (e.g. in R2) on first download and serve that on repeat hits.
 */
async function handleDownload(request, env) {
  const url = new URL(request.url);
  const product = url.searchParams.get('product');
  const exp = url.searchParams.get('exp');
  const sig = url.searchParams.get('sig');
  const orderId = url.searchParams.get('order') || '';

  const verification = await verifyDownloadToken(product, exp, sig, orderId, env);
  if (!verification.valid) {
    return json({ error: verification.reason || 'invalid token' }, 403);
  }

  const objectKey = resolveArtifactKey(product, env);
  const object = await env.PLAYBOOK_BUCKET.get(objectKey);
  if (!object) {
    return json({ error: 'artifact not found' }, 404);
  }

  const contentType = object.httpMetadata?.contentType;
  const format = detectFormat(objectKey, contentType);
  if (!format) {
    return json({ error: 'artifact format not supported for watermarking' }, 502);
  }

  // Missing here just means a degraded stamp (order id only, no email/
  // purchase date) — see watermarkStore.js for why this isn't a hard
  // failure; the order id itself came from the verified signature above,
  // so traceability is never fully lost.
  const watermarkRecord = orderId ? await getWatermarkRecord(env, orderId) : null;
  const deliveryDate = formatDateUTC(Math.floor(Date.now() / 1000));
  const watermarkText = buildWatermarkText({
    buyerEmail: watermarkRecord?.buyerEmail ?? null,
    orderId: orderId || 'unknown',
    purchaseDate: watermarkRecord?.purchaseDate ?? deliveryDate,
    deliveryDate,
  });

  let stampedBytes;
  try {
    const originalBytes = new Uint8Array(await object.arrayBuffer());
    stampedBytes = await watermarkArtifact({ format, bytes: originalBytes, text: watermarkText });
  } catch {
    return json({ error: 'failed to prepare watermarked download' }, 502);
  }

  return new Response(stampedBytes, {
    status: 200,
    headers: {
      'Content-Type': contentType || contentTypeForFormat(format),
      'Content-Disposition': `attachment; filename="${product}"`,
    },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== 'GET') {
      return json({ error: 'method not allowed' }, 405);
    }
    if (url.pathname === '/api/grant') {
      return handleGrant(request, env);
    }
    if (url.pathname === '/api/download') {
      return handleDownload(request, env);
    }
    return json({ error: 'not found' }, 404);
  },
};
