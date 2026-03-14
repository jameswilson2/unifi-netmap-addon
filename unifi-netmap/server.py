"""
UniFi Network Map - Home Assistant Add-on server
Handles:
  - Serving static files from /www (with ingress base-path rewriting)
  - Proxying /unifi/* HTTP requests to the UniFi controller
  - Proxying /unifi-ws WebSocket to the UniFi controller events stream
"""

import os
import ssl
import threading
import urllib.request
import urllib.error
import traceback
from http.server import HTTPServer, SimpleHTTPRequestHandler
import socket
import hashlib
import base64
import struct

UNIFI_HOST    = os.environ.get("UNIFI_HOST", "https://192.168.4.1")
API_KEY       = os.environ.get("API_KEY", "")
PORT          = 8765
WWW_DIR       = "/www"
INGRESS_ENTRY = os.environ.get("INGRESS_ENTRY", "")

# Derive the plain hostname + port for the WS connection
# e.g. https://192.168.4.1 → 192.168.4.1:443
_host_part = UNIFI_HOST.replace("https://", "").replace("http://", "")
UNIFI_WS_HOST = _host_part if ":" in _host_part else (
    _host_part + (":443" if UNIFI_HOST.startswith("https") else ":80")
)
UNIFI_WS_USE_SSL = UNIFI_HOST.startswith("https")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


# ── Minimal WebSocket handshake helper ────────────────────────────────────────
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def _ws_accept(key: str) -> str:
    return base64.b64encode(
        hashlib.sha1((key + WS_MAGIC).encode()).digest()
    ).decode()

def _proxy_ws(client_sock, path, api_key):
    """Tunnel a WebSocket connection between the browser and UniFi controller."""
    host, port_str = UNIFI_WS_HOST.rsplit(":", 1)
    port = int(port_str)

    raw = socket.create_connection((host, port), timeout=10)
    if UNIFI_WS_USE_SSL:
        server_sock = ctx.wrap_socket(raw, server_hostname=host)
    else:
        server_sock = raw

    # Send upstream WS upgrade request
    upgrade = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {UNIFI_WS_HOST}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: dGhlc2FtcGxla2V5MTY=\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"X-API-KEY: {api_key}\r\n"
        f"\r\n"
    )
    server_sock.sendall(upgrade.encode())

    # Read upstream response headers
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += server_sock.recv(1)
    if b"101" not in resp:
        print(f"WS upstream rejected: {resp[:200]}", flush=True)
        server_sock.close()
        client_sock.close()
        return

    # Bi-directional pipe
    def pipe(src, dst):
        try:
            while True:
                data = src.recv(4096)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass
        finally:
            try: src.close()
            except: pass
            try: dst.close()
            except: pass

    t1 = threading.Thread(target=pipe, args=(client_sock, server_sock), daemon=True)
    t2 = threading.Thread(target=pipe, args=(server_sock, client_sock), daemon=True)
    t1.start(); t2.start()
    t1.join(); t2.join()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WWW_DIR, **kwargs)

    def _ingress_path(self):
        return self.headers.get("X-Ingress-Path", INGRESS_ENTRY).rstrip("/")

    def do_GET(self):
        ingress = self._ingress_path()
        print(f"← GET raw path: {self.path!r}  ingress prefix: {ingress!r}", flush=True)

        path = self.path
        if ingress and path.startswith(ingress):
            path = path[len(ingress):] or "/"
        self.path = path

        # ── WebSocket upgrade for /unifi-ws ────────────────────────────────
        if self.path.startswith("/unifi-ws") and \
                self.headers.get("Upgrade", "").lower() == "websocket":
            self._handle_ws_upgrade()
            return

        # ── Proxy /unifi/* → UniFi controller ──────────────────────────────
        if self.path.startswith("/unifi/"):
            self._proxy_unifi()
            return

        if self.path in ("/", ""):
            self.path = "/index.html"
        super().do_GET()

    def _handle_ws_upgrade(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = _ws_accept(key)
        # Send 101 back to browser
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()

        # Now tunnel raw socket to UniFi WS endpoint
        unifi_ws_path = "/proxy/network/wss/s/default/events"
        print(f"WS tunnel → {UNIFI_WS_HOST}{unifi_ws_path}", flush=True)
        _proxy_ws(self.connection, unifi_ws_path, API_KEY)

    def _proxy_unifi(self):
        api_path = self.path[len("/unifi"):]
        url = UNIFI_HOST + api_path
        print(f"→ Proxying: {url}", flush=True)
        req = urllib.request.Request(url, headers={
            "X-API-KEY": API_KEY,
            "Accept": "application/json",
        })
        try:
            resp = urllib.request.urlopen(req, context=ctx)
            body = resp.read()
            print(f"← {resp.status} ({len(body)} bytes)", flush=True)
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
            print(f"← HTTPError {e.code} for {url}", flush=True)
            print(f"   Response body: {body[:500]}", flush=True)
            print(f"   Request headers: X-API-KEY={'SET' if API_KEY else 'MISSING'}", flush=True)
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            print(f"← Exception for {url}: {traceback.format_exc()}", flush=True)
            self.send_response(500)
            self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True

print(f"Starting UniFi Network Map on port {PORT}")
print(f"Forwarding /unifi/* → {UNIFI_HOST}")
print(f"WebSocket tunnel → {UNIFI_WS_HOST}")
print(f"Ingress entry: {INGRESS_ENTRY}")
ReusableHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

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
PORT         = 8765
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
        print(f"← GET raw path: {self.path!r}  ingress prefix: {ingress!r}", flush=True)

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
        print(f"→ Proxying: {url}", flush=True)
        req = urllib.request.Request(url, headers={
            "X-API-KEY": API_KEY,
            "Accept": "application/json",
        })
        try:
            resp = urllib.request.urlopen(req, context=ctx)
            body = resp.read()
            print(f"← {resp.status} ({len(body)} bytes)", flush=True)
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
            print(f"← HTTPError {e.code} for {url}", flush=True)
            print(f"   Response body: {body[:500]}", flush=True)
            print(f"   Request headers: X-API-KEY={'SET' if API_KEY else 'MISSING'}", flush=True)
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            print(f"← Exception for {url}: {traceback.format_exc()}", flush=True)
            self.send_response(500)
            self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def log_message(self, fmt, *args):
        print(fmt % args)


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True

print(f"Starting UniFi Network Map on port {PORT}")
print(f"Forwarding /unifi/* → {UNIFI_HOST}")
print(f"Ingress entry: {INGRESS_ENTRY}")
ReusableHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
