// Fixed entry point (not user-editable). Bridges both supported worker
// styles onto the single addEventListener('fetch', ...) API that the Spin
// JS runtime actually implements:
//   1. Classic: addEventListener('fetch', (event) => {...})
//      -> user-code.js registers its own listener at import time; nothing
//         more to do here.
//   2. Module workers: export default { fetch(request, env, ctx) {...} }
//      -> bridged below.
import './cache-shim.js';
import * as __userModule from './user-code.js';

addEventListener('fetch', (event) => {
  const exported = __userModule.default;
  if (exported && typeof exported.fetch === 'function') {
    const ctx = { waitUntil: (p) => event.waitUntil(p) };
    event.respondWith(Promise.resolve(exported.fetch(event.request, {}, ctx)));
  }
});
