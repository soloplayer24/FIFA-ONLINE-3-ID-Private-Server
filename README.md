# FO3ID Private Server

A local private server for **FIFA Online 3 Indonesia** (Garena, client build 654420), written in
Python. Garena shut the official servers down on 2020-06-30; this project re-implements the
backend the client talks to, so the game can be played offline on one PC.

> Not affiliated with EA, Spearhead or Garena. This repository contains **no game files** (no
> client, database, assets or music) — you need your own FO3 ID install. It does **not** include,
> modify or circumvent the client's anti-cheat (XIGNCODE3 / HackShield); that is out of scope.
> Built for game preservation and research.

## What works

Tested in the real client (Indonesian UI):

- Login → manager select / create (club picker over 501 licensed clubs) → lobby (FREEROAM).
- **Single → Liga** (full season: calendar, matches, table, rewards, promotion) and **Cup**
  (draw, groups, knock-out, extra time, penalties).
- **Market** (buy / sell, fifaaddict prices), **Upgrade** (grade +1…+10), **Tukar** and
  **Tukar Scout** player trades, Tim inti / Tim Cadangan, squad and tactics saving.
- **Shop** with every tab (player packs, consumables, packages, MP Shop, monthly and chained
  packages, special bonus), pack opening with random grades, Item saya, Bag, daily login reward.
- Always VIP + official PC warnet (IGR) perks, manager delete / restore, season '17 cards.
- Formation list (the club's own + the game's 33 common formations, read from the client's
  formations table) and manager / player levels: EXP after every match, player LV.1-23
  (+1 to every attribute per level), manager LV.1-100.
- Game sound and the EA Trax menu music; `tools/eatrax_song.py` puts your own song first in it.
- Admin web page on http://127.0.0.1:8088/ (EP, players, packs, gifts, daily rewards, log).

Not done: online modes (ranked, friendly, King of the Hill), Road to World, league simulation.

## Requirements

- Windows, Python 3.12, `pip install cryptography` (messenger + DB extraction).
- An FO3 ID client install (default path `D:\FO3ID\32795`).
- Optional: ffmpeg (only for `tools/eatrax_song.py`).

## Setup

1. Extract the player database from your client:
   `python extract_fifadb.py D:\FO3ID\32795` → `gamedata/fifadb.sqlite`.
2. Optional market prices: `python tools/fa_sync.py` → `gamedata/fa_prices.json` (polite
   fifaaddict.com scraper; without it prices come from a rating formula).
3. TLS certificate for the client's hardcoded API host `APSEODESK172` (self-signed is accepted):

   ```
   openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=APSEODESK172" -addext "subjectAltName=DNS:APSEODESK172" -keyout key.pem -out cert.pem
   python -c "open('certs/server.pem','w').write(open('cert.pem').read()+open('key.pem').read())"
   ```

4. Hosts file (as admin): `127.0.0.1 APSEODESK172`.
5. Start the two servers:

   ```
   python -I http_config.py 443 --tls
   python messenger.py
   ```

6. Start the client with the server arguments (`launcher/fifalauncher.cs` builds a drop-in
   launcher; `launch_game.ps1` does the same from PowerShell):

   ```
   fifazf.exe -QUICKMATCH/SERVER=127.0.0.1:28000 -QUICKMATCH/PROTOCOL=tcp -QUICKMATCH/NOTISERVER=127.0.0.1:28001 -FIFA/LAUNCHER/PID=<pid> -PUBLISHER/GARENA/TOKEN=testtoken -LOCALE/PUBLISHING_REGION=ID -DUMPTRUCK/SERVER=127.0.0.1 region id token testtoken
   ```

## Files

| File | Role |
|---|---|
| `http_config.py` | The REST backend the client calls (`https://APSEODESK172`): login, account, coaches, club creation, squad, league, cup, market, upgrade, trades, shop, items, Bag, daily reward, static tables, and `/cdn/` (serves crests and cards from the client's own `_cache`). State lives in `gamedata/state.json`. Env: `FO3_LANGUAGE` (default `ind`), `FO3_ASSETS`, `FO3_HALF_LENGTH`, `FO3_ADMIN_PORT`. |
| `messenger.py` | Notification server on `:28002`. Frames `<u16 LE len><u16 type=2><Blowfish-CBC(key "kueec", IV 0) of JSON+"\n">`; answers `auth`, `ping`, `signon`. `python messenger.py --selftest`. |
| `admin.py` + `admin/index.html` | Admin page (started by `http_config.py`, localhost only, no password). |
| `responses/` | Per-endpoint JSON bodies (`/a/b/c` → `responses/a_b_c.json`, re-read on every request). Shapes come from the client's AS3 code. |
| `extract_fifadb.py` | Client player DB (`data_0.big`, RefPack + Blowfish) → SQLite. |
| `gen_teams.py` | Starter squads and club summaries from `gamedata/fifadb.sqlite`. |
| `tools/eatrax_song.py` | Puts a song first in the game's EA Trax menu music (edits `data_0.big` / `data_1.big` by appending; `--restore` puts the originals back). |
| `tools/fa_sync.py` | fifaaddict.com price sync. |
| `tools/re/` | Reverse-engineering helpers: `bigdir.py` (BIg4 archive tables), `refpack.py` (RefPack codec), `eadb.py` (EA "DB" database tables), `abc*.py` (AS3 bytecode parser / grep / schema miner), `gfxx.py` (UI SWFs), `memgrab.py`, `gamectl.ps1` (drive the client window for tests). |
| `blaze.py`, `server.py`, `test_client.py`, `analyze_capture.py` | EA Blaze framing (Fire/TDF) and a loopback test server from the first phase of the project; the client turned out to use REST + the messenger instead. |
| `analysis/` | Early client analysis scripts (strings, PE sections, network endpoints). |
| `FO3ID-private-server.md`, `reports/` | Findings and progress reports (`reports/` is in Indonesian). |

## Tests

```
python run_tests.py
```

Runs the Blaze/loopback self-tests and `test_league.py`, which drives every backend feature
(club creation, league, cup, market, upgrade, trades, shop, daily reward, admin API, manager
delete) against a fresh account. Needs `gamedata/fifadb.sqlite`.

## Contributors

- **Wahyu** — project owner, testing in the real client.
- **Claude** (Anthropic's AI assistant, via Claude Code) — reverse engineering and most of the
  server code, written together with the owner.

## License

[GNU GPL v3](LICENSE) for the code and documentation in this repository. FIFA Online 3, its
client, data, assets and music belong to their respective owners and are not covered.
