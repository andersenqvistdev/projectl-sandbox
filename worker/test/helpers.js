import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ASSETS_DIR = join(__dirname, '..', '..', 'assets');

/** Loads a real sample deliverable from assets/ — the paid book itself is never in this repo. */
export function loadSampleBytes(filename) {
  return new Uint8Array(readFileSync(join(ASSETS_DIR, filename)));
}

export function createMockKV() {
  const store = new Map();
  return {
    store,
    async get(key) {
      return store.has(key) ? store.get(key) : null;
    },
    async put(key, value) {
      store.set(key, value);
    },
  };
}

export function createMockR2() {
  const store = new Map();
  return {
    store,
    async put(key, value, options) {
      store.set(key, { body: value, httpMetadata: options?.httpMetadata });
    },
    async get(key) {
      if (!store.has(key)) return null;
      const entry = store.get(key);
      return {
        ...entry,
        async arrayBuffer() {
          const bytes = entry.body instanceof Uint8Array ? entry.body : new TextEncoder().encode(entry.body);
          return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
        },
      };
    },
  };
}

/**
 * sessions: { [sessionId]: stripeSessionObject } — omit an id to simulate
 * an unknown session (404 from Stripe).
 */
export function createMockStripeFetch(sessions) {
  return async (url) => {
    const match = String(url).match(/\/checkout\/sessions\/([^/?]+)/);
    const sessionId = decodeURIComponent(match[1]);
    const session = sessions[sessionId];
    if (!session) {
      return new Response(JSON.stringify({ error: { message: 'No such checkout session' } }), {
        status: 404,
      });
    }
    return new Response(JSON.stringify(session), { status: 200 });
  };
}

export function createTestEnv({ sessions = {}, ...overrides } = {}) {
  return {
    STRIPE_SECRET_KEY: 'sk_test_mock_not_a_real_key_00000000',
    SIGNING_SECRET: 'test-signing-secret-not-real',
    MAX_GRANTS_PER_SESSION: '5',
    DOWNLOAD_TTL_SECONDS: '900',
    DEFAULT_PRODUCT: 'forge-playbook',
    ARTIFACT_KEYS: JSON.stringify({ 'forge-playbook': 'artifacts/forge-playbook.epub' }),
    GRANTS_KV: createMockKV(),
    PLAYBOOK_BUCKET: createMockR2(),
    fetchImpl: createMockStripeFetch(sessions),
    ...overrides,
  };
}

// Unix seconds for 2026-07-23T12:00:00Z — fixed so watermark date assertions
// in tests are deterministic rather than tied to whenever the test runs.
const FIXED_PURCHASE_UNIX_SECONDS = 1784808000;

export function paidSession(overrides = {}) {
  return {
    id: 'cs_test_paid_1',
    payment_status: 'paid',
    amount_total: 7900,
    created: FIXED_PURCHASE_UNIX_SECONDS,
    customer_details: { email: 'buyer@example.com' },
    ...overrides,
  };
}

export function unpaidSession(overrides = {}) {
  return {
    id: 'cs_test_unpaid_1',
    payment_status: 'unpaid',
    amount_total: 7900,
    created: FIXED_PURCHASE_UNIX_SECONDS,
    customer_details: { email: 'buyer@example.com' },
    ...overrides,
  };
}
