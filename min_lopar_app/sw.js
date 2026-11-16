// ES Endurance Dashboard: enkel service worker.
// Appskalet (HTML, ikoner, Chart.js) cachas så att appen startar utan nät.
// Data från Supabase hämtas alltid live och cachas aldrig.
const CACHE = 'esend-shell-v1';
const SHELL = ['./', './training-dashboard.html', './manifest.webmanifest', './icon-192.png', './icon-512.png',
  'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.hostname.endsWith('supabase.co')) return;   // data: alltid nätet
  // Nätet först (så att nya versioner slår igenom direkt), cachen som reserv utan nät
  e.respondWith(
    fetch(req).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
      return res;
    }).catch(() => caches.match(req, { ignoreSearch: true }))
  );
});
