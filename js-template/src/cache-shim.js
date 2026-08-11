// Spin's JS runtime does not implement the Cache API (`caches.default`).
// Provide a no-op, always-miss polyfill so Workers code that opportunistically
// reads/writes a cache still runs instead of throwing ReferenceError. This
// does NOT actually cache anything across requests.
// Spin's Response implementation also lacks clone(), which Workers code
// commonly calls before handing a copy to cache.put(). Since the cache
// polyfill above is a no-op anyway, a real stream-teeing clone isn't
// needed -- returning the same instance is enough to avoid the crash.
if (typeof Response !== 'undefined' && !Response.prototype.clone) {
  Response.prototype.clone = function () {
    return this;
  };
}

if (typeof globalThis.caches === 'undefined') {
  globalThis.caches = {
    default: {
      async match() {
        return undefined;
      },
      async put() {
        return undefined;
      },
      async delete() {
        return false;
      },
    },
  };
}
