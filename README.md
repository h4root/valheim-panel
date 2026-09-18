# Valheim Panel (Windows)

A launch script plus a web control panel for the official Valheim dedicated server on Windows.

## Setup

1. In Steam: Library → filter by Tools → **Valheim Dedicated Server** → Install.
2. Clone this repo next to nothing important — it creates `save/`, `logs/`, `backups/` here.
3. Set a password:
   ```powershell
   'yourpassword' | Set-Content -NoNewline .password
   ```
4. If Steam isn't installed at the default path, edit `$ServerDir` at the top of `start.ps1`.
5. Right-click `start.ps1` → "Run with PowerShell". First run, watch the window until you see `Game server connected`, then stop it once with Ctrl+C and confirm `World save (5/5) done` appears — that confirms your setup saves correctly before you rely on it.

## Panel

```powershell
python panel\server.py
```

Open `http://<this-pc-ip>:3030` from any device on the same network.

The panel can start/stop the server itself once it's running — you don't need to keep using `start.ps1` directly after the first check.

### Stopping

This build of the dedicated server does not respond to a programmatic Ctrl+C on Windows. The panel's Stop button waits for the next scheduled autosave and then ends the process — not instant, but no progress is lost. For a manual stop from the console window itself, Ctrl+C once works normally.

## Requirements

- Windows, PowerShell 7+
- Python 3.9+, no external packages

## License

MIT — see the [main branch](../../tree/main).
