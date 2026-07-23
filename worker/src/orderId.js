/**
 * Short order id shared by the ledger and per-buyer watermarking: both need
 * a stable, non-reversible identifier derived from the same
 * sha256(session id) the ledger already computes — not two independently
 * chosen prefixes that could drift apart.
 */

export const SHORT_ORDER_ID_LENGTH = 16;

export function shortOrderId(sessionIdSha256) {
  return sessionIdSha256.slice(0, SHORT_ORDER_ID_LENGTH);
}
