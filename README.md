# Siesa Dev Launcher

Aplicación de escritorio (GTK3, tema oscuro) para el desarrollo local de los servicios `business-financiero-*`:

- **VPN**: importa el `.ovpn` a NetworkManager y conecta/desconecta (pide la contraseña; opcionalmente la recuerda NetworkManager).
- **Túnel Cloud SQL**: ejecuta `business-dev.sh`, y también puede cerrar un túnel levantado desde otra terminal.
- **Servicios**: iniciar, reiniciar y detener backend + frontend, con indicador de acción en curso.
- **Servicios personalizados**: añade ramas o copias del repo (otra carpeta, otros puertos).
- **Logs** de cada servicio, con scroll libre y seguimiento del final.

## Instalación

```bash
./install.sh
```

Registra la app en el menú de aplicaciones. Requiere: `python3-gi`, `gir1.2-gtk-3.0`,
`network-manager-openvpn`, `lsof`, `gcloud`, `kubectl`, `dotnet` (SDK 10) y `node`.

También se puede abrir con `python3 launcher.py`.

## Configuración (por persona)

La primera vez se abre **Ajustes**: carpeta de los repos, script del túnel, archivo `.ovpn` y correo de la VPN.
Se guardan en `~/.config/siesa-dev-launcher/settings.json`; los servicios personalizados en
`~/.config/siesa-dev-launcher/services.json`. Nada personal vive en el repo.

Los logs se escriben en `~/siesa-dev-logs/`.
