/* Keeps the last page fetched, so that it opens without a connection.
 *
 * The network comes first: with a connection the page is always the one online, and
 * the copy kept here is renewed. Only when the network fails is that copy served.
 * What is kept is what the site publishes: the page, which holds ciphertext only,
 * its manifest and its icon. Requests to other hosts are left alone.
 */
"use strict";

var KEPT = "casecal-1";
var FILES = ["./", "manifest.webmanifest", "icon-192.png", "icon-512.png", "apple-touch-icon.png"];

self.addEventListener("install", function (event) {
  event.waitUntil(caches.open(KEPT).then(function (cache) { return cache.addAll(FILES); }));
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(names.filter(function (name) { return name !== KEPT; }).map(function (name) {
        return caches.delete(name);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET" || new URL(request.url).origin !== self.location.origin) return;
  event.respondWith(
    fetch(request, { cache: "no-store" }).then(function (response) {
      if (response.ok) {
        var copy = response.clone();
        caches.open(KEPT).then(function (cache) { cache.put(request, copy); });
      }
      return response;
    }).catch(function () {
      return caches.match(request, { ignoreSearch: true }).then(function (kept) {
        return kept || (request.mode === "navigate" ? caches.match("./") : Response.error());
      });
    })
  );
});
