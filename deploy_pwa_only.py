"""
============================================================
PWA-ONLY DEPLOYMENT SCRIPT (WITH ROOT SERVICE WORKER SCOPE)
============================================================
Run this script to deploy ONLY the PWA 'Add App to Home Screen'
feature to your server without modifying Mess Management or
other database models/views.

Usage:
    python deploy_pwa_only.py
============================================================
"""

import os
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

print("Starting PWA-Only Feature Deployment (Root Scope Fix)...")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 1. Update core_project/urls.py with Service-Worker-Allowed header
urls_path = os.path.join(BASE_DIR, "core_project", "urls.py")
if os.path.exists(urls_path):
    with open(urls_path, "r", encoding="utf-8") as f:
        urls_content = f.read()

    new_sw_func = '''def service_worker(request):
    sw_path = os.path.join(settings.BASE_DIR, 'static', 'service-worker.js')
    if os.path.exists(sw_path):
        with open(sw_path, 'r', encoding='utf-8') as f:
            content = f.read()
    else:
        content = ""
    response = HttpResponse(content, content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    return response'''

    if 'def service_worker' in urls_content and 'Service-Worker-Allowed' not in urls_content:
        old_sw_func = '''def service_worker(request):
    sw_path = os.path.join(settings.BASE_DIR, 'static', 'service-worker.js')
    if os.path.exists(sw_path):
        with open(sw_path, 'r', encoding='utf-8') as f:
            content = f.read()
    else:
        content = ""
    return HttpResponse(content, content_type="application/javascript")'''
        urls_content = urls_content.replace(old_sw_func, new_sw_func)
        with open(urls_path, "w", encoding="utf-8") as f:
            f.write(urls_content)
        print(" [OK] Updated core_project/urls.py with Service-Worker-Allowed header.")

# 2. Update static/manifest.json
manifest_path = os.path.join(BASE_DIR, "static", "manifest.json")
manifest_content = '''{
  "id": "/dashboard/",
  "name": "P&M Portal",
  "short_name": "P&M",
  "description": "Plant & Machinery Management Portal - RVJV",
  "start_url": "/dashboard/",
  "scope": "/",
  "display": "standalone",
  "background_color": "#0f172a",
  "theme_color": "#6366f1",
  "orientation": "portrait-primary",
  "icons": [
    {
      "src": "/static/icons/icon-192.png",
      "sizes": "192x192",
      "type": "image/png",
      "purpose": "any"
    },
    {
      "src": "/static/icons/icon-192.png",
      "sizes": "192x192",
      "type": "image/png",
      "purpose": "maskable"
    },
    {
      "src": "/static/icons/icon-512.png",
      "sizes": "512x512",
      "type": "image/png",
      "purpose": "any"
    },
    {
      "src": "/static/icons/icon-512.png",
      "sizes": "512x512",
      "type": "image/png",
      "purpose": "maskable"
    }
  ]
}
'''
os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
with open(manifest_path, "w", encoding="utf-8") as f:
    f.write(manifest_content)
print(" [OK] Updated static/manifest.json.")

# 3. Update static/service-worker.js
sw_path = os.path.join(BASE_DIR, "static", "service-worker.js")
sw_content = '''const CACHE_NAME = 'pm-portal-v2';
const urlsToCache = [
  '/',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(urlsToCache).catch(() => {}))
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(clients.claim());
});

self.addEventListener('fetch', event => {
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
'''
with open(sw_path, "w", encoding="utf-8") as f:
    f.write(sw_content)
print(" [OK] Updated static/service-worker.js.")

# 4. Update templates/base.html
base_path = os.path.join(BASE_DIR, "templates", "base.html")
if os.path.exists(base_path):
    with open(base_path, "r", encoding="utf-8") as f:
        base_content = f.read()

    pwa_head_block = '''    <!-- PWA Web Manifest & Service Worker Registration -->
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#0f172a">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="P&M">
    <link rel="apple-touch-icon" href="/static/icons/icon-192.png">

    <script>
        window.deferredPwaPrompt = null;
        window.addEventListener('beforeinstallprompt', function(e) {
            e.preventDefault();
            window.deferredPwaPrompt = e;
            console.log('PWA beforeinstallprompt captured!');
        });

        if ('serviceWorker' in navigator) {
            window.addEventListener('load', function() {
                navigator.serviceWorker.register('/service-worker.js', { scope: '/' })
                    .then(function(reg) {
                        console.log('Service Worker registered with scope:', reg.scope);
                    }).catch(function(err) {
                        console.log('Service Worker registration failed:', err);
                    });
            });
        }
    </script>'''

    if 'href="/manifest.json"' not in base_content:
        if "<title>P&M</title>" in base_content:
            base_content = base_content.replace("<title>P&M</title>", "<title>P&M</title>\n" + pwa_head_block)

    with open(base_path, "w", encoding="utf-8") as f:
        f.write(base_content)
    print(" [OK] Updated templates/base.html cleanly.")

print("\nPWA 'Add App to Home Screen' deployed successfully!")
print("Note: If on PythonAnywhere, remember to click 'Reload' in the Web Tab!")
