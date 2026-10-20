#!/usr/bin/env bash
# Arranca la app de verdad (sesión D-Bus y HOME aislados) y la cierra con una acción rápida.
# Atrapa errores de import y de arranque que `py_compile` no ve.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
home="$(mktemp -d)"
trap 'rm -rf "$home"' EXIT
mkdir -p "$home/.config/siesa-dev-launcher"
cat > "$home/.config/siesa-dev-launcher/settings.json" <<JSON
{"services_root": "$home", "tunnel_script": "$home/business-dev.sh", "ovpn_file": "$home/x.ovpn", "vpn_user": "ci@example.com"}
JSON
HOME="$home" dbus-run-session -- bash -c '
  set -e
  python3 "'"$root"'/launcher.py" --background & app=$!
  for _ in $(seq 1 20); do
    gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
      --method org.freedesktop.DBus.NameHasOwner com.siesa.DevLauncher | grep -q true && break
    kill -0 $app 2>/dev/null || { echo "la app terminó antes de arrancar"; exit 1; }
    sleep 0.5
  done
  sleep 2
  kill -0 $app 2>/dev/null || { echo "la app se cayó al arrancar"; exit 1; }
  python3 "'"$root"'/launcher.py" --action quit
  wait $app
'
echo "smoke test OK"
