import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/index.js';
import { createTestEnv, paidSession, PAID_SESSION_PURCHASE_DATE, readSampleAsset } from './helpers.js';
import { sha256Hex } from '../src/hash.js';
import { shortOrderId } from '../src/orderId.js';
import { PDFDocument } from 'pdf-lib';
// Reaches past pdf-lib's public entry point (cjs/index.js) into an internal
// module to decode content streams for assertions below — pdf-lib has no
// "exports" map blocking this today, but it's not a documented API. If a
// pdf-lib upgrade breaks this import, pin the dependency (see
// worker/package.json) rather than chasing the internal path further.
import { decodePDFRawStream } from 'pdf-lib/cjs/core/streams/decode.js';
import { unzipSync, strFromU8 } from 'fflate';
import { signDownloadToken } from '../src/signing.js';

// Every assertion in this file checks the same rule from three angles per
// format: the watermarked output carries the buyer/order/date stamp, and
// the raw un-watermarked sample bytes are never what comes back.

async function expectedOrderId(sessionId) {
  return shortOrderId(await sha256Hex(sessionId));
}

/**
 * pdf-lib draws text as a hex string operator (e.g. `<4C6963...> Tj`), not a
 * literal `(...)` string, and the content stream carrying it is FlateDecode
 * compressed. Decode every content stream on every page via pdf-lib's own
 * stream decoder (so this doesn't hand-roll PDF parsing) and check for the
 * substring either literally or as its uppercase hex encoding.
 */
async function pdfContainsText(bytes, substring) {
  const doc = await PDFDocument.load(bytes);
  let all = '';
  for (const page of doc.getPages()) {
    const contents = page.node.Contents();
    if (!contents) continue;
    const refs = contents.asArray ? contents.asArray() : [contents];
    for (const ref of refs) {
      const stream = doc.context.lookup(ref);
      all += Buffer.from(decodePDFRawStream(stream).decode()).toString('latin1');
    }
  }
  const hex = Buffer.from(substring, 'latin1').toString('hex').toUpperCase();
  return all.includes(substring) || all.toUpperCase().includes(hex);
}

async function grantAndDownload(env, sessionId = 'cs_test_paid_1') {
  const grantRes = await worker.fetch(
    new Request(`https://example.com/api/grant?session_id=${sessionId}`),
    env
  );
  assert.equal(grantRes.status, 200, 'grant should succeed for a paid session');
  const { url } = await grantRes.json();
  const downloadRes = await worker.fetch(new Request(url), env);
  return downloadRes;
}

test('watermarked PDF contains the order id and the purchase date in a page footer', async () => {
  const env = createTestEnv({ sessions: { cs_test_paid_1: paidSession() } });
  const original = readSampleAsset('forge-playbook-sample.pdf');
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.pdf', original, {
    httpMetadata: { contentType: 'application/pdf' },
  });
  env.ARTIFACT_KEYS = JSON.stringify({ 'forge-playbook': 'artifacts/forge-playbook.pdf' });

  const res = await grantAndDownload(env);
  assert.equal(res.status, 200);
  const bytes = new Uint8Array(await res.arrayBuffer());

  const orderId = await expectedOrderId('cs_test_paid_1');
  assert.ok(await pdfContainsText(bytes, orderId), 'expected the order id to appear in the stamped PDF');
  assert.ok(
    await pdfContainsText(bytes, PAID_SESSION_PURCHASE_DATE),
    'expected the purchase date to appear in the stamped PDF'
  );
  assert.notEqual(bytes.length, original.length, 'stamped PDF must not be byte-identical to the raw sample');
});

test('a buyer email with non-Latin-1 characters does not break PDF stamping', async () => {
  // Helvetica/WinAnsi can't encode most non-Latin-1 scripts; the stamped
  // PDF must still succeed (falling back to an ASCII-safe rendering)
  // instead of the download permanently 502ing for this buyer.
  const env = createTestEnv({
    sessions: { cs_test_paid_1: paidSession({ customer_details: { email: '用户@example.com' } }) },
  });
  const original = readSampleAsset('forge-playbook-sample.pdf');
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.pdf', original, {
    httpMetadata: { contentType: 'application/pdf' },
  });
  env.ARTIFACT_KEYS = JSON.stringify({ 'forge-playbook': 'artifacts/forge-playbook.pdf' });

  const res = await grantAndDownload(env);
  assert.equal(res.status, 200);
  const bytes = new Uint8Array(await res.arrayBuffer());

  const orderId = await expectedOrderId('cs_test_paid_1');
  assert.ok(await pdfContainsText(bytes, orderId), 'expected the order id to still appear in the stamped PDF');
});

test('watermarked HTML contains the buyer identifier and the purchase date in a footer banner', async () => {
  const env = createTestEnv({ sessions: { cs_test_paid_1: paidSession() } });
  const original = readSampleAsset('forge-playbook-sample.html');
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.html', original, {
    httpMetadata: { contentType: 'text/html' },
  });
  env.ARTIFACT_KEYS = JSON.stringify({ 'forge-playbook': 'artifacts/forge-playbook.html' });

  const res = await grantAndDownload(env);
  assert.equal(res.status, 200);
  const text = await res.text();

  // Email is obfuscated ("@" -> " at ") in the stamp itself.
  assert.ok(text.includes('buyer at example.com'), 'expected the buyer identifier in the stamped HTML');
  assert.ok(text.includes(PAID_SESSION_PURCHASE_DATE), 'expected the purchase date in the stamped HTML');
  assert.notEqual(
    Buffer.from(text).length,
    original.length,
    'stamped HTML must not be byte-identical to the raw sample'
  );
});

test('watermarked EPUB carries a colophon with the buyer identifier and the purchase date', async () => {
  const env = createTestEnv({ sessions: { cs_test_paid_1: paidSession() } });
  const original = readSampleAsset('forge-playbook-sample.epub');
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.epub', original, {
    httpMetadata: { contentType: 'application/epub+zip' },
  });

  const res = await grantAndDownload(env);
  assert.equal(res.status, 200);
  const bytes = new Uint8Array(await res.arrayBuffer());
  assert.notEqual(bytes.length, original.length, 'stamped EPUB must not be byte-identical to the raw sample');

  const files = unzipSync(bytes);
  const colophonPath = Object.keys(files).find((name) => name.endsWith('text/colophon.xhtml'));
  assert.ok(colophonPath, 'expected a colophon xhtml file to be added to the EPUB');
  const colophon = strFromU8(files[colophonPath]);
  assert.ok(colophon.includes('buyer at example.com'), 'expected the buyer identifier in the colophon');
  assert.ok(colophon.includes(PAID_SESSION_PURCHASE_DATE), 'expected the purchase date in the colophon');

  const opfPath = Object.keys(files).find((name) => name.endsWith('content.opf'));
  const opf = strFromU8(files[opfPath]);
  assert.ok(opf.includes('colophon_xhtml'), 'expected the colophon to be registered in the OPF manifest/spine');
});

test('a buyer with no email collected falls back to the order id alone', async () => {
  const env = createTestEnv({
    sessions: { cs_test_paid_1: paidSession({ customer_details: null }) },
  });
  const original = readSampleAsset('forge-playbook-sample.html');
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.html', original, {
    httpMetadata: { contentType: 'text/html' },
  });
  env.ARTIFACT_KEYS = JSON.stringify({ 'forge-playbook': 'artifacts/forge-playbook.html' });

  const res = await grantAndDownload(env);
  assert.equal(res.status, 200);
  const text = await res.text();

  const orderId = await expectedOrderId('cs_test_paid_1');
  assert.ok(text.includes(orderId), 'expected the order id to stand in for the missing email');
  assert.ok(!text.includes('buyer at example.com'), 'expected no obfuscated email fragment when no email was collected');
});

test('a valid token whose watermark KV record is missing degrades to an order-id-only stamp instead of failing', async () => {
  // Signs a token directly (bypassing /api/grant, which always writes the
  // KV record) so the download route sees a verified order id with no
  // corresponding watermarkStore entry — the "expired/race" case documented
  // in watermarkStore.js.
  const env = createTestEnv();
  const original = readSampleAsset('forge-playbook-sample.html');
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.html', original, {
    httpMetadata: { contentType: 'text/html' },
  });
  env.ARTIFACT_KEYS = JSON.stringify({ 'forge-playbook': 'artifacts/forge-playbook.html' });

  const orderId = 'deadbeefdeadbeef';
  const token = await signDownloadToken('forge-playbook', env, 900, orderId);
  const res = await worker.fetch(
    new Request(
      `https://example.com/api/download?product=${token.product}&exp=${token.exp}&sig=${token.sig}&order=${token.orderId}`
    ),
    env
  );
  assert.equal(res.status, 200, 'a missing KV record must not fail the download');
  const text = await res.text();
  assert.ok(text.includes(orderId), 'expected the order id to appear even without a KV record');
});

test('missing entitlement still returns 403 and never reaches the artifact (regression guard)', async () => {
  const env = createTestEnv({ sessions: {} });
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.epub', readSampleAsset('forge-playbook-sample.epub'), {
    httpMetadata: { contentType: 'application/epub+zip' },
  });

  const grantRes = await worker.fetch(
    new Request('https://example.com/api/grant?session_id=cs_test_does_not_exist'),
    env
  );
  assert.equal(grantRes.status, 403);

  const res = await worker.fetch(
    new Request('https://example.com/api/download?product=forge-playbook'),
    env
  );
  assert.equal(res.status, 403);
});

test('an artifact whose format cannot be resolved fails closed instead of serving raw bytes', async () => {
  const env = createTestEnv({ sessions: { cs_test_paid_1: paidSession() } });
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.bin', 'not a real book, wrong extension');
  env.ARTIFACT_KEYS = JSON.stringify({ 'forge-playbook': 'artifacts/forge-playbook.bin' });

  const res = await grantAndDownload(env);
  assert.equal(res.status, 502);
});
