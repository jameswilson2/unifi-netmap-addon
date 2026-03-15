#!/bin/sh

OPTIONS=/data/options.json

# Parse options.json with Python (already installed)
UNIFI_HOST=$(python3 -c "import json; d=json.load(open('$OPTIONS')); print(d.get('unifi_host','https://192.168.4.1'))")
API_KEY=$(python3 -c "import json; d=json.load(open('$OPTIONS')); print(d.get('api_key',''))")
UNIFI_USER=$(python3 -c "import json; d=json.load(open('$OPTIONS')); print(d.get('username','admin'))")
UNIFI_PASS=$(python3 -c "import json; d=json.load(open('$OPTIONS')); print(d.get('password',''))")

export UNIFI_HOST
export API_KEY
export UNIFI_USER
export UNIFI_PASS

echo "Starting UniFi Network Map..."
echo "UniFi host: ${UNIFI_HOST}"
echo "UniFi user: ${UNIFI_USER}"

exec python3 /server.py
