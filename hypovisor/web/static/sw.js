// Minimal service worker so the app is installable on a phone. Network-first; no offline caching of data.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('fetch', () => {});
