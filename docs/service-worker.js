/* © 2026 오늘, 전남광주 어디로? All rights reserved. 무단 복제·수정·재배포 금지 (LICENSE 참고) */
/* 오늘어디로 · 서비스 워커
   - 화면(index.html)과 일정 자료(data/*.json, embedded-data.js)는 "인터넷 먼저": 항상 최신을 보여주고, 안 되면 저장본
   - 아이콘·글꼴·지도 라이브러리는 "저장본 먼저": 두 번째부터 빨리 뜸
   - 지도 라이브러리·글꼴은 설치할 때 미리 저장 → 첫 방문 뒤 인터넷이 끊겨도 지도가 뜸
   - 행사 사진 등 바깥 이미지는 저장하지 않음(용량 절약) */
const VERSION = "v62-2026-10-21-hoursfix";
const SHELL = `shell-${VERSION}`, DATA = `data-${VERSION}`, LIB = `lib-${VERSION}`;
const SHELL_FILES = ["./", "./index.html", "./creaming.webp"];
const LIB_FILES = [
  "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css",
  "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js",
  "https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.min.css",
  "https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/leaflet.markercluster.min.js",
  "https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css",
];
const LIB_HOSTS = ["cdnjs.cloudflare.com", "cdn.jsdelivr.net", "fonts.googleapis.com", "fonts.gstatic.com"];

self.addEventListener("install", e => {
  e.waitUntil((async () => {
    await caches.open(SHELL).then(c => c.addAll(SHELL_FILES));
    // 라이브러리는 하나가 실패해도 설치는 계속 (페이지가 처음 쓸 때 다시 저장됨)
    const lib = await caches.open(LIB);
    await Promise.allSettled(LIB_FILES.map(u => fetch(u, { mode: "no-cors" }).then(r => (r.ok || r.type === "opaque") && lib.put(u, r))));
    await self.skipWaiting();
  })());
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => ![SHELL, DATA, LIB].includes(k)).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

async function networkFirst(req, cacheName, timeoutMs = 6000) {
  const cache = await caches.open(cacheName);
  // 늦게 도착한 응답도 저장해서, 다음번엔 최신본이 나오게 함
  // 브라우저 HTTP 캐시(GitHub Pages는 10분)를 건너뛰고 서버에 새 버전이 있는지 꼭 확인 → 올리자마자 바로 반영
  const fresh = req.mode === "navigate" ? fetch(req.url, { cache: "no-cache", credentials: "same-origin" }) : fetch(new Request(req, { cache: "no-cache" }));
  const net = fresh.then(res => { if (res && res.ok) cache.put(req, res.clone()); return res });
  try {
    return await Promise.race([net, new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), timeoutMs))]);
  } catch (err) {
    const hit = await cache.match(req, { ignoreSearch: true });
    if (hit) { net.catch(() => { }); return hit }
    return net;                                   // 저장본이 없으면 끝까지 기다림
  }
}
async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req, { ignoreVary: true });
  if (hit) return hit;
  const res = await fetch(req);
  if (res && (res.ok || res.type === "opaque")) cache.put(req, res.clone());
  return res;
}

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  const same = url.origin === self.location.origin;
  if (req.mode === "navigate") {                          // 화면 열기
    e.respondWith(networkFirst(req, SHELL).catch(() => caches.match("./index.html")));
    return;
  }
  if (same && (/\/data\/[^/]+\.json$/.test(url.pathname) || /embedded-data\.js$/.test(url.pathname))) { // 일정·장소 자료
    e.respondWith(networkFirst(req, DATA));
    return;
  }
  if (same && /\.(png|webp)$|manifest\.webmanifest$/.test(url.pathname)) {
    e.respondWith(cacheFirst(req, SHELL));
    return;
  }
  if (LIB_HOSTS.includes(url.hostname)) {                 // 글꼴·지도 라이브러리
    e.respondWith(cacheFirst(req, LIB));
    return;
  }
  // 그 밖(행사 사진, 날씨, 지도 타일 등)은 브라우저 기본 동작
});
