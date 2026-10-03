import os
import json
import subprocess
import shutil
import shlex
import tempfile
import zipfile
import re
import html as html_lib
import requests
import uvicorn

from fastapi import (
    FastAPI, Form, HTTPException, UploadFile, File,
    Header, Query
)
from fastapi.responses import (
    HTMLResponse, RedirectResponse, FileResponse,
    JSONResponse
)
from starlette.background import BackgroundTask

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print(
        "[WARNUNG] 'psutil' ist nicht installiert - "
        "RAM-Anzeige ist deaktiviert. "
        "Installieren mit: pip install psutil"
    )


app = FastAPI()


# ============================================================
# KONFIGURATION
# ============================================================

VELOCITY_API_KEY = ""
REFRESH_INTERVAL = 3


# ============================================================
# HAUPTPFADE
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVERS_DIR = os.path.join(BASE_DIR, "servers")
USERS_FILE = os.path.join(BASE_DIR, "users.json")

os.makedirs(SERVERS_DIR, exist_ok=True)

SERVER_JAR_NAME = "server.jar"


# ============================================================
# JSON-HILFSFUNKTIONEN
# ============================================================

def load_users() -> list:
    if not os.path.exists(USERS_FILE):
        return []
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("users", [])
    except (json.JSONDecodeError, OSError):
        return []


def save_users(users: list):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump({"users": users}, f, indent=2, ensure_ascii=False)


def server_dir(server_id: int) -> str:
    return os.path.join(SERVERS_DIR, str(server_id))


def info_file(server_id: int) -> str:
    return os.path.join(server_dir(server_id), "info.json")


def load_server_info(server_id: int):
    path = info_file(server_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_server_info(server_id: int, info: dict):
    os.makedirs(server_dir(server_id), exist_ok=True)
    info["id"] = server_id
    with open(info_file(server_id), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)


def list_server_ids():
    result = []
    if not os.path.isdir(SERVERS_DIR):
        return result
    for name in os.listdir(SERVERS_DIR):
        full = os.path.join(SERVERS_DIR, name)
        if not os.path.isdir(full):
            continue
        try:
            sid = int(name)
        except ValueError:
            continue
        if os.path.isfile(os.path.join(full, "info.json")):
            result.append(sid)
    return sorted(result)


def list_servers():
    out = []
    for sid in list_server_ids():
        info = load_server_info(sid)
        if info:
            out.append(info)
    return out


def next_free_id() -> int:
    ids = list_server_ids()
    return (max(ids) + 1) if ids else 1


def get_server_by_name(name):
    """
    Sucht einen Server anhand seines Namens.
    Groß-/Kleinschreibung wird ignoriert, Leerzeichen am
    Anfang/Ende werden entfernt.
    """
    if name is None:
        return None
    name = name.strip()
    if not name:
        return None

    name_lower = name.lower()

    for sid in list_server_ids():
        info = load_server_info(sid)
        if info and info.get("name", "").strip().lower() == name_lower:
            return info
    return None


def delete_server_files(server_id: int):
    path = server_dir(server_id)
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)


# ============================================================
# MIGRATION von data.db (einmalig)
# ============================================================

def migrate_from_sqlite_if_needed():
    db_path = os.path.join(BASE_DIR, "data.db")
    if not os.path.exists(db_path):
        return
    if list_server_ids() or os.path.exists(USERS_FILE):
        return

    try:
        import sqlite3
    except ImportError:
        return

    print("[INFO] Migriere aus data.db nach JSON...")
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()

        try:
            c.execute("""
                SELECT id, name, ram, port, online_mode, start_command
                FROM servers
            """)
            for row in c.fetchall():
                sid, name, ram, port, online_mode, start_command = row
                info = {
                    "name": name or f"Server {sid}",
                    "ram": ram or 2,
                    "port": port or 25565,
                    "online_mode": bool(online_mode),
                    "start_command": start_command or ""
                }
                save_server_info(sid, info)
                print(f"  -> Server #{sid}: {info['name']}")
        except Exception as e:
            print(f"  [WARN] Server-Migration: {e}")

        try:
            c.execute("SELECT id, username, password FROM users")
            users = []
            for row in c.fetchall():
                uid, username, password = row
                users.append({
                    "id": uid,
                    "username": username,
                    "password": password
                })
            if users:
                save_users(users)
                print(f"  -> {len(users)} User migriert")
        except Exception as e:
            print(f"  [WARN] User-Migration: {e}")

        conn.close()
        print("[INFO] Migration abgeschlossen.")
    except Exception as e:
        print(f"[WARN] Migration fehlgeschlagen: {e}")


migrate_from_sqlite_if_needed()


processes = {}


# ============================================================
# SICHERHEIT / PFADE
# ============================================================

def is_safe_path(base_dir: str, target_path: str) -> bool:
    abs_base = os.path.abspath(base_dir)
    abs_target = os.path.abspath(target_path)
    return (
        abs_target == abs_base
        or abs_target.startswith(abs_base + os.sep)
    )


# ============================================================
# RAM-FUNKTIONEN
# ============================================================

def get_process_ram_gb(pid: int):
    if not PSUTIL_AVAILABLE:
        return None
    try:
        p = psutil.Process(pid)
        total_bytes = p.memory_info().rss
        for child in p.children(recursive=True):
            try:
                total_bytes += child.memory_info().rss
            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess
            ):
                pass
        return total_bytes / (1024 ** 3)
    except (
        psutil.NoSuchProcess,
        psutil.AccessDenied,
        psutil.ZombieProcess
    ):
        return None


def get_host_ram_usage_gb():
    if not PSUTIL_AVAILABLE:
        return None, None
    vm = psutil.virtual_memory()
    return (vm.total - vm.available) / (1024 ** 3), vm.total / (1024 ** 3)


def get_other_processes_ram(exclude_pids: set, top_n: int = 10):
    if not PSUTIL_AVAILABLE:
        return []
    usage_by_name = {}
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            pid = proc.info["pid"]
            if pid in exclude_pids:
                continue
            name = proc.info["name"] or f"PID {pid}"
            mem = proc.memory_info().rss
            usage_by_name[name] = usage_by_name.get(name, 0) + mem
        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess
        ):
            continue
    sorted_items = sorted(
        usage_by_name.items(), key=lambda x: x[1], reverse=True
    )
    result = [
        (name, size / (1024 ** 3))
        for name, size in sorted_items
        if size > 0
    ]
    if len(result) > top_n:
        rest_gb = sum(gb for _, gb in result[top_n:])
        result = result[:top_n] + [("Weitere Prozesse", rest_gb)]
    return result


def get_managed_pids() -> set:
    pids = set()
    if not PSUTIL_AVAILABLE:
        for proc in processes.values():
            if proc.poll() is None:
                pids.add(proc.pid)
        return pids
    for proc in processes.values():
        if proc.poll() is None:
            try:
                p = psutil.Process(proc.pid)
                pids.add(p.pid)
                for child in p.children(recursive=True):
                    pids.add(child.pid)
            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess
            ):
                pass
    return pids


# ============================================================
# ANSI → HTML
# ============================================================

ANSI_FG = {
    30: "#000000", 31: "#f38ba8", 32: "#a6e3a1",
    33: "#f9e2af", 34: "#89b4fa", 35: "#cba6f7",
    36: "#94e2d5", 37: "#cdd6f4",
    90: "#585b70", 91: "#f38ba8", 92: "#a6e3a1",
    93: "#f9e2af", 94: "#89b4fa", 95: "#cba6f7",
    96: "#94e2d5", 97: "#ffffff",
}

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")


def ansi_to_html(text: str) -> str:
    text = text.replace("\r", "")
    text = html_lib.escape(text)
    out = []
    current_color = None
    pos = 0
    for match in ANSI_RE.finditer(text):
        chunk = text[pos:match.start()]
        if chunk:
            if current_color:
                out.append(
                    f'<span style="color:{current_color};">{chunk}</span>'
                )
            else:
                out.append(chunk)
        codes = match.group(1)
        if codes in ("", "0"):
            current_color = None
        else:
            for part in codes.split(";"):
                if not part:
                    continue
                try:
                    code = int(part)
                except ValueError:
                    continue
                if code == 0:
                    current_color = None
                elif code in ANSI_FG:
                    current_color = ANSI_FG[code]
        pos = match.end()
    rest = text[pos:]
    if rest:
        if current_color:
            out.append(
                f'<span style="color:{current_color};">{rest}</span>'
            )
        else:
            out.append(rest)
    return "".join(out)


def colorize_plain_log_line(line: str) -> str:
    escaped = html_lib.escape(line)
    if re.search(r"\[(ERROR|FATAL|SEVERE)\]", escaped):
        return f'<span style="color:#f38ba8;">{escaped}</span>'
    if re.search(r"\[(WARN|WARNING)\]", escaped):
        return f'<span style="color:#f9e2af;">{escaped}</span>'
    if re.search(r"\[INFO\]", escaped):
        return f'<span style="color:#cdd6f4;">{escaped}</span>'
    if re.search(r"\[(DEBUG|TRACE)\]", escaped):
        return f'<span style="color:#6c7086;">{escaped}</span>'
    if re.search(r"(Done \(|Starting minecraft server)", escaped):
        return f'<span style="color:#a6e3a1;">{escaped}</span>'
    return f'<span style="color:#cdd6f4;">{escaped}</span>'


def render_log_html(lines) -> str:
    out = []
    for raw in lines:
        line = raw.rstrip("\n")
        if "\x1b[" in line:
            out.append(ansi_to_html(line))
        else:
            out.append(colorize_plain_log_line(line))
    return "<br>".join(out)


# ============================================================
# ZIP
# ============================================================

def create_zip_from_dir(source_dir: str, zip_path: str):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(source_dir):
            for file in files:
                full_path = os.path.join(root, file)
                arcname = os.path.relpath(full_path, source_dir)
                zf.write(full_path, arcname)


# ============================================================
# STATUS-API
# ============================================================

@app.get("/api/status")
async def api_status():
    result = []
    for info in list_servers():
        sid = info["id"]
        is_running = sid in processes and processes[sid].poll() is None
        used_gb = None
        if is_running:
            used_gb = get_process_ram_gb(processes[sid].pid)
        result.append({
            "id": sid,
            "name": info.get("name", f"Server {sid}"),
            "ram": info.get("ram", 2),
            "port": info.get("port", 25565),
            "running": is_running,
            "used_gb": round(used_gb, 2) if used_gb is not None else None,
        })
    return JSONResponse(result)


@app.post("/clear-cache")
async def clear_cache():
    return JSONResponse({"ok": True})


# ============================================================
# DASHBOARD
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    if not load_users():
        return RedirectResponse(url="/setup")

    servers = list_servers()

    used_ports = {s.get("port", 0) for s in servers}
    suggested_port = 25565
    while suggested_port in used_ports and suggested_port < 65535:
        suggested_port += 1

    host_used_gb, host_total_gb = get_host_ram_usage_gb()
    breakdown_rows_html = ""
    minecraft_ram_total = 0.0

    for info in servers:
        sid = info["id"]
        name = info.get("name", f"Server {sid}")
        running = sid in processes and processes[sid].poll() is None
        if running:
            used = get_process_ram_gb(processes[sid].pid)
            if used is not None:
                minecraft_ram_total += used
                breakdown_rows_html += (
                    f"<tr><td>{name}</td>"
                    f"<td>{used:.2f} GB</td></tr>"
                )
            else:
                breakdown_rows_html += (
                    f"<tr><td>{name}</td>"
                    f"<td>nicht verfügbar</td></tr>"
                )

    process_ram_total = 0.0
    managed_pids = get_managed_pids()
    dashboard_pid = os.getpid()
    managed_pids.add(dashboard_pid)

    for proc_name, proc_gb in get_other_processes_ram(managed_pids):
        process_ram_total += proc_gb
        breakdown_rows_html += (
            f"<tr><td>{proc_name}</td>"
            f"<td>{proc_gb:.2f} GB</td></tr>"
        )

    dashboard_ram = get_process_ram_gb(dashboard_pid) or 0.0

    if host_used_gb is not None:
        accounted_ram = (
            minecraft_ram_total + process_ram_total + dashboard_ram
        )
        unaccounted_ram = max(0.0, host_used_gb - accounted_ram)
        if unaccounted_ram > 0.01:
            breakdown_rows_html += (
                f"<tr><td>Linux / Cache / Kernel / sonstiger RAM</td>"
                f"<td>{unaccounted_ram:.2f} GB</td></tr>"
            )

    breakdown_rows_html += (
        f"<tr><td>Minecraft-Dashboard (dieses Programm)</td>"
        f"<td>{dashboard_ram:.2f} GB</td></tr>"
    )

    if not breakdown_rows_html:
        breakdown_rows_html = (
            "<tr><td colspan='2'>Keine RAM-Daten verfügbar.</td></tr>"
        )

    if host_used_gb is not None:
        pct = (
            min(100, (host_used_gb / host_total_gb) * 100)
            if host_total_gb else 0
        )
        ram_banner_html = f"""
        <div class="ram-banner" onclick="openHostRamModal()"
             style="cursor:pointer;" title="Klicken für Details">
            🖥️ RAM-Auslastung des Hosts:
            {host_used_gb:.1f} GB / {host_total_gb:.1f} GB
            <div class="ram-bar-track">
                <div class="ram-bar-fill" style="width:{pct:.0f}%;"></div>
            </div>
        </div>

        <div id="host-ram-modal" class="modal-overlay">
            <div class="modal-box">
                <h3>Wofür wird der RAM verwendet?</h3>
                <table style="width:100%;border-collapse:collapse;">
                    <tr>
                        <th style="text-align:left;padding:4px;">
                            Server / Prozess
                        </th>
                        <th style="text-align:left;padding:4px;">
                            RAM-Nutzung
                        </th>
                    </tr>
                    {breakdown_rows_html}
                </table>
                <div class="modal-actions">
                    <button type="button"
                            onclick="closeHostRamModal()"
                            class="stop">Schließen</button>
                </div>
            </div>
        </div>
        """
    else:
        ram_banner_html = """
        <div class="ram-banner" style="color:#f38ba8;">
            🖥️ RAM-Auslastung des Hosts: nicht verfügbar
            (psutil nicht installiert - pip install psutil)
        </div>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Minecraft Dashboard</title>
        <meta charset="utf-8">
        <style>
            body {{
                font-family: Arial, sans-serif;
                background: #1e1e2e;
                color: #cdd6f4;
                margin: 20px;
                padding-top: 50px;
            }}
            .card {{
                background: #313244;
                padding: 15px;
                margin-bottom: 10px;
                border-radius: 8px;
            }}
            button, a.btn {{
                background: #89b4fa;
                border: none;
                padding: 8px 12px;
                color: #11111b;
                cursor: pointer;
                border-radius: 4px;
                font-weight: bold;
                text-decoration: none;
                display: inline-block;
            }}
            button.stop {{ background: #f38ba8; }}
            button.clear {{ background: #fab387; }}
            button.delete-server {{ background: #f38ba8; }}
            a.btn-files {{ background: #a6e3a1; color: #11111b; }}
            input, select, textarea {{
                padding: 8px;
                margin: 5px 0;
                border-radius: 4px;
                border: none;
            }}
            iframe {{
                width: 100%;
                height: 260px;
                background: #11111b;
                color: #a6e3a1;
                border: none;
                margin-top: 10px;
                border-radius: 4px;
            }}
            .error {{ color: #f38ba8; font-weight: bold; }}
            .cmd-box {{ margin-top: 8px; }}
            .cmd-box input[type="text"] {{ width: 70%; }}
            .jar-info {{ color: #94e2d5; font-size: 0.9em; }}
            label.inline {{
                display: inline-flex;
                align-items: center;
                gap: 6px;
                margin: 5px 10px 5px 0;
            }}
            .ram-banner {{
                background: #45475a;
                padding: 12px 15px;
                border-radius: 8px;
                margin-bottom: 15px;
                font-weight: bold;
            }}
            .ram-bar-track {{
                background: #11111b;
                border-radius: 4px;
                height: 8px;
                margin-top: 6px;
                overflow: hidden;
            }}
            .ram-bar-fill {{
                background: #89b4fa;
                height: 100%;
                transition: width 0.4s;
            }}
            .modal-overlay {{
                display: none;
                position: fixed;
                top: 0; left: 0;
                width: 100%; height: 100%;
                background: rgba(0,0,0,0.6);
                align-items: center;
                justify-content: center;
                z-index: 1000;
            }}
            .modal-box {{
                background: #313244;
                padding: 20px;
                border-radius: 8px;
                min-width: 260px;
                max-width: 90vw;
                max-height: 80vh;
                overflow-y: auto;
            }}
            .modal-box h3 {{ margin-top: 0; }}
            .modal-actions {{
                display: flex;
                gap: 8px;
                margin-top: 10px;
            }}
            #cache-btn {{
                position: fixed;
                top: 10px;
                right: 10px;
                z-index: 2000;
                background: #f9e2af;
                color: #11111b;
                border: none;
                padding: 8px 14px;
                border-radius: 6px;
                font-weight: bold;
                cursor: pointer;
                box-shadow: 0 2px 6px rgba(0,0,0,0.4);
            }}
            #cache-btn:hover {{ background: #fab387; }}
            .status-running {{ color: #a6e3a1; font-weight: bold; }}
            .status-stopped {{ color: #f38ba8; font-weight: bold; }}
        </style>
        <script>
            function openCommandModal(id) {{
                document.getElementById('command-modal-' + id)
                    .style.display = 'flex';
            }}
            function closeCommandModal(id) {{
                document.getElementById('command-modal-' + id)
                    .style.display = 'none';
            }}
            function openHostRamModal() {{
                var el = document.getElementById('host-ram-modal');
                if (el) el.style.display = 'flex';
            }}
            function closeHostRamModal() {{
                var el = document.getElementById('host-ram-modal');
                if (el) el.style.display = 'none';
            }}

            async function clearCacheAndReload() {{
                try {{
                    if ('caches' in window) {{
                        const keys = await caches.keys();
                        await Promise.all(keys.map(k => caches.delete(k)));
                    }}
                }} catch (e) {{}}
                try {{
                    sessionStorage.clear();
                    localStorage.clear();
                }} catch (e) {{}}
                try {{
                    await fetch('/clear-cache', {{ method: 'POST' }});
                }} catch (e) {{}}
                const url = new URL(window.location.href);
                url.searchParams.set('_', Date.now().toString());
                window.location.replace(url.toString());
            }}

            async function refreshStatus() {{
                try {{
                    const res = await fetch('/api/status', {{
                        cache: 'no-store'
                    }});
                    if (!res.ok) return;
                    const data = await res.json();
                    for (const s of data) {{
                        const statusEl = document.getElementById('status-' + s.id);
                        if (statusEl) {{
                            if (s.running) {{
                                statusEl.textContent = 'LÄUFT';
                                statusEl.className = 'status-running';
                            }} else {{
                                statusEl.textContent = 'GESTOPPT';
                                statusEl.className = 'status-stopped';
                            }}
                        }}
                        const ramEl = document.getElementById('ram-' + s.id);
                        const barEl = document.getElementById('rambar-' + s.id);
                        if (ramEl && barEl) {{
                            if (s.running && s.used_gb !== null) {{
                                const pct = Math.min(100, (s.used_gb / s.ram) * 100);
                                ramEl.textContent =
                                    s.used_gb.toFixed(2) + ' GB / ' +
                                    s.ram + ' GB';
                                barEl.style.width = pct + '%';
                            }} else if (s.running) {{
                                ramEl.textContent =
                                    'nicht verfügbar / ' +
                                    s.ram + ' GB (zugewiesen)';
                                barEl.style.width = '0%';
                            }} else {{
                                ramEl.textContent =
                                    '0 GB / ' + s.ram + ' GB (gestoppt)';
                                barEl.style.width = '0%';
                            }}
                        }}
                    }}
                }} catch (e) {{}}
            }}

            function refreshConsoles() {{
                document.querySelectorAll('iframe.console-frame')
                    .forEach(function (f) {{
                        const base = f.getAttribute('data-src');
                        if (!base) return;
                        const sep = base.includes('?') ? '&' : '?';
                        f.src = base + sep + '_=' + Date.now();
                    }});
            }}

            document.addEventListener('DOMContentLoaded', function () {{
                refreshStatus();
                setInterval(refreshStatus, {REFRESH_INTERVAL * 1000});
                setInterval(refreshConsoles, {REFRESH_INTERVAL * 1000});
            }});
        </script>
    </head>
    <body>

        <button id="cache-btn" onclick="clearCacheAndReload()">
            🧹 Cache leeren
        </button>

        <h1>Minecraft Server Manager</h1>

        {ram_banner_html}

        <div class="card">
            <h2>Neuen Server erstellen</h2>
            <form action="/create-server" method="post">
                <input type="text" name="name"
                       placeholder="Server Name" required>
                <br>
                <input type="number" name="ram"
                       placeholder="RAM in GB" value="2"
                       min="1" required>
                <br>
                <input type="number" name="port"
                       placeholder="Port" value="{suggested_port}"
                       min="1" max="65535" required>
                <br>
                <label class="inline">
                    <input type="checkbox" name="online_mode"
                           value="on" checked style="width:auto;">
                    Online-Mode (Mojang-Accountprüfung)
                </label>
                <br>
                <button type="submit">Server Erstellen</button>
            </form>
            <p class="jar-info">
                Nach dem Erstellen bitte die Server-JAR manuell über
                den Datei-Manager hochladen. Sie muss exakt
                <strong>{SERVER_JAR_NAME}</strong> heißen.
            </p>
        </div>

        <h2>Deine Server</h2>
    """

    for info in servers:
        sid = info["id"]
        name = info.get("name", f"Server {sid}")
        ram = info.get("ram", 2)
        port = info.get("port", 25565)
        online_mode = info.get("online_mode", True)
        start_command = info.get("start_command", "")

        is_running = sid in processes and processes[sid].poll() is None
        status_text = "LÄUFT" if is_running else "GESTOPPT"
        status_class = "status-running" if is_running else "status-stopped"

        sdir = server_dir(sid)
        jar_path = os.path.join(sdir, SERVER_JAR_NAME)
        jar_exists = os.path.exists(jar_path)

        if not start_command:
            start_command = (
                f"java -Xms{ram}G -Xmx{ram}G "
                f"-jar {SERVER_JAR_NAME} nogui"
            )

        start_command_escaped = (
            start_command
            .replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

        if is_running:
            used_gb = get_process_ram_gb(processes[sid].pid)
            if used_gb is not None:
                pct = min(100, (used_gb / ram) * 100) if ram else 0
                ram_usage_html = f"""
                    <span id="ram-{sid}">
                        {used_gb:.2f} GB / {ram} GB
                    </span>
                    <div class="ram-bar-track" style="max-width:220px;">
                        <div id="rambar-{sid}"
                             class="ram-bar-fill"
                             style="width:{pct:.0f}%;"></div>
                    </div>
                """
            else:
                ram_usage_html = (
                    f'<span id="ram-{sid}">nicht verfügbar / '
                    f'{ram} GB (zugewiesen)</span>'
                    f'<div class="ram-bar-track" style="max-width:220px;">'
                    f'<div id="rambar-{sid}" class="ram-bar-fill" '
                    f'style="width:0%;"></div></div>'
                )
        else:
            ram_usage_html = (
                f'<span id="ram-{sid}">0 GB / {ram} GB (gestoppt)</span>'
                f'<div class="ram-bar-track" style="max-width:220px;">'
                f'<div id="rambar-{sid}" class="ram-bar-fill" '
                f'style="width:0%;"></div></div>'
            )

        html += f"""
        <div class="card">
            <h3>
                {name} - Status:
                <span id="status-{sid}" class="{status_class}">
                    {status_text}
                </span>
            </h3>

            <p>
                Port: {port} |
                Online-Mode: {"An" if online_mode else "Aus"}
            </p>

            <p>{ram_usage_html}</p>

            <p style="font-size:0.85em;color:#a6adc8;
                      word-break:break-all;">
                Startbefehl:
                {start_command_escaped}
                <button type="button"
                        onclick="openCommandModal({sid})"
                        style="margin-left:10px;padding:4px 8px;
                               font-size:0.85em;">
                    Befehl ändern
                </button>
            </p>

            {
                "<p class='error'>WARNUNG: "
                + SERVER_JAR_NAME
                + " fehlt! Bitte im Datei-Manager hochladen.</p>"
                if not jar_exists else ""
            }

            <div id="command-modal-{sid}" class="modal-overlay">
                <div class="modal-box">
                    <h3>Startbefehl ändern - {name}</h3>
                    <form action="/edit-command/{sid}" method="post">
                        <label>Befehl zum Starten des Servers:</label>
                        <textarea name="start_command" rows="3"
                            style="width:100%;box-sizing:border-box;
                                   font-family:monospace;padding:8px;
                                   border-radius:4px;border:none;"
                            required>{start_command_escaped}</textarea>
                        <div class="modal-actions">
                            <button type="submit">Speichern</button>
                            <button type="button"
                                    onclick="closeCommandModal({sid})"
                                    class="stop">Abbrechen</button>
                        </div>
                        <p style="color:#a6adc8;font-size:0.8em;">
                            Der Befehl wird im Server-Ordner ausgeführt.
                            "{SERVER_JAR_NAME}" bezieht sich auf die
                            dort liegende Datei.
                        </p>
                        {
                            "<p style='color:#fab387;font-size:0.85em;"
                            "margin-bottom:0;'>Wirkt erst nach einem "
                            "Neustart des Servers.</p>"
                            if is_running else ""
                        }
                    </form>
                </div>
            </div>

            <form action="/action/{sid}" method="post"
                  style="display:inline;">
                <input type="hidden" name="action_type" value="start">
                <button type="submit">Starten</button>
            </form>

            <form action="/action/{sid}" method="post"
                  style="display:inline;">
                <input type="hidden" name="action_type" value="stop">
                <button type="submit" class="stop">Stoppen</button>
            </form>

            <form action="/clear-log/{sid}" method="post"
                  style="display:inline;">
                <button type="submit" class="clear">Log Leeren</button>
            </form>

            <form action="/delete-server/{sid}" method="post"
                  style="display:inline;">
                <button type="submit" class="delete-server"
                    onclick="return confirm(
                        'WARNUNG: Server \\'{name}\\' wird '
                        + 'unwiderruflich gelöscht (inkl. aller '
                        + 'Welten, Konfigurationen und Dateien '
                        + 'im Server-Ordner). Dieser Vorgang kann '
                        + 'nicht rückgängig gemacht werden. '
                        + 'Wirklich fortfahren?'
                    )">
                    Server Löschen
                </button>
            </form>

            <a href="/files/{sid}" class="btn btn-files">
                Datei-Manager
            </a>

            <h4>Konsole:</h4>

            <iframe class="console-frame"
                    data-src="/console/{sid}"
                    src="/console/{sid}"></iframe>

            <div class="cmd-box">
                <form action="/command/{sid}" method="post">
                    <input type="text" name="command"
                           placeholder="Befehl eingeben" required>
                    <button type="submit">Senden</button>
                </form>
            </div>
        </div>
        """

    html += f"""
    <p style="color:#6c7086;font-size:0.85em;margin-top:30px;">
        Auto-Refresh aktiv (Status &amp; Konsole alle
        {REFRESH_INTERVAL} Sekunden).
        Velocity-API:
        <code>/velocity/&lt;servername&gt;/&lt;action&gt;</code>
    </p>
    </body></html>
    """

    return html


# ============================================================
# KONSOLENBEFEHLE
# ============================================================

@app.post("/command/{server_id}")
async def send_command(server_id: int, command: str = Form(...)):
    if server_id in processes and processes[server_id].poll() is None:
        process = processes[server_id]
        if process.stdin:
            process.stdin.write(f"{command}\n")
            process.stdin.flush()
    return RedirectResponse(url="/", status_code=303)


@app.post("/clear-log/{server_id}")
async def clear_log(server_id: int):
    log_path = os.path.join(
        server_dir(server_id), "server.log"
    )
    if os.path.exists(log_path):
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("")
    return RedirectResponse(url="/", status_code=303)


@app.post("/delete-server/{server_id}")
async def delete_server(server_id: int):
    info = load_server_info(server_id)
    if not info:
        raise HTTPException(status_code=404, detail="Server nicht gefunden")

    if server_id in processes and processes[server_id].poll() is None:
        process = processes[server_id]
        try:
            if process.stdin:
                process.stdin.write("stop\n")
                process.stdin.flush()
            process.wait(timeout=10)
        except Exception:
            process.terminate()
        finally:
            processes.pop(server_id, None)

    delete_server_files(server_id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/edit-command/{server_id}")
async def edit_command(server_id: int, start_command: str = Form(...)):
    start_command = start_command.strip()
    if not start_command:
        raise HTTPException(
            status_code=400,
            detail="Startbefehl darf nicht leer sein."
        )

    info = load_server_info(server_id)
    if not info:
        raise HTTPException(status_code=404, detail="Server nicht gefunden")

    info["start_command"] = start_command
    save_server_info(server_id, info)

    return RedirectResponse(url="/", status_code=303)


@app.post("/create-server")
async def create_server(
    name: str = Form(...),
    ram: int = Form(...),
    port: int = Form(...),
    online_mode: str = Form("")
):
    name = name.strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail="Name darf nicht leer sein."
        )

    if ram < 1:
        raise HTTPException(
            status_code=400,
            detail="RAM muss mindestens 1 GB sein."
        )

    if port < 1 or port > 65535:
        raise HTTPException(
            status_code=400,
            detail="Port muss zwischen 1 und 65535 liegen."
        )

    for info in list_servers():
        if info.get("port") == port:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Port {port} wird bereits von einem anderen "
                    "Server verwendet."
                )
            )

    if get_server_by_name(name):
        raise HTTPException(
            status_code=400,
            detail=f"Ein Server namens '{name}' existiert bereits."
        )

    sid = next_free_id()
    online_mode_bool = online_mode == "on"

    default_start_command = (
        f"java -Xms{ram}G -Xmx{ram}G "
        f"-jar {SERVER_JAR_NAME} nogui"
    )

    info = {
        "id": sid,
        "name": name,
        "ram": ram,
        "port": port,
        "online_mode": online_mode_bool,
        "start_command": default_start_command
    }
    save_server_info(sid, info)

    sdir = server_dir(sid)
    os.makedirs(sdir, exist_ok=True)

    with open(os.path.join(sdir, "eula.txt"), "w") as f:
        f.write("eula=true\n")

    with open(os.path.join(sdir, "server.properties"), "w") as f:
        f.write(f"server-port={port}\n")
        f.write(
            "online-mode="
            f"{'true' if online_mode_bool else 'false'}\n"
        )

    return RedirectResponse(url="/", status_code=303)


@app.post("/action/{server_id}")
async def server_action(
    server_id: int,
    action_type: str = Form(...)
):
    info = load_server_info(server_id)
    if not info:
        raise HTTPException(status_code=404, detail="Server nicht gefunden")

    ram = info.get("ram", 2)
    start_command = info.get("start_command", "")

    if not start_command:
        start_command = (
            f"java -Xms{ram}G -Xmx{ram}G "
            f"-jar {SERVER_JAR_NAME} nogui"
        )

    sdir = server_dir(server_id)
    jar_path = os.path.join(sdir, SERVER_JAR_NAME)

    if action_type == "start":
        is_running = server_id in processes and processes[server_id].poll() is None
        if not is_running:
            if not os.path.exists(jar_path):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{SERVER_JAR_NAME} fehlt. Bitte im "
                        "Datei-Manager hochladen."
                    )
                )
            try:
                args = shlex.split(start_command)
            except ValueError as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Startbefehl ist ungültig: {e}"
                )
            if not args:
                raise HTTPException(
                    status_code=400,
                    detail="Startbefehl ist leer."
                )
            log_file = open(
                os.path.join(sdir, "server.log"),
                "a", encoding="utf-8"
            )
            processes[server_id] = subprocess.Popen(
                args,
                cwd=sdir,
                stdin=subprocess.PIPE,
                stdout=log_file,
                stderr=log_file,
                text=True,
                bufsize=1
            )

    elif action_type == "stop":
        if server_id in processes and processes[server_id].poll() is None:
            process = processes[server_id]
            if process.stdin:
                try:
                    process.stdin.write("stop\n")
                    process.stdin.flush()
                except Exception:
                    process.terminate()
            else:
                process.terminate()

    return RedirectResponse(url="/", status_code=303)


@app.get("/console/{server_id}", response_class=HTMLResponse)
async def console(server_id: int):
    log_path = os.path.join(server_dir(server_id), "server.log")
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-60:]
        body = render_log_html(lines)
    else:
        body = (
            '<span style="color:#6c7086;">Kein Log verfügbar.</span>'
        )

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta http-equiv="refresh" content="{REFRESH_INTERVAL}">
        <style>
            html, body {{
                margin: 0;
                padding: 8px;
                background: #11111b;
                color: #cdd6f4;
                font-family: 'Consolas', 'Menlo', monospace;
                font-size: 13px;
                line-height: 1.35;
            }}
            #log {{
                white-space: pre-wrap;
                word-break: break-word;
            }}
        </style>
        <script>
            window.addEventListener('load', function () {{
                window.scrollTo(0, document.body.scrollHeight);
            }});
        </script>
    </head>
    <body>
        <div id="log">{body}</div>
    </body>
    </html>
    """


# ============================================================
# DATEI-MANAGER
# ============================================================

@app.get("/files/{server_id}", response_class=HTMLResponse)
async def list_files(server_id: int, subpath: str = ""):
    sdir = server_dir(server_id)
    current_dir = os.path.join(sdir, subpath)

    if (
        not is_safe_path(sdir, current_dir)
        or not os.path.exists(current_dir)
    ):
        raise HTTPException(status_code=400, detail="Ungültiger Pfad.")

    items = os.listdir(current_dir)
    dirs = []
    files = []

    for item in items:
        full_item_path = os.path.join(current_dir, item)
        rel_item_path = os.path.join(subpath, item).replace("\\", "/")
        if os.path.isdir(full_item_path):
            dirs.append((item, rel_item_path))
        else:
            files.append((item, rel_item_path))

    parent_path = (
        "/".join(subpath.strip("/").split("/")[:-1])
        if subpath else None
    )

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Datei-Manager - Server #{server_id}</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                background: #1e1e2e;
                color: #cdd6f4;
                margin: 20px;
            }}
            .card {{
                background: #313244;
                padding: 15px;
                margin-bottom: 10px;
                border-radius: 8px;
            }}
            a {{ color: #89b4fa; text-decoration: none; font-weight: bold; }}
            a:hover {{ text-decoration: underline; }}
            button, .btn {{
                background: #89b4fa;
                border: none;
                padding: 6px 10px;
                color: #11111b;
                cursor: pointer;
                border-radius: 4px;
                font-weight: bold;
                text-decoration: none;
            }}
            .btn-delete {{ background: #f38ba8; }}
            .btn-download {{ background: #a6e3a1; }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 10px;
            }}
            th, td {{
                text-align: left;
                padding: 8px;
                border-bottom: 1px solid #45475a;
            }}
            input {{
                padding: 6px;
                border-radius: 4px;
                border: none;
            }}
            .row-actions form {{ display: inline; }}
        </style>
    </head>
    <body>
        <a href="/" class="btn">&larr; Zurück zum Dashboard</a>
        <h2>Datei-Manager: / {subpath}</h2>

        <div class="card">
            <h3>Erstellen & Hochladen</h3>
            <form action="/files/{server_id}/create" method="post"
                  style="display:inline-block;margin-right:15px;">
                <input type="hidden" name="subpath" value="{subpath}">
                <input type="text" name="name"
                       placeholder="Datei erstellen" required>
                <input type="hidden" name="is_dir" value="false">
                <button type="submit">Datei Erstellen</button>
            </form>
            <form action="/files/{server_id}/create" method="post"
                  style="display:inline-block;margin-right:15px;">
                <input type="hidden" name="subpath" value="{subpath}">
                <input type="text" name="name"
                       placeholder="Ordnername" required>
                <input type="hidden" name="is_dir" value="true">
                <button type="submit">Ordner Erstellen</button>
            </form>
            <hr style="border:0.5px solid #45475a;margin:15px 0;">
            <form action="/files/{server_id}/upload" method="post"
                  enctype="multipart/form-data">
                <input type="hidden" name="subpath" value="{subpath}">
                <label>Datei hochladen (Server-JAR bitte als
                    "{SERVER_JAR_NAME}" benennen):</label>
                <input type="file" name="upload_file" required>
                <button type="submit">Hochladen</button>
            </form>
            <hr style="border:0.5px solid #45475a;margin:15px 0;">
            <a href="/files/{server_id}/download-zip?subpath={subpath}"
               class="btn btn-download">
                📦 Diesen Ordner als ZIP herunterladen
            </a>
        </div>

        <div class="card">
            <table>
                <tr>
                    <th>Name</th>
                    <th>Typ</th>
                    <th>Aktionen</th>
                </tr>
    """

    if parent_path is not None:
        html += f"""
        <tr>
            <td>
                <a href="/files/{server_id}?subpath={parent_path}">
                    .. (Übergeordneter Ordner)
                </a>
            </td>
            <td>Ordner</td>
            <td></td>
        </tr>
        """

    for name, rel_p in dirs:
        html += f"""
        <tr>
            <td>📁 <a href="/files/{server_id}?subpath={rel_p}">{name}</a></td>
            <td>Ordner</td>
            <td class="row-actions">
                <a href="/files/{server_id}/download-zip?subpath={rel_p}"
                   class="btn btn-download">ZIP</a>
                <form action="/files/{server_id}/rename" method="post">
                    <input type="hidden" name="subpath" value="{rel_p}">
                    <input type="text" name="new_name" value="{name}"
                           style="width:120px;">
                    <button type="submit">Umbenennen</button>
                </form>
                <form action="/files/{server_id}/delete" method="post">
                    <input type="hidden" name="subpath" value="{rel_p}">
                    <button type="submit" class="btn-delete"
                        onclick="return confirm('Ordner wirklich löschen?')">
                        Löschen
                    </button>
                </form>
            </td>
        </tr>
        """

    for name, rel_p in files:
        html += f"""
        <tr>
            <td>📄 {name}</td>
            <td>Datei</td>
            <td class="row-actions">
                <a href="/files/{server_id}/download?filepath={rel_p}"
                   class="btn btn-download">Download</a>
                <a href="/files/{server_id}/edit?filepath={rel_p}"
                   class="btn">Bearbeiten</a>
                <form action="/files/{server_id}/rename" method="post">
                    <input type="hidden" name="subpath" value="{rel_p}">
                    <input type="text" name="new_name" value="{name}"
                           style="width:120px;">
                    <button type="submit">Umbenennen</button>
                </form>
                <form action="/files/{server_id}/delete" method="post">
                    <input type="hidden" name="subpath" value="{rel_p}">
                    <button type="submit" class="btn-delete"
                        onclick="return confirm('Datei wirklich löschen?')">
                        Löschen
                    </button>
                </form>
            </td>
        </tr>
        """

    html += """
            </table>
        </div>
    </body>
    </html>
    """
    return html


@app.get("/files/{server_id}/download")
async def download_file(server_id: int, filepath: str):
    sdir = server_dir(server_id)
    full_path = os.path.join(sdir, filepath)
    if (
        not is_safe_path(sdir, full_path)
        or not os.path.isfile(full_path)
    ):
        raise HTTPException(status_code=400, detail="Datei nicht gefunden.")
    return FileResponse(
        full_path,
        filename=os.path.basename(full_path),
        media_type="application/octet-stream"
    )


@app.get("/files/{server_id}/download-zip")
async def download_folder_zip(server_id: int, subpath: str = ""):
    sdir = server_dir(server_id)
    target_dir = os.path.join(sdir, subpath)
    if (
        not is_safe_path(sdir, target_dir)
        or not os.path.isdir(target_dir)
    ):
        raise HTTPException(status_code=400, detail="Ungültiger Ordner.")

    folder_label = (
        subpath.strip("/").split("/")[-1]
        if subpath.strip("/") else f"server-{server_id}"
    )
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(tmp_fd)
    create_zip_from_dir(target_dir, tmp_path)

    def cleanup():
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return FileResponse(
        tmp_path,
        filename=f"{folder_label}.zip",
        media_type="application/zip",
        background=BackgroundTask(cleanup)
    )


@app.post("/files/{server_id}/upload")
async def upload_file_to_server(
    server_id: int,
    subpath: str = Form(""),
    upload_file: UploadFile = File(...)
):
    sdir = server_dir(server_id)
    target_dir = os.path.join(sdir, subpath)
    if not is_safe_path(sdir, target_dir):
        raise HTTPException(status_code=400, detail="Ungültiger Zielpfad.")
    os.makedirs(target_dir, exist_ok=True)

    filename = os.path.basename(upload_file.filename)
    target_file_path = os.path.join(target_dir, filename)
    content = await upload_file.read()
    with open(target_file_path, "wb") as f:
        f.write(content)

    return RedirectResponse(
        url=f"/files/{server_id}?subpath={subpath}",
        status_code=303
    )


@app.get("/files/{server_id}/edit", response_class=HTMLResponse)
async def edit_file_page(server_id: int, filepath: str):
    sdir = server_dir(server_id)
    full_path = os.path.join(sdir, filepath)
    if (
        not is_safe_path(sdir, full_path)
        or not os.path.isfile(full_path)
    ):
        raise HTTPException(status_code=400, detail="Datei nicht gefunden.")

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        content = "Binary/Binär-Datei kann nicht dargestellt werden."

    parent_subpath = "/".join(filepath.strip("/").split("/")[:-1])

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Datei bearbeiten: {filepath}</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                background: #1e1e2e;
                color: #cdd6f4;
                margin: 20px;
            }}
            textarea {{
                width: 100%;
                height: 500px;
                background: #11111b;
                color: #a6e3a1;
                border: 1px solid #45475a;
                font-family: monospace;
                font-size: 14px;
                padding: 10px;
                border-radius: 6px;
                box-sizing: border-box;
            }}
            button, a.btn {{
                background: #89b4fa;
                border: none;
                padding: 8px 12px;
                color: #11111b;
                cursor: pointer;
                border-radius: 4px;
                font-weight: bold;
                text-decoration: none;
            }}
        </style>
    </head>
    <body>
        <a href="/files/{server_id}?subpath={parent_subpath}" class="btn">
            &larr; Zurück
        </a>
        <h2>Bearbeite: {filepath}</h2>
        <form action="/files/{server_id}/save" method="post">
            <input type="hidden" name="filepath" value="{filepath}">
            <textarea name="content">{content}</textarea>
            <br><br>
            <button type="submit">Speichern</button>
        </form>
    </body>
    </html>
    """


@app.post("/files/{server_id}/save")
async def save_file(
    server_id: int,
    filepath: str = Form(...),
    content: str = Form(...)
):
    sdir = server_dir(server_id)
    full_path = os.path.join(sdir, filepath)
    if not is_safe_path(sdir, full_path):
        raise HTTPException(status_code=400, detail="Sicherheitsverstoß.")
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    parent_subpath = "/".join(filepath.strip("/").split("/")[:-1])
    return RedirectResponse(
        url=f"/files/{server_id}?subpath={parent_subpath}",
        status_code=303
    )


@app.post("/files/{server_id}/create")
async def create_file_or_dir(
    server_id: int,
    subpath: str = Form(""),
    name: str = Form(...),
    is_dir: str = Form("false")
):
    sdir = server_dir(server_id)
    name = os.path.basename(name)
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
    ):
        raise HTTPException(status_code=400, detail="Ungültiger Name.")

    target_path = os.path.join(sdir, subpath, name)
    if not is_safe_path(sdir, target_path):
        raise HTTPException(status_code=400, detail="Ungültiger Pfad.")

    if is_dir == "true":
        os.makedirs(target_path, exist_ok=True)
    else:
        with open(target_path, "w", encoding="utf-8") as f:
            f.write("")

    return RedirectResponse(
        url=f"/files/{server_id}?subpath={subpath}",
        status_code=303
    )


@app.post("/files/{server_id}/rename")
async def rename_file_or_dir(
    server_id: int,
    subpath: str = Form(...),
    new_name: str = Form(...)
):
    sdir = server_dir(server_id)
    old_path = os.path.join(sdir, subpath)
    new_name = new_name.strip()

    if (
        not new_name
        or "/" in new_name
        or "\\" in new_name
        or new_name in (".", "..")
    ):
        raise HTTPException(status_code=400, detail="Ungültiger neuer Name.")

    parent_subpath = "/".join(subpath.strip("/").split("/")[:-1])
    new_path = os.path.join(sdir, parent_subpath, new_name)

    if (
        not is_safe_path(sdir, old_path)
        or not os.path.exists(old_path)
    ):
        raise HTTPException(status_code=400, detail="Pfad existiert nicht.")

    if not is_safe_path(sdir, new_path):
        raise HTTPException(status_code=400, detail="Ungültiger Zielpfad.")

    if os.path.exists(new_path):
        raise HTTPException(
            status_code=400,
            detail="Es existiert bereits eine Datei/ein Ordner "
                   "mit diesem Namen."
        )

    os.rename(old_path, new_path)
    return RedirectResponse(
        url=f"/files/{server_id}?subpath={parent_subpath}",
        status_code=303
    )


@app.post("/files/{server_id}/delete")
async def delete_file_or_dir(server_id: int, subpath: str = Form(...)):
    sdir = server_dir(server_id)
    target_path = os.path.join(sdir, subpath)

    if os.path.abspath(target_path) == os.path.abspath(sdir):
        raise HTTPException(
            status_code=400,
            detail="Server-Root kann nicht gelöscht werden."
        )

    if (
        not is_safe_path(sdir, target_path)
        or not os.path.exists(target_path)
    ):
        raise HTTPException(status_code=400, detail="Pfad existiert nicht.")

    if os.path.isdir(target_path):
        shutil.rmtree(target_path)
    else:
        os.remove(target_path)

    parent_subpath = "/".join(subpath.strip("/").split("/")[:-1])
    return RedirectResponse(
        url=f"/files/{server_id}?subpath={parent_subpath}",
        status_code=303
    )


# ============================================================
# SETUP
# ============================================================

@app.get("/setup", response_class=HTMLResponse)
async def setup():
    if load_users():
        return RedirectResponse(url="/")

    return """
    <html>
    <head>
        <style>
            body {
                font-family: Arial;
                background: #1e1e2e;
                color: #cdd6f4;
                padding: 50px;
            }
        </style>
    </head>
    <body>
        <h2>Ersteinrichtung: Admin-Konto erstellen</h2>
        <form action="/setup" method="post">
            <input type="text" name="username"
                   placeholder="Benutzername" required>
            <br><br>
            <input type="password" name="password"
                   placeholder="Passwort" required>
            <br><br>
            <button type="submit">Konto Erstellen</button>
        </form>
    </body>
    </html>
    """


@app.post("/setup")
async def setup_post(
    username: str = Form(...),
    password: str = Form(...)
):
    users = load_users()
    users.append({
        "id": (max([u.get("id", 0) for u in users]) + 1) if users else 1,
        "username": username,
        "password": password
    })
    save_users(users)
    return RedirectResponse(url="/", status_code=303)


# ============================================================
# VELOCITY-API
# ============================================================

def _check_velocity_auth(x_api_key):
    if not VELOCITY_API_KEY:
        return
    if x_api_key != VELOCITY_API_KEY:
        raise HTTPException(status_code=401, detail="Ungültiger API-Key.")


@app.get("/velocity/servers")
async def velocity_servers(
    x_api_key: str = Header(default=None)
):
    _check_velocity_auth(x_api_key)
    names = [s.get("name", "") for s in list_servers()]
    return JSONResponse({
        "ok": True,
        "servers": sorted(names)
    })


@app.get("/velocity/{server_name}/status")
async def velocity_status(
    server_name: str,
    x_api_key: str = Header(default=None)
):
    _check_velocity_auth(x_api_key)
    info = get_server_by_name(server_name)
    if not info:
        return JSONResponse(
            {"ok": False, "error": "Server nicht gefunden"},
            status_code=404
        )
    sid = info["id"]
    is_running = sid in processes and processes[sid].poll() is None
    used_gb = None
    if is_running:
        used_gb = get_process_ram_gb(processes[sid].pid)
    return JSONResponse({
        "ok": True,
        "name": info.get("name", f"Server {sid}"),
        "id": sid,
        "running": is_running,
        "port": info.get("port", 25565),
        "ram_assigned_gb": info.get("ram", 2),
        "ram_used_gb": round(used_gb, 2) if used_gb else None,
    })


@app.get("/velocity/{server_name}/start")
async def velocity_start(
    server_name: str,
    x_api_key: str = Header(default=None)
):
    _check_velocity_auth(x_api_key)
    info = get_server_by_name(server_name)
    if not info:
        return JSONResponse(
            {"ok": False, "error": "Server nicht gefunden"},
            status_code=404
        )
    sid = info["id"]
    name = info.get("name", f"Server {sid}")
    ram = info.get("ram", 2)
    start_command = info.get("start_command", "")

    if sid in processes and processes[sid].poll() is None:
        return JSONResponse({
            "ok": True, "message": "Server läuft bereits",
            "running": True
        })

    sdir = server_dir(sid)
    jar_path = os.path.join(sdir, SERVER_JAR_NAME)
    if not os.path.exists(jar_path):
        return JSONResponse(
            {"ok": False, "error": f"{SERVER_JAR_NAME} fehlt."},
            status_code=400
        )

    if not start_command:
        start_command = (
            f"java -Xms{ram}G -Xmx{ram}G "
            f"-jar {SERVER_JAR_NAME} nogui"
        )

    try:
        args = shlex.split(start_command)
    except ValueError as e:
        return JSONResponse(
            {"ok": False, "error": f"Startbefehl ungültig: {e}"},
            status_code=400
        )

    log_file = open(
        os.path.join(sdir, "server.log"),
        "a", encoding="utf-8"
    )
    processes[sid] = subprocess.Popen(
        args, cwd=sdir,
        stdin=subprocess.PIPE,
        stdout=log_file, stderr=log_file,
        text=True, bufsize=1
    )
    return JSONResponse({
        "ok": True, "message": f"Server '{name}' gestartet.",
        "running": True
    })


@app.get("/velocity/{server_name}/stop")
async def velocity_stop(
    server_name: str,
    x_api_key: str = Header(default=None)
):
    _check_velocity_auth(x_api_key)
    info = get_server_by_name(server_name)
    if not info:
        return JSONResponse(
            {"ok": False, "error": "Server nicht gefunden"},
            status_code=404
        )
    sid = info["id"]
    name = info.get("name", f"Server {sid}")

    if sid not in processes or processes[sid].poll() is not None:
        return JSONResponse({
            "ok": True, "message": "Server läuft nicht",
            "running": False
        })

    process = processes[sid]
    try:
        if process.stdin:
            process.stdin.write("stop\n")
            process.stdin.flush()
        else:
            process.terminate()
    except Exception:
        process.terminate()

    return JSONResponse({
        "ok": True, "message": f"Stop-Signal an '{name}' gesendet.",
        "running": False
    })


@app.get("/velocity/{server_name}/restart")
async def velocity_restart(
    server_name: str,
    x_api_key: str = Header(default=None)
):
    _check_velocity_auth(x_api_key)
    await velocity_stop(server_name, x_api_key)
    import asyncio
    await asyncio.sleep(3)
    return await velocity_start(server_name, x_api_key)


@app.get("/velocity/{server_name}/command")
async def velocity_command(
    server_name: str,
    cmd: str = Query(...),
    x_api_key: str = Header(default=None)
):
    _check_velocity_auth(x_api_key)
    info = get_server_by_name(server_name)
    if not info:
        return JSONResponse(
            {"ok": False, "error": "Server nicht gefunden"},
            status_code=404
        )
    sid = info["id"]
    name = info.get("name", f"Server {sid}")

    if sid not in processes or processes[sid].poll() is not None:
        return JSONResponse(
            {"ok": False, "error": "Server läuft nicht"},
            status_code=400
        )
    process = processes[sid]
    if not process.stdin:
        return JSONResponse(
            {"ok": False, "error": "Kein stdin verfügbar"},
            status_code=500
        )
    try:
        process.stdin.write(cmd.rstrip("\n") + "\n")
        process.stdin.flush()
    except Exception as e:
        return JSONResponse(
            {"ok": False,
             "error": f"Konnte Befehl nicht senden: {e}"},
            status_code=500
        )
    return JSONResponse({
        "ok": True, "message": f"Befehl an '{name}' gesendet.",
        "command": cmd
    })


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
