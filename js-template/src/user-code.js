// Placeholder — overwritten per-deployment by the admin UI
// (build_and_push_js() in spinapp-ui/app.py). Both worker styles work:
//
// addEventListener('fetch', (event) => {
//   event.respondWith(new Response('Hello from JS!'));
// });
//
// export default {
//   async fetch(request, env, ctx) {
//     return new Response('Hello from JS!');
//   }
// };

addEventListener('fetch', (event) => {
  event.respondWith(new Response('Hello from JS!'));
});
