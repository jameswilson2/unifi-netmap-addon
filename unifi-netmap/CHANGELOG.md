# Changelog

## 1.3.3 - Live stat updates & debug mode

### New Features
- **Debug mode** — toggle via add-on Configuration tab (`debug: true`) or the 🐛 button in the top bar. When enabled, logs every step of the SSE pipeline, device matching, and DOM patching to the browser console
- **Debug report** — clicking the debug toggle fires an instant snapshot of all device state, DOM node count, SSE connection status, and scale/pan values

### Improvements
- **Live stat updates without page refresh** — CPU, memory, and client counts now update in real-time via server-pushed SSE events rather than polling. No more full page re-renders
- **Server-side stat filtering** — the server strips `unifi-device:sync` frames down to only the 6 fields the browser needs (mac, ip, cpu, mem, clients, portsUsed) before forwarding over SSE, reducing per-update payload from ~6KB to ~200B
- **Reliable device matching** — devices are now matched using both the stat API MAC/IP (`statMac`, `statIp`) and the integration API MAC/IP. Fixes a bug where multi-interface devices (e.g. UDR7) were never matched because the integration API returns the WAN interface MAC while the stat API uses the LAN interface MAC
- **`siteId` cached after first fetch** — the `/sites` endpoint is only called once per session instead of on every refresh, eliminating the burst of repeated sites calls seen in logs
- **In-flight guards** — `fetchFromAPI` and `pollStats` now track in-flight requests and skip if a previous call hasn't completed, preventing request pile-ups under HA Ingress

### Bug Fixes
- **WebSocket replaced with SSE** — raw WebSocket tunnelling through HA Ingress was unreliable; the server now maintains one persistent WebSocket to UniFi and fans events out to browsers via Server-Sent Events (plain HTTP), which HA Ingress handles correctly
- **UniFi WS authentication fixed** — WebSocket connection to UniFi now uses cookie-based session auth (POST `/api/auth/login`) instead of API key header, which the WS endpoint does not accept. Session is refreshed automatically on expiry
- **`Origin` header added to WS handshake** — UniFi's nginx rejected WS upgrades without a valid `Origin` header (HTTP 400)
- **Random `Sec-WebSocket-Key`** — previously sent a hardcoded static key on every connection; now generates a fresh random key per connection as required by the WS spec
- **`resolveBase()` fixed for all HA Ingress path formats** — the previous regex stripped the ingress token from the path when the page was served without a trailing slash, causing all API fetch calls to 404
- **`index.html` served with `no-cache` headers** — prevents browsers from serving a stale cached page after add-on updates
- **`struct` removed from imports** — dead import in `server.py`
- **Port corrected in `DOCS.md`** — documentation incorrectly stated port 8099; corrected to 8765

### Configuration
- Added `username` and `password` options (required for WebSocket session authentication)
- Added `debug` option (boolean, default `false`)

---

## 1.2.0 - Stability & performance

### Improvements
- **Event allowlist on server** — replaced broad event blocklist with a strict allowlist (`FORWARD_TYPES`). Only meaningful topology events (device state change, client add/remove, alerts, provision, upgrade) are forwarded to SSE clients. High-frequency `unifi-device:sync`, `sta:sync`, `vpn-connection:sync` and session frames are dropped server-side, reducing SSE data volume from ~50MB/continuous to near zero
- **Unknown event type logging** — each new unknown WS event type is logged once to the add-on log for easy tuning
- **`pollStats` abort timeout** — stat poll requests now abort after 8 seconds rather than hanging indefinitely (HA Ingress times out the underlying connection on large responses)
- **`pollStats` disabled** — redundant now that live stats are pushed via SSE; remains in code as a commented-out safety net

---

## 1.1.0 - Ingress & WebSocket fixes

### Improvements
- **Ingress base-path rewriting** — server strips the HA Ingress prefix from all incoming paths so static files and API proxying work correctly behind the sidebar panel
- **Mobile sidebar** — sidebar becomes a slide-over drawer on screens ≤ 600px with a backdrop overlay
- **Touch support** — single-finger pan and two-finger pinch-to-zoom on mobile devices
- **Zoom controls hidden on mobile** — pinch gesture replaces +/− buttons on small screens
- **Minimap hidden on mobile** — not useful at phone screen sizes

---

## 1.0.0 - Initial release

- Live UniFi network topology map
- Dark / light theme toggle with localStorage persistence
- Server-side UniFi API proxy (API key never sent to browser)
- Home Assistant Ingress support (sidebar panel, no extra port)
- Auto-refresh every 5 seconds
- Drag-and-drop node layout with position memory
- Minimap overview