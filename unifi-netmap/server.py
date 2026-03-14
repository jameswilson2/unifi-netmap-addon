"""
UniFi Network Map - Home Assistant Add-on server
Handles:
  - Serving static files from /www (with ingress base-path rewriting)
  - Proxying /unifi/* requests to the UniFi controller
"""

import os
import ssl
import urllib.request
import urllib.error
import traceback
from http.server import HTTPServer, SimpleHTTPRequestHandler

UNIFI_HOST   = os.environ.get("UNIFI_HOST", "https://192.168.4.1")
API_KEY      = os.environ.get("API_KEY", "")
PORT         = 8099
WWW_DIR      = "/www"
# HA sets X-Ingress-Path on every request so the app knows its base path.
# We also expose it at /ingress-path for the JS to read on first load.
INGRESS_ENTRY = os.environ.get("INGRESS_ENTRY", "")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WWW_DIR, **kwargs)

    # ------------------------------------------------------------------ #
    #  Resolve the ingress base path from the request header              #
    # ------------------------------------------------------------------ #
    def _ingress_path(self):
        return self.headers.get("X-Ingress-Path", INGRESS_ENTRY).rstrip("/")

    def do_GET(self):
        ingress = self._ingress_path()

        # ── /ingress-path endpoint: tells the JS its base URL ──────────
        if self.path.rstrip("/") in ("/ingress-path", ingress + "/ingress-path"):
            body = ingress.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", len(body))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return

        # ── Strip the ingress prefix so file serving works ─────────────
        path = self.path
        if ingress and path.startswith(ingress):
            path = path[len(ingress):] or "/"
        self.path = path

        # ── Proxy /unifi/* → UniFi controller ──────────────────────────
        if self.path.startswith("/unifi/"):
            self._proxy_unifi()
            return

        # ── Serve static files ─────────────────────────────────────────
        if self.path in ("/", ""):
            self.path = "/index.html"
        super().do_GET()

    def _proxy_unifi(self):
        api_path = self.path[len("/unifi"):]   # /unifi/proxy/... → /proxy/...
        url = UNIFI_HOST + api_path
        print(f"→ Proxying: {url}")
        req = urllib.request.Request(url, headers={
            "X-API-KEY": API_KEY,
            "Accept": "application/json",
        })
        try:
            resp = urllib.request.urlopen(req, context=ctx)
            body = resp.read()
            print(f"← {resp.status} ({len(body)} bytes)")
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() not in ("transfer-encoding", "content-encoding"):
                    self.send_header(k, v)
            self.send_header("Content-Length", len(body))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except urllib.error.HTTPError as e:
            body = e.read()
            print(f"← HTTPError {e.code}: {body}")
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            print(f"← Exception: {traceback.format_exc()}")
            self.send_response(500)
            self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def log_message(self, fmt, *args):
        print(fmt % args)


print(f"Starting UniFi Network Map on port {PORT}")
print(f"Forwarding /unifi/* → {UNIFI_HOST}")
print(f"Ingress entry: {INGRESS_ENTRY}")
HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
