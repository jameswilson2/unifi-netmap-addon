"""
UniFi Network Map - Home Assistant Add-on server
Handles:
  - Serving static files from /www (with ingress base-path rewriting)
  - Proxying /unifi/* HTTP requests to the UniFi controller (API-key auth)
  - /unifi-sse  — Server-Sent Events stream that relays UniFi WebSocket events

WS auth strategy
----------------
The UniFi WS endpoint (/proxy/network/wss/s/default/events) accepts only
cookie-based sessions, not API keys.  Before opening the WS the background
thread logs in via POST /api/auth/login to obtain a session cookie (TOKEN +
JSESSIONID), uses those cookies on the WS upgrade request, and re-authenticates
automatically when the session expires (HTTP 401 or WS close code 4001).
"""

import os
import ssl
import threading
import queue
import json
import urllib.request
import urllib.error
import urllib.parse
import traceback
from http.server import HTTPServer, SimpleHTTPRequestHandler
import socket
import hashlib
import base64

UNIFI_HOST    = os.environ.get("UNIFI_HOST", "https://192.168.4.1")
API_KEY       = os.environ.get("API_KEY", "")
UNIFI_USER    = os.environ.get("UNIFI_USER", "admin")
UNIFI_PASS    = os.environ.get("UNIFI_PASS", "")
PORT          = 8765
WWW_DIR       = "/www"
INGRESS_ENTRY = os.environ.get("INGRESS_ENTRY", "")


# Derive plain hostname:port for the WS connection
_host_part    = UNIFI_HOST.replace("https://", "").replace("http://", "")
UNIFI_WS_HOST = _host_part if ":" in _host_part else (
    _host_part + (":443" if UNIFI_HOST.startswith("https") else ":80")
)
UNIFI_WS_USE_SSL = UNIFI_HOST.startswith("https")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode    = ssl.CERT_NONE


# ── SSE fan-out ────────────────────────────────────────────────────────────────
_sse_lock    = threading.Lock()
_sse_clients = []   # list[queue.Queue]

def _sse_subscribe():
    q = queue.Queue(maxsize=64)
    with _sse_lock:
        _sse_clients.append(q)
    return q

def _sse_unsubscribe(q):
    with _sse_lock:
        try:
            _sse_clients.remove(q)
        except ValueError:
            pass

def _sse_broadcast(data: str):
    with _sse_lock:
        for q in _sse_clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass


# ── WebSocket helpers ──────────────────────────────────────────────────────────
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def _random_ws_key():
    return base64.b64encode(os.urandom(16)).decode()

def _recv_ws_frame(sock):
    """
    Read one complete WebSocket data frame.
    Returns payload bytes, b"" for control/unknown frames, None on close/error.
    """
    try:
        hdr = b""
        while len(hdr) < 2:
            chunk = sock.recv(2 - len(hdr))
            if not chunk:
                return None
            hdr += chunk

        opcode = hdr[0] & 0x0F
        masked  = bool(hdr[1] & 0x80)
        length  = hdr[1] & 0x7F

        if length == 126:
            raw = b""
            while len(raw) < 2: raw += sock.recv(2 - len(raw))
            length = int.from_bytes(raw, "big")
        elif length == 127:
            raw = b""
            while len(raw) < 8: raw += sock.recv(8 - len(raw))
            length = int.from_bytes(raw, "big")

        mask_key = b""
        if masked:
            while len(mask_key) < 4: mask_key += sock.recv(4 - len(mask_key))

        payload = b""
        while len(payload) < length:
            chunk = sock.recv(min(4096, length - len(payload)))
            if not chunk:
                return None
            payload += chunk

        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        if opcode == 0x08:   # close
            return None
        if opcode == 0x09:   # ping → pong
            try: sock.sendall(bytes([0x8A, len(payload)]) + payload)
            except Exception: pass
            return b""
        if opcode in (0x01, 0x02):  # text / binary
            return payload
        return b""

    except Exception:
        return None


# ── Session login ──────────────────────────────────────────────────────────────
def _login() -> str:
    """
    POST to /api/auth/login and return the cookie string to use on subsequent
    requests.  Raises on failure.
    """
    url  = f"{UNIFI_HOST}/api/auth/login"
    body = json.dumps({"username": UNIFI_USER, "password": UNIFI_PASS}).encode()
    req  = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, context=ctx, timeout=15)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Login failed: HTTP {e.code} — {e.read()[:200]}")

    # Collect Set-Cookie headers (urllib flattens them, so parse manually)
    cookie_parts = []
    for k, v in resp.headers.items():
        if k.lower() == "set-cookie":
            # Only keep the name=value part, not the attributes
            cookie_parts.append(v.split(";")[0].strip())

    if not cookie_parts:
        raise RuntimeError("Login succeeded but no cookies returned")

    cookie_str = "; ".join(cookie_parts)
    print(f"[WS] Session established ({len(cookie_parts)} cookies)", flush=True)
    return cookie_str


# ── Background UniFi WebSocket thread ─────────────────────────────────────────
_WS_PATH = "/proxy/network/wss/s/default/events"

def _unifi_ws_thread():
    import time

    backoff    = 5
    cookie_str = ""

    while True:
        sock = None
        try:
            # (Re-)authenticate if we have no session
            if not cookie_str:
                cookie_str = _login()

            host, port_str = UNIFI_WS_HOST.rsplit(":", 1)
            port   = int(port_str)
            origin = UNIFI_HOST.rstrip("/")
            ws_key = _random_ws_key()

            raw = socket.create_connection((host, port), timeout=15)
            sock = ctx.wrap_socket(raw, server_hostname=host) if UNIFI_WS_USE_SSL else raw

            upgrade = (
                f"GET {_WS_PATH} HTTP/1.1\r\n"
                f"Host: {UNIFI_WS_HOST}\r\n"
                f"Origin: {origin}\r\n"
                f"Cookie: {cookie_str}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {ws_key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"\r\n"
            )
            sock.sendall(upgrade.encode())

            # Read response headers
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = sock.recv(1)
                if not chunk:
                    raise ConnectionError("Connection closed before headers")
                resp += chunk

            resp_str = resp.decode(errors="replace")

            # Session expired — force re-login on next attempt
            if "401" in resp_str or "403" in resp_str:
                print("[WS] Session rejected — clearing cookies, will re-login", flush=True)
                cookie_str = ""
                raise ConnectionError("Session expired")

            if "101" not in resp_str:
                try: extra = sock.recv(512)
                except Exception: extra = b""
                print(
                    f"[WS] Handshake rejected:\n{resp_str}{extra.decode(errors='replace')}",
                    flush=True,
                )
                raise ConnectionError("Handshake rejected")

            print(f"[WS] Connected → {UNIFI_WS_HOST}{_WS_PATH}", flush=True)
            backoff = 5   # reset on successful connect

            sock.settimeout(90)

            while True:
                payload = _recv_ws_frame(sock)
                if payload is None:
                    print("[WS] Connection closed by UniFi", flush=True)
                    break
                if not payload:
                    continue

                text = payload.decode(errors="replace")

                # ── Event filtering ───────────────────────────────────────
                # UniFi streams continuous high-frequency stat frames that
                # are hundreds of KB each.  We only want topology/state
                # events that signal something meaningful changed.
                # Drop anything that isn't worth waking the browser for.
                try:
                    msg  = __import__('json').loads(text)
                    meta = msg.get("meta", {})
                    msg_type = (
                        meta.get("message") or
                        meta.get("type")    or
                        meta.get("rc")      or ""
                    ).lower()

                    # Allowlist: only forward events that represent genuine
                    # topology or state changes worth re-fetching data for.
                    # Everything else (high-freq sync frames, counters, VPN
                    # heartbeats) is dropped here — the 10s stat poll covers
                    # those instead.
                    FORWARD_FULL = {
                        "device:state-changed",
                        "device:adopt",
                        "device:delete",
                        "client:add",
                        "client:remove",
                        "alert",
                        "events",
                        "provision",
                        "upgrade",
                    }

                    if msg_type in FORWARD_FULL:
                        print(f"[WS] forwarding type={msg_type!r} size={len(text)}B", flush=True)

                    elif msg_type == "unifi-device:sync":
                        # Strip to only the fields the browser needs for live
                        # stat updates — avoids pushing 4-8KB per frame.
                        stripped = []
                        for dev in (msg.get("data") or []):
                            sys_s = dev.get("system-stats") or {}
                            entry = {
                                "mac":     dev.get("mac", ""),
                                "ip":      dev.get("ip", ""),
                                "cpu":     sys_s.get("cpu", 0),
                                "mem":     sys_s.get("mem", 0),
                                "clients": dev.get("num_sta", 0),
                            }
                            port_table = dev.get("port_table") or []
                            entry["portsUsed"] = sum(1 for p in port_table if p.get("up"))
                            uplink = dev.get("uplink") or {}
                            entry["tx"] = round((uplink.get("tx_bytes-r") or 0) / 1024, 1)
                            entry["rx"] = round((uplink.get("rx_bytes-r") or 0) / 1024, 1)
                            stripped.append(entry)
                        if stripped:
                            import json as _json
                            out = _json.dumps({"meta": {"message": "stat-update"}, "data": stripped})
                            _sse_broadcast(out)
                        continue

                    else:
                        if msg_type not in _seen_ws_types:
                            _seen_ws_types.add(msg_type)
                            print(f"[WS] dropping type={msg_type!r} size={len(text)}B", flush=True)
                        continue

                except Exception:
                    continue

                _sse_broadcast(text)

        except Exception as exc:
            print(f"[WS] Error: {exc} — retrying in {backoff}s", flush=True)
        finally:
            if sock:
                try: sock.close()
                except Exception: pass

        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


_seen_ws_types: set = set()   # tracks unknown types so we log each only once
threading.Thread(target=_unifi_ws_thread, daemon=True).start()


# ── HTTP handler ───────────────────────────────────────────────────────────────
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WWW_DIR, **kwargs)

    def _ingress_path(self):
        return self.headers.get("X-Ingress-Path", INGRESS_ENTRY).rstrip("/")

    def do_GET(self):
        ingress = self._ingress_path()
        print(f"← GET {self.path!r}  ingress:{ingress!r}", flush=True)

        path = self.path
        if ingress and path.startswith(ingress):
            path = path[len(ingress):] or "/"
        self.path = path

        if self.path.startswith("/unifi-sse"):
            self._handle_sse()
            return

        if self.path.startswith("/unifi/"):
            self._proxy_unifi()
            return

        # Serve index.html (and root) with no-cache headers so the browser
        # always validates against the server rather than using a stale copy.
        if self.path in ("/", "", "/index.html"):
            self._serve_nocache_html()
            return

        super().do_GET()

    def _serve_nocache_html(self):
        """Serve index.html with no-cache headers and an ETag so the browser
        always revalidates but can still use a 304 when nothing changed."""
        filepath = os.path.join(WWW_DIR, "index.html")
        try:
            stat = os.stat(filepath)
            etag = f'"{int(stat.st_mtime)}-{stat.st_size}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.end_headers()
                return
            with open(filepath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type",   "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.send_header("Cache-Control",  "no-cache")
            self.send_header("ETag",           etag)
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def _handle_sse(self):
        q = _sse_subscribe()
        print(f"[SSE] Client connected ({len(_sse_clients)} total)", flush=True)
        try:
            self.send_response(200)
            self.send_header("Content-Type",      "text/event-stream")
            self.send_header("Cache-Control",     "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection",        "keep-alive")
            self._cors()
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()

            while True:
                try:
                    data = q.get(timeout=25)
                    safe = data.replace("\n", "\ndata: ")
                    self.wfile.write(f"data: {safe}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()

        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print(f"[SSE] Error: {exc}", flush=True)
        finally:
            _sse_unsubscribe(q)
            print(f"[SSE] Client gone ({len(_sse_clients)} remaining)", flush=True)

    def _proxy_unifi(self):
        url = UNIFI_HOST + self.path[len("/unifi"):]
        print(f"→ Proxy: {url}", flush=True)
        req = urllib.request.Request(url, headers={
            "X-API-KEY": API_KEY,
            "Accept":    "application/json",
        })
        try:
            resp = urllib.request.urlopen(req, context=ctx)
            body = resp.read()
            print(f"← {resp.status} ({len(body)} B)", flush=True)
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
            print(f"← HTTP {e.code}: {body[:300]}", flush=True)
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            print(f"← Exception: {traceback.format_exc()}", flush=True)
            self.send_response(500)
            self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


print(f"Starting on :{PORT}  UniFi→{UNIFI_HOST}  WS→{UNIFI_WS_HOST}{_WS_PATH}")
ReusableHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()