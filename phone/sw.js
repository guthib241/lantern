/* Lantern phone service worker: cache the shell only, never API responses. */
var SHELL = ["/m/", "/m/index.html", "/m/style.css", "/m/app.js",
             "/m/manifest.webmanifest", "/m/icons/lantern.svg",
             "/m/icons/icon-192.png", "/m/icons/icon-512.png"];
var CACHE = "lantern-shell-v1";

self.addEventListener("install", function (event) {
  event.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); })
    .then(function () { return self.skipWaiting(); }));
});

self.addEventListener("activate", function (event) {
  event.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; })
      .map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener("fetch", function (event) {
  var url = new URL(event.request.url);
  if (url.pathname === "/m/events" || url.pathname.indexOf("/m/api/") === 0 ||
      url.pathname === "/pair" || url.pathname.indexOf("/pair") === 0) {
    return; // never cache API, events or pairing
  }
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  event.respondWith(caches.match(event.request).then(function (hit) { return hit || fetch(event.request); }));
});
