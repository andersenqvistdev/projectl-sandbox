// Minimal in-memory stand-in for a Workers KV namespace binding.
export function fakeKV(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    async get(key, type) {
      const value = map.get(key);
      if (value === undefined) return null;
      return type === 'json' ? JSON.parse(value) : value;
    },
    async put(key, value) {
      map.set(key, value);
    },
    _set(key, record) {
      map.set(key, JSON.stringify(record));
    },
  };
}
