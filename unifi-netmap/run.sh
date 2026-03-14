#!/usr/bin/with-contenv bashio

# Read user configuration from HA options
UNIFI_HOST=$(bashio::config 'unifi_host')
API_KEY=$(bashio::config 'api_key')

bashio::log.info "Starting UniFi Network Map..."
bashio::log.info "UniFi host: ${UNIFI_HOST}"

# Export for the Python server
export UNIFI_HOST="${UNIFI_HOST}"
export API_KEY="${API_KEY}"

# Get the ingress entry path assigned by HA Supervisor
INGRESS_ENTRY=$(bashio::addon.ingress_entry)
export INGRESS_ENTRY="${INGRESS_ENTRY}"

bashio::log.info "Ingress path: ${INGRESS_ENTRY}"

exec python3 /server.py
