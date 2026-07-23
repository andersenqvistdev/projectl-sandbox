import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/index.js';
import { createTestEnv, paidSession, loadSampleBytes } from './helpers.js';
import { signDownloadToken } from '../src/signing.js';
import { PDFParse } from 'pdf-parse';
import { unzipSync } from 'fflate';

const EPUB_BYTES = loadSampleBytes('forge-playbook-sample.epub');
const PDF_BYTES = loadSampleBytes('forge-playbook-sample.pdf');
const HTML_BYTES = loadSampleBytes('forge-playbook-sample.html');

test('unauthenticated direct artifact route is denied with 403 (no token)', async () => {
  const env = createTestEnv();
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.epub', EPUB_BYTES);

  const res = await worker.fetch(
    new Request('https://example.com/api/download?product=forge-playbook'),
    env
  );
  assert.equal(res.status, 403);
});

test('direct artifact route with a forged signature is denied with 403', async () => {
  const env = createTestEnv();
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.epub', EPUB_BYTES);

  const exp = Math.floor(Date.now() / 1000) + 3600;
  const res = await worker.fetch(
    new Request(
      `https://example.com/api/download?product=forge-playbook&order=a1b2c3d4&exp=${exp}&sig=not-a-real-signature`
    ),
    env
  );
  assert.equal(res.status, 403);
});

test('expired signed token is denied with 403', async () => {
  const env = createTestEnv();
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.epub', EPUB_BYTES);

  const token = await signDownloadToken('forge-playbook', 'a1b2c3d4', env, -10); // already expired
  const res = await worker.fetch(
    new Request(
      `https://example.com/api/download?product=${token.product}&order=${token.orderId}&exp=${token.exp}&sig=${token.sig}`
    ),
    env
  );
  assert.equal(res.status, 403);
});

test('a validly signed token (as issued by /api/grant) downloads a watermarked copy, never the raw bytes', async () => {
  const env = createTestEnv({ sessions: { cs_test_paid_1: paidSession() } });
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.epub', EPUB_BYTES);

  const grantRes = await worker.fetch(
    new Request('https://example.com/api/grant?session_id=cs_test_paid_1'),
    env
  );
  const { url } = await grantRes.json();
  assert.ok(new URL(url).searchParams.get('order'), 'grant response URL carries an order id');

  const downloadRes = await worker.fetch(new Request(url), env);
  assert.equal(downloadRes.status, 200);
  const bytes = new Uint8Array(await downloadRes.arrayBuffer());

  assert.notEqual(
    Buffer.from(bytes).toString('base64'),
    Buffer.from(EPUB_BYTES).toString('base64'),
    'delivered bytes must differ from the raw un-watermarked artifact'
  );

  const files = unzipSync(bytes);
  const cover = new TextDecoder().decode(files['EPUB/text/cover.xhtml']);
  assert.match(cover, /Licensed to buyer at example\.com/);
});

test('token signed with a different signing secret is rejected', async () => {
  const env = createTestEnv();
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.epub', EPUB_BYTES);

  const foreignEnv = { ...env, SIGNING_SECRET: 'a-different-secret' };
  const token = await signDownloadToken('forge-playbook', 'a1b2c3d4', foreignEnv);

  const res = await worker.fetch(
    new Request(
      `https://example.com/api/download?product=${token.product}&order=${token.orderId}&exp=${token.exp}&sig=${token.sig}`
    ),
    env
  );
  assert.equal(res.status, 403);
});

test('a valid token whose order id does not match the signature is rejected', async () => {
  const env = createTestEnv();
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.epub', EPUB_BYTES);

  const token = await signDownloadToken('forge-playbook', 'a1b2c3d4', env);
  const res = await worker.fetch(
    new Request(
      `https://example.com/api/download?product=${token.product}&order=deadbeef&exp=${token.exp}&sig=${token.sig}`
    ),
    env
  );
  assert.equal(res.status, 403);
});

async function grantAndDownload(env, product) {
  const grantRes = await worker.fetch(
    new Request(`https://example.com/api/grant?session_id=cs_test_paid_1&product=${product}`),
    env
  );
  assert.equal(grantRes.status, 200);
  const { url } = await grantRes.json();
  const downloadRes = await worker.fetch(new Request(url), env);
  assert.equal(downloadRes.status, 200);
  return new Uint8Array(await downloadRes.arrayBuffer());
}

test('PDF delivery is watermarked with the order id and purchase date on every page, never the raw bytes', async () => {
  const env = createTestEnv({
    sessions: { cs_test_paid_1: paidSession() },
    DEFAULT_PRODUCT: 'forge-playbook-pdf',
    ARTIFACT_KEYS: JSON.stringify({ 'forge-playbook-pdf': 'artifacts/forge-playbook.pdf' }),
  });
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.pdf', PDF_BYTES);

  const bytes = await grantAndDownload(env, 'forge-playbook-pdf');
  assert.notEqual(Buffer.from(bytes).toString('base64'), Buffer.from(PDF_BYTES).toString('base64'));

  const parser = new PDFParse({ data: bytes });
  const { text } = await parser.getText();
  assert.match(text, /Licensed to buyer at example\.com/);
  assert.match(text, /purchased 2026-07-23/);
});

test('HTML delivery has a footer banner with the buyer identifier and date, never the raw bytes', async () => {
  const env = createTestEnv({
    sessions: { cs_test_paid_1: paidSession() },
    DEFAULT_PRODUCT: 'forge-playbook-html',
    ARTIFACT_KEYS: JSON.stringify({ 'forge-playbook-html': 'artifacts/forge-playbook.html' }),
  });
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.html', HTML_BYTES);

  const bytes = await grantAndDownload(env, 'forge-playbook-html');
  assert.notEqual(Buffer.from(bytes).toString('base64'), Buffer.from(HTML_BYTES).toString('base64'));

  const html = new TextDecoder().decode(bytes);
  assert.match(html, /Licensed to buyer at example\.com/);
  assert.match(html, /purchased 2026-07-23/);
});

test('EPUB delivery carries a colophon/watermark with the buyer identifier, never the raw bytes', async () => {
  const env = createTestEnv({ sessions: { cs_test_paid_1: paidSession() } });
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.epub', EPUB_BYTES);

  const bytes = await grantAndDownload(env, 'forge-playbook');
  assert.notEqual(Buffer.from(bytes).toString('base64'), Buffer.from(EPUB_BYTES).toString('base64'));

  const files = unzipSync(bytes);
  const cover = new TextDecoder().decode(files['EPUB/text/cover.xhtml']);
  assert.match(cover, /Licensed to buyer at example\.com/);
  assert.match(cover, /purchased 2026-07-23/);
});

test('watermark falls back to the order id alone when the buyer email is absent', async () => {
  const env = createTestEnv({
    sessions: { cs_test_paid_1: paidSession({ customer_details: null }) },
  });
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.epub', EPUB_BYTES);

  const bytes = await grantAndDownload(env, 'forge-playbook');
  const files = unzipSync(bytes);
  const cover = new TextDecoder().decode(files['EPUB/text/cover.xhtml']);
  assert.doesNotMatch(cover, /buyer at/);
  assert.match(cover, /Licensed to order [0-9a-f]{8}/);
});

test('a valid token for an artifact with an unrecognized format is denied with 500, not served raw', async () => {
  const env = createTestEnv({
    DEFAULT_PRODUCT: 'forge-playbook-txt',
    ARTIFACT_KEYS: JSON.stringify({ 'forge-playbook-txt': 'artifacts/forge-playbook.txt' }),
  });
  await env.PLAYBOOK_BUCKET.put('artifacts/forge-playbook.txt', new TextEncoder().encode('raw text'));

  const token = await signDownloadToken('forge-playbook-txt', 'a1b2c3d4', env);
  const res = await worker.fetch(
    new Request(
      `https://example.com/api/download?product=${token.product}&order=${token.orderId}&exp=${token.exp}&sig=${token.sig}`
    ),
    env
  );
  assert.equal(res.status, 500);
});
