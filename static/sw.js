// Minimalus service worker — reikalingas tik tam, kad narsykle leistu idiegti
// programa i darbalauki. NIEKO NECACHINA: kiekviena uzklausa keliauja i serveri,
// kad darbuotojos visada matytu naujausius duomenis.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => e.respondWith(fetch(e.request)));
