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
