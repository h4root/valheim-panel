# Valheim Panel

A web control panel for the official Valheim dedicated server, for Linux and Windows.

Start, stop, edit world modifiers, manage the admin/ban/permit lists, roll back to a backup, watch the live log — from a phone or any browser on your network.

## Install (Debian / Ubuntu / Linux Mint)

```
git clone https://github.com/h4root/valheim-panel.git
cd valheim-panel
./install.sh
```

It installs SteamCMD and the dedicated server, asks for a password and a server name, and sets up a `valheim-panel` systemd service that starts on boot. Then open `http://<this-machine>:3030`.

Re-run `./install.sh` any time to update the game — it keeps your world and settings.

## Install with Docker

```
cp .env.example .env
# edit .env: set SERVER_NAME and PASSWORD at least
docker compose up -d
```

Open `http://<host>:3030`. The server binary is downloaded via SteamCMD on first start and kept in the `valheim-server` volume, so later restarts don't re-download it.

## Never used Linux before?

Step by step, copy-paste each command into a terminal (open one with `Ctrl+Alt+T`, or search "Terminal" in the app menu).

1. Get the project:
   ```
   sudo apt install -y git
   git clone https://github.com/h4root/valheim-panel.git
   cd valheim-panel
   ```

2. Run the installer:
   ```
   ./install.sh
   ```
   It asks for your `sudo` password (the one you log in with), then for a game password and a server name. The download takes a few minutes.

3. Find this machine's address:
   ```
   hostname -I
   ```
   Take the first number shown, e.g. `192.168.1.50`.

4. Open `http://192.168.1.50:3030` (use your own address) in a browser — from this machine or a phone on the same network. Press **Start**, wait for the join code to appear, and give that code and the password to your friends.

Later, if you need it:
- Is it running: `systemctl status valheim-panel`
- Watch what it does: `journalctl -u valheim-panel -f`
- Update the game: `cd valheim-panel && git pull && ./install.sh`

## Config

`SERVER_NAME`, `WORLD_NAME`, `PORT`, `PASSWORD`, `PUBLIC` and `CROSSPLAY` can be set before the first start — in `.env` for Docker, or by the installer's prompts. Everything after that (world modifiers, resources, presets, access lists, backups, password) is configured from the panel UI.

| Variable | Default | Meaning |
|---|---|---|
| `SERVER_NAME` | `My Valheim Server` | name shown in the server list |
| `WORLD_NAME` | `Dedicated` | world to load or create |
| `PORT` | `2456` | game port (and `PORT+1`) |
| `PASSWORD` | — | required, 5+ characters |
| `PUBLIC` | `false` | listed in the public server browser |
| `CROSSPLAY` | `true` | PlayFab backend, no port forwarding needed |

## Stopping

Stopping the panel stops the game with it: it sends SIGINT to the game process and waits for the world to save before exiting. That applies to `systemctl stop valheim-panel`, `docker compose stop`, and the Stop button in the UI.

## Data

Native install: `~/valheim-server` — `install/` (game binary), `config/` (world saves, access lists), `logs/`, `backups/`, `panel.json`, `password`.

Docker: the same layout inside the `valheim-server` volume, under `/server`.

The panel listens on port 3030 and only answers requests from private network addresses.

## Windows

A native Windows build is on the [`windows`](../../tree/windows) branch.

## License

MIT
