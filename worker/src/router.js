import { checkEntitlement } from './entitlements.js';
import { signPath, verifySignedPath, DEFAULT_TTL_SECONDS } from './signing.js';

function json(data, status) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function bearerToken(request) {
  const header = request.headers.get('Authorization') || '';
  return header.startsWith('Bearer ') ? header.slice('Bearer '.length).trim() : null;
}

async function handleEntitlement(request, env, productId) {
  const token = bearerToken(request);
  const result = await checkEntitlement(env.ENTITLEMENTS, token, productId);
  if (!result.valid) {
    return json({ error: 'forbidden', reason: result.reason }, 403);
  }

  const contentPath = `/content/${productId}`;
  const ttl = Number(env.SIGNED_URL_TTL_SECONDS) || DEFAULT_TTL_SECONDS;
  const { exp, sig } = await signPath(env.SIGNING_SECRET, contentPath, ttl);
  return json({ url: `${contentPath}?exp=${exp}&sig=${sig}`, expiresAt: exp }, 200);
}

async function handleContent(request, env, productId, url) {
  const contentPath = `/content/${productId}`;
  const exp = url.searchParams.get('exp');
  const sig = url.searchParams.get('sig');

  const ok = await verifySignedPath(env.SIGNING_SECRET, contentPath, exp, sig);
  if (!ok) {
    return json({ error: 'forbidden' }, 403);
  }

  // Private storage binding (e.g. R2) is deploy-time configuration, wired
  // up by the promotion runbook (G5). Without it the gate still holds: a
  // valid signature just gets you a "not configured yet" instead of bytes.
  if (env.CONTENT_STORE && typeof env.CONTENT_STORE.get === 'function') {
    const object = await env.CONTENT_STORE.get(productId);
    if (!object) {
      return json({ error: 'not_found' }, 404);
    }
    return new Response(object.body ?? object, { status: 200 });
  }

  return json({ error: 'storage_not_configured' }, 501);
}

/**
 * Worker entrypoint: verify-entitlement-then-serve.
 *
 * Routes:
 *   GET /entitlement/:productId  - verify entitlement, mint a time-limited
 *                                   signed URL for the content route
 *   GET /content/:productId      - serve paid content, but only given a
 *                                   valid, unexpired signature; never
 *                                   unauthenticated
 *
 * @param {Request} request
 * @param {*} env
 * @returns {Promise<Response>}
 */
export async function handleRequest(request, env) {
  const url = new URL(request.url);
  const segments = url.pathname.split('/').filter(Boolean);

  if (request.method !== 'GET') {
    return json({ error: 'method_not_allowed' }, 405);
  }

  if (segments.length === 2 && segments[0] === 'entitlement') {
    return handleEntitlement(request, env, segments[1]);
  }

  if (segments.length === 2 && segments[0] === 'content') {
    return handleContent(request, env, segments[1], url);
  }

  return json({ error: 'not_found' }, 404);
}
