/**
 * HMAC-SHA256 signed download tokens (Web Crypto — available in both the
 * Workers runtime and Node's global `crypto`).
 *
 * A signed token authorizes exactly one product for a bounded time window;
 * it is the only way /api/download will serve an R2 object. Nothing short-
 * circuits this check, so no route can serve paid content unauthenticated.
 */

export const MAX_TTL_SECONDS = 24 * 60 * 60; // 24h hard cap

async function importKey(secret) {
  return crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign', 'verify']
  );
}

function toBase64Url(bytes) {
  let binary = '';
  for (const b of new Uint8Array(bytes)) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

// orderId is bound into the signature (not just carried alongside it) so a
// client can't swap in a different order id and get a copy watermarked —
// and thus misattributed — to someone else's purchase. It defaults to ''
// for callers that predate watermarking; that's a different payload string
// than before, so tokens signed pre-deploy won't verify post-deploy, which
// is fine given the short default TTL (DOWNLOAD_TTL_SECONDS).
function payloadString(product, exp, orderId) {
  return `${product}:${exp}:${orderId || ''}`;
}

export async function signDownloadToken(product, env, ttlSeconds = MAX_TTL_SECONDS, orderId = '') {
  const ttl = Math.min(ttlSeconds ?? MAX_TTL_SECONDS, MAX_TTL_SECONDS);
  const exp = Math.floor(Date.now() / 1000) + ttl;
  const key = await importKey(env.SIGNING_SECRET);
  const sig = await crypto.subtle.sign(
    'HMAC',
    key,
    new TextEncoder().encode(payloadString(product, exp, orderId))
  );
  return { product, exp, sig: toBase64Url(sig), orderId };
}

export async function verifyDownloadToken(product, exp, sig, orderId, env) {
  const expNum = Number(exp);
  if (!product || !Number.isFinite(expNum) || !sig) {
    return { valid: false, reason: 'malformed token' };
  }
  if (Math.floor(Date.now() / 1000) > expNum) {
    return { valid: false, reason: 'expired' };
  }

  const key = await importKey(env.SIGNING_SECRET);
  const expectedSig = await crypto.subtle.sign(
    'HMAC',
    key,
    new TextEncoder().encode(payloadString(product, expNum, orderId))
  );
  const expectedSigB64 = toBase64Url(expectedSig);

  if (!timingSafeEqual(expectedSigB64, sig)) {
    return { valid: false, reason: 'bad signature' };
  }
  return { valid: true };
}

function timingSafeEqual(a, b) {
  const maxLen = Math.max(a.length, b.length);
  let diff = a.length === b.length ? 0 : 1;
  for (let i = 0; i < maxLen; i++) {
    const ca = i < a.length ? a.charCodeAt(i) : 0;
    const cb = i < b.length ? b.charCodeAt(i) : 0;
    diff |= ca ^ cb;
  }
  return diff === 0;
}
