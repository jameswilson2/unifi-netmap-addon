# UniFi Network Map — Documentation

## Configuration

| Option | Required | Description |
|---|---|---|
| `unifi_host` | ✅ | Full URL of your UniFi controller, e.g. `https://192.168.4.1` |
| `api_key` | ✅ | API key from UniFi Network → Control Plane → API |

### Example

```yaml
unifi_host: "https://192.168.4.1"
api_key: "your-api-key-here"
```

## How it works

The add-on runs a small Python HTTP server (port 8099) that:

1. Serves the topology map web UI via Home Assistant Ingress (no extra port needed)
2. Proxies all `/unifi/*` API requests to your UniFi controller server-side, so your API key is never exposed to the browser

## Sidebar access

After starting the add-on, go to the **Info** tab and toggle **Show in sidebar**. The map will appear as a **Network Map** entry in the HA sidebar and open inside an iframe.

## Self-signed certificates

The add-on accepts self-signed TLS certificates from the UniFi controller — no configuration needed.

## Troubleshooting

- **Status shows ERROR** — check that `unifi_host` is reachable from the HA server and the `api_key` is correct
- **Blank page / 503** — make sure the add-on is running (green indicator on the Info tab)
- **Devices not appearing** — confirm your API key has read access to the Network application
