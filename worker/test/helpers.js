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
      return store.get(key);
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

export function paidSession(overrides = {}) {
  return {
    id: 'cs_test_paid_1',
    payment_status: 'paid',
    amount_total: 7900,
    ...overrides,
  };
}

export function unpaidSession(overrides = {}) {
  return {
    id: 'cs_test_unpaid_1',
    payment_status: 'unpaid',
    amount_total: 7900,
    ...overrides,
  };
}
