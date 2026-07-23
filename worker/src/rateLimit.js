import { sha256Hex } from './hash.js';

const DEFAULT_MAX_GRANTS = 5;

/**
 * KV-backed grant counter, keyed by sha256(type + grantKey) so grant keys
 * from different entitlement types never collide.
 *
 * Known limitation: Workers KV has no atomic read-modify-write, so two
 * requests racing within the same eventual-consistency window can both
 * observe count 0 and both be told isFirstGrant. Documented (with the
 * accepted-risk rationale) in docs/LEDGER.md rather than solved with a
 * Durable Object, which is out of scope for this iteration.
 */
export async function checkAndRecordGrant(env, type, grantKey) {
  const hash = await sha256Hex(`${type}:${grantKey}`);
  const key = `grant:${hash}`;
  const maxGrants = Number.parseInt(env.MAX_GRANTS_PER_SESSION, 10) || DEFAULT_MAX_GRANTS;

  const raw = await env.GRANTS_KV.get(key);
  const count = raw ? Number.parseInt(raw, 10) : 0;

  if (count >= maxGrants) {
    return { allowed: false, isFirstGrant: false, count, hash };
  }

  const newCount = count + 1;
  await env.GRANTS_KV.put(key, String(newCount));

  return { allowed: true, isFirstGrant: count === 0, count: newCount, hash };
}
