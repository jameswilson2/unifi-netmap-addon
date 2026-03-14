# UniFi Network Map

A live, interactive topology map of your UniFi network — hosted directly on your Home Assistant server and accessible from the HA sidebar.

## Features

- 🗺️ Interactive drag-and-drop network topology
- 📊 Live CPU, memory, client counts and port usage per device
- 🌗 Dark / light theme toggle (preference saved in browser)
- 🔄 Auto-refreshes every 5 seconds
- 🔒 Proxies all UniFi API calls server-side — your API key never touches the browser

## Requirements

- UniFi Network Application (self-hosted or UniFi OS)
- A UniFi API key (generated in the UniFi Network settings under **Control Plane → API**)

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**
2. Click the three-dot menu (⋮) → **Repositories**
3. Add the URL of the repository containing this add-on
4. Find **UniFi Network Map** and click **Install**
5. Set your `unifi_host` and `api_key` in the add-on **Configuration** tab
6. Click **Start**
7. Enable **Show in sidebar** on the add-on **Info** tab
