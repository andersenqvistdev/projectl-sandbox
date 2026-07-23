/**
 * HMAC-SHA256 signed download tokens (Web Crypto — available in both the
 * Workers runtime and Node's global `crypto`).
 *
 * A signed token authorizes exactly one product for a bounded time window;
 * it is the only way /api/download will serve an R2 object. Nothing short-
 * circuits this check, so no route can serve paid content unauthenticated.
 */

const MAX_TTL_SECONDS = 24 * 60 * 60; // 24h hard cap

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

function payloadString(product, exp) {
  return `${product}:${exp}`;
}

export async function signDownloadToken(product, env, ttlSeconds = MAX_TTL_SECONDS) {
  const ttl = Math.min(ttlSeconds, MAX_TTL_SECONDS);
  const exp = Math.floor(Date.now() / 1000) + ttl;
  const key = await importKey(env.SIGNING_SECRET);
  const sig = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(payloadString(product, exp)));
  return { product, exp, sig: toBase64Url(sig) };
}

export async function verifyDownloadToken(product, exp, sig, env) {
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
    new TextEncoder().encode(payloadString(product, expNum))
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
