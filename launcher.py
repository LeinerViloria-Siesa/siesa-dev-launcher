#!/usr/bin/env python3
"""Siesa Dev Launcher: VPN (NetworkManager), túnel a Cloud SQL y servicios business-financiero."""
import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

HERE = Path(__file__).resolve().parent
APP_ID = "com.siesa.DevLauncher"
SERVICES_SCRIPT = HERE / "start-services.sh"
VPN_NAME = "SIESA-VPN"
TUNNEL_PORT = 5434
LOG_DIR = Path.home() / "siesa-dev-logs"
CONFIG_DIR = Path.home() / ".config" / "siesa-dev-launcher"
CONFIG = CONFIG_DIR / "services.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# Ajustes por persona (nada de esto va en el código): se guardan en ~/.config y se editan desde la app.
SETTINGS_FIELDS = [
    ("services_root", "Carpeta que contiene los repos business-financiero-*", "dir"),
    ("tunnel_script", "Script del túnel (business-dev.sh)", "file"),
    ("ovpn_file", "Archivo .ovpn de la VPN", "file"),
    ("vpn_user", "Usuario de la VPN (correo)", None),
]
SETTINGS = {k: "" for k, _, _ in SETTINGS_FIELDS}
SETTINGS["theme"] = "dark"  # dark | light
TUNNEL_SCRIPT = OVPN_FILE = Path()
VPN_USER = ""


def load_settings():
    global TUNNEL_SCRIPT, OVPN_FILE, VPN_USER
    try:
        SETTINGS.update(json.loads(SETTINGS_FILE.read_text()))
    except (OSError, ValueError):
        pass
    TUNNEL_SCRIPT = Path(SETTINGS["tunnel_script"]).expanduser()
    OVPN_FILE = Path(SETTINGS["ovpn_file"]).expanduser()
    VPN_USER = SETTINGS["vpn_user"]


def detect_defaults():
    """Sugerencias para el primer arranque (solo prellenan el diálogo de Ajustes; no se guardan solas)."""
    d = {k: "" for k, _, _ in SETTINGS_FIELDS}
    home = Path.home()
    roots = [home / n for n in ("Escritorio", "Desktop", "Documentos", "Documents", "Projects", "dev", "src", "")]
    for base in roots:
        for depth in ("", "*", "*/*"):
            for hit in sorted(base.glob(f"{depth}/business-financiero-*".lstrip("/")) if depth else
                              base.glob("business-financiero-*")):
                if hit.is_dir():
                    d["services_root"] = str(hit.parent)
                    break
            if d["services_root"]:
                break
        if d["services_root"]:
            break
    search = [Path(d["services_root"])] if d["services_root"] else []
    search += [home / "Escritorio", home / "Documentos", home]
    for base in search:
        for pattern in ("business-dev.sh", "*/business-dev.sh", "*/*/business-dev.sh"):
            hit = next(iter(sorted(base.glob(pattern))), None) if base.is_dir() else None
            if hit:
                d["tunnel_script"] = str(hit)
                break
        if d["tunnel_script"]:
            break
    for folder in (home / "Descargas", home / "Downloads", home / "Escritorio"):
        hit = next(iter(sorted(folder.glob("*.ovpn"))), None) if folder.is_dir() else None
        if hit:
            d["ovpn_file"] = str(hit)
            m = re.search(r"-([^-]+@[^-]+?)-client", hit.name)
            d["vpn_user"] = m.group(1) if m else ""
            break
    if not d["vpn_user"]:
        r = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True)
        d["vpn_user"] = r.stdout.strip()
    return d


AUTOSTART_FILE = Path.home() / ".config" / "autostart" / f"{APP_ID}.desktop"


def autostart_enabled():
    return AUTOSTART_FILE.exists()


def set_autostart(enabled):
    if not enabled:
        AUTOSTART_FILE.unlink(missing_ok=True)
        return
    AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART_FILE.write_text(
        "[Desktop Entry]\nType=Application\nName=Siesa Dev Launcher\n"
        f"Exec={sys.executable} {Path(__file__).resolve()} --background\n"
        f"Icon={APP_ID}\nX-GNOME-Autostart-enabled=true\n")


def read_version():
    try:
        return (HERE / "VERSION").read_text().strip()
    except OSError:
        return ""


def save_settings():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(SETTINGS, indent=2, ensure_ascii=False))
    load_settings()


def script_env():
    return {**os.environ, "SERVICES_ROOT": str(Path(SETTINGS["services_root"]).expanduser()),
            "TUNNEL_SCRIPT": str(TUNNEL_SCRIPT)}

# name, backend port (el frontend es puerto + 1000)
SERVICES = [
    ("third-party", 7016),
    ("accounting", 7018),
    ("liquid-tax", 7020),
    ("base-config", 7014),
    ("segments", 7012),
]

try:  # bandeja del sistema: opcional (gir1.2-ayatanaappindicator3-0.1)
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator
    except (ValueError, ImportError):
        AppIndicator = None

REPO_DIRS = {
    "third-party": "business-financiero-third-party-service",
    "accounting": "business-financiero-accounting-service",
    "liquid-tax": "business-financiero-liquid-tax-service",
    "base-config": "business-financiero-base-config",
    "segments": "business-financiero-segments-service",
}

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CSI = re.compile(r"\x1b\[([0-9;]*)([A-Za-z])")
# Paleta para fondo oscuro: 0-7 normales y 8-15 brillantes.
ANSI_COLORS = ["#6b7280", "#f87171", "#4ade80", "#facc15", "#60a5fa", "#c084fc", "#22d3ee", "#e5e7eb",
               "#9ca3af", "#fca5a5", "#86efac", "#fde047", "#93c5fd", "#d8b4fe", "#67e8f9", "#ffffff"]


def ansi_256(n):
    if n < 16:
        return ANSI_COLORS[n]
    if n < 232:
        n -= 16
        r, g, b = (n // 36) % 6, (n // 6) % 6, n % 6
        return "#%02x%02x%02x" % tuple(0 if c == 0 else 55 + 40 * c for c in (r, g, b))
    v = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (v, v, v)


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def run(cmd, **kw):
    """Como subprocess.run, pero un comando ausente (nmcli, gcloud, lsof…) no tumba la app: devuelve rc 127."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace", **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: comando no encontrado\n")


def load_custom():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return []


def save_custom(items):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(items, indent=2, ensure_ascii=False))


TUNNELS_FILE = CONFIG_DIR / "tunnels.json"


def load_tunnels():
    """Túneles adicionales (el principal es el script de Ajustes): [{"name", "script", "port"}]."""
    try:
        return json.loads(TUNNELS_FILE.read_text())
    except (OSError, ValueError):
        return []


def save_tunnels(items):
    TUNNELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TUNNELS_FILE.write_text(json.dumps(items, indent=2, ensure_ascii=False))


class Tunnel:
    """Un túnel: script que lo levanta + puerto local que lo delata. Cada uno lleva su propio proceso, estado y log."""

    def __init__(self, key, name, script, port, main=False):
        self.key, self.name, self.port, self.main = key, name, int(port), main
        self._script = script
        self.proc = None
        self.busy = False
        self.last = None            # último estado avisado (para avisar solo cuando cambia)
        self.down_since = None      # desde cuándo no responde el puerto (margen de reconexión del watchdog)
        self.up = False             # el puerto responde
        self.foreign = None         # (pid, cmd) si el puerto lo ocupa algo que no es el túnel
        self.dot = self.lbl = self.btn = self.rail = None

    @property
    def script(self):
        return TUNNEL_SCRIPT if self.main else Path(self._script).expanduser()

    @property
    def src(self):
        """Fuente de log: `_tunnel.log` el principal, `_tunnel-<clave>.log` los demás."""
        return "tunnel" if self.main else f"tunnel-{self.key}"

    @property
    def tab(self):
        return "Túnel" if self.main else self.name

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def watchdog_log(self):
        """Archivo donde el script anota los reintentos de su watchdog (`_BT_LOG_FILE="..."`), si lo declara."""
        try:
            m = re.search(r'_LOG_FILE="([^"]+)"', self.script.read_text(errors="replace"))
            return Path(m.group(1)) if m else None
        except OSError:
            return None


def script_port(path):
    """Puerto local que declara el script del túnel (`_BT_LOCAL_PORT=5434`); None si no lo encuentra."""
    try:
        m = re.search(r'^\s*_BT_LOCAL_PORT\s*=\s*"?(\d+)"?', Path(path).expanduser().read_text(errors="replace"), re.M)
        return int(m.group(1)) if m else None
    except OSError:
        return None


def port_listener(port):
    """Quién escucha en el puerto: (pid, línea de comando) o None si no se ve (libre, o de otro usuario)."""
    for pid in run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]).stdout.split():
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            cmd = ""
        return int(pid), cmd
    return None


def foreign_listener(port):
    """Proceso que ocupa el puerto sin ser un túnel (`kubectl port-forward`), o None."""
    own = port_listener(port)
    return own if own and "port-forward" not in own[1] else None


def short_cmd(cmd, pid):
    first = Path(cmd.split()[0]).name if cmd.split() else "proceso"
    return f"{first} (PID {pid})"


def kill_port(port):
    r = run(["lsof", "-ti", f":{port}"])
    for pid in r.stdout.split():
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            pass


# Cierra lo que dejó un túnel levantado desde otra terminal (equivale a business-dev-disconnect):
# reutiliza las funciones de limpieza del script sin ejecutar su main.
EXTERNAL_TUNNEL_DOWN = r'''
f=$1; shift  # sin argumentos: el script tomaría su propia ruta como rol
source <(sed '$d' "$f")
set +e
touch "$_BT_STOP_FLAG"
pkill -f "port-forward pod/${_BT_POD_NAME}"
_BT_POD_CREATED=true
_BT_MAN_MODIFIED=true
_BT_LOCAL_IP=$(curl -4 -s --max-time 5 https://api.ipify.org | tr -d '[:space:]')
_bt_delete_tunnel_pod
_bt_remove_from_man
rm -f "$_BT_STOP_FLAG" "$_BT_PF_CHILD_PIDFILE"
'''


# `trap '' PIPE`: si la app se cierra, escribir a la tubería ya no mata el shell y la limpieza (trap EXIT) sí corre.
TUNNEL_RUNNER = 'trap "" PIPE; f=$1; shift; source "$f" || exit 1; trap _bt_cleanup EXIT; wait'


# Paletas de color: el CSS usa nombres (@bg, @card…), así que cambiar de tema es recargar estas definiciones.
THEMES = {
    "dark": {
    "bg": "#14161c",
    "fg": "#e5e7eb",
    "muted": "#9ca3af",
    "card": "#1c1f27",
    "border": "#2b303b",
    "title": "#f3f4f6",
    "fg2": "#d1d5db",
    "dim": "#6b7280",
    "panel": "#191c23",
    "rail_fg": "#8b93a5",
    "btn_bg": "#232733",
    "btn_border": "#343a48",
    "up_fg": "#86efac",
    "up_border": "#1f6f3c",
    "up_bg": "#16231d",
    "accent_fg": "#c7d2fe",
    "accent_dark": "#4f46e5",
    "busy_bg": "#232845",
    "accent": "#818cf8",
    "sel_bg": "#262b3a",
    "chk_bg": "#2c3350",
    "accent_text": "#a5b4fc",
    "input_bg": "#181b22",
    "row_border": "#262a35",
    "btn_hover": "#2c313f",
    "primary": "#6366f1",
    "stop_border": "#5b2a2f",
    "stop_fg": "#f87171",
    "stop_hover": "#2d1b1f",
    "chip_hover": "#2a2f3d",
    "hover_row": "#20242f",
    "on_primary": "#ffffff",
    "theme_icon": "#fbbf24", "theme_bg": "#3a3217",
    "shadow": "rgba(0,0,0,0.55)",
    },
    "light": {
    "bg": "#cdd1d9",
    "fg": "#1f2937",
    "muted": "#4b5563",
    "card": "#dadde4",
    "border": "#b4bac6",
    "title": "#111827",
    "fg2": "#2d3646",
    "dim": "#5b6472",
    "panel": "#c3c8d2",
    "rail_fg": "#4b5563",
    "btn_bg": "#e0e3e9",
    "btn_border": "#aeb5c2",
    "up_fg": "#14532d",
    "up_border": "#6fbf8a",
    "up_bg": "#cfe8d8",
    "accent_fg": "#3730a3",
    "accent_dark": "#5558e6",
    "busy_bg": "#cdd2f0",
    "accent": "#5558e6",
    "sel_bg": "#cfd4e8",
    "chk_bg": "#cfd4ee",
    "accent_text": "#4338ca",
    "input_bg": "#e6e8ee",
    "row_border": "#c3c8d2",
    "btn_hover": "#d3d7df",
    "primary": "#5b5ee6",
    "stop_border": "#e29a9a",
    "stop_fg": "#b91c1c",
    "stop_hover": "#ecd0d0",
    "chip_hover": "#d6daf0",
    "hover_row": "#d3d7df",
    "on_primary": "#ffffff",
    "theme_icon": "#4338ca", "theme_bg": "#c4caea",
    "shadow": "rgba(0,0,0,0.18)",
    },
}
# Color de acento por servicio (franja izquierda): uno distinto para cada uno, más oscuro en el tema claro.
SERVICE_COLORS = {
    "dark": ["#818cf8", "#2dd4bf", "#fbbf24", "#f472b6", "#38bdf8", "#a3e635", "#fb923c", "#c084fc"],
    "light": ["#4f46e5", "#0d9488", "#d97706", "#db2777", "#0284c7", "#65a30d", "#ea580c", "#9333ea"],
}
# Colores ANSI de los logs: sobre fondo oscuro o claro.
ANSI_PALETTES = {
    "dark": ["#6b7280", "#f87171", "#4ade80", "#facc15", "#60a5fa", "#c084fc", "#22d3ee", "#e5e7eb",
             "#9ca3af", "#fca5a5", "#86efac", "#fde047", "#93c5fd", "#d8b4fe", "#67e8f9", "#ffffff"],
    "light": ["#4b5563", "#b91c1c", "#15803d", "#a16207", "#1d4ed8", "#7e22ce", "#0e7490", "#374151",
              "#6b7280", "#dc2626", "#16a34a", "#ca8a04", "#2563eb", "#9333ea", "#0891b2", "#111827"],
}

CSS_BODY = """
window { background: @bg; color: @fg; }
.svc-picker { font-size: 14px; font-weight: 600; padding: 4px 14px; }
.svc-pick-item { font-size: 14px; padding: 8px 12px; }
.muted { color: @muted; }
.chip { border-radius: 14px; padding: 3px 10px; background: @btn_bg; border: 1px solid @border; }
.chip:hover { border-color: @primary; background: @chip_hover; }
.chip:disabled { opacity: 0.45; }
.section { font-size: 11px; font-weight: 700; color: @muted; letter-spacing: 1px; }
.card { background: @card; border: 1px solid @border; border-radius: 10px; padding: 14px 16px; }
.card-title { font-weight: 700; font-size: 14px; color: @title; }
.status { color: @fg2; }
.col-head { font-size: 11px; font-weight: 700; color: @dim; }
.svc-name { font-weight: 600; }
.rail { background: @panel; border-right: 1px solid @border; }
.rail-btn { min-width: 40px; padding: 7px 0; font-weight: 700; font-size: 11px; color: @rail_fg;
            background-color: @btn_bg; border: 1px solid @btn_border; border-radius: 9px; }
.rail-btn.rail-up { color: @up_fg; border-color: @up_border; background-color: @up_bg; }
.rail-btn.rail-busy { color: @accent_fg; border-color: @accent_dark; background-color: @busy_bg; }
.rail-btn.rail-sel { box-shadow: inset 3px 0 @accent; }
.drawer { background: @panel; border-right: 1px solid @border; box-shadow: 10px 0 28px @shadow; }
.svc-scroll { background: @card; border: 1px solid @border; border-radius: 10px; }
.svc-scroll row:selected { background: @sel_bg; }
button.radio { padding: 3px 12px; border: 1px solid @btn_border; background: @btn_bg; color: @muted; }
button.radio:checked { background: @chk_bg; color: @accent_fg; border-color: @accent_dark; }
button.radio:first-child { border-radius: 7px 0 0 7px; }
button.radio:last-child { border-radius: 0 7px 7px 0; }
.pending { color: @accent_text; font-weight: 600; font-size: 11px; }
searchentry, entry { background: @input_bg; color: @fg; border: 1px solid @btn_border; border-radius: 7px; }
togglebutton:checked { background-color: @chk_bg; color: @accent_fg; border-color: @accent_dark; }
list, row { background: transparent; color: @fg; }
row label { color: @fg; }
row label.col-head { color: @dim; }
row { border-bottom: 1px solid @row_border; padding: 8px 4px; }
row:last-child { border-bottom: none; }
button { background-image: none; background-color: @btn_bg; color: @fg2; border: 1px solid @btn_border;
         border-radius: 7px; padding: 4px 12px; box-shadow: none; text-shadow: none; }
button:hover { background-color: @btn_hover; }
button.btn-primary { background-color: @primary; border-color: @primary; color: @on_primary; font-weight: 600; }
button.btn-primary:hover { background-color: @accent_dark; }
button.btn-stop { color: @stop_fg; border-color: @stop_border; }
button.btn-stop:hover { background-color: @stop_hover; }
button.btn-flat { border-color: transparent; background-color: transparent; color: @accent_text; }
button.btn-flat:hover { background-color: @btn_bg; }
button:disabled { opacity: 0.4; }
textview, textview text { background-color: @input_bg; color: @fg2; }
combobox button, combobox * { color: @fg2; }
.log-side { background: @panel; border-right: 1px solid @border; }
.log-side row { padding: 0; border-bottom: none; }
.log-side row:selected { background: @sel_bg; }
.log-side row:selected label { color: @accent_fg; }
.log-side row:hover { background: @hover_row; }
notebook { background: @card; border: 1px solid @border; border-radius: 10px; }
notebook > header { background: @panel; border-bottom: 1px solid @border; }
notebook > header > tabs > tab { padding: 6px 16px; background: transparent; border: none; color: @muted; }
notebook > header > tabs > tab:checked { color: @accent_text; font-weight: 600; box-shadow: inset 0 -2px @accent; }
notebook > stack, notebook > stack > box { background-color: @card; }
button.theme-btn { color: @theme_icon; border-color: @theme_icon; background-color: @theme_bg; }
popover, popover.background { background-color: @card; color: @fg; border: 1px solid @border; }
popover modelbutton { color: @fg; }
popover modelbutton:hover { background-color: @btn_hover; }
popover separator { background-color: @border; }
scrolledwindow { border: none; }
paned > separator { background-color: @border; min-height: 1px; }
headerbar { background: @panel; color: @fg; border-bottom: 1px solid @border; box-shadow: none; }
headerbar button { color: @fg2; }
"""

SERVICE_COLOR_COUNT = len(SERVICE_COLORS["dark"])


def theme_css(theme):
    """CSS completo del tema: definiciones de color + reglas + una franja de acento por servicio."""
    defs = "".join(f"@define-color {k} {v};\n" for k, v in THEMES[theme].items())
    accents = "".join(
        f".rail-btn.rail-sel.svc-c{i} {{ box-shadow: inset 3px 0 {c}; }}\n"
        f"row.svc-row.svc-c{i} {{ box-shadow: inset 3px 0 {c}; }}\n"
        for i, c in enumerate(SERVICE_COLORS[theme]))
    return (defs + CSS_BODY + accents).encode()


def style(widget, *classes):
    ctx = widget.get_style_context()
    for c in classes:
        ctx.add_class(c)
    return widget


def set_variant(widget, variant):
    ctx = widget.get_style_context()
    for c in ("btn-primary", "btn-stop", "btn-flat"):
        ctx.remove_class(c)
    if variant:
        ctx.add_class(variant)


def card(child, title=None):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    style(box, "card")
    if title:
        box.pack_start(style(Gtk.Label(label=title, xalign=0), "card-title"), False, False, 0)
    box.pack_start(child, True, True, 0)
    return box


def section(text):
    return style(Gtk.Label(label=text.upper(), xalign=0), "section")


class Launcher(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Siesa Dev Launcher")
        self.app = app
        icon = HERE / "icon.png"
        if icon.exists():
            self.set_icon_from_file(str(icon))
        self.tray = None
        self.quitting = False
        self._last_infra = None
        self._hint_shown = False
        self.set_default_size(900, 700)
        self.maximize()
        self.tunnels = []
        self.custom_tunnels = load_tunnels()
        self._probing = False
        self.busy = False

        self.css_prov = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), self.css_prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.theme = SETTINGS.get("theme") if SETTINGS.get("theme") in THEMES else "dark"
        self._load_theme()
        self.svc_state = {}
        self.custom = load_custom()
        self.run_state = {}    # servicio -> (backend_up, frontend_up_o_sin_frontend)
        self.pending = {}      # servicio -> texto de la acción en curso ("Iniciando…")
        self.pending_side = {}  # servicio -> lado afectado por esa acción: both | backend | frontend
        self.chips = {}        # servicio -> (chip backend, chip frontend)
        self.row_ui = {}       # servicio -> {"spin", "lbl", "sub"}
        self.vpn_busy = ""

        # Barra de título: acciones globales a mano, sin ocupar espacio del contenido.
        hb = Gtk.HeaderBar(show_close_button=True, title="Siesa Dev Launcher",
                           subtitle=(f"v{read_version()}" if read_version() else None))
        self.set_titlebar(hb)

        self.connect("key-press-event", self._on_key)

        # Columna izquierda: conexión + servicios (compacta).
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=16)
        left.pack_start(section("VPN"), False, False, 0)
        vpn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        left.pack_start(vpn_box, False, False, 0)
        self.vpn_dot, self.vpn_lbl, self.vpn_btn = self._infra_card(vpn_box, "VPN Siesa", self.on_vpn)

        tun_head = Gtk.Box(spacing=8, margin_top=10)
        tun_head.pack_start(section("Túneles"), True, True, 0)
        add_tun = style(Gtk.Button(label="+ Añadir"), "btn-flat")
        add_tun.set_tooltip_text("Añadir otro túnel (otro script como business-dev.sh)")
        add_tun.connect("clicked", lambda _b: self.add_tunnel())
        tun_head.pack_end(add_tun, False, False, 0)
        left.pack_start(tun_head, False, False, 0)
        self.tun_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)  # una tarjeta por túnel
        left.pack_start(self.tun_box, False, False, 0)

        head = Gtk.Box(spacing=8, margin_top=10)
        left.pack_start(head, False, False, 0)
        head.pack_start(section("Servicios"), True, True, 0)
        add = style(Gtk.Button(label="+ Añadir"), "btn-flat")
        add.set_tooltip_text("Añadir un servicio personalizado")
        add.connect("clicked", lambda _b: self.add_custom())
        head.pack_end(add, False, False, 0)

        bulk = Gtk.Box()
        self.bulk_btn = Gtk.Button()
        face = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        face.pack_start(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        face.pack_start(Gtk.Label(label="Acciones de todos los servicios"), False, False, 0)
        self.bulk_btn.add(face)
        self.bulk_btn.set_tooltip_text("Iniciar, reiniciar o detener todos los servicios")
        self.bulk_btn.connect("clicked", lambda b: self._bulk_popover(b))
        bulk.pack_start(self.bulk_btn, True, True, 0)

        # La lista de servicios es lo único que se desplaza: cabecera y botones globales quedan fijos.
        self.svc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        svc_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                        vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                        overlay_scrolling=False, min_content_height=120)
        svc_scroll.add(self.svc_box)
        # El marco de Servicios incluye su botón de acciones masivas: así no parece que afecte a VPN y túneles.
        frame = style(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), "svc-scroll")
        frame.pack_start(svc_scroll, True, True, 0)
        frame.pack_start(Gtk.Separator(), False, False, 0)
        bulk.set_margin_start(8)
        bulk.set_margin_end(8)
        bulk.set_margin_top(8)
        bulk.set_margin_bottom(8)
        frame.pack_start(bulk, False, False, 0)
        left.pack_start(frame, True, True, 0)
        left.set_size_request(540, -1)

        # Columna derecha: actividad a todo lo alto.
        nb = Gtk.Notebook(show_border=False)
        self._src_lock = threading.Lock()
        self._src_started = set()
        self.log_selected = None  # archivo del servicio elegido (<servicio>-backend|frontend)
        self.log_service = None
        self.log_side = "backend"
        self.log_dots = {}
        self.log_ports = {}
        self._auto_select = False
        self.src_views = {}
        self.src_views["vpn"] = self._textview()
        nb.append_page(self._log_panel(self.src_views["vpn"]), Gtk.Label(label=self.SOURCE_LABELS["vpn"]))
        logbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        lhead = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=10)
        self.log_title = Gtk.Label()  # sin mostrar: el selector de servicio ocupa su lugar
        lhead.pack_start(style(Gtk.Label(label="Servicio:"), "muted"), False, False, 0)
        # Botón con popover (no un combo): el desplegable de un combo se alinea con el elemento activo y,
        # arriba en la ventana, se corta y obliga a hacer scroll.
        self.svc_menu = style(Gtk.MenuButton(), "svc-picker")
        face = Gtk.Box(spacing=8)
        self.svc_menu_lbl = Gtk.Label(label="Elige un servicio", xalign=0)
        face.pack_start(self.svc_menu_lbl, True, True, 0)
        face.pack_end(Gtk.Image.new_from_icon_name("pan-down-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        self.svc_menu.add(face)
        self.svc_menu.set_size_request(260, 36)
        self.svc_menu.set_tooltip_text("Servicio cuyo log se muestra")
        self.svc_pop = Gtk.Popover()
        self.svc_menu.set_popover(self.svc_pop)
        self.svc_pop.connect("show", self._fill_service_picker)
        lhead.pack_start(self.svc_menu, False, False, 0)
        self.side_b = Gtk.RadioButton()
        self.side_f = Gtk.RadioButton.new_from_widget(self.side_b)
        for rb, side, icon, text in ((self.side_b, "backend", "network-server-symbolic", "Backend"),
                                     (self.side_f, "frontend", "video-display-symbolic", "Frontend")):
            face = Gtk.Box(spacing=6)
            face.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON), False, False, 0)
            face.pack_start(Gtk.Label(label=text), False, False, 0)
            rb.add(face)
            rb.set_mode(False)
            rb.set_size_request(-1, 36)  # misma altura que el selector
            rb.connect("toggled", lambda w, sd=side: self._on_side(w, sd))
            lhead.pack_start(rb, False, False, 0)
        logbox.pack_start(lhead, False, False, 0)
        self.log_view = self.src_views["service"] = self._textview()
        logbox.pack_start(self._log_panel(self.log_view), True, True, 0)
        nb.append_page(logbox, Gtk.Label(label=self.SOURCE_LABELS["service"]))
        self.src_views["actions"] = self._textview()
        nb.append_page(self._log_panel(self.src_views["actions"]), Gtk.Label(label=self.SOURCE_LABELS["actions"]))
        self.log_pages = ["vpn", "service", "actions"]  # fuente de cada tab, en orden (los túneles se insertan tras VPN)
        nb.connect("switch-page", lambda _nb, _page, _n: GLib.idle_add(lambda: self.refresh_log() and False))
        self.notebook = nb
        right = Gtk.Box(margin=16, margin_left=0)
        right.pack_start(nb, True, True, 0)

        # Riel siempre visible (estado de un vistazo) + desplegable que se abre encima de los logs.
        self.rail_btns = {}
        rail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=8)
        rail_wrap = Gtk.Box()
        rail_wrap.pack_start(rail, True, True, 0)
        rail_wrap.set_size_request(64, -1)
        style(rail_wrap, "rail")

        self.panel_btn = Gtk.ToggleButton(active=False)
        self.panel_btn.add(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        self.panel_btn.set_tooltip_text("Conexión y servicios (F9)")
        self.panel_btn.connect("toggled", self._on_panel_toggled)
        rail.pack_start(self.panel_btn, False, False, 0)

        self.rail_vpn = Gtk.Label(use_markup=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.pack_start(self.rail_vpn, False, False, 0)
        box.pack_start(style(Gtk.Label(label="VPN"), "col-head"), False, False, 0)
        rail.pack_start(box, False, False, 2)
        self.rail_tun_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)  # un punto por túnel
        rail.pack_start(self.rail_tun_box, False, False, 2)
        rail.pack_start(Gtk.Separator(), False, False, 2)

        self.rail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        rail_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, overlay_scrolling=True)
        rail_scroll.add(self.rail_box)
        rail.pack_start(rail_scroll, True, True, 0)
        gear = Gtk.Button.new_from_icon_name("preferences-system-symbolic", Gtk.IconSize.BUTTON)
        gear.set_tooltip_text("Ajustes")
        gear.connect("clicked", lambda _b: self.settings_dialog())
        rail.pack_end(gear, False, False, 0)
        self.theme_btn = style(Gtk.Button(), "theme-btn")
        self.theme_btn.add(Gtk.Image())
        self.theme_btn.connect("clicked", lambda _b: self.toggle_theme())
        rail.pack_end(self.theme_btn, False, False, 0)
        self._load_theme()  # ahora que existe el botón, fija su icono

        main = Gtk.EventBox()  # un clic fuera del desplegable lo cierra
        main.add(right)
        main.connect("button-press-event", self._on_main_click)
        # El log (TextView) consume el clic antes de que llegue al contenedor: se engancha también ahí.
        self._hook_click(right)

        drawer = Gtk.Box()
        drawer.pack_start(left, True, True, 0)
        style(drawer, "drawer")
        self.drawer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_RIGHT,
                                   transition_duration=140, halign=Gtk.Align.START, valign=Gtk.Align.FILL)
        self.drawer.add(drawer)

        overlay = Gtk.Overlay()
        overlay.add(main)
        overlay.add_overlay(self.drawer)

        shell = Gtk.Box()
        shell.pack_start(rail_wrap, False, False, 0)
        shell.pack_start(overlay, True, True, 0)
        self.add(shell)

        self.build_tunnels()
        nb.show_all()
        nb.set_current_page(self.log_pages.index("service"))
        self.build_services()
        self.refresh_status()
        GLib.timeout_add_seconds(3, self.refresh_status)
        GLib.timeout_add_seconds(1, self.refresh_log)
        self.connect("delete-event", self.on_close)
        self.setup_tray()

    # ---------- Segundo plano: bandeja, notificaciones y acciones rápidas ----------
    def setup_tray(self):
        if AppIndicator is None:
            self.append_out("[bandeja] no disponible. Para correr en segundo plano instala:\n"
                            "  sudo apt install gir1.2-ayatanaappindicator3-0.1\n")
            return
        ind = AppIndicator.Indicator.new_with_path(APP_ID, APP_ID, AppIndicator.IndicatorCategory.APPLICATION_STATUS,
                                                   str(HERE / "tray"))
        ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        ind.set_title("Siesa Dev Launcher")
        menu = Gtk.Menu()
        self.tray_items = {}

        def item(key, label, cb=None, sensitive=True, parent=None):
            mi = Gtk.MenuItem(label=label)
            mi.set_sensitive(sensitive)
            if cb:
                mi.connect("activate", lambda _m: cb())
            (parent or menu).append(mi)
            self.tray_items[key] = mi
            return mi

        def group(label, key):
            """Grupo colapsado: una sola fila que se expande al pulsarla."""
            head = Gtk.MenuItem(label=label)
            sub = Gtk.Menu()
            head.set_submenu(sub)
            menu.append(head)
            self.tray_items[key] = head
            return sub

        # Cada fila es estado + acción: un clic conecta si está apagado y desconecta si está encendido.
        item("vpn_row", "VPN", lambda: self.on_vpn(None))
        menu.append(Gtk.SeparatorMenuItem())
        item("tun_header", "Túneles", sensitive=False)       # las filas de cada túnel se insertan debajo
        menu.append(Gtk.SeparatorMenuItem())
        self.tray_svc_menu = group("Servicios", "svc_header")
        menu.append(Gtk.SeparatorMenuItem())
        item("show", "Mostrar ventana", self.present)
        item("down", "Desconectar todo", self.disconnect_all)
        item("quit", "Salir", self.request_quit)
        menu.show_all()
        ind.set_menu(menu)
        self.tray = ind
        self.tray_menu = menu
        self.tray_tun_rows = {}
        self.rebuild_tray_services()
        self.rebuild_tray_tunnels()

    @staticmethod
    def _tray_label(text):
        # El menú de la bandeja trata "_" como atajo de teclado y lo borra ("Accounting_2" -> "Accounting2").
        return text.replace("_", "__")

    def rebuild_tray_tunnels(self):
        """Un solo túnel: su fila directa bajo el título «Túneles». Con varios: grupo desplegable, como los servicios."""
        if self.tray is None:
            return
        for mi in self.tray_tun_rows.values():
            mi.get_parent().remove(mi)
        self.tray_tun_rows = {}
        head = self.tray_items["tun_header"]
        self.tray_tun_many = len(self.tunnels) > 1
        if self.tray_tun_many:
            container = Gtk.Menu()
            head.set_submenu(container)
            head.set_sensitive(True)
            pos = None
        else:
            head.set_submenu(None)
            head.set_sensitive(False)
            head.set_label("Túneles")
            container = self.tray_menu
            pos = self.tray_menu.get_children().index(head) + 1
        for t in self.tunnels:
            mi = Gtk.MenuItem(label="…")
            mi.connect("activate", lambda _m, t=t: self.on_tunnel(t))
            if pos is None:
                container.append(mi)
            else:
                container.insert(mi, pos)
                pos += 1
            self.tray_tun_rows[t.key] = mi
        container.show_all()
        self.tray_menu.show_all()
        if getattr(self, "_infra", None):
            self.update_tray(*self._infra)

    def rebuild_tray_services(self):
        """Varios servicios: grupo «Servicios» con un submenú por servicio. Uno solo: sus acciones directas en el menú."""
        if self.tray is None:
            return
        names = [n for n, _ in SERVICES] + [c["name"] for c in self.custom]
        flat = len(names) == 1
        for w in self.tray_svc_menu.get_children():
            self.tray_svc_menu.remove(w)
        for mi in getattr(self, "tray_svc_flat", []):
            if mi.get_parent() is not None:
                mi.get_parent().remove(mi)
        self.tray_svc_flat = []
        self.tray_svc = {}
        self.tray_svc_is_flat = flat
        svc_head = self.tray_items["svc_header"]
        pos = None
        if flat:
            svc_head.set_submenu(None)
            svc_head.set_sensitive(False)  # pasa a ser el título del servicio
            pos = self.tray_menu.get_children().index(svc_head) + 1
        else:
            svc_head.set_submenu(self.tray_svc_menu)
            svc_head.set_sensitive(True)

        def put(container, mi):
            """Añade al submenú, o en el menú principal (tras el título) si hay un solo servicio."""
            nonlocal pos
            if flat:
                self.tray_menu.insert(mi, pos)
                self.tray_svc_flat.append(mi)
                pos += 1
            else:
                container.append(mi)

        # Acciones sobre todos los servicios: con uno solo serían redundantes, así que no se muestran.
        sub_all = Gtk.Menu()
        for key, label, args in (("start", "Iniciar todos", []), ("reload", "Reiniciar todos", ["--reload"]),
                                 ("stop", "Detener todos", ["--stop"])):
            mi = Gtk.MenuItem(label=label)
            mi.set_no_show_all(True)  # su visibilidad la decide update_tray según el estado
            mi.connect("activate", lambda _m, a=args: self.run_services(a))
            sub_all.append(mi)
            self.tray_items[key] = mi
        if not flat:
            head_all = Gtk.MenuItem(label="Acciones de todos")
            head_all.set_submenu(sub_all)
            self.tray_svc_menu.append(head_all)
            self.tray_svc_menu.append(Gtk.SeparatorMenuItem())

        for name in names:
            if flat:
                head, sub = svc_head, None
            else:
                head = Gtk.MenuItem(label=self._tray_label(name))
                sub = Gtk.Menu()
                head.set_submenu(sub)

            def mk(act=None, side="both", toggle=False, n=name):
                mi = Gtk.MenuItem(label="…")
                mi.set_no_show_all(True)  # su visibilidad la decide update_tray según el estado
                if toggle:
                    mi.connect("activate", lambda _m: self._tray_side_toggle(n, side))
                else:
                    mi.connect("activate", lambda _m: self.service_action(n, act, side))
                put(sub, mi)
                return mi

            tb = mk(toggle=True, side="backend")
            tf = mk(toggle=True, side="frontend")
            sep_r = Gtk.SeparatorMenuItem()
            sep_r.set_no_show_all(True)
            put(sub, sep_r)
            rb = mk("reload", "backend")
            rf = mk("reload", "frontend")
            rboth = mk("reload", "both")
            put(sub, Gtk.SeparatorMenuItem())
            vs = Gtk.MenuItem(label="Abrir en VS Code")
            vs.connect("activate", lambda _m, n=name: self.open_in_vscode(n))
            put(sub, vs)
            self.tray_svc[name] = (head, tb, tf, rb, rf, rboth, sep_r)
            if not flat:
                self.tray_svc_menu.append(head)
        self.tray_svc_menu.show_all()
        self.tray_menu.show_all()
        if getattr(self, "_infra", None):
            self.update_tray(*self._infra)

    def _tray_side_toggle(self, name, side):
        bu, fu = self.run_state.get(name, (False, False))
        up = bu if side == "backend" else fu
        self.service_action(name, "stop" if up else "start", side)

    def update_tray(self, vpn, tun, mine):
        if self.tray is None:
            return
        it = self.tray_items
        if self.vpn_busy:
            it["vpn_row"].set_label(f"◌  VPN · {self.vpn_busy}")
        else:
            it["vpn_row"].set_label(f"{'●' if vpn else '○'}  VPN · {'Desconectar' if vpn else 'Conectar'}")
        it["vpn_row"].set_sensitive(not self.vpn_busy)
        for t in self.tunnels:
            row = self.tray_tun_rows.get(t.key)
            if row is None:
                continue
            name = self._tray_label(t.name)
            if t.busy:
                row.set_label(f"◌  {name} · Trabajando…")
            elif t.foreign:
                row.set_label(f"◌  {name} · Puerto ocupado")
            elif t.up or t.alive:
                row.set_label(f"●  {name} · Desconectar")
            else:
                row.set_label(f"○  {name} · Conectar")
            row.set_sensitive(not t.busy and not t.foreign)
        if getattr(self, "tray_tun_many", False):
            n = sum(1 for t in self.tunnels if (t.up and not t.foreign) or t.alive)
            it["tun_header"].set_label(f"Túneles · {n} activo{'' if n == 1 else 's'}")
        self.tray.set_icon_full(APP_ID, "VPN conectada" if vpn else "VPN desconectada")
        states = [self.run_state.get(n, (False, False)) for n, _ in SERVICES]
        all_up = bool(states) and all(b and f for b, f in states)
        any_up = any(b or f for b, f in states)
        # Solo lo que aplica: iniciar si falta alguno arriba; reiniciar y detener si hay alguno arriba.
        it["start"].set_label("Iniciar los que faltan" if any_up else "Iniciar todos")
        it["start"].set_visible(not all_up)
        it["reload"].set_visible(any_up)
        it["stop"].set_visible(any_up)
        for key in ("start", "reload", "stop"):
            it[key].set_sensitive(not self.busy)
        running = sum(1 for n in getattr(self, "tray_svc", {}) if any(self._real_state(n)))
        if not getattr(self, "tray_svc_is_flat", False):  # con un solo servicio, ese título lo pone el propio servicio
            it["svc_header"].set_label(f"Servicios · {running} activo{'' if running == 1 else 's'}")
        for name, (head, tb, tf, rb, rf, rboth, sep_r) in getattr(self, "tray_svc", {}).items():
            bu, fu = self.run_state.get(name, (False, False))
            has_front = bool(self.svc_state.get(name, (None, None, None, 0))[3])
            fu = fu and has_front
            text = self.pending.get(name)
            overall = "●" if bu and (fu or not has_front) else "◐" if bu or fu else "○"
            head.set_label(self._tray_label(f"{overall}  {name}" + (f" · {text}" if text else "")))
            tb.set_label(f"{'●' if bu else '○'}  Backend · {'Detener' if bu else 'Iniciar'}")
            tf.set_label(f"{'●' if fu else '○'}  Frontend · {'Detener' if fu else 'Iniciar'}")
            rb.set_label("↻ Reiniciar backend")
            rf.set_label("↻ Reiniciar frontend")
            rboth.set_label("↻ Reiniciar ambos")
            # Se muestra solo lo que aplica al estado real: reiniciar un lado únicamente si ese lado está arriba.
            tb.set_visible(True)
            tf.set_visible(has_front)
            rb.set_visible(bu)
            rf.set_visible(fu)
            rboth.set_visible(bu and fu)
            sep_r.set_visible(bu or fu)
            for w in (tb, tf, rb, rf, rboth):
                w.set_sensitive(not text)

    def _load_theme(self):
        """Aplica la paleta del tema actual: CSS, tema GTK (menús y popovers) y colores ANSI de los logs."""
        ANSI_COLORS[:] = ANSI_PALETTES[self.theme]
        self.css_prov.load_from_data(theme_css(self.theme))
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", self.theme == "dark")
        btn = getattr(self, "theme_btn", None)
        if btn is not None:
            dark = self.theme == "dark"
            btn.get_child().set_from_icon_name("weather-clear-symbolic" if dark else "weather-clear-night-symbolic",
                                               Gtk.IconSize.BUTTON)
            btn.set_tooltip_text("Cambiar a tema claro" if dark else "Cambiar a tema oscuro")

    def toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        self._load_theme()
        SETTINGS["theme"] = self.theme
        save_settings()
        for view in getattr(self, "src_views", {}).values():  # los logs se dibujan de nuevo con la paleta nueva
            view.log_name = None
        self.refresh_log()

    def notify(self, title, body=""):
        """Notificación del escritorio, solo si la ventana no está al frente."""
        if self.is_active():
            return
        n = Gio.Notification.new(title)
        if body:
            n.set_body(body)
        self.app.send_notification(None, n)

    def request_quit(self):
        self.present()
        live = [t for t in self.tunnels if t.alive]
        if live:
            names = ", ".join(t.name for t in live)
            d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                  buttons=Gtk.ButtonsType.YES_NO,
                                  text=f"Sigue activo: {names}. ¿Desconectar antes de salir?")
            resp = d.run()
            d.destroy()
            if resp == Gtk.ResponseType.YES:
                for t in live:
                    t.proc.send_signal(signal.SIGTERM)
                for t in live:
                    try:
                        t.proc.wait(timeout=90)
                    except subprocess.TimeoutExpired:
                        pass
        self.quitting = True
        self.app.quit()

    def do_action(self, name):
        """Acciones rápidas (bandeja, menú del ícono y `launcher.py --action NAME`)."""
        actions = {
            "start-all": lambda: self.run_services([]),
            "reload-all": lambda: self.run_services(["--reload"]),
            "stop-all": lambda: self.run_services(["--stop"]),
            "vpn-toggle": lambda: self.on_vpn(None),
            "tunnel-toggle": lambda: self.on_tunnel(self.tunnels[0]),
            "disconnect-all": self.disconnect_all,
            "quit": self.request_quit,
        }
        fn = actions.get(name)
        if fn:
            fn()

    # ---------- Tabla de servicios ----------
    def build_services(self):
        # La lista vieja se destruye: hay que desconectar su señal antes, o emite "row-selected" sobre filas muertas.
        old = getattr(self, "svc_list", None)
        if old is not None:
            old.disconnect(self._svc_sel_id)
        for c in self.svc_box.get_children():
            self.svc_box.remove(c)
        sga = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)  # columna de acciones: mismo ancho en todas las filas
        lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.svc_box.pack_start(lst, True, True, 0)
        self.svc_state = {}
        self.chips = {}
        self.row_ui = {}
        svc_rows = {}

        def add_row(cells, actions, svc=None):
            row = Gtk.ListBoxRow(activatable=svc is not None, selectable=svc is not None)
            row.svc = svc
            if svc is not None:
                style(row, "svc-row", svc_class[svc])
                svc_rows[svc] = row
                row.connect("button-press-event", self._on_right_click, svc)
            box = Gtk.Box(spacing=10, margin_start=4, margin_end=4)
            row.add(box)
            box.pack_start(cells[0], True, True, 0)
            for w in cells[1:]:
                box.pack_start(w, False, False, 0)
            box.pack_end(actions, False, False, 0)
            sga.add_widget(actions)
            lst.add(row)

        spacer = Gtk.Label(label="")
        colb = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)  # chips Backend/Frontend: mismo ancho en todas las filas
        colf = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        add_row([style(Gtk.Label(label="SERVICIO", xalign=0), "col-head")], spacer)

        rows = [(n, p, None) for n, p in SERVICES] + [(c["name"], c["backend_port"], c) for c in self.custom]
        # Un color de acento por servicio (franja izquierda de su fila y de su botón del riel).
        svc_class = {n: f"svc-c{i % SERVICE_COLOR_COUNT}" for i, (n, _p, _c) in enumerate(rows)}
        for name, port, cfg in rows:
            title = name if cfg is None else f"{name} ★"
            namebox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
            namebox.pack_start(style(Gtk.Label(label=title, xalign=0), "svc-name"), False, False, 0)
            sub = Gtk.Box(spacing=6, no_show_all=True)
            spin = Gtk.Spinner()
            plbl = style(Gtk.Label(xalign=0), "pending")
            sub.pack_start(spin, False, False, 0)
            sub.pack_start(plbl, False, False, 0)
            spin.show()
            plbl.show()
            namebox.pack_start(sub, False, False, 0)
            fport = cfg.get("frontend_port", port + 1000) if cfg else port + 1000
            (chip_b, b), (chip_f, f) = (self._side_chip(name, "backend", f"Backend :{port}"),
                                        self._side_chip(name, "frontend", f"Frontend :{fport}" if fport else "Frontend"))
            colb.add_widget(chip_b)
            colf.add_widget(chip_f)
            self.chips[name] = (chip_b, chip_f)
            self.svc_state[name] = (b, f, port, fport)
            acts = Gtk.Box(spacing=4)
            menu_btn = Gtk.Button.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON)
            menu_btn.set_tooltip_text("Acciones")
            menu_btn.connect("clicked", lambda b, n=name: self._row_popover(n, b))
            acts.pack_start(menu_btn, False, False, 0)
            if cfg is not None:
                rm = style(Gtk.Button.new_from_icon_name("edit-delete-symbolic", Gtk.IconSize.BUTTON), "btn-flat")
                rm.set_tooltip_text("Quitar de la lista")
                rm.connect("clicked", lambda _b, c=cfg: self.remove_custom(c))
                acts.pack_start(rm, False, False, 0)
            add_row([namebox, chip_b, chip_f], acts, svc=name)
            self.row_ui[name] = {"spin": spin, "lbl": plbl, "sub": sub}
        for c in self.rail_box.get_children():
            self.rail_box.remove(c)
        self.rail_btns = {}
        for name, port, cfg in rows:
            words = [w for w in re.split(r"[-_ ]+", name) if w]
            abbr = (words[0][0] + words[1][0] if len(words) > 1 else name[:2]).upper()
            rb = style(Gtk.Button(label=abbr), "rail-btn", svc_class[name])
            rb.set_tooltip_text(name)
            rb.connect("clicked", lambda _b, n=name: self.svc_list.select_row(svc_rows[n]))
            rb.connect("button-press-event", self._on_right_click, name)
            rb.set_tooltip_text(f"{name}  ·  clic derecho: acciones")
            self.rail_box.pack_start(rb, False, False, 0)
            self.rail_btns[name] = rb
        self.rail_box.show_all()
        self.svc_list = lst
        self._svc_sel_id = lst.connect("row-selected", self._on_service_selected)
        self.svc_box.show_all()
        self._svc_names = list(svc_rows)
        self._fill_service_picker()
        self._select_default_service(svc_rows)
        self.apply_pending()
        self.rebuild_tray_services()

    def _fill_service_picker(self, *_):
        """Lista del selector de logs: solo los servicios en ejecución (más el que se está viendo);
        si no hay ninguno corriendo, todos."""
        names = getattr(self, "_svc_names", [])
        running = [n for n in names if any(self._real_state(n))]
        shown = [n for n in names if n in running or n == self.log_service] if running else names
        old = self.svc_pop.get_child()
        if old is not None:
            self.svc_pop.remove(old)
        pick = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=6, spacing=2)
        for n in shown:
            item = style(Gtk.ModelButton(label=n, halign=Gtk.Align.FILL), "svc-pick-item")
            item.set_size_request(240, -1)
            item.connect("clicked", lambda _b, n=n: self._pick_service(n))
            pick.pack_start(item, False, False, 0)
        pick.show_all()
        self.svc_pop.add(pick)

    def _select_default_service(self, svc_rows):
        """Conserva la selección al reconstruir; si no hay, el primer servicio en ejecución (o el primero)."""
        name = self.log_service if self.log_service in svc_rows else None
        if name is None:
            running = [n for n, (b, f, port, fport) in self.svc_state.items()
                       if port_open(port) or (fport and port_open(fport))]
            name = running[0] if running else next(iter(svc_rows), None)
        if name is not None:
            self._auto_select = True
            self.svc_list.select_row(svc_rows[name])
            self._auto_select = False

    def _on_service_selected(self, _lst, row):
        if row is None or getattr(row, "svc", None) is None:
            return
        self.log_service = row.svc
        self.svc_menu_lbl.set_text(row.svc)
        has_front = bool(self.svc_state[row.svc][3])
        self.side_f.set_sensitive(has_front)
        if not has_front:
            self.log_side = "backend"
            self.side_b.set_active(True)
        self._update_log_source()
        if not self._auto_select:
            self._show_service_tab()
            self.panel_btn.set_active(False)
        GLib.idle_add(lambda: self.refresh_status() and False)

    def _pick_service(self, name):
        self.svc_pop.popdown()
        if name != self.log_service:
            self.svc_list_select(name)

    def _on_side(self, widget, side):
        if widget.get_active() and side != self.log_side:
            self.log_side = side
            self._update_log_source()

    def _update_log_source(self):
        if self.log_service is None:
            return
        self.log_title.set_text(self.log_service)
        (self.side_b if self.log_side == "backend" else self.side_f).set_active(True)
        self.log_selected = f"{self.log_service}-{self.log_side}"
        self.refresh_log()

    def _show_service_tab(self):
        self.notebook.set_current_page(self.log_pages.index("service"))

    # ---------- Menú contextual (clic derecho) por servicio ----------
    def service_dir(self, name):
        cfg = next((c for c in self.custom if c["name"] == name), None)
        if cfg is not None:
            return Path(cfg["dir"]).expanduser()
        return Path(SETTINGS["services_root"]).expanduser() / REPO_DIRS[name]

    def open_in_vscode(self, name):
        folder = self.service_dir(name)
        if not folder.is_dir():
            self.append_out(f"[vscode] no existe la carpeta de {name}: {folder}\n")
            self.notify("No se pudo abrir VS Code", f"No existe {folder}")
            return
        try:
            subprocess.Popen(["code", str(folder)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            self.append_out(f"[vscode] abriendo {folder}\n")
        except FileNotFoundError:
            self.append_out("[vscode] no encuentro el comando `code`. En VS Code: "
                            "Ctrl+Shift+P → «Shell Command: Install 'code' command in PATH».\n")
            self.notify("VS Code no encontrado", "Instala el comando `code` en el PATH.")

    def _real_state(self, name):
        """(backend arriba, frontend arriba): un servicio sin frontend nunca cuenta el frontend como arriba."""
        bu, fu = self.run_state.get(name, (False, False))
        return bu, fu and bool(self.svc_state.get(name, (None, None, None, 0))[3])

    def _state(self, name):
        return (*self._real_state(name), name in self.pending)

    def _add_actions(self, menu, name, side, bu, fu, busy):
        up = bu and fu if side == "both" else (bu if side == "backend" else fu)
        any_up = bu or fu if side == "both" else up
        for txt, act, enabled in (("Iniciar", "start", not busy and not up),
                                  ("Reiniciar", "reload", not busy and any_up),
                                  ("Detener", "stop", not busy and any_up)):
            mi = Gtk.MenuItem(label=txt)
            mi.set_sensitive(enabled)
            mi.connect("activate", lambda _m, a=act: self.service_action(name, a, side))
            menu.append(mi)

    def service_menu(self, name, event):
        """Menú flotante junto al cursor (clic derecho): nunca se corta como los submenús de la bandeja."""
        bu, fu, busy = self._state(name)
        has_front = bool(self.svc_state[name][3])
        menu = Gtk.Menu()
        hdr = Gtk.MenuItem(label=name.replace("_", "__"))
        hdr.set_sensitive(False)
        menu.append(hdr)
        menu.append(Gtk.SeparatorMenuItem())
        self._add_actions(menu, name, "both", bu, fu, busy)
        menu.append(Gtk.SeparatorMenuItem())
        for label, side, avail in (("Solo backend", "backend", True), ("Solo frontend", "frontend", has_front)):
            head = Gtk.MenuItem(label=label)
            head.set_sensitive(avail)
            sub = Gtk.Menu()
            self._add_actions(sub, name, side, bu, fu, busy)
            sub.append(Gtk.SeparatorMenuItem())
            logs = Gtk.MenuItem(label="Ver log")
            logs.connect("activate", lambda _m, n=name, sd=side: self._show_logs(n, sd))
            sub.append(logs)
            head.set_submenu(sub)
            menu.append(head)
        menu.append(Gtk.SeparatorMenuItem())
        code = Gtk.MenuItem(label="Abrir en VS Code")
        code.connect("activate", lambda _m: self.open_in_vscode(name))
        menu.append(code)
        menu.show_all()
        menu.popup_at_pointer(event)

    def _side_chip(self, name, side, text):
        """Botón del lado (Backend/Frontend): círculo de estado + nombre + flecha; abre sus acciones."""
        dot = Gtk.Label(use_markup=True)
        face = Gtk.Box(spacing=6)
        face.pack_start(dot, False, False, 0)
        face.pack_start(Gtk.Label(label=text), True, True, 0)
        face.pack_end(Gtk.Image.new_from_icon_name("pan-down-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        chip = style(Gtk.Button(), "chip")
        chip.add(face)
        chip.connect("clicked", lambda b, n=name, sd=side: self._chip_popover(n, sd, b))
        return chip, dot

    def _chip_popover(self, name, side, chip):
        """Acciones del lado según su estado: activo → Reiniciar/Detener; detenido → Iniciar; en curso → solo informa."""
        bu, fu, busy = self._state(name)
        up = bu if side == "backend" else fu
        pop = Gtk.Popover(relative_to=chip)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=6, spacing=2)

        def item(label, fn=None):
            mb = style(Gtk.ModelButton(label=label, halign=Gtk.Align.FILL), "svc-pick-item")
            mb.set_sensitive(fn is not None)
            if fn is not None:
                mb.connect("clicked", lambda _b: (pop.popdown(), fn()))
            box.pack_start(mb, False, False, 0)

        if busy:
            item(self.pending.get(name, "…"))
        elif up:
            item("↻  Reiniciar", lambda: self.service_action(name, "reload", side))
            item("■  Detener", lambda: self.service_action(name, "stop", side))
        else:
            item("▶  Iniciar", lambda: self.service_action(name, "start", side))
        box.pack_start(Gtk.Separator(), False, False, 2)
        item("Ver log", lambda: self._show_logs(name, side))
        pop.add(box)
        box.show_all()
        pop.connect("closed", lambda p: GLib.idle_add(p.destroy))
        pop.popup()

    def _actions_popover(self, anchor, busy_text, entries):
        """Popover de Acciones: `entries` = [(texto, callback)]; None es un separador."""
        pop = Gtk.Popover(relative_to=anchor)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=6, spacing=2)

        def item(label, fn=None):
            mb = style(Gtk.ModelButton(label=label, halign=Gtk.Align.FILL), "svc-pick-item")
            mb.set_sensitive(fn is not None)
            if fn is not None:
                mb.connect("clicked", lambda _b: (pop.popdown(), fn()))
            box.pack_start(mb, False, False, 0)

        if busy_text:
            item(busy_text)
        else:
            for e in entries:
                box.pack_start(Gtk.Separator(), False, False, 2) if e is None else item(*e)
        pop.add(box)
        box.show_all()
        pop.connect("closed", lambda p: GLib.idle_add(p.destroy))
        pop.popup()

    def _row_popover(self, name, btn):
        """Acciones sobre el servicio completo según lo que ya está arriba: no ofrece iniciar todo si un lado ya corre."""
        bu, fu, busy = self._state(name)
        has_front = bool(self.svc_state[name][3])
        fu = fu and has_front
        act = lambda a, side="both": (lambda: self.service_action(name, a, side))
        if bu and (fu or not has_front):
            entries = [("↻  Reiniciar todo", act("reload")), ("■  Detener todo", act("stop"))]
        elif bu or fu:
            missing, label = ("frontend", "Frontend") if bu else ("backend", "Backend")
            entries = [(f"▶  Iniciar {label}", act("start", missing)),
                       ("↻  Reiniciar ambos", act("reload")), ("■  Detener todo", act("stop"))]
        else:
            entries = [("▶  Iniciar todo", act("start"))]
        self._actions_popover(btn, self.pending.get(name) if busy else "", entries)

    def _bulk_popover(self, btn):
        """Acciones sobre todos los servicios según cuántos estén arriba."""
        names = [n for n, _ in SERVICES]
        states = [self.run_state.get(n, (False, False)) for n in names]
        all_up = bool(states) and all(b and f for b, f in states)
        any_up = any(b or f for b, f in states)
        run = lambda args: (lambda: self.run_services(args))
        if all_up:
            entries = [("↻  Reiniciar todos", run(["--reload"])), ("■  Detener todos", run(["--stop"]))]
        elif any_up:
            entries = [("▶  Iniciar los que faltan", run([])),
                       ("↻  Reiniciar todos", run(["--reload"])), ("■  Detener todos", run(["--stop"]))]
        else:
            entries = [("▶  Iniciar todos", run([]))]
        self._actions_popover(btn, "Hay una acción en curso…" if self.busy else "", entries)

    def _paint_chips(self, name):
        """Círculo de cada lado: verde activo, amarillo con una acción en curso, gris detenido."""
        b, f, port, fport = self.svc_state[name]
        bu, fu = self.run_state.get(name, (False, False))
        pend, pside = self.pending.get(name), self.pending_side.get(name, "both")
        chip_b, chip_f = self.chips[name]
        for side, dot, chip, up, tcp, label in (("backend", b, chip_b, bu, port, "Backend"),
                                                ("frontend", f, chip_f, fu, fport, "Frontend")):
            if side == "frontend" and not fport:
                dot.set_markup(self._dot(False))
                chip.set_sensitive(False)
                chip.set_tooltip_text("Sin frontend")
                continue
            busy = bool(pend) and pside in ("both", side)
            dot.set_markup(self._dot(up, busy))
            chip.set_sensitive(True)
            chip.set_tooltip_text(f"{label} · :{tcp} · {pend if busy else 'Activo' if up else 'Detenido'}")

    def _show_logs(self, name, side):
        self.svc_list_select(name)
        (self.side_b if side == "backend" else self.side_f).set_active(True)
        self._show_service_tab()

    def svc_list_select(self, name):
        for row in self.svc_list.get_children():
            if getattr(row, "svc", None) == name:
                self.svc_list.select_row(row)
                return

    def _on_right_click(self, _w, event, name):
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 3:
            self.service_menu(name, event)
            return True
        return False

    # ---------- Indicador de acción en curso ----------
    def set_pending(self, names, text, side="both"):
        def _do():
            for n in names:
                if text:
                    self.pending[n] = text
                    self.pending_side[n] = side
                else:
                    self.pending.pop(n, None)
                    self.pending_side.pop(n, None)
            self.apply_pending()
            return False

        GLib.idle_add(_do)

    def apply_pending(self):
        for name, ui in self.row_ui.items():
            text = self.pending.get(name)
            ui["lbl"].set_text(text or "")
            ui["sub"].set_visible(bool(text))
            ui["sub"].show_all() if text else None
            ui["spin"].start() if text else ui["spin"].stop()
            self._paint_chips(name)
        if getattr(self, "_infra", None):
            self.update_tray(*self._infra)  # la bandeja refleja al instante una acción en curso

    def add_custom(self):
        d = Gtk.Dialog(title="Servicio personalizado", transient_for=self, modal=True)
        d.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Guardar", Gtk.ResponseType.OK)
        g = Gtk.Grid(column_spacing=8, row_spacing=6, margin=12)
        d.get_content_area().add(g)
        fields = [
            ("Nombre", "accounting-mi-rama", None),
            ("Carpeta del repo", str(Path(SETTINGS["services_root"]).expanduser() / "business-financiero-accounting-service"), "dir"),
            ("Backend .csproj (relativo)", "src/backend/AccountingService.API/AccountingService.API.csproj", None),
            ("Puerto backend", "7018", None),
            ("Puerto frontend (0 = sin frontend)", "8018", None),
            ("Variables extra (KEY=VAL ...)", "", None),
        ]
        entries = []
        for i, (lbl, default, kind) in enumerate(fields):
            g.attach(Gtk.Label(label=lbl, xalign=0), 0, i, 1, 1)
            e = Gtk.Entry(text=default, width_chars=60, hexpand=True)
            g.attach(e, 1, i, 1, 1)
            entries.append(e)
        g.attach(Gtk.Label(
            label="Si usas el mismo puerto que un servicio estándar, al iniciar se detiene el que lo ocupe.",
            xalign=0, wrap=True), 0, len(fields), 2, 1)
        d.show_all()
        while d.run() == Gtk.ResponseType.OK:
            name, repo, csproj, bport, fport, env = [e.get_text().strip() for e in entries]
            err = None
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                err = "El nombre solo admite letras, números, - _ ."
            elif name in self.svc_state:
                err = "Ya existe un servicio con ese nombre."
            elif not (Path(repo) / csproj).is_file():
                err = f"No existe {Path(repo) / csproj}"
            elif not (bport.isdigit() and fport.isdigit()):
                err = "Los puertos deben ser números."
            if err:
                self.message("Datos inválidos", err)
                continue
            self.custom.append({"name": name, "dir": repo, "csproj": csproj, "backend_port": int(bport),
                                "frontend_port": int(fport), "env": env})
            save_custom(self.custom)
            self.build_services()
            self.refresh_status()
            break
        d.destroy()

    def remove_custom(self, cfg):
        self.custom = [c for c in self.custom if c["name"] != cfg["name"]]
        save_custom(self.custom)
        self.build_services()

    PENDING_TEXT = {"start": "Iniciando…", "reload": "Reiniciando…", "stop": "Deteniendo…"}
    SIDE_TEXT = {"both": "", "backend": " backend", "frontend": " frontend"}

    def pending_text(self, verb, side):
        return self.PENDING_TEXT[verb].replace("…", self.SIDE_TEXT[side] + "…")

    def service_action(self, name, act, side="both"):
        """Una acción sobre un servicio, o solo sobre su backend o su frontend."""
        cfg = next((c for c in self.custom if c["name"] == name), None)
        if cfg is None:
            args = [name] + (["--reload"] if act == "reload" else ["--stop"] if act == "stop" else [])
            if side != "both":
                args.append(f"--{side}")
            self.run_services(args)
        else:
            self.custom_action(cfg, act, side)

    def custom_action(self, cfg, action, side="both"):
        self.panel_btn.set_active(False)
        self.set_pending([cfg["name"]], self.pending_text(action, side), side)
        threading.Thread(target=self._custom_action_tracked, args=(cfg, action, side), daemon=True).start()

    def _custom_action_tracked(self, cfg, action, side="both"):
        try:
            self._custom_action(cfg, action, side)
            if action in ("start", "reload") and side != "frontend":
                # `dotnet run` compila primero: se mantiene el indicador hasta que el backend responda.
                import time
                for _ in range(120):
                    if port_open(cfg["backend_port"]):
                        break
                    time.sleep(2)
        finally:
            self.set_pending([cfg["name"]], "")

    def _custom_action(self, cfg, action, side="both"):
        name, repo, csproj = cfg["name"], Path(cfg["dir"]), cfg["csproj"]
        bport, fport = cfg["backend_port"], cfg["frontend_port"]
        do_b = side in ("both", "backend")
        do_f = side in ("both", "frontend") and bool(fport)
        if action in ("stop", "reload"):
            self.append_out(f"[{name}] deteniendo{self.SIDE_TEXT[side]}\n")
            if do_b:
                kill_port(bport)
                run(["pkill", "-TERM", "-f", f"dotnet run --project {csproj}"])
            if do_f:
                kill_port(fport)
        if action in ("start", "reload"):
            if do_b:
                if action == "start" and port_open(bport):
                    self.append_out(f"[{name}] backend ya está arriba (:{bport}); usa Reiniciar\n")
                else:
                    if action == "start":
                        kill_port(bport)
                    import time
                    time.sleep(1)
                    env = {**os.environ, "ASPNETCORE_ENVIRONMENT": "Development",
                           "ASPNETCORE_URLS": f"http://localhost:{bport}", "SKIP_MIGRATIONS": "true"}
                    env.update(dict(kv.split("=", 1) for kv in cfg.get("env", "").split() if "=" in kv))
                    self.append_out(f"[{name}] arrancando backend en :{bport}\n")
                    subprocess.Popen(["dotnet", "run", "--project", csproj, "--no-launch-profile"], cwd=repo, env=env,
                                     stdout=open(LOG_DIR / f"{name}-backend.log", "w"), stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
            fe = repo / "src" / "frontend"
            if do_f and fe.is_dir() and not (action == "start" and port_open(fport)):
                self.append_out(f"[{name}] arrancando frontend en :{fport}\n")
                subprocess.Popen(["npx", "vite", "--force"], cwd=fe,
                                 stdout=open(LOG_DIR / f"{name}-frontend.log", "w"), stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        GLib.idle_add(self.refresh_status)

    # ---------- Ajustes ----------
    def settings_dialog(self):
        d = Gtk.Dialog(title="Ajustes", transient_for=self, modal=True)
        d.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Guardar", Gtk.ResponseType.OK)
        g = Gtk.Grid(column_spacing=8, row_spacing=6, margin=12)
        d.get_content_area().add(g)
        entries = {}
        defaults = detect_defaults()
        for i, (key, label, kind) in enumerate(SETTINGS_FIELDS):
            g.attach(Gtk.Label(label=label, xalign=0), 0, i, 1, 1)
            e = Gtk.Entry(text=SETTINGS[key] or defaults.get(key, ""), width_chars=60, hexpand=True)
            g.attach(e, 1, i, 1, 1)
            entries[key] = e
            if kind:
                b = Gtk.Button(label="…")
                b.connect("clicked", lambda _b, e=e, k=kind: self._browse(e, k))
                g.attach(b, 2, i, 1, 1)
        auto = Gtk.CheckButton(label="Iniciar en segundo plano al abrir sesión", active=autostart_enabled())
        g.attach(auto, 0, len(SETTINGS_FIELDS), 2, 1)
        d.show_all()
        saved = False
        while d.run() == Gtk.ResponseType.OK:
            vals = {k: e.get_text().strip() for k, e in entries.items()}
            bad = [l for k, l, kind in SETTINGS_FIELDS
                   if kind == "dir" and not Path(vals[k]).expanduser().is_dir()
                   or kind == "file" and not Path(vals[k]).expanduser().is_file()]
            if bad:
                self.message("Ruta inválida", "No existe: " + "; ".join(bad))
                continue
            SETTINGS.update(vals)
            save_settings()
            set_autostart(auto.get_active())
            saved = True
            break
        d.destroy()
        return saved

    def _browse(self, entry, kind):
        action = Gtk.FileChooserAction.SELECT_FOLDER if kind == "dir" else Gtk.FileChooserAction.OPEN
        c = Gtk.FileChooserDialog(title="Elegir", transient_for=self, action=action)
        c.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Elegir", Gtk.ResponseType.OK)
        if c.run() == Gtk.ResponseType.OK:
            entry.set_text(c.get_filename())
        c.destroy()

    # ---------- Desconectar todo ----------
    def disconnect_all(self):
        threading.Thread(target=self._disconnect_all, daemon=True).start()

    def _disconnect_all(self):
        everyone = [n for n, _ in SERVICES] + [c["name"] for c in self.custom]
        self.set_pending(everyone, "Deteniendo…")
        self.append_out("\n[todo] deteniendo servicios…\n")
        self.run_services_sync(["--stop"])
        for c in self.custom:
            self._custom_action(c, "stop")
        self.set_pending(everyone, "")
        self.append_out("[todo] desconectando túneles…\n")
        for t in self.tunnels:
            self._tunnel_down_any(t)
        if self.vpn_active():
            self.append_out("[todo] desconectando VPN…\n")
            self._vpn_down()
        self.append_out("[todo] listo\n")

    def run_services_sync(self, args):
        p = subprocess.Popen(["bash", str(SERVICES_SCRIPT), *args], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                             env=script_env())
        for line in p.stdout:
            self.append_out(line, "actions")
        p.wait()

    def _tunnel_down_any(self, t):
        if t.alive:
            self._tunnel_down(t)
        elif port_open(t.port):
            p = subprocess.Popen(["bash", "-c", EXTERNAL_TUNNEL_DOWN, "_", str(t.script)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace")
            for line in p.stdout:
                self.append_out(line, t.src)
            p.wait()

    # ---------- UI helpers ----------
    def _infra_card(self, parent, title, handler, on_remove=None):
        box = Gtk.Box(spacing=12)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.pack_start(style(Gtk.Label(label=title, xalign=0), "card-title"), False, False, 0)
        status = Gtk.Box(spacing=6)
        dot = Gtk.Label(use_markup=True)
        lbl = style(Gtk.Label(xalign=0), "status")
        status.pack_start(dot, False, False, 0)
        status.pack_start(lbl, False, False, 0)
        text.pack_start(status, False, False, 0)
        box.pack_start(text, True, True, 0)
        btn = style(Gtk.Button(label="Conectar", valign=Gtk.Align.CENTER), "btn-primary")
        btn.connect("clicked", handler)
        box.pack_end(btn, False, False, 0)
        if on_remove is not None:
            rm = style(Gtk.Button.new_from_icon_name("edit-delete-symbolic", Gtk.IconSize.BUTTON), "btn-flat")
            rm.set_valign(Gtk.Align.CENTER)
            rm.set_tooltip_text("Quitar de la lista")
            rm.connect("clicked", lambda _b: on_remove())
            box.pack_end(rm, False, False, 0)
        parent.pack_start(card(box), True, True, 0)
        return dot, lbl, btn

    def _on_panel_toggled(self, btn):
        self.drawer.set_reveal_child(btn.get_active())

    def _hook_click(self, w):
        if isinstance(w, Gtk.TextView):
            w.connect("button-press-event", self._on_main_click)
        if isinstance(w, Gtk.Container):
            w.foreach(self._hook_click)

    def _on_main_click(self, _w, _ev):
        if self.panel_btn.get_active():
            self.panel_btn.set_active(False)
        return False

    def _on_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_q and ev.state & Gdk.ModifierType.CONTROL_MASK:
            self.request_quit()
            return True
        if ev.keyval == Gdk.KEY_Escape and self.panel_btn.get_active():
            self.panel_btn.set_active(False)
            return True
        if ev.keyval == Gdk.KEY_F9:
            self.panel_btn.set_active(not self.panel_btn.get_active())
            return True
        return False

    def _log_panel(self, view):
        """Visor + barra de herramientas: buscar, pausar el seguimiento, ir al final y limpiar."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=6, margin=8)
        search = Gtk.SearchEntry(placeholder_text="Buscar en el log…", hexpand=True)
        up = Gtk.Button.new_from_icon_name("go-up-symbolic", Gtk.IconSize.BUTTON)
        down = Gtk.Button.new_from_icon_name("go-down-symbolic", Gtk.IconSize.BUTTON)
        up.set_tooltip_text("Coincidencia anterior")
        down.set_tooltip_text("Coincidencia siguiente")
        follow = Gtk.ToggleButton(label="Seguir", active=True)
        follow.set_tooltip_text("Desmárcalo para pausar el seguimiento del final")
        bottom = Gtk.Button.new_from_icon_name("go-bottom-symbolic", Gtk.IconSize.BUTTON)
        bottom.set_tooltip_text("Ir al final")
        clear = Gtk.Button.new_from_icon_name("edit-clear-all-symbolic", Gtk.IconSize.BUTTON)
        clear.set_tooltip_text("Limpiar la vista (no borra el archivo)")
        for w in (search, up, down, follow, bottom, clear):
            bar.pack_start(w, w is search, w is search, 0)
        view.follow_btn = follow
        search.connect("activate", lambda e: self._find(view, e.get_text(), True))
        search.connect("search-changed", lambda e: self._find(view, e.get_text(), True, from_start=True))
        down.connect("clicked", lambda _b: self._find(view, search.get_text(), True))
        up.connect("clicked", lambda _b: self._find(view, search.get_text(), False))

        def go_bottom(_b):
            follow.set_active(True)
            self._scroll_end(view)

        bottom.connect("clicked", go_bottom)
        clear.connect("clicked", lambda _b: view.get_buffer().set_text(""))
        box.pack_start(bar, False, False, 0)
        box.pack_start(self._scroll(view), True, True, 0)
        return box

    def _find(self, view, text, forward, from_start=False):
        if not text:
            return
        buf = view.get_buffer()
        flags = Gtk.TextSearchFlags.CASE_INSENSITIVE | Gtk.TextSearchFlags.VISIBLE_ONLY
        if buf.get_has_selection():
            start, end = buf.get_selection_bounds()
            origin = start if (from_start or not forward) else end
        else:
            origin = buf.get_iter_at_mark(buf.get_insert())
        if forward:
            hit = origin.forward_search(text, flags, None) or buf.get_start_iter().forward_search(text, flags, None)
        else:
            hit = origin.backward_search(text, flags, None) or buf.get_end_iter().backward_search(text, flags, None)
        if hit:
            view.follow_btn.set_active(False)  # se queda en la coincidencia
            buf.select_range(hit[0], hit[1])
            view.scroll_to_iter(hit[0], 0.1, True, 0.0, 0.3)

    @staticmethod
    def _textview():
        tv = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                          left_margin=8, right_margin=8, top_margin=6, bottom_margin=6)
        tv.modify_font(Pango.FontDescription("monospace 9"))
        tv.ansi = {"fg": None, "bold": False}  # estado de color entre trozos del log
        return tv

    @staticmethod
    def _ansi_apply(state, params):
        codes = [int(c) if c else 0 for c in params.split(";")] if params else [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                state["fg"], state["bold"] = None, False
            elif c == 1:
                state["bold"] = True
            elif c == 22:
                state["bold"] = False
            elif 30 <= c <= 37:
                state["fg"] = ANSI_COLORS[c - 30]
            elif 90 <= c <= 97:
                state["fg"] = ANSI_COLORS[c - 90 + 8]
            elif c == 39:
                state["fg"] = None
            elif c in (38, 48):
                rest = codes[i + 1:]
                if rest[:1] == [5] and len(rest) >= 2:
                    if c == 38:
                        state["fg"] = ansi_256(rest[1])
                    i += 2
                elif rest[:1] == [2] and len(rest) >= 4:
                    if c == 38:
                        state["fg"] = "#%02x%02x%02x" % tuple(min(v, 255) for v in rest[1:4])
                    i += 4
            i += 1

    def _ansi_insert(self, view, text):
        """Inserta texto dibujando los colores ANSI (fondos y otras secuencias se ignoran)."""
        buf, state = view.get_buffer(), view.ansi

        def emit(seg):
            if not seg:
                return
            tags = []
            table = buf.get_tag_table()
            if state["fg"]:
                tags.append(table.lookup("fg" + state["fg"]) or buf.create_tag("fg" + state["fg"], foreground=state["fg"]))
            if state["bold"]:
                tags.append(table.lookup("bold") or buf.create_tag("bold", weight=Pango.Weight.BOLD))
            buf.insert_with_tags(buf.get_end_iter(), seg, *tags)

        pos = 0
        for m in CSI.finditer(text):
            emit(text[pos:m.start()])
            pos = m.end()
            if m.group(2) == "m":
                self._ansi_apply(state, m.group(1))
        emit(text[pos:])

    @staticmethod
    def _complete_chunk(data):
        """Recorta del final un escape ANSI o un carácter UTF-8 a medias: se leerán completos en el siguiente refresco."""
        m = re.search(rb"\x1b(\[[0-9;]*)?$", data)
        if m:
            data = data[:m.start()]
        for k in range(4):
            try:
                end = len(data) - k
                return data[:end].decode("utf-8"), end
            except UnicodeDecodeError:
                continue
        return data.decode(errors="replace"), len(data)

    @staticmethod
    def _scroll(w):
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(w)
        return sw

    @staticmethod
    def _dot(up, busy=False):
        color = "#facc15" if busy else "#22c55e" if up else "#6b7280"
        return f'<span foreground="{color}">●</span>'

    SOURCE_LOG_MAX, SOURCE_LOG_KEEP = 1_000_000, 200_000
    SOURCE_LABELS = {"vpn": "VPN", "tunnel": "Túnel", "service": "Servicios", "actions": "General servicios"}

    @classmethod
    def _source_of(cls, text):
        """Fuente por el prefijo del mensaje (la salida cruda de un proceso la indica quien la lee).
        Lo que no es de la VPN ni del túnel va al log general."""
        t = text.lstrip("\n")
        if t.startswith("[vpn]"):
            return "vpn"
        if t.startswith("[túnel]"):
            return "tunnel"
        return "actions"

    def _write_source_log(self, src, text):
        """Cada fuente conserva su propio archivo (~/siesa-dev-logs/_<fuente>.log): sobrevive al cierre de la app."""
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = LOG_DIR / f"_{src}.log"
            with self._src_lock:
                if src not in self._src_started:
                    self._src_started.add(src)
                    if path.exists() and path.stat().st_size > self.SOURCE_LOG_MAX:
                        path.write_bytes(path.read_bytes()[-self.SOURCE_LOG_KEEP:])
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(text)
        except OSError:
            pass

    def append_out(self, text, src=None):
        """Escribe en el log de su fuente; el visor lo recoge en el siguiente refresco (refresh_log)."""
        self._write_source_log(src or self._source_of(text), text)

    def message(self, title, text, kind=Gtk.MessageType.ERROR):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=kind,
                              buttons=Gtk.ButtonsType.OK, text=title)
        d.format_secondary_text(text)
        d.run()
        d.destroy()

    # ---------- Estado ----------
    def vpn_active(self):
        r = run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"])
        return any(l.split(":")[0] == VPN_NAME for l in r.stdout.splitlines())

    def vpn_password_saved(self):
        r = run(["nmcli", "-s", "-g", "vpn.secrets", "connection", "show", "id", VPN_NAME])
        return r.returncode == 0 and re.search(r"password\s*=\s*\S", r.stdout) is not None

    def vpn_exists(self):
        r = run(["nmcli", "-t", "-f", "NAME", "connection", "show"])
        return VPN_NAME in r.stdout.splitlines()

    def refresh_status(self):
        """Sondea en un hilo (nmcli, lsof y sockets bloquean si el equipo va cargado) y pinta en el principal."""
        if not self._probing:
            self._probing = True
            threading.Thread(target=self._probe_status, daemon=True).start()
        return True

    def _probe_status(self):
        try:
            ports = {("tun", t.key): t.port for t in list(self.tunnels)}
            ports.update({("svc", n): v[2] for n, v in list(self.svc_state.items())})
            ports.update({("fsvc", n): v[3] for n, v in list(self.svc_state.items()) if v[3]})
            ports.update({("log", k): p for k, p in list(self.log_ports.items())})
            with ThreadPoolExecutor(max_workers=8) as ex:
                vpn_f = ex.submit(self.vpn_active)
                open_ = dict(zip(ports, ex.map(port_open, ports.values())))
                foreign = {t.key: foreign_listener(t.port) for t in list(self.tunnels)
                           if open_.get(("tun", t.key)) and not t.alive}
                vpn = vpn_f.result()
            GLib.idle_add(self._apply_status, vpn, open_, foreign)
        except Exception:
            self._probing = False
            raise

    def _apply_status(self, vpn, open_, foreign):
        self._probing = False
        self.vpn_dot.set_markup(self._dot(vpn))
        self.vpn_lbl.set_text(self.vpn_busy or ("Conectada" if vpn else "Desconectada"))
        self.vpn_btn.set_label("Desconectar" if vpn else "Conectar")
        set_variant(self.vpn_btn, "btn-stop" if vpn else "btn-primary")

        for t in self.tunnels:
            up = open_.get(("tun", t.key), False)
            mine = t.alive
            if t.proc is not None and not mine:
                t.proc = None
            other = foreign.get(t.key) if up and not mine else None  # el puerto lo usa algo que no es el túnel
            t.up, t.foreign = up, other
            t.dot.set_markup(self._dot(up and not other, bool(other)))
            t.rail.set_markup(self._dot(up and not other, bool(other)))
            if other:
                t.lbl.set_text(f"Puerto ocupado por {short_cmd(other[1], other[0])}")
            elif up:
                t.lbl.set_text("Activo" if mine else "Activo (levantado fuera de la app)")
            else:
                t.lbl.set_text("Conectando…" if mine else "Desconectado")
            t.btn.set_label("Desconectar" if (up and not other) or mine else "Conectar")
            set_variant(t.btn, "btn-stop" if (up and not other) or mine else "btn-primary")
            t.btn.set_sensitive(not t.busy and not other)
            t.btn.set_tooltip_text(f"Libera el puerto :{t.port} para poder conectar" if other else None)
            self._notify_tunnel(t, up, mine)
        tun = open_.get(("tun", self.tunnels[0].key), False)
        mine = self.tunnels[0].alive

        # Avisos de cambios de estado de la VPN (también los hechos fuera de la app).
        if self._last_infra is not None and vpn != self._last_infra[0]:
            self.notify("VPN conectada" if vpn else "VPN desconectada")
        self._last_infra = (vpn, tun)

        for name, (b, f, port, fport) in self.svc_state.items():
            bu, fu = open_.get(("svc", name), False), fport and open_.get(("fsvc", name), False)
            self.run_state[name] = (bool(bu), bool(fu) or not fport)
        self.apply_pending()
        self.rail_vpn.set_markup(self._dot(vpn))
        for name, rb in self.rail_btns.items():
            bu, fu = self._real_state(name)
            ctx = rb.get_style_context()
            for c in ("rail-up", "rail-busy", "rail-sel"):
                ctx.remove_class(c)
            if name in self.pending:
                ctx.add_class("rail-busy")
            elif bu or fu:
                ctx.add_class("rail-up")
            if name == self.log_service:
                ctx.add_class("rail-sel")
        for key, dot in self.log_dots.items():
            dot.set_markup(self._dot(open_.get(("log", key), False)))
        self._infra = (vpn, tun, mine)
        self.update_tray(vpn, tun, mine)
        return False

    TUNNEL_GRACE = 45  # s: el watchdog del script reconecta solo; una caída más corta no se avisa

    def _notify_tunnel(self, t, up, mine):
        """`t.last` es el último estado avisado. Un túnel propio que cae y vuelve dentro del margen
        (reconexión del watchdog) no genera avisos; solo si no regresa a tiempo."""
        import time
        if up:
            t.down_since = None
            if t.last is False:
                self.notify(f"Túnel {t.name} activo")
            t.last = True
            return
        if t.last is None:
            t.last = False
        elif t.last:
            if t.down_since is None:
                t.down_since = time.monotonic()
            if not mine or time.monotonic() - t.down_since >= self.TUNNEL_GRACE:
                self.notify(f"Túnel {t.name} desconectado")
                t.last = False

    LOG_INITIAL_BYTES = 500_000

    @staticmethod
    def _at_bottom(view):
        adj = view.get_parent().get_vadjustment()
        return adj.get_value() >= adj.get_upper() - adj.get_page_size() - 40

    @staticmethod
    def _scroll_end(view):
        def _do():
            adj = view.get_parent().get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())
            return False

        GLib.idle_add(_do)

    def refresh_log(self):
        """Refresca el log del tab visible (cada tab lleva su propia posición de lectura)."""
        kind = self.log_pages[self.notebook.get_current_page()]
        name = self.log_selected if kind == "service" else f"_{kind}"
        if name:
            self._refresh_view(self.src_views[kind], name)
        return True

    def _refresh_view(self, view, name):
        """Añade solo lo nuevo del log y deja el scroll donde el usuario lo puso (sigue el final solo si ya estaba ahí)."""
        path = LOG_DIR / f"{name}.log"
        buf = view.get_buffer()
        try:
            size = path.stat().st_size
        except OSError:
            if getattr(view, "log_name", None) != name or getattr(view, "log_pos", None) != -1:
                buf.set_text(f"(sin log todavía: {path})")
                view.log_name, view.log_pos = name, -1
            return

        switched = getattr(view, "log_name", None) != name
        if switched or size < getattr(view, "log_pos", 0) or view.log_pos == -1:
            # Log nuevo, rotado o recién creado: carga la cola completa una vez.
            start = max(0, size - self.LOG_INITIAL_BYTES)
            with open(path, "rb") as fh:
                fh.seek(start)
                data = fh.read()
            text, used = self._complete_chunk(data)
            buf.set_text("")
            view.ansi.update(fg=None, bold=False)
            self._ansi_insert(view, text)
            view.log_name, view.log_pos = name, start + used
            self._scroll_end(view)
            return

        if size > view.log_pos:
            follow = view.follow_btn.get_active() and self._at_bottom(view)
            with open(path, "rb") as fh:
                fh.seek(view.log_pos)
                data = fh.read(size - view.log_pos)
            text, used = self._complete_chunk(data)
            view.log_pos += used
            self._ansi_insert(view, text)
            if follow:
                self._scroll_end(view)

    # ---------- VPN ----------
    def on_vpn(self, _btn):
        if self.vpn_active():
            self.vpn_btn.set_sensitive(False)
            self.vpn_busy = "Desconectando…"
            self.vpn_lbl.set_text(self.vpn_busy)
            threading.Thread(target=self._vpn_down, daemon=True).start()
            return
        if not OVPN_FILE.exists():
            self.message("No encuentro el archivo .ovpn", str(OVPN_FILE))
            return
        if self.vpn_password_saved():
            password, remember = None, False  # NetworkManager ya la tiene: no se pide
        else:
            password, remember = self.ask_password()
            if password is None:
                return
        self.vpn_btn.set_sensitive(False)
        self.vpn_busy = "Conectando…"
        self.vpn_lbl.set_text(self.vpn_busy)
        threading.Thread(target=self._vpn_up, args=(password, remember), daemon=True).start()

    def _retry_vpn_password(self):
        password, remember = self.ask_password()
        if password is not None:
            self.vpn_btn.set_sensitive(False)
            self.vpn_busy = "Conectando…"
            self.vpn_lbl.set_text(self.vpn_busy)
            threading.Thread(target=self._vpn_up, args=(password, remember), daemon=True).start()
        return False

    def ask_password(self):
        d = Gtk.Dialog(title="VPN Siesa", transient_for=self, modal=True)
        d.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Conectar", Gtk.ResponseType.OK)
        d.set_default_response(Gtk.ResponseType.OK)
        box = d.get_content_area()
        box.set_spacing(6)
        box.set_border_width(12)
        box.pack_start(Gtk.Label(label=f"Contraseña para {VPN_USER}", xalign=0), False, False, 0)
        entry = Gtk.Entry(visibility=False, activates_default=True)
        entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, "view-reveal-symbolic")
        entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Mostrar contraseña")

        def toggle_reveal(e, *_):
            show = not e.get_visibility()
            e.set_visibility(show)
            e.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY,
                                      "view-conceal-symbolic" if show else "view-reveal-symbolic")
            e.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY,
                                    "Ocultar contraseña" if show else "Mostrar contraseña")
        entry.connect("icon-press", toggle_reveal)
        box.pack_start(entry, False, False, 0)
        chk = Gtk.CheckButton(label="Guardar contraseña")
        box.pack_start(chk, False, False, 0)
        d.show_all()
        ok = d.run() == Gtk.ResponseType.OK
        pw, rem = entry.get_text(), chk.get_active()
        d.destroy()
        return (pw, rem) if ok and pw else (None, False)

    def _vpn_up(self, password, remember):
        try:
            self._vpn_up_inner(password, remember)
        finally:
            self.vpn_busy = ""
            GLib.idle_add(self.refresh_status)

    @staticmethod
    def _import_ovpn():
        """Importa el .ovpn sin las líneas `route remote_host … net_gateway` (FortiGate): el importador de
        NetworkManager las rechaza, y él ya enruta el servidor VPN por la puerta de enlace. El original no se toca."""
        lines = OVPN_FILE.read_text(errors="replace").splitlines(keepends=True)
        clean = [l for l in lines if not re.match(r"\s*route\s+remote_host\b", l)]
        tmp = Path(tempfile.mkdtemp(prefix="ovpn-")) / OVPN_FILE.name
        try:
            tmp.write_text("".join(clean))
            tmp.chmod(0o600)
            # LC_ALL=C: la salida de nmcli se traduce («Conexión …») y aquí se lee el UUID
            return run(["nmcli", "connection", "import", "type", "openvpn", "file", str(tmp)],
                       env={**os.environ, "LC_ALL": "C"})
        finally:
            tmp.unlink(missing_ok=True)
            tmp.parent.rmdir()

    def _vpn_up_inner(self, password, remember):
        self.append_out(f"[vpn] conectando {VPN_NAME}…\n")
        if not self.vpn_exists():
            r = self._import_ovpn()
            self.append_out(r.stdout + r.stderr, "vpn")
            imported = re.search(r"Connection '[^']+' \(([0-9a-f-]{36})\)", r.stdout)
            if r.returncode != 0 or not imported:
                self.append_out("[vpn] falló la importación del .ovpn\n")
                return GLib.idle_add(self.vpn_btn.set_sensitive, True)
            # never-default: solo las rutas que empuja el servidor van por la VPN; internet sigue por la red local
            run(["nmcli", "connection", "modify", imported.group(1), "connection.id", VPN_NAME,
                 "ipv4.never-default", "yes", "ipv6.never-default", "yes"])
        if password is None:
            r = run(["nmcli", "connection", "up", "id", VPN_NAME])
        else:
            flags = "0" if remember else "2"
            run(["nmcli", "connection", "modify", VPN_NAME,
                 "+vpn.data", f"username={VPN_USER}", "+vpn.data", f"password-flags={flags}"])
            if remember:
                run(["nmcli", "connection", "modify", VPN_NAME, "vpn.secrets", f"password={password}"])

            fd, pwfile = tempfile.mkstemp(prefix="vpnpw-")
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(f"vpn.secrets.password:{password}\n")
                r = run(["nmcli", "connection", "up", "id", VPN_NAME, "passwd-file", pwfile])
            finally:
                os.unlink(pwfile)
        self.append_out(r.stdout + r.stderr or "", "vpn")
        self.append_out("[vpn] conectada\n" if r.returncode == 0 else "[vpn] no se pudo conectar\n")
        if r.returncode != 0 and password is None:
            GLib.idle_add(self._retry_vpn_password)  # la guardada pudo cambiar: se pide la nueva
        GLib.idle_add(self.vpn_btn.set_sensitive, True)
        GLib.idle_add(self.refresh_status)

    def _vpn_down(self):
        r = run(["nmcli", "connection", "down", "id", VPN_NAME])
        self.vpn_busy = ""
        self.append_out(r.stdout + r.stderr, "vpn")
        GLib.idle_add(self.vpn_btn.set_sensitive, True)
        GLib.idle_add(self.refresh_status)

    # ---------- Túneles ----------
    @staticmethod
    def _tunnel_key(name):
        return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "tunel"

    def build_tunnels(self):
        """Tarjeta, punto del riel y tab de log por túnel. Reconstruir conserva los procesos en curso."""
        old = {t.key: t for t in self.tunnels}
        main = old.get("main") or Tunnel("main", "Cloud SQL", TUNNEL_SCRIPT, TUNNEL_PORT, main=True)
        self.tunnels = [main]
        for cfg in self.custom_tunnels:
            key = self._tunnel_key(cfg["name"])
            t = old.get(key) or Tunnel(key, cfg["name"], cfg["script"], cfg["port"])
            t.name, t._script, t.port = cfg["name"], cfg["script"], int(cfg["port"])
            self.tunnels.append(t)

        for box in (self.tun_box, self.rail_tun_box):
            for c in box.get_children():
                box.remove(c)
        for t in self.tunnels:
            title = f"Túnel Cloud SQL · :{t.port}" if t.main else f"Túnel {t.name} · :{t.port}"
            t.dot, t.lbl, t.btn = self._infra_card(
                self.tun_box, title, lambda _b, t=t: self.on_tunnel(t),
                on_remove=None if t.main else (lambda t=t: self.remove_tunnel(t)))
            t.rail = Gtk.Label(use_markup=True)
            short = "BD" if t.main else (t.name[:3].upper())
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            col.pack_start(t.rail, False, False, 0)
            col.pack_start(style(Gtk.Label(label=short), "col-head"), False, False, 0)
            col.set_tooltip_text(t.name)
            self.rail_tun_box.pack_start(col, False, False, 0)
        self.tun_box.show_all()
        self.rail_tun_box.show_all()
        self.rebuild_tray_tunnels()

        # Un tab de log por túnel, justo después de VPN.
        nb = self.notebook
        for kind in [k for k in self.log_pages if k.startswith("tunnel")]:
            nb.remove_page(self.log_pages.index(kind))
            self.log_pages.remove(kind)
            self.src_views.pop(kind, None)
        for i, t in enumerate(self.tunnels, start=1):
            view = self.src_views[t.src] = self._textview()
            panel = self._log_panel(view)
            self._hook_click(panel)
            panel.show_all()
            nb.insert_page(panel, Gtk.Label(label=t.tab), i)
            self.log_pages.insert(i, t.src)
        self.refresh_status()

    def add_tunnel(self):
        d = Gtk.Dialog(title="Añadir túnel", transient_for=self, modal=True)
        d.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Guardar", Gtk.ResponseType.OK)
        g = Gtk.Grid(column_spacing=8, row_spacing=6, margin=12)
        d.get_content_area().add(g)
        here = TUNNEL_SCRIPT.parent if TUNNEL_SCRIPT.name else Path.home()
        fields = [("Nombre", "stg", None),
                  ("Script del túnel", str(here / "business-stg.sh"), "file")]
        entries = []
        for i, (lbl, default, kind) in enumerate(fields):
            g.attach(Gtk.Label(label=lbl, xalign=0), 0, i, 1, 1)
            e = Gtk.Entry(text=default, width_chars=60, hexpand=True)
            g.attach(e, 1, i, 1, 1)
            if kind:
                b = Gtk.Button(label="…")
                b.connect("clicked", lambda _b, e=e, k=kind: self._browse(e, k))
                g.attach(b, 2, i, 1, 1)
            entries.append(e)
        g.attach(Gtk.Label(
            label="Debe ser un script como business-dev.sh (mismas funciones de limpieza); "
                  "el puerto local se lee de su _BT_LOCAL_PORT.",
            xalign=0, wrap=True, max_width_chars=70), 0, len(fields), 3, 1)
        d.show_all()
        while d.run() == Gtk.ResponseType.OK:
            name, script = [e.get_text().strip() for e in entries]
            err = None
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                err = "El nombre solo admite letras, números, - _ ."
            elif self._tunnel_key(name) in {t.key for t in self.tunnels} or name.lower() == "main":
                err = "Ya existe un túnel con ese nombre."
            elif not Path(script).expanduser().is_file():
                err = f"No existe {script}"
            else:
                num = script_port(script)
                other = foreign_listener(num) if num else None
                if num is None:
                    err = "No encontré _BT_LOCAL_PORT en el script, así que no sé qué puerto usa."
                elif num in {t.port for t in self.tunnels}:
                    err = f"El puerto :{num} ya lo usa otro túnel."
                elif other:
                    err = f"El puerto :{num} ya está ocupado por {short_cmd(other[1], other[0])}."
            if err:
                self.message("Datos inválidos", err)
                continue
            self.custom_tunnels.append({"name": name, "script": script, "port": num})
            save_tunnels(self.custom_tunnels)
            self.build_tunnels()
            break
        d.destroy()

    def remove_tunnel(self, t):
        if t.alive or port_open(t.port):
            self.message("Túnel activo", f"Desconecta «{t.name}» antes de quitarlo de la lista.",
                         Gtk.MessageType.WARNING)
            return
        self.custom_tunnels = [c for c in self.custom_tunnels if self._tunnel_key(c["name"]) != t.key]
        save_tunnels(self.custom_tunnels)
        self.build_tunnels()

    def on_tunnel(self, t):
        if not t.alive and port_open(t.port):
            other = foreign_listener(t.port)
            if other:
                self.message("Puerto ocupado",
                             f"El puerto :{t.port} ya lo usa {short_cmd(other[1], other[0])}.\n"
                             "Libéralo o cambia el puerto del túnel; no se levantó nada.", Gtk.MessageType.WARNING)
                return
        if port_open(t.port) or t.alive:
            self.append_out(f"[túnel] {t.name}: desconectando (borra pod, remueve la IP de MAN)…\n", t.src)
            t.busy = True
            t.btn.set_sensitive(False)
            threading.Thread(target=self._tunnel_down_ui, args=(t,), daemon=True).start()
            return
        acct = run(["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"])
        if not acct.stdout.strip():
            self.message("Sin sesión de gcloud",
                         "Corre en una terminal:  gcloud auth login --no-launch-browser")
            return
        if not t.script.is_file():
            self.message("Script no encontrado", f"No existe {t.script}")
            return
        self.append_out(f"[túnel] {t.name}: conectando…\n", t.src)
        # Con `source` (como en una terminal) el script no activa `set -e`: su watchdog sobrevive a las
        # caídas del port-forward y reconecta. Ejecutado a secas, la primera caída lo mataba y el trap
        # borraba el pod. El shell se queda esperando y limpia al recibir SIGTERM.
        t.proc = subprocess.Popen(
            ["bash", "-c", TUNNEL_RUNNER, "_", str(t.script)], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
            start_new_session=True,
        )
        threading.Thread(target=self._pump, args=(t.proc, t), daemon=True).start()
        threading.Thread(target=self._watch_tunnel_log, args=(t.proc, t), daemon=True).start()

    def _watch_tunnel_log(self, proc, t):
        """El watchdog del script escribe sus reintentos en un archivo, no en stdout: se muestran aquí
        (los primeros) para saber por qué el túnel se reinicia, sin inundar la consola."""
        import time
        log = t.watchdog_log()
        if log is None:
            return
        shown, limit = 0, 6
        try:
            pos = log.stat().st_size  # lo anterior es de una sesión previa: el script lo trunca al arrancar
        except OSError:
            pos = 0
        while proc.poll() is None:
            time.sleep(2)
            try:
                size = log.stat().st_size
                if size < pos:  # el script lo trunca al arrancar
                    pos = 0
                with open(log, "rb") as fh:
                    fh.seek(pos)
                    data = fh.read(size - pos).decode(errors="replace")
                pos = size
            except OSError:
                continue
            for line in data.splitlines():
                if "Handling connection" in line or "Forwarding from" in line or shown > limit:
                    continue
                if "lost connection to pod" in line or "port-forward cayó" in line:
                    continue  # reconexión rutinaria del watchdog: queda en el archivo, no en la consola
                if "[watchdog]" in line or "unable to listen" in line.lower() or line.startswith("error:"):
                    if shown == limit:
                        self.append_out(f"[túnel] más reintentos omitidos; detalle en {log}\n", t.src)
                    else:
                        self.append_out(f"[túnel] {line[:200]}\n", t.src)
                    shown += 1
                    if "already in use" in line:
                        self.append_out(f"[túnel] otro proceso ya usa :{t.port} (revisa `lsof -i :{t.port}`)\n", t.src)

    def _pump(self, proc, t):
        for line in proc.stdout:
            self.append_out(line, t.src)
        self.append_out("[túnel] proceso terminado\n", t.src)

    def _tunnel_down_ui(self, t):
        try:
            self._tunnel_down_any(t)
        finally:
            t.busy = False
            GLib.idle_add(self.refresh_status)

    def _tunnel_down(self, t):
        proc = t.proc
        # SIGTERM solo al bash: su trap EXIT hace la limpieza (pod, watchdog, IP en MAN).
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            self.append_out("[túnel] la limpieza tardó demasiado; revisa MAN y el pod a mano\n", t.src)
        GLib.idle_add(self.refresh_status)

    # ---------- Servicios ----------
    def run_services(self, args):
        if self.busy:
            self.append_out("[servicios] ya hay una acción en curso\n")
            return
        self.busy = True
        self.panel_btn.set_active(False)  # el desplegable tapa el log: se cierra al lanzar la acción
        names = [a for a in args if not a.startswith("--")] or [n for n, _ in SERVICES]
        verb = "stop" if "--stop" in args else "reload" if "--reload" in args else "start"
        self._pending_names = names
        only_b, only_f = "--backend" in args, "--frontend" in args
        side = "backend" if only_b and not only_f else "frontend" if only_f and not only_b else "both"
        self.set_pending(names, self.pending_text(verb, side), side)
        self.append_out(f"\n$ start-services.sh {' '.join(args)}\n")
        threading.Thread(target=self._run_services, args=(args,), daemon=True).start()

    def _run_services(self, args):
        try:
            p = subprocess.Popen(["bash", str(SERVICES_SCRIPT), *args], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                                 env=script_env())
            for line in p.stdout:
                self.append_out(line, "actions")
            p.wait()
        finally:
            self.busy = False
            self.set_pending(self._pending_names, "")
            GLib.idle_add(self.refresh_status)
            GLib.idle_add(self.notify, "Servicios", f"{', '.join(self._pending_names)}: acción terminada")

    # ---------- Cierre ----------
    def on_close(self, *_):
        if self.tray is not None and not self.quitting:
            self.hide()  # sigue en segundo plano; se sale desde la bandeja o con Ctrl+Q
            if not self._hint_shown:
                self._hint_shown = True
                self.notify("Siesa Dev Launcher sigue en segundo plano",
                            "Usa el ícono de la bandeja para acciones rápidas o para salir.")
            return True
        self.request_quit()
        return True


def import_shell_env():
    """Al abrir la app desde el menú no se lee ~/.bashrc: `dotnet` resolvía a /usr/bin (SDK 8) en vez de
    ~/.dotnet (SDK 10), y node/gcloud tampoco estaban en el PATH. Se toma el entorno de una shell interactiva."""
    try:
        r = subprocess.run(["bash", "-ic", 'echo "__ENV__PATH=$PATH"; echo "__ENV__DOTNET_ROOT=$DOTNET_ROOT"'],
                           capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return
    for line in r.stdout.splitlines():
        if line.startswith("__ENV__"):
            key, _, val = line[len("__ENV__"):].partition("=")
            if val:
                os.environ[key] = val


class App(Gtk.Application):
    """Una sola instancia: `launcher.py --action stop-all` se reenvía a la que ya corre en segundo plano."""

    ACTIONS = ["start-all", "reload-all", "stop-all", "vpn-toggle", "tunnel-toggle", "disconnect-all", "quit"]

    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        import_shell_env()
        LOG_DIR.mkdir(exist_ok=True)
        load_settings()
        self.hold()  # no terminar al ocultar la ventana

    def do_command_line(self, cmdline):
        ap = argparse.ArgumentParser(prog="launcher.py")
        ap.add_argument("--action", choices=self.ACTIONS)
        ap.add_argument("--background", action="store_true", help="arranca sin mostrar la ventana")
        args, _ = ap.parse_known_args(cmdline.get_arguments()[1:])

        first = self.win is None
        if first:
            self.win = Launcher(self)
            self.win.show_all()
        win = self.win
        configured = all(SETTINGS[k] for k, _, _ in SETTINGS_FIELDS)

        if args.action:
            if first and not args.background:
                win.hide()
            if args.action == "vpn-toggle" and not win.vpn_active():
                win.present()  # necesita pedir la contraseña
            win.do_action(args.action)
        elif args.background and first and win.tray is not None:
            win.hide()
        else:
            win.present()
            if not configured:
                GLib.idle_add(lambda: win.settings_dialog() and False)
        return 0


def single_instance_guard():
    """Candado de archivo además del de D-Bus: si el bus de sesión falla, dos launchers se pisan los túneles."""
    import fcntl
    fh = open(Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"{APP_ID}.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return fh


if __name__ == "__main__":
    GLib.set_prgname(APP_ID)
    _lock = single_instance_guard()
    app = App()
    if _lock is None:
        # ya hay uno corriendo: solo se le reenvía la orden; si D-Bus no lo encuentra, no se arranca otro
        app.register()
        if not app.get_is_remote():
            sys.exit(0)
    sys.exit(app.run(sys.argv))
