import ipaddress
import json
import re
import subprocess
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
SAVE_DIR = ROOT / "save"
WORLDS_DIR = SAVE_DIR / "worlds_local"
BACKUP_DIR = ROOT / "backups"
START_SCRIPT = ROOT / "start.ps1"
PASSWORD_FILE = ROOT / ".password"
BIND = ("0.0.0.0", 3030)
EXE_NAME = "valheim_server.exe"

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

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

_managed_lock = threading.Lock()
_managed_proc = None  # subprocess.Popen of the server started by this panel, or None
_schedule_timer = None
_schedule_info = None
_stop_lock = threading.Lock()
_stop_state = None  # {"requested_at": epoch, "hard_deadline": epoch} while a stop is in flight


def run_ps(script, timeout=8):
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def allowed(host):
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


# ---------- reading/writing start.ps1 ----------

def _ps_get_string(text, name):
    m = re.search(rf'^\${name}\s*=\s*"([^"]*)"', text, re.MULTILINE)
    return m.group(1) if m else ""


def _ps_set_string(text, name, value):
    escaped = str(value).replace('"', '`"')
    return re.sub(rf'^\${name}\s*=\s*"[^"]*"', f'${name} = "{escaped}"', text, count=1, flags=re.MULTILINE)


def _ps_get_int(text, name, default=0):
    m = re.search(rf'^\${name}\s*=\s*(-?\d+)', text, re.MULTILINE)
    return int(m.group(1)) if m else default


def _ps_set_int(text, name, value):
    return re.sub(rf'^\${name}\s*=\s*-?\d+', f'${name} = {int(value)}', text, count=1, flags=re.MULTILINE)


def _ps_get_bool(text, name):
    m = re.search(rf'^\${name}\s*=\s*\$(true|false)', text, re.MULTILINE | re.IGNORECASE)
    return bool(m) and m.group(1).lower() == "true"


def _ps_set_bool(text, name, value):
    lit = "$true" if value else "$false"
    return re.sub(rf'^\${name}\s*=\s*\$(true|false)', f'${name} = {lit}', text, count=1,
                  flags=re.MULTILINE | re.IGNORECASE)


def _ps_get_array(text, name):
    m = re.search(rf'^\${name}\s*=\s*@\(([^)]*)\)', text, re.MULTILINE)
    return re.findall(r'"([^"]*)"', m.group(1)) if m else []


def _ps_set_array(text, name, values):
    literal = "@(" + ", ".join(f'"{v}"' for v in values) + ")" if values else "@()"
    return re.sub(rf'^\${name}\s*=\s*@\([^)]*\)', f'${name} = {literal}', text, count=1, flags=re.MULTILINE)


CONFIG_GET = {
    "server_name": lambda t: _ps_get_string(t, "ServerName"),
    "world_name": lambda t: _ps_get_string(t, "WorldName"),
    "port": lambda t: _ps_get_int(t, "Port", 2456),
    "public": lambda t: bool(_ps_get_int(t, "Public", 0)),
    "crossplay": lambda t: _ps_get_bool(t, "Crossplay"),
    "instance_id": lambda t: _ps_get_string(t, "InstanceId"),
    "preset": lambda t: _ps_get_string(t, "Preset"),
    "combat": lambda t: _ps_get_string(t, "Combat"),
    "death_penalty": lambda t: _ps_get_string(t, "DeathPenalty"),
    "resources": lambda t: _ps_get_string(t, "Resources"),
    "raids": lambda t: _ps_get_string(t, "Raids"),
    "portals": lambda t: _ps_get_string(t, "Portals"),
    "setkeys": lambda t: _ps_get_array(t, "SetKeys"),
    "save_interval": lambda t: _ps_get_int(t, "SaveInterval", 1800),
    "backups": lambda t: _ps_get_int(t, "Backups", 4),
    "backup_short": lambda t: _ps_get_int(t, "BackupShort", 7200),
    "backup_long": lambda t: _ps_get_int(t, "BackupLong", 43200),
}


def _valid(field, value):
    return value if value in VALID[field] else ""


CONFIG_SET = {
    "server_name": lambda t, v: _ps_set_string(t, "ServerName", str(v).strip()[:64] or "Server"),
    "port": lambda t, v: _ps_set_int(t, "Port", min(65000, max(1024, int(v)))),
    "public": lambda t, v: _ps_set_int(t, "Public", 1 if v else 0),
    "crossplay": lambda t, v: _ps_set_bool(t, "Crossplay", bool(v)),
    "instance_id": lambda t, v: _ps_set_string(t, "InstanceId", str(v).strip()[:64]),
    "preset": lambda t, v: _ps_set_string(t, "Preset", _valid("preset", v)),
    "combat": lambda t, v: _ps_set_string(t, "Combat", _valid("combat", v)),
    "death_penalty": lambda t, v: _ps_set_string(t, "DeathPenalty", _valid("death_penalty", v)),
    "resources": lambda t, v: _ps_set_string(t, "Resources", _valid("resources", v)),
    "raids": lambda t, v: _ps_set_string(t, "Raids", _valid("raids", v)),
    "portals": lambda t, v: _ps_set_string(t, "Portals", _valid("portals", v)),
    "setkeys": lambda t, v: _ps_set_array(t, "SetKeys", [k for k in v if k in SETKEY_CHOICES]),
    "save_interval": lambda t, v: _ps_set_int(t, "SaveInterval", max(60, int(v))),
    "backups": lambda t, v: _ps_set_int(t, "Backups", max(0, int(v))),
    "backup_short": lambda t, v: _ps_set_int(t, "BackupShort", max(60, int(v))),
    "backup_long": lambda t, v: _ps_set_int(t, "BackupLong", max(60, int(v))),
}


def read_config():
    text = START_SCRIPT.read_text(encoding="utf-8")
    return {name: getter(text) for name, getter in CONFIG_GET.items()}


def write_config(patch):
    text = START_SCRIPT.read_text(encoding="utf-8")
    for key, value in patch.items():
        setter = CONFIG_SET.get(key)
        if setter is None:
            continue
        text = setter(text, value)
    START_SCRIPT.write_text(text, encoding="utf-8")


def server_dir():
    text = START_SCRIPT.read_text(encoding="utf-8")
    m = re.search(r'^\$ServerDir\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return Path(m.group(1)) if m else None


def world_dir(name=None):
    return WORLDS_DIR / (name or read_config()["world_name"])


def action_set_password(password):
    password = password.strip()
    if len(password) < 5:
        return {"ok": False, "error": "password must be at least 5 characters"}
    ROOT.mkdir(parents=True, exist_ok=True)
    PASSWORD_FILE.write_text(password, encoding="utf-8")
    return {"ok": True}


# ---------- server state ----------

def server_pid():
    global _managed_proc
    with _managed_lock:
        if _managed_proc is not None and _managed_proc.poll() is None:
            return _managed_proc.pid
        if _managed_proc is not None:
            _managed_proc = None
    out = run_ps(f"(Get-Process -Name '{EXE_NAME[:-4]}' -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Id)")
    out = out.strip()
    return int(out) if out.isdigit() else None


def proc_stats(pid):
    script = f"""
$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue
if (-not $p) {{ "null"; exit }}
$cpu1 = $p.CPU
Start-Sleep -Milliseconds 300
$p.Refresh()
$cpu2 = $p.CPU
$pct = [math]::Round((($cpu2 - $cpu1) / 0.3) * 100, 1)
$uptime = [int]((Get-Date) - $p.StartTime).TotalSeconds
[PSCustomObject]@{{ cpu = $pct; rss = $p.WorkingSet64; uptime = $uptime }} | ConvertTo-Json -Compress
"""
    out = run_ps(script, timeout=6).strip()
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    if data is None:
        return None
    return data["cpu"], data["rss"], data["uptime"]


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
    out = run_ps(
        "Get-CimInstance Win32_Battery | Select-Object EstimatedChargeRemaining,BatteryStatus | ConvertTo-Json -Compress"
    ).strip()
    if not out:
        return None
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    if not data:
        return None
    on_ac = 1 if data.get("BatteryStatus") == 2 else 0
    return data.get("EstimatedChargeRemaining", 0), on_ac


def system_stats():
    drive = ROOT.drive.rstrip(":")
    script = f"""
$os = Get-CimInstance Win32_OperatingSystem
$memPct = [math]::Round((1 - $os.FreePhysicalMemory / $os.TotalVisibleMemorySize) * 100, 1)
$cpuPct = (Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor | Where-Object {{ $_.Name -eq '_Total' }}).PercentProcessorTime
$vol = Get-Volume -DriveLetter '{drive}' -ErrorAction SilentlyContinue
$page = (Get-CimInstance Win32_PageFileUsage | Measure-Object CurrentUsage -Sum).Sum
[PSCustomObject]@{{
    mem_pct = $memPct
    cpu_pct = [math]::Round($cpuPct, 1)
    disk_free = if ($vol) {{ $vol.SizeRemaining }} else {{ 0 }}
    disk_total = if ($vol) {{ $vol.Size }} else {{ 0 }}
    pagefile_mb = if ($page) {{ $page }} else {{ 0 }}
    ncpu = [Environment]::ProcessorCount
}} | ConvertTo-Json -Compress
"""
    out = run_ps(script, timeout=8).strip()
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        data = {}
    return {
        "mem_pct": data.get("mem_pct", 0) or 0,
        "cpu_pct": data.get("cpu_pct", 0) or 0,
        "disk_free": data.get("disk_free", 0) or 0,
        "disk_total": data.get("disk_total", 0) or 0,
        "pagefile_bytes": (data.get("pagefile_mb", 0) or 0) * 1048576,
        "ncpu": data.get("ncpu", 1) or 1,
    }


def world_stats():
    wdir = world_dir()
    if not wdir.is_dir():
        return 0, 0
    size = sum(f.stat().st_size for f in wdir.glob("*") if f.is_file())
    prefix = wdir.name
    backups = len(list(BACKUP_DIR.glob(f"{prefix}_*.zip"))) if BACKUP_DIR.is_dir() else 0
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

    sdir = server_dir()
    if sdir is None:
        return {"ok": False, "error": "$ServerDir not found in start.ps1"}
    exe_path = sdir / EXE_NAME
    if not exe_path.is_file():
        return {"ok": False, "error": f"not found: {exe_path}"}
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

    log_handle = open(log_path, "wb")
    proc = subprocess.Popen(
        args, cwd=str(sdir),
        stdout=log_handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
    )
    with _managed_lock:
        _managed_proc = proc
    run_ps(f"(Get-Process -Id {proc.pid} -ErrorAction SilentlyContinue).PriorityClass = 'BelowNormal'")
    return {"ok": True}


def _terminate(pid):
    run_ps(f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue")


def _wait_for_save_then_kill(pid, baseline_saves, hard_deadline):
    global _managed_proc, _stop_state
    while time.time() < hard_deadline:
        if server_pid() != pid:
            break
        if log_stats(current_log())["saves"] > baseline_saves:
            break
        time.sleep(2)
    if server_pid() == pid:
        _terminate(pid)
    with _managed_lock:
        if _managed_proc is not None and _managed_proc.pid == pid:
            _managed_proc = None
    with _stop_lock:
        _stop_state = None


def action_stop():
    # This build of the server does not react to a programmatic Ctrl+C/Ctrl+Break
    # on Windows (verified: GenerateConsoleCtrlEvent is delivered, but no
    # save-and-exit happens). So we wait for the next scheduled autosave and
    # only then end the process — not instant, but guaranteed not to lose
    # world progress.
    global _stop_state
    pid = server_pid()
    if not pid:
        return {"ok": False, "error": "server not running"}
    with _stop_lock:
        if _stop_state is not None:
            return {"ok": True, "pending": True}
        baseline = log_stats(current_log())["saves"]
        save_interval = read_config()["save_interval"]
        hard_deadline = time.time() + save_interval + 180
        _stop_state = {"requested_at": time.time(), "hard_deadline": hard_deadline}
    threading.Thread(target=_wait_for_save_then_kill, args=(pid, baseline, hard_deadline), daemon=True).start()
    return {"ok": True, "pending": True}


def action_config(patch):
    unknown = [k for k in patch if k not in CONFIG_SET]
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
    if not name or not re.match(r'^[A-Za-z0-9_-]{1,32}$', name):
        return {"ok": False, "error": "invalid world name (letters/digits/-/_ up to 32 chars)"}
    if server_pid():
        return {"ok": False, "error": "stop the server first"}
    text = START_SCRIPT.read_text(encoding="utf-8")
    text = _ps_set_string(text, "WorldName", name)
    START_SCRIPT.write_text(text, encoding="utf-8")
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
        out.append({
            "file": f.name,
            "save": save_num,
            "mtime": int(f.stat().st_mtime),
            "size": f.stat().st_size,
        })
    return out


def action_rollback(filename):
    if server_pid():
        return {"ok": False, "error": "stop the server first"}
    if "/" in filename or "\\" in filename or filename.startswith(".") or not filename.endswith(".zip"):
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
    ids = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            ids.append(line)
    return ids


def read_access_lists():
    return {kind: _read_access(kind) for kind in ACCESS_FILES}


def action_access(kind, action, platform_id):
    if kind not in ACCESS_FILES:
        return {"ok": False, "error": "unknown list"}
    if not re.match(r'^[A-Za-z]+_[\w-]+$', platform_id):
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
            self.send_error(403, "only private network")
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
            self.send_error(403, "only private network")
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


if __name__ == "__main__":
    ROOT.joinpath("panel").mkdir(exist_ok=True)
    ThreadingHTTPServer(BIND, Handler).serve_forever()
