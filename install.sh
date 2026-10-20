#!/usr/bin/env bash
# Registra el launcher en el menú de aplicaciones del usuario actual, con acciones rápidas
# (clic derecho sobre el ícono). Con --autostart arranca en segundo plano al iniciar sesión.
set -euo pipefail
dir="$(cd "$(dirname "$0")" && pwd)"
app_id="com.siesa.DevLauncher"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$apps"
rm -f "$apps/siesa-dev-launcher.desktop"  # nombre de versiones anteriores

icons="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"
mkdir -p "$icons"
cp "$dir/icon.png" "$icons/$app_id.png"
gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true

cat > "$apps/$app_id.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Siesa Dev Launcher
Comment=VPN, túnel Cloud SQL y servicios business-financiero
Exec=python3 ${dir}/launcher.py
Icon=${app_id}
Terminal=false
Categories=Development;
StartupNotify=true
Actions=StartAll;StopAll;VpnToggle;TunnelToggle;DisconnectAll;

[Desktop Action StartAll]
Name=Iniciar todos los servicios
Exec=python3 ${dir}/launcher.py --action start-all

[Desktop Action StopAll]
Name=Detener todos los servicios
Exec=python3 ${dir}/launcher.py --action stop-all

[Desktop Action VpnToggle]
Name=Conectar / desconectar VPN
Exec=python3 ${dir}/launcher.py --action vpn-toggle

[Desktop Action TunnelToggle]
Name=Conectar / desconectar túnel
Exec=python3 ${dir}/launcher.py --action tunnel-toggle

[Desktop Action DisconnectAll]
Name=Desconectar todo
Exec=python3 ${dir}/launcher.py --action disconnect-all
DESK

if [[ "${1:-}" == "--autostart" ]]; then
  mkdir -p ~/.config/autostart
  sed -e "s|^Exec=python3 ${dir}/launcher.py\$|Exec=python3 ${dir}/launcher.py --background|" \
      "$apps/$app_id.desktop" > ~/.config/autostart/$app_id.desktop
  echo "Autoarranque en segundo plano activado."
fi

echo "Instalado."
echo "Requiere: python3-gi, gir1.2-gtk-3.0, network-manager-openvpn, lsof, gcloud, kubectl, dotnet (SDK 10), node."
echo "Para la bandeja del sistema (segundo plano): sudo apt install gir1.2-ayatanaappindicator3-0.1"
