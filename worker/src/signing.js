// Time-limited signed URLs for the content route. HMAC-SHA256 over
// `${path}:${exp}` keyed by env.SIGNING_SECRET, verified with a
// constant-time comparison.

const DEFAULT_TTL_SECONDS = 300;

function toHex(buffer) {
  return Array.from(new Uint8Array(buffer))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

async function hmac(secret, message) {
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const signature = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(message));
  return toHex(signature);
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let mismatch = 0;
  for (let i = 0; i < a.length; i++) {
    mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return mismatch === 0;
}

/**
 * Sign `path` so it is usable until now + ttlSeconds.
 * @returns {Promise<{exp: number, sig: string}>}
 */
export async function signPath(secret, path, ttlSeconds = DEFAULT_TTL_SECONDS) {
  const exp = Date.now() + ttlSeconds * 1000;
  const sig = await hmac(secret, `${path}:${exp}`);
  return { exp, sig };
}

/**
 * Verify a (path, exp, sig) triple: signature must match and exp must not
 * have passed.
 * @returns {Promise<boolean>}
 */
export async function verifySignedPath(secret, path, exp, sig) {
  if (!exp || !sig) return false;
  const expNum = Number(exp);
  if (!Number.isFinite(expNum) || Date.now() > expNum) return false;

  const expected = await hmac(secret, `${path}:${expNum}`);
  return timingSafeEqual(expected, sig);
}

export { DEFAULT_TTL_SECONDS };
