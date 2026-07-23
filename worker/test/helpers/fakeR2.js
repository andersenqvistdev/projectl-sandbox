/**
 * Minimal in-memory stand-in for an R2Bucket binding — just enough of the
 * put/get/head surface for the ledger tests below. Models the one piece of
 * real R2 semantics this module depends on: a put with
 * `onlyIf: { etagDoesNotMatch: '*' }` against a key that already has an
 * object resolves to `null` instead of overwriting (mirrors real R2, which
 * resolves — does not throw — on a failed conditional put).
 */
export function createFakeR2() {
  const store = new Map();
  return {
    store,
    async put(key, value, options) {
      if (options?.onlyIf?.etagDoesNotMatch === '*' && store.has(key)) {
        return null;
      }
      store.set(key, { body: value, httpMetadata: options?.httpMetadata });
      return { key };
    },
    async get(key) {
      return store.has(key) ? { key, ...store.get(key) } : null;
    },
    async head(key) {
      return store.has(key) ? { key } : null;
    },
  };
}
