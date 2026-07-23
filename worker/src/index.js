import { EntitlementType, registerEntitlementVerifier, getEntitlementVerifier } from './entitlements.js';
import { verifyPurchase } from './stripe.js';
import { checkAndRecordGrant } from './rateLimit.js';
import { appendSale } from './ledger.js';
import { signDownloadToken, verifyDownloadToken } from './signing.js';
import { sha256Hex } from './hash.js';
import {
  WatermarkFormat,
  registerWatermarker,
  getWatermarker,
  detectFormat,
  contentTypeForFormat,
  buildWatermarkText,
} from './watermark.js';
import { stamp as stampPdf } from './watermark/pdf.js';
import { stamp as stampHtml } from './watermark/html.js';
import { stamp as stampEpub } from './watermark/epub.js';

registerEntitlementVerifier(EntitlementType.PURCHASE, verifyPurchase);
registerWatermarker(WatermarkFormat.PDF, stampPdf);
registerWatermarker(WatermarkFormat.HTML, stampHtml);
registerWatermarker(WatermarkFormat.EPUB, stampEpub);

const WATERMARK_KV_PREFIX = 'watermark:';
// Margin over the download token's own TTL so the GRANTS_KV watermark record
// outlives every signed URL minted against it (the token is the thing that
// actually gates access; this is just a buffer against clock skew / a
// download landing right at expiry).
const WATERMARK_KV_TTL_MARGIN_SECONDS = 300;
const MIN_KV_EXPIRATION_TTL_SECONDS = 60; // Workers KV's own floor

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

function deliveryFilename(objectKey, product) {
  const base = objectKey.includes('/') ? objectKey.slice(objectKey.lastIndexOf('/') + 1) : objectKey;
  return base || product;
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

  // Computed once and reused for the ledger's sha256'd session identifier
  // (full hash) and the watermark/order id (its first 8 hex chars) — both
  // derive from the same grant, so there's a single source of truth.
  const grantKeySha256 = await sha256Hex(result.grantKey);
  const orderId = grantKeySha256.slice(0, 8);

  // The ledger is a *sales* ledger — only entitlements tied to a real
  // transaction (amount !== null) are recorded. A future non-revenue
  // entitlement type (e.g. membership) can return amount: null and simply
  // won't appear here; no change to this route is needed either way.
  if (rl.isFirstGrant && result.amount !== null) {
    await appendSale(env, {
      sessionIdSha256: grantKeySha256,
      product: result.product,
      amount: result.amount,
    });
  }

  const ttlSeconds = Number.parseInt(env.DOWNLOAD_TTL_SECONDS, 10) || undefined;

  // Buyer identity for the watermark is looked up at download time from
  // GRANTS_KV, keyed by orderId — never carried in the signed URL itself.
  // The URL is a bearer credential in browser history/referrers/logs for as
  // long as it's valid; a buyer's email doesn't belong riding along with it.
  await env.GRANTS_KV.put(
    WATERMARK_KV_PREFIX + orderId,
    JSON.stringify({ buyerEmail: result.buyerEmail ?? null, purchasedAt: result.purchasedAt ?? null }),
    {
      expirationTtl: Math.max(
        (ttlSeconds ?? 24 * 60 * 60) + WATERMARK_KV_TTL_MARGIN_SECONDS,
        MIN_KV_EXPIRATION_TTL_SECONDS
      ),
    }
  );

  const token = await signDownloadToken(result.product, orderId, env, ttlSeconds);
  const downloadUrl = new URL('/api/download', url.origin);
  downloadUrl.searchParams.set('product', token.product);
  downloadUrl.searchParams.set('order', token.orderId);
  downloadUrl.searchParams.set('exp', String(token.exp));
  downloadUrl.searchParams.set('sig', token.sig);

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
 * The raw artifact is never returned as-is: every delivery fetches the raw
 * bytes from private storage, stamps a per-buyer watermark into an in-
 * memory copy, and streams that copy back. There is no code path — success
 * or failure — that responds with the unstamped object body.
 */
async function handleDownload(request, env) {
  const url = new URL(request.url);
  const product = url.searchParams.get('product');
  const orderId = url.searchParams.get('order');
  const exp = url.searchParams.get('exp');
  const sig = url.searchParams.get('sig');

  const verification = await verifyDownloadToken(product, orderId, exp, sig, env);
  if (!verification.valid) {
    return json({ error: verification.reason || 'invalid token' }, 403);
  }

  const objectKey = resolveArtifactKey(product, env);
  const format = detectFormat(objectKey);
  const watermarker = format && getWatermarker(format);
  if (!watermarker) {
    // Fail closed — an unrecognized deliverable format must never fall
    // through to serving the raw object un-stamped.
    return json({ error: 'unsupported artifact format' }, 500);
  }

  const object = await env.PLAYBOOK_BUCKET.get(objectKey);
  if (!object) {
    return json({ error: 'artifact not found' }, 404);
  }

  let watermarkRecord = {};
  const rawRecord = await env.GRANTS_KV.get(WATERMARK_KV_PREFIX + orderId);
  if (rawRecord) {
    try {
      watermarkRecord = JSON.parse(rawRecord);
    } catch {
      watermarkRecord = {};
    }
  }

  const watermarkText = buildWatermarkText({
    buyerEmail: watermarkRecord.buyerEmail || null,
    orderId,
    purchasedAt: watermarkRecord.purchasedAt || null,
    deliveryDate: new Date().toISOString().slice(0, 10),
  });

  let stampedBytes;
  try {
    const rawBytes = new Uint8Array(await object.arrayBuffer());
    stampedBytes = await watermarker(rawBytes, watermarkText);
  } catch (err) {
    return json({ error: 'failed to prepare watermarked delivery' }, 500);
  }

  return new Response(stampedBytes, {
    status: 200,
    headers: {
      'Content-Type': contentTypeForFormat(format),
      'Content-Disposition': `attachment; filename="${deliveryFilename(objectKey, product)}"`,
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
