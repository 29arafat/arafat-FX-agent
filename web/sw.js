// Service Worker: অ্যাপের ফাইল ক্যাশ করে, অফলাইনেও খোলে। ডেটা (JSON) এখানে ক্যাশ হয় না, অ্যাপ নিজে শেষ ডেটা রাখে।
// ফাইল বদলালে VERSION বাড়ান, নইলে ফোনে পুরনো সংস্করণ থেকে যেতে পারে।
const VERSION = "fx-v3";
const SHELL = ["./", "index.html", "style.css", "app.js", "config.js", "manifest.webmanifest",
               "icons/icon-192.png", "icons/icon-512.png", "icons/maskable-512.png", "icons/favicon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;            // ডেটা ও বাইরের অনুরোধ সরাসরি নেটওয়ার্কে
  e.respondWith(
    caches.match(req, { ignoreSearch: true }).then((hit) => {
      const net = fetch(req).then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(VERSION).then((c) => c.put(req, copy)); }
        return res;
      }).catch(() => hit);
      return hit || net;                                       // ক্যাশ থাকলে সাথে সাথে, পেছনে নতুন আনে
    })
  );
});
