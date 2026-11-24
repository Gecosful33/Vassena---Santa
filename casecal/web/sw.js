/* Keeps the last page fetched, so that it opens without a connection.
 *
 * The network comes first: with a connection the page is always the one online, and
 * the copy kept here is renewed. Only when the network fails is that copy served.
 * What is kept is what the site publishes: the page, which holds ciphertext only,
 * its manifest and its icon. Requests to other hosts are left alone.
 */
"use strict";

// Both are filled in when the site is written (see site.py): the files it publishes, and a
// name for the store that changes when that list does.
var KEPT = "__KEPT__";
var FILES = __FILES__;

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
    fetch(request, { cache: "no-cache" }).then(function (response) {
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
