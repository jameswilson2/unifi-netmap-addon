"""
UniFi Network Map - Home Assistant Add-on server
Handles:
  - Serving static files from /www (with ingress base-path rewriting)
  - Proxying /unifi/* HTTP requests to the UniFi controller
  - /unifi-sse  — Server-Sent Events stream that relays UniFi WebSocket events
                  to the browser.  HA Ingress handles plain HTTP fine; raw WS
                  tunnelling is unreliable through the Ingress proxy layer.
"""

import os
import ssl
import threading
import queue
import urllib.request
import urllib.error
import traceback
from http.server import HTTPServer, SimpleHTTPRequestHandler
import socket
import hashlib
import base64

UNIFI_HOST    = os.environ.get("UNIFI_HOST", "https://192.168.4.1")
API_KEY       = os.environ.get("API_KEY", "")
PORT          = 8765
WWW_DIR       = "/www"
INGRESS_ENTRY = os.environ.get("INGRESS_ENTRY", "")

# Derive the plain hostname + port for the upstream WS connection
# e.g. https://192.168.4.1 → 192.168.4.1:443
_host_part = UNIFI_HOST.replace("https://", "").replace("http://", "")
UNIFI_WS_HOST = _host_part if ":" in _host_part else (
    _host_part + (":443" if UNIFI_HOST.startswith("https") else ":80")
)
UNIFI_WS_USE_SSL = UNIFI_HOST.startswith("https")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode    = ssl.CERT_NONE

# ── SSE fan-out registry ───────────────────────────────────────────────────────
# Each connected browser gets its own Queue.  The background WS thread puts
# raw event strings in here; the SSE handler reads them and streams to browser.
_sse_clients_lock = threading.Lock()
_sse_clients = []   # list of queue.Queue

def _sse_subscribe():
    q = queue.Queue(maxsize=64)
    with _sse_clients_lock:
        _sse_clients.append(q)
    return q

def _sse_unsubscribe(q):
    with _sse_clients_lock:
        try:
            _sse_clients.remove(q)
        except ValueError:
            pass

def _sse_broadcast(data):
    """Fan out a single event string to every waiting SSE client."""
    with _sse_clients_lock:
        for q in _sse_clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass  # slow client — drop rather than block


# ── WebSocket helpers ──────────────────────────────────────────────────────────
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def _ws_accept(key):
    return base64.b64encode(
        hashlib.sha1((key + WS_MAGIC).encode()).digest()
    ).decode()

def _random_ws_key():
    return base64.b64encode(os.urandom(16)).decode()

def _recv_ws_frame(sock):
    """
    Read one complete WebSocket frame from *sock* and return its payload bytes.
    Returns None on close / unrecoverable error, b"" for control frames.
    Handles text, binary, ping, pong and close opcodes.
    """
    try:
        # 2-byte fixed header
        header = b""
        while len(header) < 2:
            chunk = sock.recv(2 - len(header))
            if not chunk:
                return None
            header += chunk

        opcode = header[0] & 0x0F
        masked  = (header[1] & 0x80) != 0
        length  = header[1] & 0x7F

        if length == 126:
            raw = b""
            while len(raw) < 2:
                raw += sock.recv(2 - len(raw))
            length = int.from_bytes(raw, "big")
        elif length == 127:
            raw = b""
            while len(raw) < 8:
                raw += sock.recv(8 - len(raw))
            length = int.from_bytes(raw, "big")

        mask_key = b""
        if masked:
            while len(mask_key) < 4:
                mask_key += sock.recv(4 - len(mask_key))

        payload = b""
        while len(payload) < length:
            chunk = sock.recv(min(4096, length - len(payload)))
            if not chunk:
                return None
            payload += chunk

        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        if opcode == 0x08:          # close frame
            return None
        if opcode == 0x09:          # ping — send pong
            pong = bytes([0x8A, len(payload)]) + payload
            try:
                sock.sendall(pong)
            except Exception:
                pass
            return b""
        if opcode in (0x01, 0x02):  # text or binary
            return payload

        return b""  # continuation / unknown — skip

    except Exception:
        return None


# ── Background UniFi WebSocket thread ─────────────────────────────────────────
_WS_PATH = "/proxy/network/wss/s/default/events"

def _unifi_ws_thread():
    """
    Persistent background thread: maintain one WebSocket connection to the
    UniFi controller and broadcast every received event to all SSE clients.
    Reconnects automatically on failure with exponential back-off.
    """
    import time

    backoff = 5
    while True:
        sock = None
        try:
            host, port_str = UNIFI_WS_HOST.rsplit(":", 1)
            port    = int(port_str)
            origin  = UNIFI_HOST.rstrip("/")
            ws_key  = _random_ws_key()

            raw = socket.create_connection((host, port), timeout=15)
            if UNIFI_WS_USE_SSL:
                sock = ctx.wrap_socket(raw, server_hostname=host)
            else:
                sock = raw

            # Upgrade to WebSocket
            upgrade = (
                f"GET {_WS_PATH} HTTP/1.1\r\n"
                f"Host: {UNIFI_WS_HOST}\r\n"
                f"Origin: {origin}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {ws_key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"X-API-KEY: {API_KEY}\r\n"
                f"\r\n"
            )
            sock.sendall(upgrade.encode())

            # Read response headers
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = sock.recv(1)
                if not chunk:
                    raise ConnectionError("Upstream closed before headers complete")
                resp += chunk

            if b"101" not in resp:
                try:
                    extra = sock.recv(512)
                except Exception:
                    extra = b""
                print(
                    f"[WS] Upstream rejected handshake:\n"
                    f"{(resp + extra).decode(errors='replace')}",
                    flush=True,
                )
                raise ConnectionError("Handshake rejected")

            print(f"[WS] Connected to {UNIFI_WS_HOST}{_WS_PATH}", flush=True)
            backoff = 5  # reset on success

            # Generous timeout — UniFi sends events infrequently
            sock.settimeout(90)

            while True:
                payload = _recv_ws_frame(sock)
                if payload is None:
                    print("[WS] Connection closed by UniFi", flush=True)
                    break
                if payload:
                    text = payload.decode(errors="replace")
                    _sse_broadcast(text)

        except Exception as exc:
            print(f"[WS] Error: {exc} — reconnecting in {backoff}s", flush=True)
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


# Start the background WS thread once at server startup
threading.Thread(target=_unifi_ws_thread, daemon=True).start()


# ── HTTP handler ───────────────────────────────────────────────────────────────
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

        # ── SSE endpoint ────────────────────────────────────────────────────
        if self.path.startswith("/unifi-sse"):
            self._handle_sse()
            return

        # ── Proxy /unifi/* → UniFi controller ──────────────────────────────
        if self.path.startswith("/unifi/"):
            self._proxy_unifi()
            return

        if self.path in ("/", ""):
            self.path = "/index.html"
        super().do_GET()

    def _handle_sse(self):
        """Stream UniFi events to the browser as Server-Sent Events."""
        q = _sse_subscribe()
        print(f"[SSE] Client connected (total: {len(_sse_clients)})", flush=True)
        try:
            self.send_response(200)
            self.send_header("Content-Type",      "text/event-stream")
            self.send_header("Cache-Control",     "no-cache")
            self.send_header("X-Accel-Buffering", "no")   # disable nginx buffering
            self.send_header("Connection",        "keep-alive")
            self._cors()
            self.end_headers()
            self.wfile.flush()

            # Opening comment — tells the browser the stream is live
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()

            while True:
                try:
                    data = q.get(timeout=25)
                    # Escape embedded newlines (SSE spec requirement)
                    safe = data.replace("\n", "\ndata: ")
                    msg  = f"data: {safe}\n\n".encode()
                    self.wfile.write(msg)
                    self.wfile.flush()
                except queue.Empty:
                    # Keepalive — prevents browsers and HA Ingress from
                    # closing an idle stream
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()

        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print(f"[SSE] Error: {exc}", flush=True)
        finally:
            _sse_unsubscribe(q)
            print(
                f"[SSE] Client disconnected (remaining: {len(_sse_clients)})",
                flush=True,
            )

    def _proxy_unifi(self):
        api_path = self.path[len("/unifi"):]
        url = UNIFI_HOST + api_path
        print(f"→ Proxying: {url}", flush=True)
        req = urllib.request.Request(url, headers={
            "X-API-KEY": API_KEY,
            "Accept":    "application/json",
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
            print(f"   X-API-KEY={'SET' if API_KEY else 'MISSING'}", flush=True)
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
print(f"Background WS → {UNIFI_WS_HOST}{_WS_PATH}")
print(f"SSE endpoint  → /unifi-sse")
print(f"Ingress entry: {INGRESS_ENTRY}")
ReusableHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
