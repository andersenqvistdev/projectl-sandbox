// Minimal stand-in for a Cloudflare Workers KV namespace binding, just
// enough surface area (get/put with expirationTtl) for the tests below.
export function createFakeKv() {
  const store = new Map();
  return {
    async get(key) {
      const entry = store.get(key);
      if (!entry) return null;
      if (entry.expiresAt !== null && entry.expiresAt < Date.now()) {
        store.delete(key);
        return null;
      }
      return entry.value;
    },
    async put(key, value, opts = {}) {
      const expiresAt = opts.expirationTtl ? Date.now() + opts.expirationTtl * 1000 : null;
      store.set(key, { value, expiresAt });
    },
    _size() {
      return store.size;
    },
  };
}
