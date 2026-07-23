import { EntitlementType, registerEntitlementVerifier, getEntitlementVerifier } from './entitlements.js';
import { verifyPurchase } from './stripe.js';
import { checkAndRecordGrant } from './rateLimit.js';
import { appendSale } from './ledger.js';
import { signDownloadToken, verifyDownloadToken } from './signing.js';
import { sha256Hex } from './hash.js';

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

  // The ledger is a *sales* ledger — only entitlements tied to a real
  // transaction (amount !== null) are recorded. A future non-revenue
  // entitlement type (e.g. membership) can return amount: null and simply
  // won't appear here; no change to this route is needed either way.
  if (rl.isFirstGrant && result.amount !== null) {
    const sessionIdSha256 = await sha256Hex(result.grantKey);
    await appendSale(env, {
      sessionIdSha256,
      product: result.product,
      amount: result.amount,
    });
  }

  const ttlSeconds = Number.parseInt(env.DOWNLOAD_TTL_SECONDS, 10) || undefined;
  const token = await signDownloadToken(result.product, env, ttlSeconds);
  const downloadUrl = new URL('/api/download', url.origin);
  downloadUrl.searchParams.set('product', token.product);
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
 */
async function handleDownload(request, env) {
  const url = new URL(request.url);
  const product = url.searchParams.get('product');
  const exp = url.searchParams.get('exp');
  const sig = url.searchParams.get('sig');

  const verification = await verifyDownloadToken(product, exp, sig, env);
  if (!verification.valid) {
    return json({ error: verification.reason || 'invalid token' }, 403);
  }

  const objectKey = resolveArtifactKey(product, env);
  const object = await env.PLAYBOOK_BUCKET.get(objectKey);
  if (!object) {
    return json({ error: 'artifact not found' }, 404);
  }

  return new Response(object.body, {
    status: 200,
    headers: {
      'Content-Type': object.httpMetadata?.contentType || 'application/octet-stream',
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
