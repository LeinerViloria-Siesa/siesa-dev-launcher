# Siesa Dev Launcher

Aplicación de escritorio (GTK3, tema oscuro) para el desarrollo local de los servicios `business-financiero-*`.

- **VPN**: importa el `.ovpn` a NetworkManager y conecta/desconecta (pide la contraseña; opcionalmente la recuerda NetworkManager).
- **Túneles Cloud SQL**: ejecuta `business-dev.sh` (el de Ajustes) y puede cerrar uno levantado desde otra terminal. Con **+ Añadir túnel** agregas otros scripts como ese (solo nombre y script; el puerto se lee del `_BT_LOCAL_PORT` del script); pueden estar activos a la vez, cada uno con su estado, su punto en el riel y su log. Si otro proceso ya ocupa el puerto, no se levanta y se avisa. Se guardan en `~/.config/siesa-dev-launcher/tunnels.json`.
- **Servicios**: iniciar, reiniciar y detener backend + frontend, con indicador de acción en curso y botones según el estado real.
- **Servicios personalizados**: añade ramas o copias del repo (otra carpeta, otros puertos).
- **Logs** a todo lo alto en tabs internos: VPN, Túnel, Servicios (por servicio, backend o frontend) y General servicios (lo que ejecuta `start-services.sh`). Con colores, buscador, pausa de seguimiento y scroll libre; VPN, Túnel y General quedan en `~/siesa-dev-logs/_*.log`. El panel de control se puede colapsar (F9).
- **Segundo plano**: bandeja del sistema, notificaciones y acciones rápidas.

## Instalar (Ubuntu / Debian)

Cada push a `main` genera un paquete nuevo y lo publica en
[Releases](../../releases/latest). Descarga el `.deb` y:

```bash
sudo apt install ./siesa-dev-launcher_*_all.deb
```

Con `gh` (el repo es privado):

```bash
gh release download --repo LeinerViloria-Siesa/siesa-dev-launcher --pattern '*.deb'
sudo apt install ./siesa-dev-launcher_*_all.deb
```

`apt` instala solo las dependencias (GTK, bandeja del sistema, NetworkManager + OpenVPN, lsof, curl).
Actualizar es instalar el `.deb` más nuevo encima. Luego busca **Siesa Dev Launcher** en el menú de aplicaciones.

También necesitas en tu `PATH` (no los instala el paquete): `gcloud`, `kubectl`, `dotnet` (SDK 10) y `node`.

## Primer uso

Se abre **Ajustes** con valores detectados: carpeta que contiene los repos `business-financiero-*`,
script del túnel (`business-dev.sh`), archivo `.ovpn` y correo de la VPN. Revisa y guarda.
Ahí mismo puedes activar "Iniciar en segundo plano al abrir sesión".

La configuración es por persona y vive en `~/.config/siesa-dev-launcher/` (`settings.json` y
`services.json`); los logs de los servicios, en `~/siesa-dev-logs/`. Nada personal está en el repo.

## Segundo plano y acciones rápidas

- Cerrar la ventana deja la app en la **bandeja**; salir de verdad: "Salir" en la bandeja o `Ctrl+Q`.
- Avisos de escritorio cuando cambia la VPN o el túnel, o termina una acción de servicios.
- Clic derecho sobre el icono o por terminal (se reenvía a la instancia en curso):
  `siesa-dev-launcher --action start-all | reload-all | stop-all | vpn-toggle | tunnel-toggle | disconnect-all | quit`
- `siesa-dev-launcher --background` arranca sin ventana.
- En GNOME hace falta la extensión AppIndicator (Ubuntu la trae activada).

## Desarrollo

```bash
python3 launcher.py            # ejecutar desde el código
./install.sh [--autostart]     # registrar en el menú desde el código fuente
packaging/build-deb.sh 1.0.0   # construir el .deb en dist/
```

El flujo `.github/workflows/build.yml` valida la sintaxis, construye el `.deb` (versión `1.0.<n.º de ejecución>`)
y crea el release.
