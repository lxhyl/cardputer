#!/usr/bin/env bash
# Generate a self-signed TLS cert + key for the Morse decoder web UI.
# Browsers require https for camera/mic getUserMedia, even on a LAN IP.
# Re-run if your LAN IP changes.

set -euo pipefail
cd "$(dirname "$0")"

LAN_IP=${1:-$(ifconfig 2>/dev/null \
    | awk '/inet 192\.168\.|inet 10\.|inet 172\.(1[6-9]|2[0-9]|3[0-1])\./{print $2; exit}')}

if [ -z "${LAN_IP:-}" ]; then
  echo "usage: $0 <lan-ip>      e.g. $0 192.168.1.42" >&2
  exit 1
fi

echo "Generating cert for IP: $LAN_IP"
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout key.pem -out cert.pem \
  -subj "/CN=$LAN_IP" \
  -addext "subjectAltName=IP:$LAN_IP,IP:127.0.0.1,DNS:localhost"

echo "OK. Now: python3 serve.py  →  https://$LAN_IP:8443/decoder.html"
