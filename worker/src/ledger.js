/**
 * Sales ledger — one R2 object per sale, each containing exactly one JSONL
 * line. See docs/LEDGER.md for the full schema and the rationale for this
 * append pattern (avoids read-modify-write races on a single growing
 * object; a production consumer concatenates objects under the `ledger/`
 * prefix, in key order, to reconstitute the full JSONL file).
 */

function pad(n, width) {
  return String(n).padStart(width, '0');
}

function ledgerObjectKey(timestamp, sessionIdSha256) {
  const d = new Date(timestamp);
  const datePrefix = `${d.getUTCFullYear()}/${pad(d.getUTCMonth() + 1, 2)}/${pad(d.getUTCDate(), 2)}`;
  return `ledger/${datePrefix}/${timestamp}-${sessionIdSha256.slice(0, 16)}.jsonl`;
}

export async function appendSale(env, { sessionIdSha256, product, amount, timestamp = Date.now() }) {
  const record = {
    ts: new Date(timestamp).toISOString(),
    session_id_sha256: sessionIdSha256,
    product,
    amount,
  };
  const key = ledgerObjectKey(timestamp, sessionIdSha256);
  const bucket = env.LEDGER_BUCKET || env.PLAYBOOK_BUCKET;
  await bucket.put(key, `${JSON.stringify(record)}\n`, {
    httpMetadata: { contentType: 'application/x-ndjson' },
  });
  return { key, record };
}
