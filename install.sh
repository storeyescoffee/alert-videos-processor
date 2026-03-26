#!/usr/bin/env bash
# Minimal install: distro Python modules via apt (python3-* packages), no venv/pip.
set -euo pipefail

if ! command -v apt-get >/dev/null 2>&1; then
  echo "error: apt-get not found (this script is for Debian/Ubuntu)" >&2
  exit 1
fi

# Matches requirements.txt: boto3, requests, tqdm, paho-mqtt
sudo apt-get install -y --no-install-recommends \
  python3 \
  python3-boto3 \
  python3-requests \
  python3-tqdm \
  python3-paho-mqtt

echo "done: python3-* packages installed"
