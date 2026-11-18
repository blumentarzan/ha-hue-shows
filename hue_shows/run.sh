#!/usr/bin/with-contenv bashio
# Credentials/state live in /data, which survives add-on updates.
export HUE_SHOW_CONFIG=/data/hue_show.json
export HUE_BRIDGE="$(bashio::config 'bridge')"
bashio::log.info "Starting Hue Shows on port 8765 (Ingress)"
exec python3 /app/hue_live.py --host 0.0.0.0 --port 8765
