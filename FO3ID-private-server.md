# FIFA Online 3 Indonesia — Private Server Research

Client: `D:\FO3ID\32795` · build `20200217` · engine EA/Spearhead, publisher Garena.
Status: client-side flow mapped from static + runtime analysis. Server not yet built. One
hard blocker (XignCode update) stands before the game reaches its network stage.

---

## 1. What the client is

| Piece | Finding |
|---|---|
| `fifalauncher.exe` | Garena launcher (`fifalauncher_garena_zf.pdb`, project `FO3_SEA_STREAM`). Packed with Themida/WinLicense. Exits silently if not invoked by the Garena client with a token. |
| `fifazf.exe` | Game client (`build/runtime/pc/fifazf.pdb`). Themida-packed. Reads server config from command-line args. |
| `fifazf.hsb` | AhnLab HackShield config — chunks `TOOLINFO`, `SVRCONF`, `MEMCRC`, `FILEHASH`. Verifies file + memory integrity, so patching the exe is detected here. |
| `nmcogame.dll` | Generic Nexon messenger SDK. IPs inside are Nexon KR/JP, NOT FO3ID servers. |
| `XignCode/` | Wellbia XIGNCODE3 anti-cheat (`x3.xem` v3.5.0.63, `xcorona*.xem`, `xm.exe`). Loads a kernel driver at runtime. |
| `data/*.big` | EA "BIg4" archives (~3.8 GB). Offsets in header valid; entry table obfuscated. Not needed — assets already local. |
| `launcherConfig.ini` | Per-line XOR-ish encrypted (bytes 0–127). Key lives inside the packed launcher; no key reuse across lines, so not crackable standalone. Likely holds server URLs. |

## 2. Login flow (from unpacked launcher strings)

1. Version check over HTTPS (libcurl + OpenSSL).
2. Authenticate to Garena SSO server.
3. Authenticate to game server — **two stages** ("Koneksi sukses setelah otentikasi server game yang kedua").
4. Connect to notification server.
5. Launcher spawns `fifazf.exe` with server address as arguments.

IPs seen in memory (both presumed dead): `125.141.155.155` (KR), `203.117.155.193:8080` (Garena SG).

## 3. fifazf.exe command line (CONFIRMED working format)

Launcher can be bypassed. `fifazf.exe` parses args as `-SECTION/KEY=value`:

```
fifazf.exe -QUICKMATCH/SERVER=127.0.0.1:28000 -QUICKMATCH/PROTOCOL=tcp \
  -QUICKMATCH/NOTISERVER=127.0.0.1:28001 -FIFA/LAUNCHER/PID=<pid> \
  -PUBLISHER/GARENA/TOKEN=<token> -LOCALE/PUBLISHING_REGION=ID \
  -DUMPTRUCK/SERVER=127.0.0.1
```

Args are read (verified by diffing process memory across runs). Other known keys:
`QUICKMATCH/RELAYSERVER`, `QUICKMATCH/ACCOUNTID`, `QUICKMATCH/NOTISERVER`,
`RELEASE/SERVER`, `RELEASE/UID`, `CHECK/TESTSERVER`, `DUMPTRUCK/PORT`.

## 4. Networking engine = EA Blaze

Memory strings confirm the Blaze stack (same as other EA titles):
`Blaze::Redirector::ServerInstance`, `redirector/getServerInstance`,
`gosredirector.ea.com`, `gosredirector.online.ea.com`, `LOGIN %s %s`,
`BLAZESERVER_CONN_LOST`, `DUPLICATE_LOGIN`. Credit screen: "FIFA ONLINE3 - Developed by SPEARHEAD".

HTTP service endpoints (relative paths) found in client:
`/info/server/config`, `/game/my/ipaddress`, `/publisher/garena/sign`,
`/publisher/nexon/2ndauth`, `/telemetry/gameconnection`, `/shop/cash/charge/url`,
`/shop/cash/history/url`.

→ A private server reimplements the Blaze Redirector + login/lobby + these HTTP endpoints.
Existing open-source Blaze emulators for other EA games are the reference starting point
(protocol family is shared; FO3 component IDs/messages differ and must be mapped).

## 5. THE BLOCKER: XignCode update (open)

Every run with valid args reaches XignCode, then exits code 1 at ~9–19s, **before** any
game-server connection. Root cause captured on the wire:

```
proc xcoronahost.xem  ->  GET http://dl.garenanow.com/games/fo3id/xigncode/CG_cjbpitNjWID/List/current.rev
real Garena server    ->  403 Forbidden   (FO3ID files removed)
```

Redirect of `dl.garenanow.com` -> local HTTP server succeeded (needs a local DNS server;
the `hosts` file is ignored because XignCode resolves DNS itself). But the correct
`current.rev` body is unknown — 6 plaintext guesses all rejected, so it is binary/signed.

**Recovery of the real file failed:** Wayback never archived the `xigncode/` path (only
installers, shop art, and fo3th/fo3sg patchers are archived). The update files were fetched
live and never crawled.

Remaining legitimate options for this blocker (not yet done):
- Pull `current.rev` + file-list from another intact FO3ID (Garena) install, or an official
  installer/patcher that still contains them, and serve them unmodified from the local host.
- Check whether an existing FO3 private-server community (VN/KR/TH) already solved the
  XignCode-offline step and documented it.

Checked, did NOT help: `D:\FOID MOK\downloads\FIFA Online 3 (Nexon)` (Korean/Nexon build,
2017). No `current.rev` and no `List/` dir. Different publisher entirely — Nexon `NGClient.aes`
+ `BlackCipher/` (nProtect GameGuard: `BlackXchg.aes`, `config.bc`), not Garena's
`dl.garenanow.com` update flow — so it carries none of FO3ID's update files. Its `.xem` are a
different, older, incomplete set (missing `xcoronahost.xem`/`xmag.xem`); `fifazf_org.exe` is
0 bytes (empty placeholder, not a usable original). The KR build also stacks a second
anti-cheat (BlackCipher), so it is harder, not a shortcut. Still need an intact FO3ID install.

Checked the user's OWN install `D:\FO3ID\32795` for a cached authentic `current.rev` — also a
dead end. The Garena patcher data (`.gpipe/manifest/.manifest`, `.gpipe/index/depot_19...txt`,
`appinfo.gpipe`) is encrypted/high-entropy, no plaintext file list. `xigncode.log` is binary
(only the `CG_cjbpitNjW` id is readable, no rev value). No standalone `.rev` file anywhere.
`_cache/` holds only club cache. **Key realisation:** `current.rev` is a transient server
response XignCode fetches live and validates — it is never persisted to disk, so it cannot be
recovered from any install (this one or another). The "serve the real file" path is therefore
not viable: the file does not exist to copy. Passing XignCode for a dead-server game realistically
needs either a community that documented an offline method, or neutralising XignCode — the latter
is out of scope (see below). This is the project's blocking wall.

> Not pursued: writing a stub/replacement for `x3.xem` or defeating the HackShield/Themida
> integrity checks. That is circumvention of a commercial anti-cheat still used by live
> games, so it is out of scope here.

## 6. Suggested build order (once XignCode is unblocked)

1. Local DNS + HTTP host for `dl.garenanow.com` serving the real XignCode update files.
2. Blaze Redirector on `:28000` answering `getServerInstance` with the local game-server endpoint.
3. Blaze login/lobby component — handle `LOGIN`, user identification, persona.
4. HTTP config service for `/info/server/config`, `/publisher/garena/sign` (stub token accept), `/game/my/ipaddress`.
5. Notification server on `:28001`.
6. Capture + document the Blaze message IDs the client sends at each step (protocol RE),
   fill in responses until the client advances.

## 7. Legal note

Copyright is EA / Nexon / Spearhead / Garena. Running a private server from your own client
for offline/personal use is the assumed scope. Public hosting or distribution carries real
legal risk.

---

### Analysis scripts (session scratchpad)
`strs.py` (string extractor) · `pe.py` (PE sections/entropy/imports) · `hx.py` (hex dump) ·
`cfg.py` (hsb/config inspector) · `mtp.py` (XOR key-reuse test) · `run.py`/`fast.py`/`game.py`
(launch + netstat + memory-string monitor) · `xcserver.py` (local HTTP mirror) ·
`xcdns.py` (local DNS override) · `dns.py` (pcapng DNS/HTTP parser).

All analysis was read-only against the game files; nothing in `D:\FO3ID\32795` was modified.
