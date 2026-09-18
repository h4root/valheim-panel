# Valheim Panel

A web control panel for the official Valheim dedicated server, plus a Docker image that runs both.

Start, stop, edit world modifiers, manage the admin/ban/permit lists, roll back to a backup, watch the live log — from a phone or any browser on your network.

## Run

```
cp .env.example .env
# edit .env: set SERVER_NAME and PASSWORD at least
docker compose up -d
```

Open `http://<host>:3030`.

The server binary is downloaded via SteamCMD on first start and kept in the `valheim-server` volume, so later restarts don't re-download it.

## Never used Linux before?

Step by step, copy-paste each command into a terminal (open one with `Ctrl+Alt+T`, or search "Terminal" in the app menu).

1. Install Docker:
   ```
   curl -fsSL https://get.docker.com | sudo sh
   ```

2. Download this project — either:
   ```
   git clone https://github.com/h4root/valheim-panel.git
   cd valheim-panel
   ```
   or, without git: click the green "Code" button on this page → "Download ZIP", extract it, then `cd` into the extracted folder in the terminal.

3. Create the config file and fill it in:
   ```
   cp .env.example .env
   nano .env
   ```
   Change `SERVER_NAME` and `PASSWORD`. Save with `Ctrl+O`, `Enter`, exit with `Ctrl+X`.

4. Start it:
   ```
   sudo docker compose up -d
   ```
   First start downloads the ~1 GB server, so it takes a minute or two.

5. Find this machine's address:
   ```
   hostname -I
   ```
   Take the first number shown, e.g. `192.168.1.50`.

6. Open `http://192.168.1.50:3030` (use your own address) in a browser — from the same machine or a phone on the same network.

Later, if you need it:
- Watch it run: `sudo docker compose logs -f`
- Stop it: `sudo docker compose down` (the world is kept in a separate volume, this doesn't delete it)
- Update: `git pull && sudo docker compose up -d --build`

## Config

Set once in `.env` before the first start — the panel takes over from there and everything else (world modifiers, resources, presets, access lists, backups) is configured from the UI:

| Variable | Default | Meaning |
|---|---|---|
| `SERVER_NAME` | `My Valheim Server` | name shown in the server list |
| `WORLD_NAME` | `Dedicated` | world to load or create |
| `PORT` | `2456` | game port (and `PORT+1`) |
| `PASSWORD` | — | required, 5+ characters |
| `PUBLIC` | `false` | listed in the public server browser |
| `CROSSPLAY` | `true` | PlayFab backend, no port forwarding needed |

## Stopping

`docker compose stop` sends SIGTERM to the panel, which forwards SIGINT to the game process and waits for it to save before exiting. Stopping through the panel UI does the same thing directly.

## Data

Everything lives in the `valheim-server` volume: `/server/install` (game binary), `/server/config` (world saves, access lists), `/server/logs`, `/server/backups`, `/server/panel.json` (panel settings), `/server/password`.

## Windows

A native Windows build (no Docker) is on the [`windows`](../../tree/windows) branch.

## License

MIT
