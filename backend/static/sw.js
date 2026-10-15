self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
  // Прямой проброс сетевых запросов (для live-видео и API)
  event.respondWith(fetch(event.request));
});