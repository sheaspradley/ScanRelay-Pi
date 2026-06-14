/**
 * ScanRelay v3.1 Service Worker
 * App-shell + runtime caching strategy
 */

const CACHE_VERSION = 'scanrelay-v3.1.0';
const API_CACHE = 'scanrelay-api-v3.1.0';
const FONT_CACHE = 'scanrelay-fonts-v3.1.0';
const AUDIO_CACHE = 'scanrelay-audio-v3.1.0';

// App shell — precached on install
const APP_SHELL = [
  '/',
  '/static/index.html',
  '/static/style.css',
  '/static/app.js',
  '/manifest.webmanifest',
  '/static/manifest.webmanifest',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/icon-512-maskable.png',
  '/static/icons/apple-touch-icon.png',
  '/static/icons/icon-monochrome-512.png',
  '/static/icons/favicon.ico',
  '/static/icons/splash-1170x2532.png',
  // Google Fonts CSS (just the CSS, font files cached at runtime)
  'https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap',
];

// ─── Install ─────────────────────────────────────────────────────────────────

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then(async (cache) => {
      // Precache app shell — ignore failures for external resources
      const results = await Promise.allSettled(
        APP_SHELL.map(url =>
          cache.add(url).catch(err => {
            console.warn('[SW] Failed to precache:', url, err.message);
          })
        )
      );
      console.log('[SW] Install complete — app shell cached');
    }).then(() => self.skipWaiting())
  );
});

// ─── Activate ────────────────────────────────────────────────────────────────

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then(async (keys) => {
      const validCaches = [CACHE_VERSION, API_CACHE, FONT_CACHE, AUDIO_CACHE];
      await Promise.all(
        keys
          .filter(key => !validCaches.includes(key))
          .map(key => {
            console.log('[SW] Purging old cache:', key);
            return caches.delete(key);
          })
      );
      console.log('[SW] Activate complete');
    }).then(() => self.clients.claim())
  );
});

// ─── Fetch ───────────────────────────────────────────────────────────────────

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Ignore non-GET and chrome-extension etc.
  if (request.method !== 'GET') return;
  if (!url.protocol.startsWith('http')) return;

  // SSE — never cache, always network-only
  if (url.pathname === '/api/stream') {
    return; // fall through to network
  }

  // Google Fonts — cache-first (rarely changes)
  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    event.respondWith(fontsCacheFirst(request));
    return;
  }

  // Audio files — cache-on-use (large, cache after first fetch)
  if (url.pathname.startsWith('/api/audio/')) {
    event.respondWith(audioCacheOnUse(request));
    return;
  }

  // API events + summary — network-first with cache fallback
  if (url.pathname === '/api/events' || url.pathname === '/api/summary') {
    event.respondWith(apiNetworkFirstWithCache(request));
    return;
  }

  // Other API routes — network-only (no cache)
  if (url.pathname.startsWith('/api/')) {
    return; // fall through to network
  }

  // HTML navigation — network-first with cache fallback
  if (request.mode === 'navigate' || request.headers.get('accept')?.includes('text/html')) {
    event.respondWith(htmlNetworkFirst(request));
    return;
  }

  // Static assets (/static/* and manifest) — stale-while-revalidate
  if (
    url.pathname.startsWith('/static/') ||
    url.pathname === '/manifest.webmanifest' ||
    url.pathname === '/sw.js'
  ) {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }

  // Everything else — stale-while-revalidate
  event.respondWith(staleWhileRevalidate(request));
});

// ─── Strategies ──────────────────────────────────────────────────────────────

async function htmlNetworkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE_VERSION);
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) return cached;
    // Final fallback — root app shell
    const fallback = await caches.match('/');
    if (fallback) return fallback;
    return new Response('<h1>Offline</h1><p>ScanRelay is offline. Connect to view the dashboard.</p>', {
      headers: { 'Content-Type': 'text/html' }
    });
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE_VERSION);
  const cached = await cache.match(request);

  const fetchPromise = fetch(request).then(response => {
    if (response.ok) {
      cache.put(request, response.clone());
    }
    return response;
  }).catch(() => null);

  return cached || await fetchPromise || new Response('Not found', { status: 404 });
}

async function apiNetworkFirstWithCache(request) {
  const cache = await caches.open(API_CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) {
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) {
      // Return cached response with a custom header so app knows it's stale
      const headers = new Headers(cached.headers);
      headers.set('X-SW-Cached', 'true');
      const body = await cached.blob();
      return new Response(body, {
        status: cached.status,
        statusText: cached.statusText,
        headers
      });
    }
    return new Response(JSON.stringify({ error: 'offline', events: [] }), {
      status: 503,
      headers: { 'Content-Type': 'application/json', 'X-SW-Cached': 'true' }
    });
  }
}

async function fontsCacheFirst(request) {
  const cache = await caches.open(FONT_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;

  try {
    const response = await fetch(request);
    if (response.ok) {
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    return new Response('', { status: 503 });
  }
}

async function audioCacheOnUse(request) {
  const cache = await caches.open(AUDIO_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;

  try {
    const response = await fetch(request);
    if (response.ok) {
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    return new Response('', { status: 503 });
  }
}

// ─── Background Sync ─────────────────────────────────────────────────────────

self.addEventListener('sync', (event) => {
  if (event.tag === 'scanrelay-offline-actions') {
    event.waitUntil(flushOfflineQueue());
  }
});

async function flushOfflineQueue() {
  // Retrieve queued actions from IndexedDB (populated by app.js when offline)
  try {
    const db = await openDB();
    const tx = db.transaction('offline-queue', 'readwrite');
    const store = tx.objectStore('offline-queue');
    const actions = await getAllFromStore(store);

    for (const action of actions) {
      try {
        await fetch(action.url, {
          method: action.method || 'POST',
          headers: action.headers || { 'Content-Type': 'application/json' },
          body: action.body ? JSON.stringify(action.body) : undefined,
        });
        await deleteFromStore(store, action.id);
      } catch (err) {
        console.warn('[SW] Failed to flush action:', action.url, err);
      }
    }
  } catch (err) {
    console.warn('[SW] Offline queue flush error:', err);
  }
}

function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('scanrelay-sw', 1);
    req.onupgradeneeded = (e) => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains('offline-queue')) {
        db.createObjectStore('offline-queue', { keyPath: 'id', autoIncrement: true });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function getAllFromStore(store) {
  return new Promise((resolve, reject) => {
    const req = store.getAll();
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function deleteFromStore(store, id) {
  return new Promise((resolve, reject) => {
    const req = store.delete(id);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
}

// ─── Push Notifications ───────────────────────────────────────────────────────

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: 'ScanRelay', body: event.data ? event.data.text() : 'New scanner event' };
  }

  const title = data.title || 'ScanRelay';
  const options = {
    body: data.body || 'New scanner event detected',
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-monochrome-512.png',
    tag: data.tag || 'scanrelay-event',
    data: { url: data.url || '/' },
    vibrate: [100, 50, 100],
    requireInteraction: data.requireInteraction || false,
    actions: [
      { action: 'view', title: 'View' },
      { action: 'dismiss', title: 'Dismiss' }
    ]
  };

  event.waitUntil(
    self.registration.showNotification(title, options)
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  if (event.action === 'dismiss') return;

  const url = event.notification.data?.url || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      for (const client of clientList) {
        if (client.url.includes(self.location.origin) && 'focus' in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(url);
      }
    })
  );
});

// ─── Message Handling ─────────────────────────────────────────────────────────

self.addEventListener('message', (event) => {
  if (event.data?.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }

  if (event.data?.type === 'QUEUE_ACTION') {
    // App.js calls this to queue an action while offline
    queueOfflineAction(event.data.action).catch(err => {
      console.warn('[SW] Failed to queue action:', err);
    });
  }
});

async function queueOfflineAction(action) {
  const db = await openDB();
  const tx = db.transaction('offline-queue', 'readwrite');
  tx.objectStore('offline-queue').add(action);
  return new Promise((resolve, reject) => {
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}
