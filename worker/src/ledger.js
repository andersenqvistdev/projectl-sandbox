/**
 * Sales ledger — one R2 object per sale, keyed deterministically off the
 * (hashed) checkout session id so a retried/duplicated grant call can never
 * write a second record for the same sale. See docs/LEDGER.md for the full
 * schema, the key layout, and why this isn't date-prefixed.
 *
 * Callers must already have hashed the raw Stripe session id (sha256, hex)
 * before calling this — the raw id is never accepted or stored here.
 */

const SHA256_HEX = /^[0-9a-f]{64}$/;

function ledgerObjectKey(sessionIdSha256) {
  return `ledger/${sessionIdSha256.slice(0, 2)}/${sessionIdSha256}.jsonl`;
}

/**
 * Idempotent: if a ledger object already exists for this session hash, the
 * call is a no-op (written: false) rather than appending a duplicate sale.
 * The head-then-put has a small race window under true concurrent calls for
 * the same session; the conditional put (onlyIf.etagDoesNotMatch) narrows
 * it further on real R2 — but a *failed* conditional put resolves to
 * `null`, it does not throw, so the `put` result must be checked explicitly
 * or the losing side of a race would wrongly report `written: true`. See
 * docs/LEDGER.md for the residual case this doesn't close.
 */
export async function appendSale(env, { sessionIdSha256, product, amount, timestamp = Date.now() }) {
  if (!SHA256_HEX.test(sessionIdSha256)) {
    throw new Error('appendSale: sessionIdSha256 must be a 64-char lowercase hex sha256 digest');
  }
  const bucket = env.LEDGER_BUCKET;
  if (!bucket) {
    throw new Error('appendSale: env.LEDGER_BUCKET is not configured');
  }

  const key = ledgerObjectKey(sessionIdSha256);
  const existing = await bucket.head(key);
  if (existing) {
    return { key, record: null, written: false };
  }

  const record = {
    ts: new Date(timestamp).toISOString(),
    session_id_sha256: sessionIdSha256,
    product,
    amount,
  };
  const putResult = await bucket.put(key, `${JSON.stringify(record)}\n`, {
    httpMetadata: { contentType: 'application/x-ndjson' },
    onlyIf: { etagDoesNotMatch: '*' },
  });
  if (!putResult) {
    // Precondition failed: another writer landed an object at this key
    // between our head() check and this put(). Not an error, not a
    // duplicate sale — just a losing race that resolves to a no-op.
    return { key, record: null, written: false };
  }
  return { key, record, written: true };
}
