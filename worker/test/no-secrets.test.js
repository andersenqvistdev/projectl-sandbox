import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const SRC_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'src');

// Real Stripe secret key formats. If any of these show up as a literal in
// src/, someone hardcoded a key instead of reading it from env.
const SECRET_PATTERNS = [
  /sk_live_[A-Za-z0-9]{10,}/,
  /sk_test_[A-Za-z0-9]{10,}/,
  /rk_live_[A-Za-z0-9]{10,}/,
  /-----BEGIN[A-Z ]*PRIVATE KEY-----/,
];

test('no literal Stripe/secret key material in worker/src', () => {
  const files = readdirSync(SRC_DIR).filter((f) => f.endsWith('.js'));
  assert.ok(files.length > 0, 'expected source files to scan');

  for (const file of files) {
    const content = readFileSync(path.join(SRC_DIR, file), 'utf8');
    for (const pattern of SECRET_PATTERNS) {
      assert.doesNotMatch(content, pattern, `${file} appears to contain literal key material`);
    }
    // All Stripe / signing secrets must flow through env, never a literal.
    assert.doesNotMatch(
      content,
      /STRIPE_SECRET_KEY\s*=\s*['"]/,
      `${file} assigns STRIPE_SECRET_KEY to a literal instead of reading env`
    );
  }
});
