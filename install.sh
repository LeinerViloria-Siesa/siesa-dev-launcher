#!/usr/bin/env bash
# Registra el launcher en el menú de aplicaciones del usuario actual.
set -euo pipefail
dir="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/siesa-dev-launcher.desktop <<DESK
[Desktop Entry]
Type=Application
Name=Siesa Dev Launcher
Comment=VPN, túnel Cloud SQL y servicios business-financiero
Exec=python3 ${dir}/launcher.py
Icon=network-vpn
Terminal=false
Categories=Development;
DESK
echo "Instalado. Requiere: python3-gi, gir1.2-gtk-3.0, network-manager-openvpn, lsof, gcloud, kubectl, dotnet, node."
