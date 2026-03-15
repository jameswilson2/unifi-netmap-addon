# Changelog

## 1.3.5 - Port colours, uptime fixes & auto theme

### New Features
- **Port speed colours** — port dots on both the canvas nodes and the sidebar now use colour to indicate negotiated link speed: purple (10Gb), sky blue (2.5Gb), teal (1Gb), amber (100Mb), red (10Mb). Inactive ports remain grey as before
- **PoE indicator** — ports actively delivering Power over Ethernet show a `+` symbol inside the dot (requires `poe_mode` active and `poe_good` confirmed by the controller)
- **Auto theme mode** — the dark/light toggle now cycles through three modes: auto (⊙), dark (☀), light (☾). In auto mode the UI follows the OS preference in real time, including responding to live OS theme changes via `prefers-color-scheme`. The previously saved preference is preserved on upgrade

### Bug Fixes
- **Uptime showing 0d 0h on Flex Mini and similar devices** — some devices report `system-stats.uptime` as 0 while the correct value is in `sys_stats.uptime` or the top-level `uptime` field. The mapping now falls back through all three sources. Devices with genuinely zero uptime now show `—` instead of `0h 0m`
- **TX/RX showing 0 KB/s** — the rate fields were checked with a falsy test that treated a valid `0` as missing. Now uses an explicit `!= null` check, and falls back to the device-level `bytes-r` field as a secondary source for devices where uplink rate is not separately reported
- **U6+ and other APs showing 4 ports, 0 used** — access points expose virtual radio interfaces in their `port_table` alongside the single physical Ethernet port, inflating the port count. APs are now detected and their port table is filtered to physical Ethernet ports only (`media === 'GE'` or `port_idx <= 1`), so the U6+ correctly shows 1 port connected
- **UDR7 port count showing 0** — the full `portTable` array was not being stored on the device object after the initial mapping, so the renderer had nothing to work with. Now stored and passed through to both canvas and sidebar rendering

### Technical notes
- New `portSpeedColor(speedMbps)` helper maps speed values to CSS colours
- New `buildPortDot(port, cssClass, index)` helper constructs port dot HTML with speed colour, PoE indicator, and tooltip — used by both sidebar and canvas renderers
- New `applyThemeClasses(mode)` separates class application from the toggle logic, making auto mode a clean no-class state driven entirely by the CSS media query
- The OS theme change listener now correctly targets `prefers-color-scheme: dark` (previously `light`) so the `event.matches` value is unambiguous

---

## 1.3.4 - Gateway live stats

### New Features
- **Live stats for the gateway/router (UDR7 and equivalents)** — the gateway device now updates in real-time alongside all other devices. Previously it showed only the values from the initial page load and never refreshed, because the Network application WebSocket (`/proxy/network/wss/s/default/events`) does not emit stat frames for the device hosting the controller itself
- **CPU temperature for the gateway** — `cpu.temperature` is now captured from the UniFi OS system WebSocket and included in the stat-update payload, available for future display in the detail panel
- **Second background WebSocket thread** — `server.py` now connects to `/api/ws/system` (the UniFi OS-level endpoint, one layer above the Network application) in addition to the existing Network events stream. This endpoint streams `DEVICE_STATE_CHANGED` frames containing CPU load, memory, uptime and temperature for the gateway

### Improvements
- **Fully dynamic gateway discovery** — the gateway MAC address is read from the first `DEVICE_STATE_CHANGED` frame at runtime and normalised to standard colon-separated lowercase format. Nothing is hardcoded; the add-on works with any UniFi OS gateway without modification
- **Unified stat-update format** — gateway stats are broadcast to SSE clients in exactly the same `stat-update` format as regular device stats, so no frontend changes were required. The existing `applyStatUpdate()` matching logic handles the gateway automatically
- **Memory percentage derived correctly** — the system WS reports raw bytes (`total`, `free`, `available`); the server converts this to a percentage using `(total - available) / total * 100` to match the format used by other devices

### Technical notes
- `/api/ws/system` uses the same cookie-based session auth as `/proxy/network/wss/s/default/events` — no additional credentials required
- The system WS thread has the same exponential back-off reconnect and automatic session re-login behaviour as the Network events thread
- Startup log now shows both WS endpoints: `Network WS → ...` and `System WS → ...`

---

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