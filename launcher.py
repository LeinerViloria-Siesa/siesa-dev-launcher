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

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def run(cmd, **kw):
    """Como subprocess.run, pero un comando ausente (nmcli, gcloud, lsof…) no tumba la app: devuelve rc 127."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
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
source <(sed '$d' "$1")
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


CSS = b"""
window { background: #14161c; color: #e5e7eb; }
.muted { color: #9ca3af; }
.section { font-size: 11px; font-weight: 700; color: #9ca3af; letter-spacing: 1px; }
.card { background: #1c1f27; border: 1px solid #2b303b; border-radius: 10px; padding: 14px 16px; }
.card-title { font-weight: 700; font-size: 14px; color: #f3f4f6; }
.status { color: #d1d5db; }
.col-head { font-size: 11px; font-weight: 700; color: #6b7280; }
.svc-name { font-weight: 600; }
.rail { background: #191c23; border-right: 1px solid #2b303b; }
.rail-btn { min-width: 40px; padding: 7px 0; font-weight: 700; font-size: 11px; color: #8b93a5;
            background-color: #232733; border: 1px solid #343a48; border-radius: 9px; }
.rail-btn.rail-up { color: #86efac; border-color: #1f6f3c; background-color: #16231d; }
.rail-btn.rail-busy { color: #c7d2fe; border-color: #4f46e5; background-color: #232845; }
.rail-btn.rail-sel { box-shadow: inset 3px 0 #818cf8; }
.drawer { background: #191c23; border-right: 1px solid #2b303b; box-shadow: 10px 0 28px rgba(0,0,0,0.55); }
.svc-scroll { background: #1c1f27; border: 1px solid #2b303b; border-radius: 10px; }
.svc-scroll row:selected { background: #262b3a; }
button.radio { padding: 3px 12px; border: 1px solid #343a48; background: #232733; color: #9ca3af; }
button.radio:checked { background: #2c3350; color: #c7d2fe; border-color: #4f46e5; }
button.radio:first-child { border-radius: 7px 0 0 7px; }
button.radio:last-child { border-radius: 0 7px 7px 0; }
.pending { color: #a5b4fc; font-weight: 600; font-size: 11px; }
searchentry, entry { background: #181b22; color: #e5e7eb; border: 1px solid #343a48; border-radius: 7px; }
togglebutton:checked { background-color: #2c3350; color: #c7d2fe; border-color: #4f46e5; }
list, row { background: transparent; color: #e5e7eb; }
row label { color: #e5e7eb; }
row label.col-head { color: #6b7280; }
row { border-bottom: 1px solid #262a35; padding: 8px 4px; }
row:last-child { border-bottom: none; }
button { background-image: none; background-color: #232733; color: #d1d5db; border: 1px solid #343a48;
         border-radius: 7px; padding: 4px 12px; box-shadow: none; text-shadow: none; }
button:hover { background-color: #2c313f; }
button.btn-primary { background-color: #6366f1; border-color: #6366f1; color: #ffffff; font-weight: 600; }
button.btn-primary:hover { background-color: #4f46e5; }
button.btn-stop { color: #f87171; border-color: #5b2a2f; }
button.btn-stop:hover { background-color: #2d1b1f; }
button.btn-flat { border-color: transparent; background-color: transparent; color: #a5b4fc; }
button.btn-flat:hover { background-color: #232733; }
button:disabled { opacity: 0.4; }
textview, textview text { background-color: #181b22; color: #d1d5db; }
combobox button, combobox * { color: #d1d5db; }
.log-side { background: #191c23; border-right: 1px solid #2b303b; }
.log-side row { padding: 0; border-bottom: none; }
.log-side row:selected { background: #262b3a; }
.log-side row:selected label { color: #c7d2fe; }
.log-side row:hover { background: #20242f; }
notebook { background: #1c1f27; border: 1px solid #2b303b; border-radius: 10px; }
notebook > header { background: #191c23; border-bottom: 1px solid #2b303b; }
notebook > header > tabs > tab { padding: 6px 16px; background: transparent; border: none; color: #9ca3af; }
notebook > header > tabs > tab:checked { color: #a5b4fc; font-weight: 600; box-shadow: inset 0 -2px #818cf8; }
scrolledwindow { border: none; }
paned > separator { background-color: #2b303b; min-height: 1px; }
headerbar { background: #191c23; color: #e5e7eb; border-bottom: 1px solid #2b303b; box-shadow: none; }
headerbar button { color: #d1d5db; }
"""


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
        self.tunnel_proc = None
        self.busy = False
        self.tunnel_busy = False

        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.svc_state = {}
        self.custom = load_custom()
        self.run_state = {}    # servicio -> (backend_up, frontend_up_o_sin_frontend)
        self.pending = {}      # servicio -> texto de la acción en curso ("Iniciando…")
        self.row_ui = {}       # servicio -> {"spin", "lbl", "btns"}
        self.global_btns = []
        self.vpn_busy = ""

        # Barra de título: acciones globales a mano, sin ocupar espacio del contenido.
        hb = Gtk.HeaderBar(show_close_button=True, title="Siesa Dev Launcher",
                           subtitle=(f"v{read_version()}" if read_version() else None))
        self.set_titlebar(hb)
        cfg = Gtk.Button.new_from_icon_name("preferences-system-symbolic", Gtk.IconSize.BUTTON)
        cfg.set_tooltip_text("Ajustes")
        cfg.connect("clicked", lambda _b: self.settings_dialog())
        hb.pack_end(cfg)
        down = style(Gtk.Button(label="Desconectar todo"), "btn-stop")
        down.set_tooltip_text("Detiene servicios, cierra el túnel y la VPN")
        down.connect("clicked", lambda _b: self.disconnect_all())
        hb.pack_end(down)

        self.connect("key-press-event", self._on_key)

        # Columna izquierda: conexión + servicios (compacta).
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=16)
        left.pack_start(section("Conexión"), False, False, 0)
        infra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.pack_start(infra, False, False, 0)
        self.vpn_dot, self.vpn_lbl, self.vpn_btn = self._infra_card(infra, "VPN Siesa", self.on_vpn)
        self.tun_dot, self.tun_lbl, self.tun_btn = self._infra_card(
            infra, f"Túnel Cloud SQL · :{TUNNEL_PORT}", self.on_tunnel)

        head = Gtk.Box(spacing=8, margin_top=10)
        left.pack_start(head, False, False, 0)
        head.pack_start(section("Servicios"), True, True, 0)
        add = style(Gtk.Button(label="+ Añadir"), "btn-flat")
        add.set_tooltip_text("Añadir un servicio personalizado")
        add.connect("clicked", lambda _b: self.add_custom())
        head.pack_end(add, False, False, 0)

        bulk = Gtk.Box(spacing=6, homogeneous=True)
        for txt, args, var in [("Iniciar todos", [], "btn-primary"),
                               ("Reiniciar todos", ["--reload"], None),
                               ("Detener todos", ["--stop"], "btn-stop")]:
            btn = style(Gtk.Button(label=txt), var) if var else Gtk.Button(label=txt)
            btn.connect("clicked", lambda _b, a=args: self.run_services(a))
            bulk.pack_start(btn, True, True, 0)
            self.global_btns.append(btn)
        self.global_btns.reverse()  # orden interno: Detener, Reiniciar, Iniciar (apply_pending lo invierte)

        # La lista de servicios es lo único que se desplaza: cabecera y botones globales quedan fijos.
        self.svc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        svc_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                        vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                        overlay_scrolling=False, min_content_height=120)
        svc_scroll.add(self.svc_box)
        style(svc_scroll, "svc-scroll")
        left.pack_start(svc_scroll, True, True, 0)
        left.pack_start(bulk, False, False, 0)
        left.set_size_request(400, -1)

        # Columna derecha: actividad a todo lo alto.
        nb = Gtk.Notebook()
        self.out_view = self._textview()
        nb.append_page(self._log_panel(self.out_view), Gtk.Label(label="Salida"))
        logbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.log_selected = None
        self.log_service = None
        self.log_side = "backend"
        self.log_dots = {}
        self.log_ports = {}
        self._auto_select = False
        lhead = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=10)
        self.log_title = style(Gtk.Label(label="Elige un servicio", xalign=0), "card-title")
        lhead.pack_start(self.log_title, True, True, 8)
        self.side_b = Gtk.RadioButton.new_with_label_from_widget(None, "Backend")
        self.side_f = Gtk.RadioButton.new_with_label_from_widget(self.side_b, "Frontend")
        for rb, side in ((self.side_b, "backend"), (self.side_f, "frontend")):
            rb.set_mode(False)
            rb.connect("toggled", lambda w, sd=side: self._on_side(w, sd))
            lhead.pack_start(rb, False, False, 0)
        logbox.pack_start(lhead, False, False, 0)
        self.log_view = self._textview()
        logbox.pack_start(self._log_panel(self.log_view), True, True, 0)
        nb.append_page(logbox, Gtk.Label(label="Logs de servicios"))
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
        self.rail_tun = Gtk.Label(use_markup=True)
        for lbl, name in ((self.rail_vpn, "VPN"), (self.rail_tun, "BD")):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            box.pack_start(lbl, False, False, 0)
            box.pack_start(style(Gtk.Label(label=name), "col-head"), False, False, 0)
            rail.pack_start(box, False, False, 2)
        rail.pack_start(Gtk.Separator(), False, False, 2)

        self.rail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        rail_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, overlay_scrolling=True)
        rail_scroll.add(self.rail_box)
        rail.pack_start(rail_scroll, True, True, 0)

        main = Gtk.EventBox()  # un clic fuera del desplegable lo cierra
        main.add(right)
        main.connect("button-press-event", self._on_main_click)

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

        self.build_services()
        self.refresh_status()
        GLib.timeout_add_seconds(3, self.refresh_status)
        GLib.timeout_add_seconds(2, self.refresh_log)
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

        # Estado y acción en una sola fila: un clic conecta si está apagado y desconecta si está encendido.
        item("vpn_row", "VPN", lambda: self.on_vpn(None))
        item("tun_row", "Túnel", lambda: self.on_tunnel(None))
        menu.append(Gtk.SeparatorMenuItem())
        item("show", "Mostrar ventana", self.present)
        menu.append(Gtk.SeparatorMenuItem())
        all_svc = group("Todos los servicios", "grp_all")
        item("start", "Iniciar todos", lambda: self.run_services([]), parent=all_svc)
        item("reload", "Reiniciar todos", lambda: self.run_services(["--reload"]), parent=all_svc)
        item("stop", "Detener todos", lambda: self.run_services(["--stop"]), parent=all_svc)
        self.tray_svc_menu = group("Servicios", "svc_header")
        menu.append(Gtk.SeparatorMenuItem())
        conn = group("Conexión", "grp_conn")
        item("vpn", "Conectar VPN", lambda: self.on_vpn(None), parent=conn)
        item("tunnel", "Conectar túnel", lambda: self.on_tunnel(None), parent=conn)
        item("down", "Desconectar todo", self.disconnect_all, parent=conn)
        menu.append(Gtk.SeparatorMenuItem())
        item("quit", "Salir", self.request_quit)
        menu.show_all()
        ind.set_menu(menu)
        self.tray = ind
        self.tray_menu = menu
        self.rebuild_tray_services()

    @staticmethod
    def _tray_label(text):
        # El menú de la bandeja trata "_" como atajo de teclado y lo borra ("Accounting_2" -> "Accounting2").
        return text.replace("_", "__")

    def rebuild_tray_services(self):
        """Servicios como filas planas del menú (sin submenús: GNOME los despliega en acordeón y se cortan)."""
        if self.tray is None:
            return
        for w in self.tray_svc_menu.get_children():
            self.tray_svc_menu.remove(w)
        self.tray_svc = {}
        names = [(n, None) for n, _ in SERVICES] + [(c["name"], c) for c in self.custom]
        for name, cfg in names:
            toggle = Gtk.MenuItem(label=self._tray_label(name))
            toggle.connect("activate", lambda _m, n=name, c=cfg: self._tray_toggle(n, c))
            self.tray_svc_menu.append(toggle)
            self.tray_svc[name] = toggle
        self.tray_svc_menu.show_all()

    def _tray_service_action(self, name, cfg, act):
        if cfg is None:
            args = {"start": [name], "reload": [name, "--reload"], "stop": [name, "--stop"]}[act]
            self.run_services(args)
        else:
            self.custom_action(cfg, act)

    def _tray_toggle(self, name, cfg):
        bu, fu = self.run_state.get(name, (False, False))
        self._tray_service_action(name, cfg, "stop" if (bu and fu) else "start")

    def update_tray(self, vpn, tun, mine):
        if self.tray is None:
            return
        it = self.tray_items
        if self.vpn_busy:
            it["vpn_row"].set_label(f"◌  VPN · {self.vpn_busy}")
        else:
            it["vpn_row"].set_label(f"{'●' if vpn else '○'}  VPN · {'Desconectar' if vpn else 'Conectar'}")
        if self.tunnel_busy:
            it["tun_row"].set_label("◌  Túnel · Trabajando…")
        else:
            it["tun_row"].set_label(f"{'●' if tun else '○'}  Túnel · "
                                    f"{'Desconectar' if (tun or mine) else 'Conectar'}")
        it["vpn_row"].set_sensitive(not self.vpn_busy)
        it["tun_row"].set_sensitive(not self.tunnel_busy)
        it["vpn"].set_label("Desconectar VPN" if vpn else "Conectar VPN")
        it["tunnel"].set_label("Desconectar túnel" if tun or mine else "Conectar túnel")
        it["vpn"].set_sensitive(not self.vpn_busy)
        it["tunnel"].set_sensitive(not self.tunnel_busy)
        self.tray.set_icon_full(APP_ID, "VPN conectada" if vpn else "VPN desconectada")
        states = [self.run_state.get(n, (False, False)) for n, _ in SERVICES]
        all_up = bool(states) and all(b and f for b, f in states)
        any_up = any(b or f for b, f in states)
        it["start"].set_sensitive(not self.busy and not all_up)
        it["reload"].set_sensitive(not self.busy and any_up)
        it["stop"].set_sensitive(not self.busy and any_up)
        running = sum(1 for n in getattr(self, "tray_svc", {}) if any(self.run_state.get(n, (False, False))))
        it["svc_header"].set_label(f"Servicios · {running} activo{'' if running == 1 else 's'}")
        for name, toggle in getattr(self, "tray_svc", {}).items():
            bu, fu = self.run_state.get(name, (False, False))
            text = self.pending.get(name)
            if text:
                toggle.set_label(self._tray_label(f"◌  {name} · {text}"))
            elif bu and fu:
                toggle.set_label(self._tray_label(f"●  {name} · Detener"))
            else:
                toggle.set_label(self._tray_label(f"{'◐' if (bu or fu) else '○'}  {name} · Iniciar"))
            toggle.set_sensitive(not text)

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
        if self.tunnel_proc is not None and self.tunnel_proc.poll() is None:
            d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                  buttons=Gtk.ButtonsType.YES_NO,
                                  text="El túnel sigue activo. ¿Desconectarlo antes de salir?")
            resp = d.run()
            d.destroy()
            if resp == Gtk.ResponseType.YES:
                self.tunnel_proc.send_signal(signal.SIGTERM)
                try:
                    self.tunnel_proc.wait(timeout=90)
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
            "tunnel-toggle": lambda: self.on_tunnel(None),
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
        self.row_ui = {}
        svc_rows = {}

        def add_row(cells, actions, svc=None):
            row = Gtk.ListBoxRow(activatable=svc is not None, selectable=svc is not None)
            row.svc = svc
            if svc is not None:
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
        colb = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)  # B/F: encabezado y puntos con el mismo ancho
        colf = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        hb_, hf_ = style(Gtk.Label(label="B"), "col-head"), style(Gtk.Label(label="F"), "col-head")
        colb.add_widget(hb_)
        colf.add_widget(hf_)
        add_row([style(Gtk.Label(label="SERVICIO", xalign=0), "col-head"), hb_, hf_], spacer)

        rows = [(n, p, None) for n, p in SERVICES] + [(c["name"], c["backend_port"], c) for c in self.custom]
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
            b, f = Gtk.Label(use_markup=True), Gtk.Label(use_markup=True)
            colb.add_widget(b)
            colf.add_widget(f)
            fport = cfg.get("frontend_port", port + 1000) if cfg else port + 1000
            self.svc_state[name] = (b, f, port, fport)
            acts = Gtk.Box(spacing=4)
            btns = []
            for icon, tip, act in [("media-playback-start-symbolic", "Iniciar", "start"),
                                   ("view-refresh-symbolic", "Reiniciar", "reload"),
                                   ("media-playback-stop-symbolic", "Detener", "stop")]:
                btn = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
                btn.set_tooltip_text(tip)
                btns.append(btn)
                if act == "stop":
                    style(btn, "btn-stop")
                if cfg is None:
                    args = {"start": [name], "reload": [name, "--reload"], "stop": [name, "--stop"]}[act]
                    btn.connect("clicked", lambda _b, a=args: self.run_services(a))
                else:
                    btn.connect("clicked", lambda _b, c=cfg, a=act: self.custom_action(c, a))
                acts.pack_start(btn, False, False, 0)
            if cfg is not None:
                rm = style(Gtk.Button.new_from_icon_name("edit-delete-symbolic", Gtk.IconSize.BUTTON), "btn-flat")
                rm.set_tooltip_text("Quitar de la lista")
                rm.connect("clicked", lambda _b, c=cfg: self.remove_custom(c))
                acts.pack_start(rm, False, False, 0)
            add_row([namebox, b, f], acts, svc=name)
            self.row_ui[name] = {"spin": spin, "lbl": plbl, "sub": sub, "btns": btns}
        for c in self.rail_box.get_children():
            self.rail_box.remove(c)
        self.rail_btns = {}
        for name, port, cfg in rows:
            words = [w for w in re.split(r"[-_ ]+", name) if w]
            abbr = (words[0][0] + words[1][0] if len(words) > 1 else name[:2]).upper()
            rb = style(Gtk.Button(label=abbr), "rail-btn")
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
        self._select_default_service(svc_rows)
        self.apply_pending()
        self.rebuild_tray_services()

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
        has_front = bool(self.svc_state[row.svc][3])
        self.side_f.set_sensitive(has_front)
        if not has_front:
            self.log_side = "backend"
            self.side_b.set_active(True)
        self._update_log_source()
        if not self._auto_select:
            self.notebook.set_current_page(1)
            self.panel_btn.set_active(False)
        GLib.idle_add(lambda: self.refresh_status() and False)

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

    # ---------- Menú contextual (clic derecho) por servicio ----------
    def service_menu(self, name, event):
        """Menú flotante junto al cursor: nunca se corta como los submenús de la bandeja."""
        cfg = next((c for c in self.custom if c["name"] == name), None)
        bu, fu = self.run_state.get(name, (False, False))
        busy = name in self.pending
        menu = Gtk.Menu()
        hdr = Gtk.MenuItem(label=name.replace("_", "__"))
        hdr.set_sensitive(False)
        menu.append(hdr)
        menu.append(Gtk.SeparatorMenuItem())
        entries = [("Iniciar", "start", not busy and not (bu and fu)),
                   ("Reiniciar", "reload", not busy and (bu or fu)),
                   ("Detener", "stop", not busy and (bu or fu))]
        for txt, act, enabled in entries:
            mi = Gtk.MenuItem(label=txt)
            mi.set_sensitive(enabled)
            mi.connect("activate", lambda _m, a=act: self._tray_service_action(name, cfg, a))
            menu.append(mi)
        menu.append(Gtk.SeparatorMenuItem())
        for txt, side in (("Ver log del backend", "backend"), ("Ver log del frontend", "frontend")):
            mi = Gtk.MenuItem(label=txt)
            mi.set_sensitive(side == "backend" or bool(self.svc_state[name][3]))
            mi.connect("activate", lambda _m, n=name, sd=side: self._show_logs(n, sd))
            menu.append(mi)
        menu.show_all()
        menu.popup_at_pointer(event)

    def _show_logs(self, name, side):
        self.svc_list_select(name)
        (self.side_b if side == "backend" else self.side_f).set_active(True)
        self.notebook.set_current_page(1)

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
    def set_pending(self, names, text):
        def _do():
            for n in names:
                if text:
                    self.pending[n] = text
                else:
                    self.pending.pop(n, None)
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
            # Iniciar/Reiniciar/Detener solo cuando aplican al estado real del servicio.
            bu, fu = self.run_state.get(name, (False, False))
            start_b, reload_b, stop_b = ui["btns"]
            start_b.set_sensitive(not text and not (bu and fu))
            reload_b.set_sensitive(not text and (bu or fu))
            stop_b.set_sensitive(not text and (bu or fu))
        states = [self.run_state.get(n, (False, False)) for n in self.row_ui if n in (s[0] for s in SERVICES)]
        all_up = bool(states) and all(b and f for b, f in states)
        any_up = any(b or f for b, f in states)
        start_g, reload_g, stop_g = self.global_btns[::-1]  # creados como Detener, Reiniciar, Iniciar
        start_g.set_sensitive(not self.busy and not all_up)
        reload_g.set_sensitive(not self.busy and any_up)
        stop_g.set_sensitive(not self.busy and any_up)
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

    def custom_action(self, cfg, action):
        self.set_pending([cfg["name"]], self.PENDING_TEXT[action])
        threading.Thread(target=self._custom_action_tracked, args=(cfg, action), daemon=True).start()

    def _custom_action_tracked(self, cfg, action):
        try:
            self._custom_action(cfg, action)
            if action in ("start", "reload"):
                # `dotnet run` compila primero: se mantiene el indicador hasta que el backend responda.
                import time
                for _ in range(120):
                    if port_open(cfg["backend_port"]):
                        break
                    time.sleep(2)
        finally:
            self.set_pending([cfg["name"]], "")

    def _custom_action(self, cfg, action):
        name, repo, csproj = cfg["name"], Path(cfg["dir"]), cfg["csproj"]
        bport, fport = cfg["backend_port"], cfg["frontend_port"]
        if action in ("stop", "reload"):
            self.append_out(f"[{name}] deteniendo\n")
            kill_port(bport)
            if fport:
                kill_port(fport)
            run(["pkill", "-TERM", "-f", f"dotnet run --project {csproj}"])
        if action in ("start", "reload"):
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
            if fport and fe.is_dir() and not (action == "start" and port_open(fport)):
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
        self.append_out("[todo] desconectando túnel…\n")
        self._tunnel_down_any()
        if self.vpn_active():
            self.append_out("[todo] desconectando VPN…\n")
            self._vpn_down()
        self.append_out("[todo] listo\n")

    def run_services_sync(self, args):
        p = subprocess.Popen(["bash", str(SERVICES_SCRIPT), *args], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             env=script_env())
        for line in p.stdout:
            self.append_out(line)
        p.wait()

    def _tunnel_down_any(self):
        if self.tunnel_proc is not None and self.tunnel_proc.poll() is None:
            self._tunnel_down()
        elif port_open(TUNNEL_PORT):
            p = subprocess.Popen(["bash", "-c", EXTERNAL_TUNNEL_DOWN, "_", str(TUNNEL_SCRIPT)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
            for line in p.stdout:
                self.append_out(line)
            p.wait()

    # ---------- UI helpers ----------
    def _infra_card(self, parent, title, handler):
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
        parent.pack_start(card(box), True, True, 0)
        return dot, lbl, btn

    def _on_panel_toggled(self, btn):
        self.drawer.set_reveal_child(btn.get_active())

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
        return tv

    @staticmethod
    def _scroll(w):
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(w)
        return sw

    @staticmethod
    def _dot(up):
        return f'<span foreground="{"#22c55e" if up else "#6b7280"}">●</span>'

    def append_out(self, text):
        def _do():
            buf = self.out_view.get_buffer()
            follow = self.out_view.follow_btn.get_active() and self._at_bottom(self.out_view)
            buf.insert(buf.get_end_iter(), ANSI.sub("", text))
            if follow:
                self._scroll_end(self.out_view)
            return False

        GLib.idle_add(_do)

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

    def vpn_exists(self):
        r = run(["nmcli", "-t", "-f", "NAME", "connection", "show"])
        return VPN_NAME in r.stdout.splitlines()

    def refresh_status(self):
        vpn = self.vpn_active()
        self.vpn_dot.set_markup(self._dot(vpn))
        self.vpn_lbl.set_text(self.vpn_busy or ("Conectada" if vpn else "Desconectada"))
        self.vpn_btn.set_label("Desconectar" if vpn else "Conectar")
        set_variant(self.vpn_btn, None if vpn else "btn-primary")

        tun = port_open(TUNNEL_PORT)
        mine = self.tunnel_proc is not None and self.tunnel_proc.poll() is None
        if self.tunnel_proc is not None and not mine:
            self.tunnel_proc = None
        self.tun_dot.set_markup(self._dot(tun))
        if tun:
            self.tun_lbl.set_text("Activo" if mine else "Activo (levantado fuera de la app)")
        else:
            self.tun_lbl.set_text("Conectando…" if mine else "Desconectado")
        self.tun_btn.set_label("Desconectar" if tun or mine else "Conectar")
        set_variant(self.tun_btn, None if tun or mine else "btn-primary")
        self.tun_btn.set_sensitive(not self.tunnel_busy)

        # Avisos de cambios de estado cuando la ventana no está al frente (también los hechos fuera de la app).
        if self._last_infra is not None:
            if vpn != self._last_infra[0]:
                self.notify("VPN conectada" if vpn else "VPN desconectada")
            if tun != self._last_infra[1]:
                self.notify("Túnel Cloud SQL activo" if tun else "Túnel Cloud SQL desconectado")
        self._last_infra = (vpn, tun)

        for name, (b, f, port, fport) in self.svc_state.items():
            bu, fu = port_open(port), fport and port_open(fport)
            b.set_markup(self._dot(bu))
            b.set_tooltip_text(f"Backend · :{port} · {'Activo' if bu else 'Detenido'}")
            f.set_markup(self._dot(fu) if fport else "—")
            f.set_tooltip_text(f"Frontend · :{fport} · {'Activo' if fu else 'Detenido'}" if fport else "Sin frontend")
            self.run_state[name] = (bool(bu), bool(fu) or not fport)
        self.apply_pending()
        self.rail_vpn.set_markup(self._dot(vpn))
        self.rail_tun.set_markup(self._dot(tun))
        for name, rb in self.rail_btns.items():
            bu, fu = self.run_state.get(name, (False, False))
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
            dot.set_markup(self._dot(port_open(self.log_ports[key])))
        self._infra = (vpn, tun, mine)
        self.update_tray(vpn, tun, mine)
        return True

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
        """Añade solo lo nuevo del log y deja el scroll donde el usuario lo puso (sigue el final solo si ya estaba ahí)."""
        name = self.log_selected
        if not name:
            return True
        path = LOG_DIR / f"{name}.log"
        buf = self.log_view.get_buffer()
        try:
            size = path.stat().st_size
        except OSError:
            if getattr(self, "_log_name", None) != name or getattr(self, "_log_pos", None) != -1:
                buf.set_text(f"(sin log todavía: {path})")
                self._log_name, self._log_pos = name, -1
            return True

        switched = getattr(self, "_log_name", None) != name
        if switched or size < getattr(self, "_log_pos", 0) or self._log_pos == -1:
            # Log nuevo, rotado o recién creado: carga la cola completa una vez.
            start = max(0, size - self.LOG_INITIAL_BYTES)
            with open(path, "rb") as fh:
                fh.seek(start)
                data = fh.read()
            buf.set_text(ANSI.sub("", data.decode(errors="replace")))
            self._log_name, self._log_pos = name, size
            self._scroll_end(self.log_view)
            return True

        if size > self._log_pos:
            follow = self.log_view.follow_btn.get_active() and self._at_bottom(self.log_view)
            with open(path, "rb") as fh:
                fh.seek(self._log_pos)
                data = fh.read(size - self._log_pos)
            self._log_pos = size
            buf.insert(buf.get_end_iter(), ANSI.sub("", data.decode(errors="replace")))
            if follow:
                self._scroll_end(self.log_view)
        return True

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
        password, remember = self.ask_password()
        if password is None:
            return
        self.vpn_btn.set_sensitive(False)
        self.vpn_busy = "Conectando…"
        self.vpn_lbl.set_text(self.vpn_busy)
        threading.Thread(target=self._vpn_up, args=(password, remember), daemon=True).start()

    def ask_password(self):
        d = Gtk.Dialog(title="VPN Siesa", transient_for=self, modal=True)
        d.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Conectar", Gtk.ResponseType.OK)
        d.set_default_response(Gtk.ResponseType.OK)
        box = d.get_content_area()
        box.set_spacing(6)
        box.set_border_width(12)
        box.pack_start(Gtk.Label(label=f"Contraseña para {VPN_USER}", xalign=0), False, False, 0)
        entry = Gtk.Entry(visibility=False, activates_default=True)
        box.pack_start(entry, False, False, 0)
        chk = Gtk.CheckButton(label="Recordar en NetworkManager (para no pedirla otra vez)")
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

    def _vpn_up_inner(self, password, remember):
        self.append_out(f"[vpn] conectando {VPN_NAME}…\n")
        if not self.vpn_exists():
            r = run(["nmcli", "connection", "import", "type", "openvpn", "file", str(OVPN_FILE)])
            self.append_out(r.stdout + r.stderr)
            imported = re.search(r"Connection '([^']+)'", r.stdout)
            if r.returncode != 0 or not imported:
                self.append_out("[vpn] falló la importación del .ovpn\n")
                return GLib.idle_add(self.vpn_btn.set_sensitive, True)
            run(["nmcli", "connection", "modify", imported.group(1), "connection.id", VPN_NAME])
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
        self.append_out(r.stdout + r.stderr or "")
        self.append_out("[vpn] conectada\n" if r.returncode == 0 else "[vpn] no se pudo conectar\n")
        GLib.idle_add(self.vpn_btn.set_sensitive, True)
        GLib.idle_add(self.refresh_status)

    def _vpn_down(self):
        r = run(["nmcli", "connection", "down", "id", VPN_NAME])
        self.vpn_busy = ""
        self.append_out(r.stdout + r.stderr)
        GLib.idle_add(self.vpn_btn.set_sensitive, True)
        GLib.idle_add(self.refresh_status)

    # ---------- Túnel ----------
    def on_tunnel(self, _btn):
        if port_open(TUNNEL_PORT) or (self.tunnel_proc is not None and self.tunnel_proc.poll() is None):
            self.append_out("[túnel] desconectando (borra pod, remueve la IP de MAN)…\n")
            self.tunnel_busy = True
            self.tun_btn.set_sensitive(False)
            threading.Thread(target=self._tunnel_down_ui, daemon=True).start()
            return
        acct = run(["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"])
        if not acct.stdout.strip():
            self.message("Sin sesión de gcloud",
                         "Corre en una terminal:  gcloud auth login --no-launch-browser")
            return
        self.append_out("[túnel] conectando…\n")
        self.tunnel_proc = subprocess.Popen(
            ["bash", str(TUNNEL_SCRIPT)], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        threading.Thread(target=self._pump, args=(self.tunnel_proc,), daemon=True).start()

    def _pump(self, proc):
        for line in proc.stdout:
            self.append_out(line)
        self.append_out("[túnel] proceso terminado\n")

    def _tunnel_down_ui(self):
        try:
            self._tunnel_down_any()
        finally:
            self.tunnel_busy = False
            GLib.idle_add(self.refresh_status)

    def _tunnel_down(self):
        proc = self.tunnel_proc
        # SIGTERM solo al bash: su trap EXIT hace la limpieza (pod, watchdog, IP en MAN).
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            self.append_out("[túnel] la limpieza tardó demasiado; revisa MAN y el pod a mano\n")
        GLib.idle_add(self.refresh_status)

    # ---------- Servicios ----------
    def run_services(self, args):
        if self.busy:
            self.append_out("[servicios] ya hay una acción en curso\n")
            return
        self.busy = True
        names = [a for a in args if not a.startswith("--")] or [n for n, _ in SERVICES]
        verb = "stop" if "--stop" in args else "reload" if "--reload" in args else "start"
        self._pending_names = names
        self.set_pending(names, self.PENDING_TEXT[verb])
        self.append_out(f"\n$ start-services.sh {' '.join(args)}\n")
        threading.Thread(target=self._run_services, args=(args,), daemon=True).start()

    def _run_services(self, args):
        try:
            p = subprocess.Popen(["bash", str(SERVICES_SCRIPT), *args], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 env=script_env())
            for line in p.stdout:
                self.append_out(line)
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


if __name__ == "__main__":
    GLib.set_prgname(APP_ID)
    sys.exit(App().run(sys.argv))
