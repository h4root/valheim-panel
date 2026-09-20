import ipaddress
import json
import os
import re
import signal
import subprocess
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("VALHEIM_ROOT", "/server"))
INSTALL_DIR = Path(os.environ.get("VALHEIM_INSTALL_DIR", str(ROOT / "install")))
SAVE_DIR = ROOT / "config"
WORLDS_DIR = SAVE_DIR / "worlds_local"
LOG_DIR = ROOT / "logs"
BACKUP_DIR = ROOT / "backups"
CONFIG_FILE = ROOT / "panel.json"
PASSWORD_FILE = ROOT / "password"
BIND = ("0.0.0.0", int(os.environ.get("PANEL_PORT", "3030")))
EXE_NAME = "valheim_server.x86_64"

VALID = {
    "preset": ["", "normal", "casual", "easy", "hard", "hardcore", "immersive", "hammer"],
    "combat": ["", "veryeasy", "easy", "hard", "veryhard"],
    "death_penalty": ["", "casual", "veryeasy", "easy", "hard", "hardcore"],
    "resources": ["", "muchless", "less", "more", "muchmore", "most"],
    "raids": ["", "none", "muchless", "less", "more", "muchmore"],
    "portals": ["", "casual", "hard", "veryhard"],
}
RESOURCE_LABELS = {"": "x1 (default)", "muchless": "x0.5", "less": "x0.75",
                    "more": "x1.5", "muchmore": "x2", "most": "x3"}
RAID_LABELS = {"": "default", "none": "off", "muchless": "x0.25",
               "less": "x0.5", "more": "x1.5", "muchmore": "x2"}

SETKEY_CHOICES = ["nobuildcost", "playerevents", "passivemobs", "nomap"]
SETKEY_LABELS = {
    "nobuildcost": "Free building",
    "playerevents": "Any player can trigger events",
    "passivemobs": "Passive mobs",
    "nomap": "No map",
}

ACCESS_FILES = {
    "admin": SAVE_DIR / "adminlist.txt",
    "banned": SAVE_DIR / "bannedlist.txt",
    "permitted": SAVE_DIR / "permittedlist.txt",
}

DEFAULT_CONFIG = {
    "server_name": "My Valheim Server",
    "world_name": "Dedicated",
    "port": 2456,
    "public": False,
    "crossplay": True,
    "instance_id": "",
    "preset": "",
    "combat": "",
    "death_penalty": "",
    "resources": "",
    "raids": "",
    "portals": "",
    "setkeys": [],
    "save_interval": 1800,
    "backups": 4,
    "backup_short": 7200,
    "backup_long": 43200,
}

ENV_SEED = {
    "server_name": "SERVER_NAME", "world_name": "WORLD_NAME", "port": "PORT",
    "public": "PUBLIC", "crossplay": "CROSSPLAY", "instance_id": "INSTANCE_ID",
    "preset": "PRESET", "combat": "COMBAT", "death_penalty": "DEATH_PENALTY",
    "resources": "RESOURCES", "raids": "RAIDS", "portals": "PORTALS",
    "save_interval": "SAVE_INTERVAL", "backups": "BACKUPS",
    "backup_short": "BACKUP_SHORT", "backup_long": "BACKUP_LONG",
}

_managed_lock = threading.Lock()
_managed_proc = None  # subprocess.Popen of the server started by this panel, or None
_schedule_timer = None
_schedule_info = None
_stop_lock = threading.Lock()
_stop_state = None  # {"requested_at": epoch, "hard_deadline": epoch} while a stop is in flight


def allowed(host):
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


# ---------- config storage (JSON file, seeded from env on first boot) ----------

def _seed_from_env():
    cfg = dict(DEFAULT_CONFIG)
    for key, env_name in ENV_SEED.items():
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        if isinstance(DEFAULT_CONFIG[key], bool):
            cfg[key] = raw.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(DEFAULT_CONFIG[key], int):
            try:
                cfg[key] = int(raw)
            except ValueError:
                pass
        else:
            cfg[key] = raw
    setkeys_raw = os.environ.get("SETKEYS", "")
    if setkeys_raw:
        cfg["setkeys"] = [k.strip() for k in setkeys_raw.split(",") if k.strip() in SETKEY_CHOICES]
    for field in VALID:
        if cfg[field] not in VALID[field]:
            cfg[field] = ""
    return cfg


def _write_config_full(cfg):
    ROOT.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def read_config():
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return {**DEFAULT_CONFIG, **data}
        except (json.JSONDecodeError, OSError):
            pass
    cfg = _seed_from_env()
    _write_config_full(cfg)
    password = os.environ.get("PASSWORD", "")
    if password and not PASSWORD_FILE.exists():
        action_set_password(password)
    return cfg


def _valid(field, value):
    return value if value in VALID[field] else ""


def write_config(patch):
    cfg = read_config()
    for key, value in patch.items():
        if key not in DEFAULT_CONFIG:
            continue
        if key in VALID:
            value = _valid(key, value)
        elif key == "setkeys":
            value = [k for k in value if k in SETKEY_CHOICES]
        elif key == "port":
            value = min(65000, max(1024, int(value)))
        elif key == "save_interval":
            value = max(60, int(value))
        elif key in ("backups", "backup_short", "backup_long"):
            value = max(0, int(value))
        elif key == "server_name":
            value = str(value).strip()[:64] or "Server"
        elif key == "instance_id":
            value = str(value).strip()[:64]
        cfg[key] = value
    _write_config_full(cfg)


def world_dir(name=None):
    return WORLDS_DIR / (name or read_config()["world_name"])


def action_set_password(password):
    password = password.strip()
    if len(password) < 5:
        return {"ok": False, "error": "password must be at least 5 characters"}
    ROOT.mkdir(parents=True, exist_ok=True)
    PASSWORD_FILE.write_text(password, encoding="utf-8")
    try:
        os.chmod(PASSWORD_FILE, 0o600)
    except OSError:
        pass
    return {"ok": True}


# ---------- server state ----------

def server_pid():
    global _managed_proc
    with _managed_lock:
        if _managed_proc is not None and _managed_proc.poll() is None:
            return _managed_proc.pid
        if _managed_proc is not None:
            _managed_proc = None
    # Matched on argv[0] rather than the process name: the kernel truncates
    # comm to 15 characters, which is shorter than the binary's own name.
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv0 = (entry / "cmdline").read_bytes().split(b"\0")[0].decode(errors="replace")
        except OSError:
            continue
        if argv0.endswith("/" + EXE_NAME) or argv0 == EXE_NAME:
            return int(entry.name)
    return None


def _proc_stat_ticks(text):
    after = text.rsplit(")", 1)[1].split()
    return int(after[11]) + int(after[12])  # utime + stime


def proc_stats(pid):
    try:
        stat1 = Path(f"/proc/{pid}/stat").read_text()
        t1 = time.time()
        time.sleep(0.3)
        stat2 = Path(f"/proc/{pid}/stat").read_text()
        t2 = time.time()
    except OSError:
        return None
    clk = os.sysconf("SC_CLK_TCK")
    dt = t2 - t1
    cpu_pct = round((_proc_stat_ticks(stat2) - _proc_stat_ticks(stat1)) / clk / dt * 100, 1) if dt else 0.0

    rss = 0
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * 1024
                break
    except OSError:
        pass

    uptime = 0
    try:
        starttime_ticks = int(stat2.rsplit(")", 1)[1].split()[19])
        sys_uptime = float(Path("/proc/uptime").read_text().split()[0])
        uptime = max(0, int(sys_uptime - starttime_ticks / clk))
    except (OSError, IndexError, ValueError):
        pass

    return cpu_pct, rss, uptime


def current_log():
    logs = sorted(LOG_DIR.glob("server_*.log"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def log_stats(path):
    stats = {
        "players": 0, "zdos": 0, "sent": 0, "recv": 0, "saves": 0,
        "network_errors": 0, "connection_losses": 0, "join_code": 0,
        "last_save_epoch": 0,
    }
    if path is None:
        return stats
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return stats

    active = re.findall(r"is active with (\d+) player", text)
    joined = re.findall(r"now (\d+) player\(s\)", text)
    if joined:
        stats["players"] = int(joined[-1])
    elif active:
        stats["players"] = int(active[-1])

    conn = re.findall(r"Connections (\d+) ZDOS:(\d+)\s+sent:(\d+) recv:(\d+)", text)
    if conn:
        _, zdos, sent, recv = conn[-1]
        stats["zdos"], stats["sent"], stats["recv"] = int(zdos), int(sent), int(recv)

    stats["saves"] = len(re.findall(r"World save \(5/5\) done", text))
    stats["network_errors"] = len(re.findall(r"PlayFab network error", text))
    stats["connection_losses"] = len(re.findall(r"Player connection lost", text))

    codes = re.findall(r"with join code (\d+) and IP", text)
    if codes:
        stats["join_code"] = int(codes[-1])

    save_times = re.findall(
        r"(\d{2})/(\d{2})/(\d{4}) (\d{2}):(\d{2}):(\d{2}): World save \(5/5\) done", text
    )
    if save_times:
        mo, d, y, h, mi, s = save_times[-1]
        stats["last_save_epoch"] = int(
            time.mktime((int(y), int(mo), int(d), int(h), int(mi), int(s), 0, 0, -1))
        )
    return stats


def battery():
    base = Path("/sys/class/power_supply")
    if not base.is_dir():
        return None
    for bat in sorted(base.glob("BAT*")):
        try:
            capacity = int((bat / "capacity").read_text().strip())
            status = (bat / "status").read_text().strip()
        except OSError:
            continue
        return capacity, 1 if status in ("Charging", "Full") else 0
    return None


def system_stats():
    def cpu_line():
        with open("/proc/stat") as f:
            vals = list(map(int, f.readline().split()[1:]))
        return vals[3] + vals[4], sum(vals)  # idle+iowait, total

    idle1, total1 = cpu_line()
    time.sleep(0.2)
    idle2, total2 = cpu_line()
    dt = total2 - total1
    cpu_pct = round((1 - (idle2 - idle1) / dt) * 100, 1) if dt else 0.0

    meminfo = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            meminfo[k] = int(v.strip().split()[0]) * 1024
    mem_total = meminfo.get("MemTotal", 0)
    mem_avail = meminfo.get("MemAvailable", mem_total)
    mem_pct = round((1 - mem_avail / mem_total) * 100, 1) if mem_total else 0.0
    pagefile_bytes = meminfo.get("SwapTotal", 0) - meminfo.get("SwapFree", 0)

    st = os.statvfs(str(ROOT))
    return {
        "cpu_pct": cpu_pct, "mem_pct": mem_pct,
        "disk_free": st.f_bavail * st.f_frsize, "disk_total": st.f_blocks * st.f_frsize,
        "pagefile_bytes": pagefile_bytes, "ncpu": os.cpu_count() or 1,
    }


def world_stats():
    wdir = world_dir()
    if not wdir.is_dir():
        return 0, 0
    size = sum(f.stat().st_size for f in wdir.glob("*") if f.is_file())
    backups = len(list(BACKUP_DIR.glob(f"{wdir.name}_*.zip"))) if BACKUP_DIR.is_dir() else 0
    return size, backups


def build_status():
    pid = server_pid()
    stats = log_stats(current_log())
    bat = battery()
    sysinfo = system_stats()
    world_size, backups_count = world_stats()
    cfg = read_config()
    cpu = rss = uptime = 0
    if pid:
        got = proc_stats(pid)
        if got:
            cpu, rss, uptime = got

    now = time.time()
    since_save = int(now - stats["last_save_epoch"]) if stats["last_save_epoch"] else -1
    next_save_in = max(cfg["save_interval"] - since_save, 0) if since_save >= 0 else -1

    return {
        "running": pid is not None,
        "players": stats["players"],
        "zdos": stats["zdos"],
        "net_sent": stats["sent"],
        "net_recv": stats["recv"],
        "cpu_percent": cpu,
        "memory_bytes": rss,
        "uptime_seconds": uptime,
        "world_saves": stats["saves"],
        "network_errors": stats["network_errors"],
        "connection_losses": stats["connection_losses"],
        "join_code": stats["join_code"],
        "seconds_since_save": since_save,
        "next_save_in_seconds": next_save_in,
        "world_size_bytes": world_size,
        "backups_total": backups_count,
        "world_name": cfg["world_name"],
        "server_name": cfg["server_name"],
        "resources_label": RESOURCE_LABELS.get(cfg["resources"], cfg["resources"]),
        "battery_percent": bat[0] if bat else None,
        "on_ac_power": bat[1] if bat else None,
        "has_battery": bat is not None,
        "cpu_used_percent": sysinfo["cpu_pct"],
        "cpu_count": sysinfo["ncpu"],
        "memory_used_percent": sysinfo["mem_pct"],
        "pagefile_used_bytes": sysinfo["pagefile_bytes"],
        "disk_free_bytes": sysinfo["disk_free"],
        "disk_total_bytes": sysinfo["disk_total"],
        "scheduled_stop": _schedule_info,
        "stop_pending": _stop_state is not None,
    }


# ---------- actions ----------

def _backup_world(name):
    wdir = world_dir(name)
    if not wdir.is_dir():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_file = BACKUP_DIR / f"{name}_{stamp}.zip"
    with zipfile.ZipFile(backup_file, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in wdir.glob("*"):
            zf.write(f, arcname=f"{name}/{f.name}")
    kept = sorted(BACKUP_DIR.glob(f"{name}_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in kept[5:]:
        old.unlink(missing_ok=True)
    return backup_file


def action_start():
    global _managed_proc
    if server_pid():
        return {"ok": False, "error": "server already running"}

    exe_path = INSTALL_DIR / EXE_NAME
    if not exe_path.is_file():
        return {"ok": False, "error": f"{exe_path} not found"}
    if not PASSWORD_FILE.exists():
        return {"ok": False, "error": "no password set yet"}
    password = PASSWORD_FILE.read_text(encoding="utf-8").strip()
    if len(password) < 5:
        return {"ok": False, "error": "password must be at least 5 characters"}

    cfg = read_config()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _backup_world(cfg["world_name"])

    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"server_{stamp}.log"
    args = [
        str(exe_path),
        "-nographics", "-batchmode",
        "-name", cfg["server_name"],
        "-port", str(cfg["port"]),
        "-world", cfg["world_name"],
        "-password", password,
        "-savedir", str(SAVE_DIR),
        "-public", "1" if cfg["public"] else "0",
        "-saveinterval", str(cfg["save_interval"]),
        "-backups", str(cfg["backups"]),
        "-backupshort", str(cfg["backup_short"]),
        "-backuplong", str(cfg["backup_long"]),
    ]
    if cfg["crossplay"]:
        args.append("-crossplay")
    if cfg["instance_id"]:
        args += ["-instanceid", cfg["instance_id"]]
    if cfg["preset"]:
        args += ["-preset", cfg["preset"]]
    for mod, val in (("combat", cfg["combat"]), ("deathpenalty", cfg["death_penalty"]),
                     ("resources", cfg["resources"]), ("raids", cfg["raids"]), ("portals", cfg["portals"])):
        if val:
            args += ["-modifier", mod, val]
    for key in cfg["setkeys"]:
        args += ["-setkey", key]

    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = f"{exe_path.parent}/linux64:{env.get('LD_LIBRARY_PATH', '')}"
    env["SteamAppId"] = "892970"

    log_handle = open(log_path, "wb")
    proc = subprocess.Popen(
        args, cwd=str(exe_path.parent),
        stdout=log_handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        env=env, start_new_session=True,
    )
    with _managed_lock:
        _managed_proc = proc
    try:
        os.setpriority(os.PRIO_PROCESS, proc.pid, 10)
    except OSError:
        pass
    return {"ok": True}


def _wait_for_exit_then_clear(pid, hard_deadline):
    global _managed_proc, _stop_state
    try:
        os.kill(pid, signal.SIGINT)
    except OSError:
        pass
    while time.time() < hard_deadline:
        if server_pid() != pid:
            break
        time.sleep(1)
    if server_pid() == pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    with _managed_lock:
        if _managed_proc is not None and _managed_proc.pid == pid:
            _managed_proc = None
    with _stop_lock:
        _stop_state = None


def action_stop():
    global _stop_state
    pid = server_pid()
    if not pid:
        return {"ok": False, "error": "server not running"}
    with _stop_lock:
        if _stop_state is not None:
            return {"ok": True, "pending": True}
        hard_deadline = time.time() + 60
        _stop_state = {"requested_at": time.time(), "hard_deadline": hard_deadline}
    threading.Thread(target=_wait_for_exit_then_clear, args=(pid, hard_deadline), daemon=True).start()
    return {"ok": True, "pending": True}


def action_config(patch):
    unknown = [k for k in patch if k not in DEFAULT_CONFIG]
    if unknown:
        return {"ok": False, "error": f"unknown fields: {', '.join(unknown)}"}
    write_config(patch)
    return {"ok": True, "applies_now": server_pid() is None}


def list_worlds():
    if not WORLDS_DIR.is_dir():
        return []
    return sorted(d.name for d in WORLDS_DIR.iterdir() if d.is_dir() and "_backup_auto-" not in d.name)


def action_switch_world(name):
    name = name.strip()
    if not name or not re.match(r"^[A-Za-z0-9_-]{1,32}$", name):
        return {"ok": False, "error": "invalid world name (letters/digits/-/_ up to 32 chars)"}
    if server_pid():
        return {"ok": False, "error": "stop the server first"}
    write_config({"world_name": name})
    return {"ok": True, "new_world": not world_dir(name).is_dir()}


def list_backups():
    if not BACKUP_DIR.is_dir():
        return []
    prefix = read_config()["world_name"]
    out = []
    for f in sorted(BACKUP_DIR.glob(f"{prefix}_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        save_num = "?"
        try:
            with zipfile.ZipFile(f) as zf:
                for name in zf.namelist():
                    m = re.search(r"_main\.(\d+)\.fwl2$", name)
                    if m:
                        save_num = m.group(1)
                        break
        except (zipfile.BadZipFile, OSError):
            pass
        out.append({"file": f.name, "save": save_num, "mtime": int(f.stat().st_mtime), "size": f.stat().st_size})
    return out


def action_rollback(filename):
    if server_pid():
        return {"ok": False, "error": "stop the server first"}
    if "/" in filename or filename.startswith(".") or not filename.endswith(".zip"):
        return {"ok": False, "error": "invalid file name"}
    target = BACKUP_DIR / filename
    if not target.is_file():
        return {"ok": False, "error": "backup not found"}

    world_name = read_config()["world_name"]
    wdir = world_dir(world_name)
    if wdir.is_dir():
        _backup_world(world_name)
        import shutil
        shutil.rmtree(wdir)

    wdir.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target) as zf:
        zf.extractall(wdir.parent)
    return {"ok": True}


def _run_scheduled_stop(hour, minute):
    global _schedule_info, _schedule_timer
    action_stop()
    _schedule_info = None
    _schedule_timer = None


def action_schedule_stop(hour, minute):
    global _schedule_timer, _schedule_info
    action_cancel_schedule()
    import datetime
    now = datetime.datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    delay = (target - now).total_seconds()
    _schedule_timer = threading.Timer(delay, _run_scheduled_stop, args=(hour, minute))
    _schedule_timer.daemon = True
    _schedule_timer.start()
    _schedule_info = {"hour": hour, "minute": minute}
    return {"ok": True}


def action_cancel_schedule():
    global _schedule_timer, _schedule_info
    if _schedule_timer is not None:
        _schedule_timer.cancel()
        _schedule_timer = None
    _schedule_info = None
    return {"ok": True}


# ---------- players & access ----------

def player_history():
    log = current_log()
    if log is None:
        return []
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    seen = {}
    for m in re.finditer(
        r"Player history entry with index \d+:\s+(.+?) \(([A-Za-z]+_[\w-]+), [0-9A-Fa-f]+\)", text
    ):
        name, pid = m.groups()
        seen[pid] = name.strip()
    return [{"name": n, "id": pid} for pid, n in seen.items()]


def _read_access(kind):
    path = ACCESS_FILES[kind]
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.strip().startswith("//")]


def read_access_lists():
    return {kind: _read_access(kind) for kind in ACCESS_FILES}


def action_access(kind, action, platform_id):
    if kind not in ACCESS_FILES:
        return {"ok": False, "error": "unknown list"}
    if not re.match(r"^[A-Za-z]+_[\w-]+$", platform_id):
        return {"ok": False, "error": "invalid ID format (expected Platform_UserID)"}
    path = ACCESS_FILES[kind]
    current = _read_access(kind)
    if action == "add":
        if platform_id not in current:
            current.append(platform_id)
    elif action == "remove":
        current = [x for x in current if x != platform_id]
    else:
        return {"ok": False, "error": "unknown action"}
    header = {"admin": "// List admin players ID  ONE per line",
              "banned": "// List banned players ID  ONE per line",
              "permitted": "// List permitted players ID ONE per line"}[kind]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n" + "\n".join(current) + ("\n" if current else ""), encoding="utf-8")
    return {"ok": True}


def tail_log(n=250):
    log = current_log()
    if log is None:
        return []
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-n:]


# ---------- HTTP ----------

PAGE_PATH = Path(__file__).parent / "index.html"
STYLE_PATH = Path(__file__).parent / "styles.css"


class Handler(BaseHTTPRequestHandler):
    server_version = "valheim-panel"

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, content_type):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > 8192:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        if not allowed(self.client_address[0]):
            self.send_error(403)
            return
        path = self.path.split("?")[0].rstrip("/")
        query = {}
        if "?" in self.path:
            for part in self.path.split("?", 1)[1].split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    query[k] = v

        if path in ("", "/index.html"):
            self._file(PAGE_PATH, "text/html; charset=utf-8")
        elif path == "/styles.css":
            self._file(STYLE_PATH, "text/css; charset=utf-8")
        elif path == "/api/status":
            self._json(build_status())
        elif path == "/api/config":
            self._json({"config": read_config(), "valid": VALID,
                        "resource_labels": RESOURCE_LABELS, "raid_labels": RAID_LABELS,
                        "setkey_choices": SETKEY_CHOICES, "setkey_labels": SETKEY_LABELS})
        elif path == "/api/worlds":
            self._json({"worlds": list_worlds(), "active": read_config()["world_name"]})
        elif path == "/api/backups":
            self._json({"backups": list_backups()})
        elif path == "/api/players":
            self._json({"players": player_history(), "access": read_access_lists()})
        elif path == "/api/access":
            self._json(read_access_lists())
        elif path == "/api/log":
            n = int(query.get("n", 250)) if query.get("n", "").isdigit() else 250
            self._json({"lines": tail_log(min(n, 2000))})
        else:
            self.send_error(404)

    def do_POST(self):
        if not allowed(self.client_address[0]):
            self.send_error(403)
            return
        path = self.path.split("?")[0].rstrip("/")
        body = self._read_json_body()

        if path == "/api/start":
            self._json(action_start())
        elif path == "/api/stop":
            self._json(action_stop())
        elif path == "/api/config":
            self._json(action_config(body if isinstance(body, dict) else {}))
        elif path == "/api/password":
            self._json(action_set_password(str(body.get("password", ""))))
        elif path == "/api/world":
            self._json(action_switch_world(str(body.get("name", ""))))
        elif path == "/api/rollback":
            self._json(action_rollback(str(body.get("file", ""))))
        elif path == "/api/access":
            self._json(action_access(str(body.get("list", "")), str(body.get("action", "")),
                                      str(body.get("id", ""))))
        elif path == "/api/schedule_stop":
            try:
                hour, minute = int(body.get("hour")), int(body.get("minute"))
                assert 0 <= hour <= 23 and 0 <= minute <= 59
            except (TypeError, ValueError, AssertionError):
                self._json({"ok": False, "error": "invalid time"}, 400)
                return
            self._json(action_schedule_stop(hour, minute))
        elif path == "/api/cancel_schedule":
            self._json(action_cancel_schedule())
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


def _handle_container_stop(signum, frame):
    pid = server_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGINT)
        except OSError:
            pass
        deadline = time.time() + 55
        while time.time() < deadline and server_pid() == pid:
            time.sleep(1)
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_container_stop)
    ROOT.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(BIND, Handler).serve_forever()
