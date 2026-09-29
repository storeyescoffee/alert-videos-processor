#!/usr/bin/env bash
# Client-mode install (the default `python3 main.py` on the Pi): distro Python modules
# via apt (python3-* packages), no venv/pip, then sanity checks that this device can
# resolve its ID and reach the MQTT broker the server listens on.
#
# The server (`main.py --server`) is installed separately; see alert-processor-server.service.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v apt-get >/dev/null 2>&1; then
  echo "error: apt-get not found (this script is for Debian/Ubuntu)" >&2
  exit 1
fi

# Matches requirements.txt: boto3, requests, tqdm, paho-mqtt. The client only talks
# MQTT, but main.py imports the processing modules at load time, so all are needed.
sudo apt-get install -y --no-install-recommends \
  python3 \
  python3-boto3 \
  python3-requests \
  python3-tqdm \
  python3-paho-mqtt

echo "done: python3-* packages installed"

chmod +x main.py scripts/start.sh stop.sh 2>/dev/null || true

# Everything main.py imports must load.
if ! python3 -c "import main" >/dev/null; then
  echo "error: main.py failed to import; see the traceback above" >&2
  exit 1
fi

# The client publishes on storeyes/<device-id>/alert-processing, so the device ID must
# resolve (Pi hardware serial, else the .device.id file).
if ! device_id="$(python3 -c "from src.utils.device_utils import get_device_id; print(get_device_id())" 2>/dev/null)"; then
  echo "error: could not resolve a device ID (not a Pi and no .device.id file in $SCRIPT_DIR)" >&2
  exit 1
fi
echo "device ID: $device_id"

# The broker must be reachable for the request and the server's summary. Uses the same
# MQTT_* environment overrides as main.py.
if python3 - <<'PY'
import sys, threading
import paho.mqtt.client as mqtt
from main import MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS

connected = threading.Event()
client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
client.username_pw_set(MQTT_USER, MQTT_PASS)
client.on_connect = lambda c, u, f, rc, p=None: rc == 0 and connected.set()
try:
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=10)
    client.loop_start()
    ok = connected.wait(timeout=10)
    client.loop_stop()
    client.disconnect()
except OSError as e:
    print(f"MQTT connect error: {e}", file=sys.stderr)
    ok = False
print(f"MQTT broker {MQTT_HOST}:{MQTT_PORT}: {'reachable' if ok else 'NOT reachable'}")
sys.exit(0 if ok else 1)
PY
then
  :
else
  echo "warning: MQTT broker not reachable; the client will fail until it is" >&2
fi

echo
echo "Client mode installed. Run it with:"
echo "  $SCRIPT_DIR/scripts/start.sh        # logs under $SCRIPT_DIR/logs/"
echo "  python3 $SCRIPT_DIR/main.py [--fallback | --date-cursor N] [--timeout SECONDS]"
