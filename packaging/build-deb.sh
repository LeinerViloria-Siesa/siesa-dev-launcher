#!/usr/bin/env bash
# Construye dist/siesa-dev-launcher_<versión>_all.deb  —  uso: packaging/build-deb.sh [versión]
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
version="${1:-0.0.0-dev}"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

lib="$stage/usr/lib/siesa-dev-launcher"
install -d "$lib/tray" "$stage/usr/bin" "$stage/usr/share/applications" \
  "$stage/usr/share/icons/hicolor/256x256/apps" "$stage/DEBIAN"

install -m 755 "$root/launcher.py" "$root/start-services.sh" "$lib/"
install -m 644 "$root/icon.png" "$lib/icon.png"
install -m 644 "$root/tray/com.siesa.DevLauncher.png" "$lib/tray/"
echo "$version" > "$lib/VERSION"
install -m 755 "$root/packaging/siesa-dev-launcher" "$stage/usr/bin/siesa-dev-launcher"
install -m 644 "$root/packaging/com.siesa.DevLauncher.desktop" "$stage/usr/share/applications/"
install -m 644 "$root/icon.png" "$stage/usr/share/icons/hicolor/256x256/apps/com.siesa.DevLauncher.png"
sed "s/@VERSION@/$version/" "$root/packaging/control" > "$stage/DEBIAN/control"
install -m 755 "$root/packaging/postinst" "$stage/DEBIAN/postinst"

mkdir -p "$root/dist"
out="$root/dist/siesa-dev-launcher_${version}_all.deb"
dpkg-deb --root-owner-group --build "$stage" "$out" >/dev/null
echo "$out"
