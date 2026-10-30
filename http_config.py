"""HTTP config/publisher service stub for FO3ID.

The client requests these relative paths (found in client memory):
    /info/server/config        -> expected: server list / endpoints config
    /game/my/ipaddress         -> expected: client's public IP echo
    /publisher/garena/sign     -> publisher token sign/verify
    /publisher/nexon/2ndauth   -> secondary auth
    /telemetry/gameconnection  -> telemetry sink (can be a no-op)
    /shop/cash/charge/url, /shop/cash/history/url -> cash shop URLs

Phase 1: log every request (method, path, query, headers, body) and return a minimal
placeholder so we can see which endpoint the client hits first and what it expects.
The real response shapes are unknown until captured — every handler is a guess.

Usage: python -I http_config.py [port=8080] [--tls]
Default port 8080 to avoid colliding with a local Apache on :80.

--tls serves HTTPS with certs/server.pem. fifazf.exe calls a hardcoded
https://APSEODESK172 (port 443) for /info/server/locale etc. — no launch arg
overrides it — so run `http_config.py 443 --tls` and map APSEODESK172 to
127.0.0.1 in the hosts file.
"""
import bisect, collections, functools, gzip, http.server, math, os, random, re, sqlite3, ssl, sys, threading, time
from contextlib import closing

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # `python -I` leaves the script dir off sys.path
import gen_teams
TLS = "--tls" in sys.argv
_args = [a for a in sys.argv[1:] if a != "--tls"]
PORT = int(_args[0]) if _args else 8080
LOG = os.path.join(HERE, "captures", "http_config.log")
CERT = os.path.join(HERE, "certs", "server.pem")  # self-signed, CN/SAN APSEODESK172
os.makedirs(os.path.dirname(LOG), exist_ok=True)

import json

# Where this service tells the client the game servers live.
REDIR_HOST, REDIR_PORT = "127.0.0.1", 28000
NOTI_HOST, NOTI_PORT = "127.0.0.1", 28001

# --------------------------------------------------------------------------- #
# Endpoint bodies — TENTATIVE. The real response format (XML vs JSON, field
# names, the token/sign scheme) is unknown: the client has not been captured
# reaching these (XignCode blocks it earlier — see README). These return minimal
# plausible JSON so the client has *something* to parse; correct each shape from
# a capture. Every request is logged regardless, which is the point for now.
# --------------------------------------------------------------------------- #

def _json(obj):
    return ("application/json", json.dumps(obj).encode())


def ep_server_config(query, body):
    return _json({
        "redirector": {"host": REDIR_HOST, "port": REDIR_PORT},
        "notification": {"host": NOTI_HOST, "port": NOTI_PORT},
        "region": "ID",
        "status": "ok",
    })


def ep_locale(query, body):
    # First call fifazf.exe makes. Body "OK" -> JSON_PARSING_ERROR in-game; {"result": true}
    # passes. `language` must be a WebCache.setBaseUrl key (kor eng chi vie tha ind jpn,
    # default kor): "id" crashed the UI (the old black screen); "ind" = Indonesian, its
    # externalAssets/language_ind.big is in the client cache. Publisher (Garena) is chosen
    # by launch args `region id token <t>`, not by this response.
    # `url` = WebCache base for externalAssets (crests, uniforms, player cards...). Without
    # it the client uses the dead Garena CDN, every refresh fails and crests show
    # "PLACE HOLDER" even though the files sit in its own _cache. Served by cdn_file().
    return _json({"result": True, "language": LANGUAGE, "url": f"https://APSEODESK172/{CDN_PREFIX}"})


LANGUAGE = os.environ.get("FO3_LANGUAGE", "ind")


# The client's own downloaded copy of the Garena CDN (externalAssets/...).
ASSETS = os.environ.get("FO3_ASSETS", r"D:\FO3ID\32795\_cache\games\fo3id")
CDN_PREFIX = "cdn/"


def cdn_file(rel):
    """(content-type, bytes) of ASSETS/rel, or None (-> 404, client shows its default)."""
    f = os.path.normpath(os.path.join(ASSETS, rel))
    if not f.startswith(os.path.normpath(ASSETS) + os.sep) or not os.path.isfile(f):
        return None
    ctype = {".png": "image/png", ".jpg": "image/jpeg", ".swf": "application/x-shockwave-flash"}
    with open(f, "rb") as fh:
        return (ctype.get(os.path.splitext(f)[1].lower(), "application/octet-stream"), fh.read())


def ep_server_time(query, body):
    # TimerUtil.getTime: retries every second (and holds the bootflow) until `timestamp`
    # (ms, fed to new Date()) is present. timezone guessed as JS getTimezoneOffset() for
    # WIB (UTC+7) — the backend looked like Node/Express.
    return _json({"timestamp": int(time.time() * 1000), "timezone": -420})


def ep_my_ip(query, body):
    # real HTTP PUT from the lobby; every reply is JSON-parsed
    return _json({"result": True, "ipaddress": "127.0.0.1"})


def ep_garena_sign(query, body):
    # Accept any token for local offline use; echo a success + a fake signed token.
    return _json({"result": "ok", "token": "LOCAL-SIGNED-TOKEN", "uid": 1})


def ep_nexon_2ndauth(query, body):
    return _json({"result": "ok", "passport": "LOCAL-PASSPORT"})


def ep_telemetry(query, body):
    # Client JSON-parses every reply; plain "OK" -> JSON_PARSING_ERROR in-game.
    return _json({"result": True})


# The starter song (music/starter.mp3) lives in the game's own EA Trax playlist as TrackID 1,
# put there by tools/eatrax_song.py; the server does not play music.


def ep_empty(query, body):
    return _json({})


def ep_shop_url(query, body):
    return _json({"url": "about:blank"})


# ponytail: one local account kept in a JSON file; a real multi-user store comes later.
STATE = os.path.join(HERE, "gamedata", "state.json")
# Handlers read-modify-write state.json; game requests and the admin page (admin.py) take this
# so one never reads a half-written file or overwrites the other's change.
STATE_LOCK = threading.Lock()


def _state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"coaches": [], "defaultcoachid": "", "teams": {}}


def _save(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(st, f)


def coach_id(slot):
    return f"{slot + 1:024x}"  # Mongo-style _id


def ep_coaches(query, body):
    # StartupCoachCreationScreen: coachlist entries need _id, slot, name, state "active"
    # ("postpone" = deleted, restorable until hideat; recoveryperiod s goes in the delete notice).
    # initializeMyCoach also shows team name/crest from summary and teamvalue.val, same
    # values as the lobby HUD (coach_me).
    st = _state()
    if purge_deleted_coaches(st):
        _save(st)
    def entry(c):
        me = coach_me(st, c["slot"])
        return dict(c, summary=me["summary"], teamvalue={"val": sum(p["price"] for p in me["team"]["players"])},
                    levelinfo=me["levelinfo"],
                    recoveryperiod=COACH_RECOVERY, **({"hideat": w3c(c["deleteat"])} if "deleteat" in c else {}))
    usable = {c["_id"] for c in st["coaches"] if c.get("state", "active") == "active"}
    return _json({"coachlist": [entry(c) for c in st["coaches"]],
                  "defaultcoachid": st["defaultcoachid"] if st["defaultcoachid"] in usable else ""})


def ep_new_team(slot, query, body):
    # POST /coach/<slot>/new/step/3 {"id": teamid, "spids": [...]}: the client passes this
    # body on as the new club and reads data.team.players ("could not take the random
    # players!" otherwise). Built from gen_teams.py's roster file for that team.
    req = json.loads(body or b"{}")
    chosen = set(req.get("spids", []))
    team = roster(int(req.get("id") or 0))["team"]
    team["players"] = [p for p in team["players"] if p["spid"] in chosen]
    team["squad"]["positions"] = [p for p in team["squad"]["positions"] if p["spid"] in chosen]
    fix_formation(team, int(req.get("id") or 0))
    st = _state(); st["teams"][str(slot)] = team; _save(st)
    return _json({"result": True, "team": team})


def ep_create_coach(slot, query, body):
    # POST /coach/<slot> {"id": teamid, "coachName"}: needs body.result
    req = json.loads(body or b"{}")
    st = _state()
    purge_deleted_coaches(st)
    st["coaches"] = [c for c in st["coaches"] if c["slot"] != slot] + [{
        "_id": coach_id(slot), "slot": slot, "name": req.get("coachName", ""), "state": "active",
        # StartupCoachCreationScreen.initializeMyCoach reads teamvalue.val, levelinfo.level, summary
        "teamid": req.get("id"), "teamvalue": {"val": 0}, "levelinfo": {"level": 1}, "summary": {}}]
    _save(st)
    return _json({"result": True})


# Deleting a manager (StartupCoachCreationScreen > Delete): PUT /coach/<slot>/postponed puts it in
# state "postpone" until hideat; "Kembalikan manager" = DELETE /coach/<_id>/postponed undoes it.
# Once hideat has passed CoachUtil.shouldDeleteCoach shows the slot as free ("Buat manager baru").
COACH_RECOVERY = 300  # s a deleted manager can still be restored (the real game: 72 h)


def purge_deleted_coaches(st):
    """Drop managers whose restore time is over, with their club. The account-wide club data
    (league, cups, market offers, last match, trade) belongs to the default manager's club."""
    gone = [c for c in st["coaches"] if c.get("state") == "postpone" and c.get("deleteat", 0) <= time.time()]
    for c in gone:
        st["coaches"].remove(c)
        st["teams"].pop(str(c["slot"]), None)
        if c["_id"] == st["defaultcoachid"]:
            st["defaultcoachid"] = ""
            for k in ("league2", "cups", "offers", "match", "trade"):
                st.pop(k, None)
    return gone


def ep_coach_postponed(key, query, body):
    """PUT /coach/<slot>/postponed: delete a manager; DELETE /coach/<_id>/postponed: restore it."""
    st = _state()
    purge_deleted_coaches(st)
    c = next((c for c in st["coaches"] if key in (str(c["slot"]), c["_id"])), None)
    if c is None:
        reply = {"result": False, "reason": "already removed"}  # restore: COACH_CREATION_RESTORE_ERROR
    elif json.loads(body or b"{}").get("_method") == "DELETE":
        c["state"] = "active"
        c.pop("deleteat", None)
        reply = {"result": True}
    else:
        c["state"] = "postpone"
        c["deleteat"] = time.time() + COACH_RECOVERY
        reply = {"result": True}
    _save(st)
    return _json(reply)


POS_SUB = 28  # Enums.PLAYERPOSITION: 0..27 pitch positions, 28 SUB, 29.. RESERVE


# OptionManager.applySoundSetting hands native volume * Master (each 0..10) for Commentary,
# PAVolume, PlayerCalls, SFX, Crowd and BGM; a missing key is NaN = silent (no music).
SOUND_DEFAULTS = {"Master": 10, "Commentary": 10, "PAVolume": 10, "PlayerCalls": 10, "SFX": 10, "Crowd": 10, "BGM": 10}


def ep_account(query, body):
    """GET /account (AccountProxy.accountData): responses/account_base.json, with the options
    the client saved through PUT /account/option layered over `option`."""
    with open(os.path.join(RESPONSES, "account_base.json"), encoding="utf-8") as f:
        acc = json.load(f)
    acc["option"].update(_state().get("option", {}))
    acc["option"]["soundOption"] = {**SOUND_DEFAULTS, **(acc["option"].get("soundOption") or {})}
    return _json(acc)


def ep_account_option(query, body):
    """PUT /account/option {soundOption, screenOption, chatHotKeyOption, aiOption, cameraOption}
    (sent e.g. when leaving a match). Never store controlOption: {} there crashes the input
    thread (see account_base.json)."""
    req = json.loads(body or b"{}")
    st = _state()
    st["option"] = {k: v for k, v in req.items() if k.endswith("Option") and k != "controlOption"}
    _save(st)
    return _json({"result": True})


def ep_coach_squad(query, body):
    """PUT /coach/me/squad {squad:{positions,formation,...}, kickers, captain} from Tim > Formasi.
    Player ids are instance ids (iid)."""
    req = json.loads(body or b"{}")
    st = _state(); slot = _default_slot(st); team = st["teams"].get(str(slot))
    sq = req.get("squad") or {}
    spid_of = {iid(p): p["spid"] for p in team["players"]} if team is not None else {}
    positions = [{"id": str(p["id"]), "spid": spid_of[str(p["id"])], "pos": p["pos"],
                  **({"starting": p["starting"]} if p.get("starting") else {})}
                 for p in sq.get("positions") or [] if str(p.get("id")) in spid_of and p.get("pos", 99) <= POS_SUB]
    # a client still holding old instance ids (e.g. from before a card migration) must not
    # wipe the XI: keep the stored squad unless the new one has 11 known starters
    if sum(p["pos"] < POS_SUB for p in positions) == 11:
        team["squad"]["positions"] = positions
        for k in ("formation", "workrate"):
            if k in sq:
                team["squad"][k] = sq[k]
        if str(req.get("captain")) in spid_of:
            team["squad"]["captain"] = str(req["captain"])
        if req.get("kickers") and all(str(k) in spid_of for k in req["kickers"] if k):
            team["squad"]["kickers"] = [str(k) for k in req["kickers"] if k]
        _save(st)
    return _json({"result": True, "resources": coach_me_resources(st, slot)})


def roster(teamid):
    """{summary, team} of a club: the gen_teams.py roster file if there is one, else built
    the same way straight from fifadb (league opponents)."""
    f = os.path.join(RESPONSES, f"static_info_authentic_team_all_{teamid}.json")
    if os.path.isfile(f):
        with open(f) as fh:
            return json.load(fh)
    with closing(_db()) as c:
        return gen_teams.team(c, teamid) or {"summary": {}, "team": {"players": [], "squad": {"positions": [], "kickers": []}}}


def roster_team(teamid):
    """A club as a stored team, for AI opponents."""
    return roster(teamid)["team"]


def match_side(raw, teamid, user=None, tactics=None):
    """match.side.<left|right>: PlayerListUtil.generateCaptainFromMatchInfo reads side.team
    {teamid, squad, players}, side.players (the users: id, name, levelinfo), MatchProxy
    fetchMatchSide* read squad, tactics, ready. tactics is the preset ARRAY (as
    /game/my/tactic/all): native 0x949170 iterates it ({id, bus, cc, def, teammentality})."""
    team = owned_team(raw)
    team.update(teamid=teamid, summary=team_summary(team, teamid), kit=team_kit(teamid))
    # ready: a user side starts unready -- MatchController.sendReady skips the PUT .../ready
    # (no kick-off) when isMySideReady is already true; AI sides are always ready.
    return {"team": team, "squad": team["squad"], "players": [user] if user else [],
            "tactics": tactics or [default_tactic(0)], "ready": user is None, "kit": team_kit(teamid), "club": {}}


# gamesettings.halfLength, in 1/10 minute per half (the game setting screen shows
# halfLength/10*2 "menit"): 30 = a 6-minute match.
HALF_LENGTH = int(os.environ.get("FO3_HALF_LENGTH", "30"))


def new_match(mtype, left, right, **extra):
    """A /match/<id> resource (MatchProxy, base uri /match). MatchController.onMatchChangeReceived
    runs processMatch* over: side.X {ready, club, players, squad, team{teamid, players, captain,
    kit, summary.league}}, elo.X.rating, gamesettings {kit, ball, stadium, tod, weather,
    halfLength, difficulty}, result, reward, boostweekid, stage (created|squad|playing|canceled).
    These run inside a native->AS3 callback: a TypeError there aborts the client (0xDEAD)."""
    mid = f"{int(time.time()):08x}{os.urandom(8).hex()}"  # Mongo-style id
    lt, rt = left["team"]["teamid"], right["team"]["teamid"]
    match = {"id": mid, "type": mtype, "created": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
             "stage": "created", "side": {"left": left, "right": right},
             "elo": {"left": {"rating": 1000}, "right": {"rating": 1000}},
             "result": None, "reward": None, "boostweekid": 0,
             # native ApplyGameSettings gets this object as-is.
             # kit: [left uniform id, right uniform id] as STRINGS: AS3 reads them in
             # processMatchSideStandingPlayerResource, native (0x9480D0) with asCString
             # home: Boolean (MatchProxy.isLeftTeamHome = gamesettings.home as Boolean)
        "gamesettings": {"home": True, "simulated": False, "canHaveReferee": False,
                              "stadiumchange": False, "timechange": False, "weatherchange": False,
                              "matchtimechange": False, "cardstrictness": 0, "foulstrictness": 0,
                              # kit: per side the KIT TYPE ("home"/"away"); native (0x948260) loads
                              # side kit.availables[<type>].id -- uniform ids here = checkerboard kits.
                              # ball.availables feeds MatchProxy.ballsDataProvider; empty -> the
                              # game setting screen's ball stepper reads item 0 = null (UI error).
                              # 0 = the ball the practice match loads (id -> db.ball model).
                              "kit": ["home", "away"],
                              "ball": {"id": 0, "availables": [{"id": 0}]},
                              # stadium 112 = the lobby stadium (data/sceneassets/stadium/stadium_112.rx3
                              # is known to load); null -> crash while loading the GAME scene
                              "stadium": 112, "tod": 0, "weather": 0, "difficulty": 0,
                              "halfLength": HALF_LENGTH,
                              # the rest is what native ApplyGameSettings (0x946FD0) reads; values are
                              # its own defaults (0x959050). Missing gameSpeed broke the match clock.
                              "gameSpeed": 1, "crowd": 100, "pitchWear": 1, "refereeIdSelector": 845,
                              "subRefereeIds": [0, 0], "disableMomentum": False, "bookingsOn": True,
                              "offsidesOn": True, "injuriesOn": True, "foulsOn": True, "fatigueOn": True,
                              "subOnBench": 5, "availableSubs": 3, "drawMode": 0, "gameType": mtype,
                              "shootoutmode": False, "prevScore": {"home": 0, "away": 0}}}
    match.update(extra)
    return match


def ep_match_campaign(query, body):
    """POST /match/campaign (tutorial > Training Ground): MatchController
    .onMatchPracticeResponseReceived registers resources[] and fires ON_PRACTICE_STARTED with
    the match body -> ApplyGameSettings + StartSingleMatch (a local match in the client)."""
    st = _state(); slot = _default_slot(st); me = coach_me(st, slot)
    user = {"id": me["id"], "name": me["name"], "levelinfo": me["levelinfo"]}
    ai = 243 if me["teamid"] != 243 else 241
    # type must be a non-PvP one (MatchController.isPvPMatchType: League_league, Cup_PvE,
    # WorldCup_PvE, WorldCup_Live, League_2_league, League_2_league_MGR); anything else makes
    # stage "created" call connectToTheSession -> native LobbyManager::ConnectSession, which
    # needs relayServer.address (crash without it).
    match = new_match("League_league", match_side(st["teams"][str(slot)], me["teamid"], user, _tactics(st)[1]),
                      match_side(roster_team(ai), ai))
    return _json({"result": True, "resources": [{"uri": f"/match/{match['id']}", "body": match}]})


def roster_summary(teamid):
    """Static team info (name, league, nation, rival) written by gen_teams.py."""
    s = roster(teamid).get("summary", {})
    return {k: s[k] for k in ("name", "league", "nation", "rival") if k in s}


# --------------------------------------------------------------------------- #
# Player cards. Ratings come from the client's own fifadb, EP prices from fifaaddict.com's
# FO3 DB (Indonesia server, grade +1; gamedata/fa_prices.json, see tools/fa_sync.py).
# --------------------------------------------------------------------------- #
# all-attribute boost by grade +0..+10 (= responses/static_db_PLAYERGRADE.json): fifaaddict's
# playerEnchantList, e.g. World Best Neymar 83 base shows 88 at +1 and 93 at +5
GRADE_BOOST = (0, 5, 6, 7, 8, 10, 12, 14, 17, 20, 24)
# EP price of grade g relative to +1: geometric mean over 16 fifaaddict cards (Indonesia server)
GRADE_PRICE = (0, 1, 2.65, 7.1, 19.6, 53, 162, 420, 1090, 2680, 6290)
FA_PRICES = os.path.join(HERE, "gamedata", "fa_prices.json")
_FA = None
_OVR = {}


def fa_prices():
    """{spid: Indonesia-server EP price at +1 (0 = listed on fifaaddict without a price)};
    {} when the file is missing."""
    global _FA
    if _FA is None:
        try:
            with open(FA_PRICES) as f:
                _FA = {int(k): int(v) for k, v in json.load(f).items()}
        except OSError:
            _FA = {}
    return _FA


# The rating the client shows for a card ("Atribut"/OVR, as on fifaaddict) is Enums.calcPosOvr
# at its preferredposition1: floor(sum(stat * weight) / 100) with STAT_POSOVRS weights below
# (fifadb's overallrating column runs ~5 higher for 2017 cards). POS_ROLE[Enums.PLAYERPOSITION].
ROLE_WEIGHTS = {
    "GK": {"gkdiving": 21, "gkhandling": 21, "gkkicking": 5, "gkpositioning": 21, "gkreflexes": 21, "reactions": 11},
    "SW": {"aggression": 8, "ballcontrol": 5, "headingaccuracy": 10, "jumping": 4, "marking": 15, "reactions": 5,
           "shortpassing": 5, "slidingtackle": 15, "standingtackle": 15, "strength": 10, "interceptions": 8},
    "WB": {"acceleration": 4, "ballcontrol": 8, "crossing": 12, "dribbling": 4, "marking": 7, "reactions": 8, "shortpassing": 10,
           "slidingtackle": 11, "sprintspeed": 6, "stamina": 10, "standingtackle": 8, "interceptions": 12},
    "FB": {"acceleration": 5, "ballcontrol": 7, "crossing": 9, "headingaccuracy": 4, "marking": 8, "reactions": 8, "shortpassing": 7,
           "slidingtackle": 14, "sprintspeed": 7, "stamina": 8, "standingtackle": 11, "interceptions": 12},
    "CB": {"aggression": 7, "ballcontrol": 4, "headingaccuracy": 10, "jumping": 3, "marking": 14, "reactions": 5, "shortpassing": 5,
           "slidingtackle": 17, "sprintspeed": 2, "standingtackle": 10, "strength": 10, "interceptions": 13},
    "DM": {"aggression": 5, "ballcontrol": 10, "longpassing": 10, "marking": 9, "reactions": 7, "shortpassing": 14, "slidingtackle": 5,
           "stamina": 6, "standingtackle": 12, "strength": 4, "interceptions": 14, "vision": 4},
    "WM": {"acceleration": 7, "ballcontrol": 13, "crossing": 10, "dribbling": 15, "finishing": 6, "longpassing": 5, "positioning": 8,
           "reactions": 7, "shortpassing": 11, "sprintspeed": 6, "stamina": 5, "vision": 7},
    "CM": {"ballcontrol": 14, "dribbling": 7, "finishing": 2, "longpassing": 13, "longshots": 4, "positioning": 6, "reactions": 8,
           "shortpassing": 17, "stamina": 6, "standingtackle": 5, "interceptions": 5, "vision": 13},
    "AM": {"acceleration": 4, "agility": 3, "ballcontrol": 15, "dribbling": 13, "finishing": 7, "longpassing": 4, "longshots": 5,
           "positioning": 9, "reactions": 7, "shortpassing": 16, "sprintspeed": 3, "vision": 14},
    "CF": {"acceleration": 5, "ballcontrol": 15, "dribbling": 14, "finishing": 11, "headingaccuracy": 2, "longshots": 4, "positioning": 13,
           "reactions": 9, "shortpassing": 9, "shotpower": 5, "sprintspeed": 5, "vision": 8},
    "W": {"acceleration": 7, "agility": 3, "ballcontrol": 14, "crossing": 9, "dribbling": 16, "finishing": 10, "longshots": 4,
          "positioning": 9, "reactions": 7, "shortpassing": 9, "sprintspeed": 6, "vision": 6},
    "ST": {"acceleration": 4, "ballcontrol": 10, "dribbling": 7, "finishing": 18, "headingaccuracy": 10, "longshots": 3, "positioning": 13,
           "reactions": 8, "shortpassing": 5, "shotpower": 10, "sprintspeed": 5, "strength": 5, "volleys": 2},
}
POS_ROLE = "GK SW WB FB CB CB CB FB WB DM DM DM WM CM CM CM WM AM AM AM CF CF CF W ST ST ST W".split()
POSOVR = "(CASE p.preferredposition1 {} END)".format(" ".join(  # SQL: client rating of row p
    f"WHEN {i} THEN ({' + '.join(f'{w} * p.{k}' for k, w in ROLE_WEIGHTS[r].items())}) / 100" for i, r in enumerate(POS_ROLE)))


def card_ratings(spids):
    """{spid: (client rating, fifadb overallrating)} (cached)."""
    spids = [int(s) for s in spids]
    need = [s for s in set(spids) if s not in _OVR]
    if need:
        with closing(_db()) as c:
            for i in range(0, len(need), 500):
                chunk = need[i:i + 500]
                rows = {s: (o, d) for s, o, d in c.execute(
                    f"SELECT p.spid, {POSOVR}, p.overallrating FROM players p WHERE p.spid IN ({','.join('?' * len(chunk))})", chunk)}
                _OVR.update({s: rows.get(s, (0, 0)) for s in chunk})
    return {s: _OVR[s] for s in spids}


def card_ovrs(spids):
    """{spid: rating the client shows}."""
    return {s: r[0] for s, r in card_ratings(spids).items()}


def card_ovr(spid):
    return card_ovrs([spid])[int(spid)]


def grade_of(p):
    """+0..+10. A club's starting players (stored without a grade) are +0: no market value
    (CoachProxy.expandTeam counts only grade > 0), not sellable; bought/packed cards start at +1."""
    return min(10, max(0, int(p.get("grade") or 0)))


def player_ovr(p):
    """Card rating plus its enhancement boost."""
    return card_ovr(p["spid"]) + GRADE_BOOST[grade_of(p)]


def sig3(x):
    x = int(x)
    return round(x, 2 - len(str(x))) if x >= 1000 else x


def price_formula(ovr):
    """Fallback for cards fifaaddict lists without a price: least-squares fit of its
    Indonesia-server prices on the client rating (10,451 cards): exp(2.53 + 0.1113 * ovr),
    65 ~ 17k, 75 ~ 53k, 85 ~ 160k."""
    return sig3(max(1000, math.exp(2.53 + 0.1113 * ovr)))


def on_market(spid):
    """Tradeable card: listed on fifaaddict (season-0 engine cards are not real cards)."""
    fa = fa_prices()
    return int(spid) in fa if fa else int(spid) >= 1000000


def card_price(spid, grade=1):
    base = fa_prices().get(int(spid)) or price_formula(card_ovr(spid))
    return sig3(base * GRADE_PRICE[min(10, max(0, int(grade)))])  # +0 = 0 EP


def iid(p):
    """Player instance id: str(spid) for squads made by gen_teams, a hex id for cards bought,
    opened or upgraded here (so two cards of one player can coexist, e.g. for upgrades)."""
    return str(p.get("_id") or p["spid"])


def new_iid():
    return os.urandom(12).hex()  # Mongo-style


# Levels (user: "akun dan pemain belum bisa dilevel up"). Both levelinfo = {level, exp within
# the level, to = exp the level needs}. Players: the client adds level - 1 to every attribute
# (PlayerListUtil.fillPlayerStaticData: totalBonus = level - 1 + grade boost; fifaaddict lists
# LV.1-23); EXPPLAYER {level: {requiredplayerexp}}, max level = rows - 1 (getMaxLevel). Coach:
# EXPCOACH {level: {totalexp}} = exp to finish levels 1..level (TeamManagementCoachExpScreen),
# max from STATICVALUES/max_coachlevel. The original tables were server data; curves made up.
PLAYER_MAX_LEVEL, COACH_MAX_LEVEL = 23, 100
EXP_WIN, EXP_DRAW, EXP_LOSS = (300, 150), (200, 100), (100, 60)  # (coach, each player who played)


def player_need(level):
    return 100 * level


def coach_need(level):
    return 500 + 100 * (level - 1)


EXPPLAYER = {str(lv): {"requiredplayerexp": player_need(lv)} for lv in range(1, PLAYER_MAX_LEVEL + 2)}
EXPCOACH = {str(lv): {"totalexp": sum(coach_need(k) for k in range(1, lv + 1))} for lv in range(1, COACH_MAX_LEVEL + 1)}


def make_levelinfo(info, need, top):
    level = min(top, max(1, int((info or {}).get("level") or 1)))
    return {"level": level, "exp": 0 if level >= top else int((info or {}).get("exp") or 0), "to": need(level)}


def player_levelinfo(p):
    return make_levelinfo(p.get("levelinfo"), player_need, PLAYER_MAX_LEVEL)


def coach_levelinfo(coach):
    return make_levelinfo(coach.get("levelinfo"), coach_need, COACH_MAX_LEVEL)


def gain_exp(owner, n, need, top):
    """Add n exp to owner["levelinfo"] (levels up, stops at top); (prev, current) levelinfo."""
    prev = make_levelinfo(owner.get("levelinfo"), need, top)
    level, exp = prev["level"], prev["exp"] + n
    while level < top and exp >= need(level):
        exp -= need(level)
        level += 1
    owner["levelinfo"] = {"level": level, "exp": 0 if level >= top else exp}
    return prev, make_levelinfo(owner["levelinfo"], need, top)


# Tim > Formasi combobox = coach team.formations (SquadScreenHandler.initializeFormationComboBox:
# entry 0 is shown with the club's name; picking one runs changeFormation: positions[i] -> squad
# slot i, offsets[i] -> starting x/y, then SquadUtil.getBestSquadPositions). Entries come from the
# client's own formations table (data_0.big data/db/fifa_ng_db.db): teamid 1 holds the 33 common
# shapes (their sweeper variants left out, like FORMATIONS_COMMON_WITHOUT_SWEEPER), other teams
# their own. That table numbers positions without SW: client Enums.PLAYERPOSITION = code + 1 (GK 0).
GAME = os.environ.get("FO3_GAME", r"D:\FO3ID\32795")
FORMATIONS_CACHE = os.path.join(HERE, "gamedata", "formations.json")
_FORMATIONS = None


def formation_table():
    """{"common": [entry], "teams": {teamid: entry}}; entry = {formationid, formationname,
    positions, offsets}. Read once from the client, then cached in gamedata/formations.json."""
    global _FORMATIONS
    if _FORMATIONS is None:
        try:
            with open(FORMATIONS_CACHE) as f:
                _FORMATIONS = json.load(f)
        except OSError:
            _FORMATIONS = {"common": [], "teams": {}}
            try:
                sys.path.insert(0, os.path.join(HERE, "tools", "re"))
                import bigdir, eadb
                big = os.path.join(GAME, "data", "data_0.big")
                rows = eadb.rows(bigdir.read(big, "data/db/fifa_ng_db.db"),
                                 bigdir.read(big, "data/db/fifa_ng_db-meta.xml").decode("utf-8", "replace"), "formations")
            except (OSError, KeyError, ImportError) as e:
                print("formations unavailable:", e, flush=True)
                return _FORMATIONS
            entry = lambda r: {"formationid": r["formationid"], "formationname": r["formationname"],
                               "positions": [r[f"position{i}"] + (r[f"position{i}"] > 0) for i in range(11)],
                               "offsets": [{"x": round(r[f"offset{i}x"], 4), "y": round(r[f"offset{i}y"], 4)} for i in range(11)]}
            _FORMATIONS["common"] = sorted((entry(r) for r in rows if r["teamid"] == 1 and not r["issweeper"]),
                                           key=lambda e: e["formationname"])
            _FORMATIONS["teams"] = {str(r["teamid"]): entry(r) for r in rows if r["teamid"] != 1 and not r["issweeper"]}
            os.makedirs(os.path.dirname(FORMATIONS_CACHE), exist_ok=True)
            with open(FORMATIONS_CACHE, "w") as f:
                json.dump(_FORMATIONS, f)
    return _FORMATIONS


def team_formations(teamid):
    """The club's own formation first (else 4-4-2(1), the shape of the gen_teams XI), then the
    common ones."""
    t = formation_table()
    own = t["teams"].get(str(teamid)) or next((e for e in t["common"] if e["formationname"] == "4-4-2(1)"), None)
    return ([own] if own else []) + [e for e in t["common"] if own is None or e["formationid"] != own["formationid"]]


def fix_formation(team, teamid):
    """A stored team built by gen_teams carries formations = [its 4-4-2, id 1] and squad
    .formation 1, which is 3-4-1-2 in the client's table: point it at the club's own entry."""
    if team.pop("formations", None) is not None and team["squad"].get("formation") in (None, 1):
        forms = team_formations(teamid)
        if forms:
            team["squad"]["formation"] = forms[0]["formationid"]


def owned_team(raw):
    """A stored team ({players:[{spid,...}], squad:{positions,captain,kickers,...}}) in the
    shape the client expects for a club it can show and play with (coach.team, match side.team)."""
    team = dict(raw)
    # An owned club refers to player *instances*: PlayerListUtil.getBestEleven maps
    # squad.positions[].id -> players[]._id (spid alone gives an empty XI -> null -> the
    # MenuScreen constructor throws = black FREEROAM).
    # CoachProxy.expandTeam: teamvalue = sum(price) of players with state "first" and grade > 0
    # getBestEleven matches positions[].id on players[]._id, attachSquadInfo and
    # generateFormatedPlayerList (Tim > Formasi / Peran pemain) on players[].id: send both.
    # Instance defaults: life = remaining contract matches (SquadUtil.needToReject drops <= 0),
    # levelinfo.level (FirstTeamInfo.updateAvgInfo average level). price = EP market value.
    # state "first" = Tim inti; no state = Tim Cadangan (Enums.getPlayerState: undefined ->
    # SECONDTEAM), set by /player/<id>/move/secondteam (stored as team2); Tukar Scout only
    # lists Tim Cadangan cards.
    team["players"] = [{**({} if p.get("team2") else {"state": "first"}), "grade": 0, "life": 100,
                        **{k: v for k, v in p.items() if k != "team2"}, "levelinfo": player_levelinfo(p),
                        "_id": iid(p), "id": iid(p), "price": card_price(p["spid"], grade_of(p))}
                       for p in team["players"]]
    sq = dict(team["squad"])
    sq["positions"] = [dict(p, id=str(p.get("id") or p["spid"])) for p in sq["positions"]]
    if not any(p["pos"] == POS_SUB for p in sq["positions"]):
        # 11 starters + 7 subs (Enums.PLAYERPOSITION "SUB"); without a bench the squad screen
        # says "Tidak cukup pemain". Best remaining players by rating.
        used = {p["id"] for p in sq["positions"]}
        bench = sorted((p for p in team["players"] if p["id"] not in used), key=lambda p: -player_ovr(p))
        sq["positions"] += [{"spid": p["spid"], "pos": POS_SUB, "id": p["id"]} for p in bench[:7]]
    sq["captain"] = str(sq["captain"]) if sq.get("captain") else ""
    sq["kickers"] = [str(k) for k in sq.get("kickers", [])]
    team["squad"] = sq
    team.update({"staffs": [], "staffroles": [], "staffroleidx": 0, "maxstaff": 0, "staffsmc": {}})
    # PlayerListUtil.generateCaptainFromCoachInfo (Tim > Formasi) reads these at team level
    team.update({"captain": sq["captain"], "kickers": sq["kickers"], "presetidx": 0})
    team.setdefault("formations", [])
    return team


def team_summary(team, teamid):
    # MenuScreen TeamInfoNew.setData: TEAMNAME(summary.id) + loadTeamCrest(summary.id), setOVR(ovr);
    # TeamStaticInfo (Tim > Klub) also reads name, league.id, nation{id,name}, rival.name.
    # TeamInfoNew.setOVR reads ovr.FW/MF/DF (rating bars); Enums.PLAYERPOSITION index ranges.
    ovr = {iid(p): player_ovr(p) for p in team["players"]}
    lines = {"DF": range(1, 9), "MF": range(9, 20), "FW": range(20, 28)}
    def line_ovr(r):
        v = [ovr.get(str(p.get("id") or p["spid"]), 0) for p in team["squad"]["positions"] if p.get("pos") in r]
        return round(sum(v) / len(v)) if v else 0
    return dict(roster_summary(teamid), id=teamid, ovr={k: line_ovr(r) for k, r in lines.items()})


def team_kit(teamid):
    # kit ids go to native as-is (freeroam kicker, 3D previews); {} = default goalie kit.
    # kit id = teamid << 10 | kittype (0 home, 1 away, 2 goalie): Enums.GOALIE_DEFAULT_KIT_ID
    # 45058 = 44 << 10 | 2; checked in FREEROAM (Barca home kit). id = teamid alone renders
    # a missing-texture checkerboard.
    return {"availables": {k: {"id": teamid << 10 | t} for k, t in (("home", 0), ("away", 1), ("goalie", 2))}}


START_EP = 1000000
# Enums.VIP_DETAIL_*: NONE 0, VIP 1<<1, VVIP 1<<2, VVIP_SEA 1<<4
VIP_DETAIL = 2
VIP_UNTIL = 2051222400000  # 2035-01-01, ms


def add_ep(st, n):
    """Credit match/season/cup EP to the active coach (shown by /coach/me money.ep)."""
    coach = next(c for c in st["coaches"] if c["slot"] == _default_slot(st))
    coach["ep"] = coach.get("ep", START_EP) + int(n)


def coach_me(st, slot):
    """Body of the /coach/me resource (CoachProxy). Field paths are the ones its
    fetchCoach* methods read; values are a fresh account. startFreeRoam waits for this
    resource to validate before signing on."""
    coach = next(c for c in st["coaches"] if c["slot"] == slot)
    team = owned_team(st["teams"].get(str(slot), {"squad": {"positions": [], "kickers": []}, "players": []}))
    teamid = coach["teamid"]
    team["formations"] = team_formations(teamid)
    if team["formations"] and team["squad"].get("formation") is None:
        team["squad"]["formation"] = team["formations"][0]["formationid"]
    return {
        "id": coach["_id"], "name": coach["name"], "slot": slot, "teamid": coach["teamid"], "team": team,
        # CoachProxy.expandMoney parses money.mpdata.until as a W3C date; an exception there
        # silently aborts onChangeCoachMeResource before squad/players are read.
        "money": {"ep": coach.get("ep", START_EP), "cash": 0, "mp": 0, "useable_mp": 0,
                  "mpdata": {"point": 0, "until": "2030-01-01T00:00:00.000Z"}},
        # always a VIP member playing from an official PC warnet (IGR / GCA): CoachProxy.igr,
        # isVip() = vipDetail > 0 and vipExpireAt in the future; vipStatus.VIP1 = VIP expiry
        # after a VVIP period (updateVipExpireAt)
        "igr": True, "vipDetail": VIP_DETAIL, "vipExpireAt": VIP_UNTIL, "vipStatus": {"VIP1": VIP_UNTIL},
        "sp": {"point": 0}, "gpdata": {"gp": 0}, "badges": [], "currentbadge": 0,
        # league.leagueid -> CoachProxy.leagueId: Single > Liga builds the league from it
        "levelinfo": coach_levelinfo(coach), "statistics": {},
        "league": {"leagueid": roster_summary(teamid).get("league", {}).get("id", 0)},
        # limits.cups -> CupProxy.playMax (cups you can run at once); limits.players/baseplayers
        # -> the market's player-slot check (TransferPurchaseTabPage._limitPlayerCount)
        # limits.firstTeam: Skuad tab moveToFirstTeam reads it first and silently gives up when
        # it is missing, so Tim Cadangan cards (packs, trades) could never reach Tim inti/Formasi
        "club": {}, "uimode": 0, "limits": {"cups": 3, "players": MAX_PLAYERS, "baseplayers": MAX_PLAYERS,
                                             "firstTeam": FIRST_TEAM_MAX},
        "transferfavorites": [], "currdivision": "",
        # playlimit {modeid: {playcount}}: Liga's Mulai/Simulasi buttons compare playcount with
        # PLAYLIMIT.maxplay; without the key playcount stays undefined and they stay disabled
        "playlimit": {},
        "kit": team_kit(teamid), "summary": team_summary(team, teamid),
        # cups -> CupProxy.cupPlayList: {metaid, cupid} of cups not left yet (front screen resumes
        # them; an ended one shows its won/eliminated screen with the reward button -> /leave)
        "items": coach_items(st, slot), "matchstats": [], "scouters": coach_scouters(st, slot),
        "cups": [{"metaid": c["metaid"], "cupid": c["_id"]} for c in (st.get("cups") or {}).values()],
        # SquadScreenHandler: formation combobox reads /coach/me?path=formations
        "formations": team["formations"], "teamsquadlist": [],
    }


def coach_me_resources(st, slot):
    """The /coach/me resource for a reply's `resources`. CoachProxy's fetchCoach* read
    getData("/coach/me?path=X"), which NetEvent.getDataEx resolves by walking this body; pushing
    every ?path= part too re-ran onChangeCoachMeResource per part and nested the client's
    resource chain until AS3 "Error #1023: Stack overflow" at login."""
    return [{"uri": "/coach/me", "body": coach_me(st, slot)}]


def _default_slot(st):
    return next((c["slot"] for c in st["coaches"] if c["_id"] == st["defaultcoachid"]),
                st["coaches"][0]["slot"] if st["coaches"] else 0)  # default manager deleted: the other one


def ep_coach_me(query, body):
    st = _state()
    return _json(coach_me(st, _default_slot(st)))


def ep_coach_onready(query, body):
    # POST /coach/me/onready comes right after CoachProxy.invalidate() -> reset() (which nulls
    # _squad); without /coach/me in this reply the menu is built with squad null (black FREEROAM).
    st = _state()
    return _json({"result": True, "resources": coach_me_resources(st, _default_slot(st))})


def ep_default_coach(slot, query, body):
    # PUT /coach/<slot>/default: the client calls setLocalCoachId(body.resources[0].body.id);
    # resources are also fed to cached resources, so /coach/me (CoachProxy) goes first.
    st = _state(); st["defaultcoachid"] = coach_id(slot)
    daily_login(st)  # first login of the day: today's reward goes to the Bag
    if str(slot) in st["teams"]:
        balance_first_team(st["teams"][str(slot)])
        fix_formation(st["teams"][str(slot)], next(c["teamid"] for c in st["coaches"] if c["slot"] == slot))
    _save(st)
    return _json({"result": True, "resources": coach_me_resources(st, slot)})


TACTIC_SLOTS = 6


def default_tactic(i):
    # TacticsController.setCurrentTactics fields; sliders 1..100, positioning/defenderline are
    # 0/1 radios, teammentality 1..5. name "" -> client fills the localized preset name.
    return {"id": i, "name": "", "teammentality": 3,
            "bus": {"buildupspeed": 50, "passing": 50, "positioning": 0},
            "cc": {"passing": 50, "crossing": 50, "shooting": 50, "positioning": 0},
            "def": {"mentality": 50, "aggression": 50, "teamwidth": 50, "defenderline": 0}}


def _tactics(st):
    slot = str(_default_slot(st))
    saved = st.setdefault("tactics", {}).setdefault(slot, {})
    return saved, [saved.get(str(i)) or default_tactic(i) for i in range(TACTIC_SLOTS)]


def ep_tactic_all(query, body):
    # GET /game/my/tactic/all -> Array of 6 tactics (TeamManagementTeamTacticsTabPage.onReceived)
    return _json(_tactics(_state())[1])


def ep_tactic(i, query, body):
    """Save (PUT, body = tactic) or reset (DELETE -> {result, resources:[{body: default}]})."""
    req = json.loads(body or b"{}")
    st = _state(); saved, _ = _tactics(st)
    if "bus" in req:
        saved[str(i)] = dict({k: v for k, v in req.items() if k != "_method"}, id=i)
        _save(st)
        return _json({"result": True})
    saved.pop(str(i), None); _save(st)
    return _json({"result": True, "resources": [{"uri": f"/game/my/tactic/{i}", "body": default_tactic(i)}]})


# --------------------------------------------------------------------------- #
# Single > Liga (League 2.0). LeagueCupProxy builds the whole calendar client-side from
# /league2/me: league.teams + openingday, one match per round from createSchedule (ported
# below as league_round). The server keeps results and the table:
#   creation screen: GET /league2/me (difficultybest/difficultyselect), /league2/reward,
#     POST /league2/sponsor/<difficulty>, POST /league2/calendar {difficulty, sponsor}
#   a round: POST /league2/match (-> /match/<id> resource, stage created) ->
#     PUT /match/<id>/side/<s>/ready (-> stage playing -> StartSingleMatch) ->
#     PUT /match/<id>/end {home:{id,score}, away:{id,score}} -> leagueparam + /league2/me
#   or POST /league2/match/sim (no play), POST /league2/sleep (bye round)
# laststatus "league:<round>" tells the client which day it is (LeagueCupProxy.setToday).
# --------------------------------------------------------------------------- #
LEAGUE_SIZE = 10  # the creation screen shows 10 opponent crests


def _db():
    c = sqlite3.connect(gen_teams.DB); c.row_factory = sqlite3.Row
    return c


def league_teams(leagueid, mine):
    """My club + the strongest clubs of its league in fifadb (base cards, full squads)."""
    with closing(_db()) as c:
        rows = c.execute("""SELECT p.teamid FROM players p JOIN permid m ON m.spid = p.spid AND m.licensed = 1
                            WHERE p.leagueid = ? AND p.spid < 1000000 GROUP BY p.teamid
                            HAVING COUNT(*) >= 16 ORDER BY MAX(p.overallrating) DESC""", (leagueid,)).fetchall()
    return [mine] + [r[0] for r in rows if r[0] != mine][:LEAGUE_SIZE - 1]


def league_round(teams, rnd):
    """Fixtures [(home, away)] of round rnd: LeagueCupProxy.createSchedule/getRotatedIndexAt
    (circle method; second half swaps home/away). None = bye (odd team count)."""
    n = len(teams); size = n + n % 2; per = size - 1
    flip, r = (rnd // per) % 2 == 1, rnd % per

    def rot(i):
        k = 0 if i == 0 else (i - 1 + r) % per + 1
        return teams[k] if k < n else None
    pairs = []
    for k in range(size // 2):
        a, b = rot(k), rot(size - 1 - k)
        f = (not flip) if (k == 0 and r % 4 > 1) else flip
        pairs.append((b, a) if f else (a, b))
    return pairs


def _poisson(rng, lam):
    lim, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= lim:
            return k
        k += 1


def team_strength(st, teamid):
    """Average rating (+ grade boost) of the best 11."""
    raw = st["teams"].get(str(_default_slot(st))) if teamid == my_teamid(st) else None
    ovrs = sorted((player_ovr(p) for p in (raw or roster_team(teamid))["players"]), reverse=True)[:11]
    return sum(ovrs) / len(ovrs) if ovrs else 60


def sim_score(rng, home, away):
    """ponytail: Poisson goals from the rating gap (+3 home edge); no match engine."""
    d = (home + 3 - away) / 10
    return _poisson(rng, max(0.3, 1.4 + 0.6 * d)), _poisson(rng, max(0.2, 1.1 - 0.6 * d))


def league_stats(teams, records, prev=None):
    """league.statistics {teamid: {wins, draws, losses, goals, goalsconceded, awaygoals,
    point, rank, before{rank, point}}} (LeagueCupProxy.makeLeagueRanking)."""
    s = {str(t): {"wins": 0, "draws": 0, "losses": 0, "goals": 0, "goalsconceded": 0,
                  "awaygoals": 0, "point": 0} for t in teams}
    for m in records:
        h, a = s[str(m["home"])], s[str(m["away"])]
        gh, ga = m["goals"]["home"], m["goals"]["away"]
        h["goals"] += gh; h["goalsconceded"] += ga
        a["goals"] += ga; a["goalsconceded"] += gh; a["awaygoals"] += ga
        if gh != ga:
            win, lose = (h, a) if gh > ga else (a, h)
            win["wins"] += 1; win["point"] += 3; lose["losses"] += 1
        else:
            h["draws"] += 1; a["draws"] += 1; h["point"] += 1; a["point"] += 1
    order = sorted(teams, key=lambda t: tuple(-v for v in (
        s[str(t)]["point"], s[str(t)]["goals"] - s[str(t)]["goalsconceded"], s[str(t)]["goals"], s[str(t)]["awaygoals"])))
    for i, t in enumerate(order):
        s[str(t)]["rank"] = i + 1
    for t, v in s.items():
        p = (prev or {}).get(t) or v
        v["before"] = {"rank": p["rank"], "point": p["point"]}
    return s


def my_teamid(st):
    return next(c["teamid"] for c in st["coaches"] if c["slot"] == _default_slot(st))


def l2_me(st):
    """GET /league2/me body; LeagueCupProxy.fetch* read each key as a path."""
    teamid = my_teamid(st)
    lg = st.get("league2") or {}
    me = {"teamid": teamid, "leagueid": lg.get("leagueid") or roster_summary(teamid).get("league", {}).get("id", 0),
          # creation screen: selectable = difficultybest - 10 (24 unlocks all 10 normal + 5
          # ultimate levels), preselected = difficultyselect - 10
          "difficultybest": 24, "difficultyselect": lg.get("difficultyselect", 10),
          # no cupqualifier/cupfinal keys: any object there makes fetchCupQualifier build
          # cup groups (makeLeagueCupGroupTeams -> TypeError on missing teamgroups)
          "laststatus": lg.get("laststatus", ""), "cupinfo": {"entered": False},
          "leader": {"league": {}, "cup": {}},
          # MenuScreen: season.status started/finished -> league front, else creation
          "sponsoroffice": lg.get("sponsoroffice", {"season": {"status": "none"}})}
    if lg.get("league"):
        me.update(openingday=lg["openingday"], league=lg["league"])
    return me


def l2_resources(st):
    return [{"uri": "/league2/me", "body": l2_me(st)}]


def ep_l2_me(query, body):
    return _json(l2_me(_state()))


def ep_l2_reward(query, body):
    # LeagueCupCreationScreen.onCalendarRewardsResourceReceived: Array indexed by difficulty
    # level (0-9 normal, 10-14 ultimate) of {ep, items[]}
    return _json([{"ep": 20000 * (i + 1), "items": []} for i in range(15)])


# sponsorid -> its mission id; LEAGUE2SPONSOR.mission[mid] = League2_sponsor_title_<mid> with
# the target in "lv1": 2 = finish in the top N, 3 = N wins in a row, 4 = N clean sheets in a row
SPONSORS = {"1": "2", "2": "3", "3": "4"}
MISSION_TARGET = {"2": 3, "3": 3, "4": 2}  # keep in sync with LEAGUE2SPONSOR lv1


def ep_l2_sponsor(difficulty, query, body):
    """POST /league2/sponsor/<difficulty>: offers. OnReceivedSponsorList reads level, maxround
    and sponsors[] {_id, sponsorid, missions[{mid}]} (names/logos/goals from LEAGUE2SPONSOR)."""
    return _json({"result": True, "level": 1, "maxround": 2 * (LEAGUE_SIZE - 1) - 1,
                  "sponsors": [{"_id": sid, "sponsorid": sid, "missions": [{"mid": mid}]} for sid, mid in SPONSORS.items()]})


def mission_progress(st, mid):
    """(progress args, achieved) of a sponsor mission from my league results so far."""
    lg = st.get("league2") or {}; league = lg.get("league") or {}
    me = my_teamid(st)
    mine = sorted((m for m in league.get("matchrecords", []) if me in (m["home"], m["away"])), key=lambda m: m["round"])
    goals = [(m["goals"]["home"], m["goals"]["away"]) if m["home"] == me else (m["goals"]["away"], m["goals"]["home"]) for m in mine]

    def best_run(ok):
        best = run = 0
        for g in goals:
            run = run + 1 if ok(g) else 0; best = max(best, run)
        return best
    target = MISSION_TARGET.get(mid, 1)
    if mid == "2":
        rank = league.get("statistics", {}).get(str(me), {}).get("rank", 0)
        done = len(mine) > league.get("maxround", 0)  # season over
        return [rank], done and 0 < rank <= target
    value = best_run((lambda g: g[0] > g[1]) if mid == "3" else (lambda g: g[1] == 0))
    return [value], value >= target


def ep_l2_achievement(query, body):
    """PUT /league2/sponsor/achievement -> the chosen sponsor's goals for the calendar tab
    (setSponsorGoalList: missions[{mid, achievement, progress[args]}]; getProgressString fills
    LEAGUE2SPONSOR.mission[mid].progress with progress)."""
    st = _state(); lg = st.get("league2") or {}
    mid = SPONSORS.get(str(lg.get("sponsor")), "2")
    progress, done = mission_progress(st, mid)
    return _json({"result": True, "level": 1, "missions": [{"mid": mid, "achievement": done, "progress": progress}]})


# Club creation > "Tim lain" (StartupTeamCreationScreen + TeamSelectionUtil): nation -> league ->
# club steppers from SORTNATION [nationid], SORTLEAGUE {nationid: [leagueid]} and SORTTEAM
# {leagueid: [teamid]}; the picked club's squad is /static/info/authentic/team/licensed/<id>
# ({teamid, summary, team}, built by gen_teams.team like the six recommended clubs).
CLUB_MIN_PLAYERS = 16  # licensed 2017 cards a club needs to be listed
_CLUBS = []


def club_index():
    """[(nationid, [(leagueid, [teamid])])] of clubs with a full 2017 squad, strongest first (by
    their best eleven); a league's nation is its players' most common nationality."""
    if not _CLUBS:
        with closing(_db()) as c:
            rows = c.execute(f"""SELECT p.leagueid, p.teamid, p.nationality, {POSOVR} FROM players p
                                 JOIN permid m ON m.spid = p.spid AND m.licensed = 1 WHERE p.spid / 1000000 = ?""",
                             (gen_teams.CURRENT_SEASON,)).fetchall()
        squads, nats = {}, {}
        for lid, tid, nat, ovr in rows:
            squads.setdefault((lid, tid), []).append(ovr)
            nats.setdefault(lid, collections.Counter())[nat] += 1
        power = {k: sum(sorted(v)[-11:]) / 11 for k, v in squads.items() if len(v) >= CLUB_MIN_PLAYERS}
        leagues = {}
        for (lid, tid), p in power.items():
            leagues.setdefault(lid, []).append((p, tid))
        by_nation = {}
        for lid, teams in leagues.items():
            teams.sort(reverse=True)
            by_nation.setdefault(nats[lid].most_common(1)[0][0], []).append((teams[0][0], lid, [t for _, t in teams]))
        _CLUBS.extend((n, [(lid, ts) for _, lid, ts in sorted(ls, reverse=True)])
                      for n, ls in sorted(by_nation.items(), key=lambda kv: -max(l[0] for l in kv[1])))
    return _CLUBS


def ep_sortnation(query, body):
    return _json([n for n, _ in club_index()])


def ep_sortleague(query, body):
    return _json({str(n): [lid for lid, _ in ls] for n, ls in club_index()})


def ep_sortteam(query, body):
    """/static/db/SORTTEAM {leagueid: [teamid]}: the club picker above; Liga's creation screen
    counts my own league's clubs (the ones its calendar will use)."""
    st = _state()
    out = {str(lid): ts for _, ls in club_index() for lid, ts in ls}
    if st["coaches"]:
        teamid = my_teamid(st)
        lid = roster_summary(teamid).get("league", {}).get("id", 0)
        out[str(lid)] = league_teams(lid, teamid)
    return _json(out)


def ep_authentic_team(kind, teamid, query, body):
    """/static/info/authentic/team/licensed|all/<teamid>: a club's starter squad {teamid, summary,
    team} (responses/ files override for the recommended clubs). StartupPlayerSelectionScreen
    (after "Tim lain") looks up team.captain among team.players by id and dresses its 3D player
    in kit.availables.home/goalie."""
    r = roster(int(teamid))
    team = dict(r["team"], players=[dict(p, id=str(p["spid"])) for p in r["team"]["players"]])
    team["captain"] = str(team["squad"].get("captain") or "")
    return _json({**r, "team": team, "teamid": int(teamid), "kit": team_kit(int(teamid))})


def ep_l2_calendar(query, body):
    """POST /league2/calendar {difficulty, sponsor}: start a season."""
    req = json.loads(body or b"{}")
    st = _state(); teamid = my_teamid(st)
    lid = roster_summary(teamid).get("league", {}).get("id", 0)
    teams = league_teams(lid, teamid)
    day = int(time.time() // 86400 * 86400 * 1000)  # today 00:00 UTC, ms
    st["league2"] = {"leagueid": lid, "difficultyselect": int(req.get("difficulty") or 10),
                     "sponsor": req.get("sponsor"), "laststatus": "opening", "openingday": day,
                     "sponsoroffice": {"season": {"status": "started"}},
                     "league": {"teams": teams, "maxround": 2 * (len(teams) - 1) - 1, "nextround": 0,
                                "matchrecords": [], "statistics": league_stats(teams, [])}}
    _save(st)
    return _json({"result": True, "resources": l2_resources(st)})


def league_play_round(st, rnd, mine=None, seed=None):
    """Play league round rnd: my fixture gets `mine` (home, away goals) or is simulated,
    the rest is simulated. Updates table + laststatus, returns the client's leagueparam."""
    lg = st["league2"]; league = lg["league"]
    if any(m["round"] == rnd for m in league["matchrecords"]):
        return {"matchtype": "league", "round": rnd, "leaguematches": [m for m in league["matchrecords"] if m["round"] == rnd]}
    rng = random.Random(seed)
    me = my_teamid(st); played = []
    for h, a in league_round(league["teams"], rnd):
        if h is None or a is None:
            continue
        if mine is not None and me in (h, a):
            gh, ga = mine
        else:
            gh, ga = sim_score(rng, team_strength(st, h), team_strength(st, a))
        played.append({"type": "league", "matchtype": "league", "round": rnd, "home": h, "away": a,
                       "goals": {"home": gh, "away": ga}})
    league["matchrecords"] += played
    league["statistics"] = league_stats(league["teams"], league["matchrecords"], league["statistics"])
    league["nextround"] = rnd + 1
    lg["laststatus"] = f"league:{rnd}"
    return {"matchtype": "league", "round": rnd, "leaguematches": played}


def league_match_body(st, req, playtype):
    me = coach_me(st, _default_slot(st)); teamid = me["teamid"]
    opp = req["away"] if req["home"] == teamid else req["home"]
    user = {"id": me["id"], "name": me["name"], "levelinfo": me["levelinfo"]}
    lg = st["league2"]
    match = new_match("League_2_league_MGR" if playtype == "mgr" else "League_2_league",
                      match_side(st["teams"][str(_default_slot(st))], teamid, user, _tactics(st)[1]),
                      match_side(roster_team(opp), opp))
    gs = match["gamesettings"]
    gs["home"] = req["home"] == teamid  # I am always the left side
    gs["kit"] = ["home", "away"] if gs["home"] else ["away", "home"]
    # league level 10..24: the in-match overlay shows LEAGUE2_DIFFICULTY_<difficulty - 9>
    gs["difficulty"] = lg.get("difficultyselect", 10)
    # MatchProxy.leagueparam: LeagueCupGameSettingScreen.onPlaying reads leagueparam.matchtype
    # unguarded (`p != null && t == "cupqualifier" || p.matchtype == "cupfinal"`) -> required
    match["leagueparam"] = {"matchtype": req.get("matchtype", "league"), "round": req.get("round", 0),
                            "home": req["home"], "away": req["away"], "leagueid": lg.get("leagueid")}
    return match


def ep_l2_match(query, body, sim=False):
    """POST /league2/match {matchtype, playtype, round, home, away, date} -> a /match/<id>
    resource (MatchProxy.registerByResource). /match/sim plays the round right away."""
    req = json.loads(body or b"{}")
    st = _state(); lg = st.get("league2")
    if not lg or not lg.get("league"):
        return _json({"result": False, "reason": "LEAGUE_FAIL"})
    match = league_match_body(st, req, req.get("playtype"))
    st["match"] = match
    lg["pending"] = {"id": match["id"], "round": req.get("round", 0), "home": req["home"], "away": req["away"]}
    out = {"result": True}
    if sim:
        param = league_play_round(st, req.get("round", 0))
        mine = next((m for m in param["leaguematches"] if my_teamid(st) in (m["home"], m["away"])), None)
        match.update(stage="closed", simulated=True)
        match["gamesettings"]["simulated"] = True
        out.update(leagueparam=param, eventlist=[], score=mine and mine["goals"])
    _save(st)
    out["resources"] = [{"uri": f"/match/{match['id']}", "body": match}] + (l2_resources(st) if sim else [])
    return _json(out)


def ep_l2_sleep(query, body):
    """POST /league2/sleep {matchtype, round}: my bye round; the others still play."""
    req = json.loads(body or b"{}")
    st = _state(); lg = st.get("league2")
    param = league_play_round(st, req.get("round", 0)) if lg and lg.get("league") else {}
    _save(st)
    return _json({"result": True, "leagueparam": param, "resources": l2_resources(st)})


def ep_l2_finish(query, body):
    """PUT /league2/abandon: give up the season (back to the creation screen)."""
    st = _state()
    lg = st.get("league2") or {}
    st["league2"] = {"difficultyselect": lg.get("difficultyselect", 10)}
    _save(st)
    return _json({"result": True, "resources": l2_resources(st)})


def season_leaders(st, me, rank, goals):
    """leaderRewards for LeagueCupLeagueRewardScreen: best XI by line (FW/MF/DF/GK, 11 in
    all) + one leader per stat. ponytail: no per-player season stats are kept, so these
    come from my own squad (rating = card rating, stat values derived from team goals)."""
    team = owned_team(st["teams"][str(_default_slot(st))])
    rating = {p["spid"]: player_ovr(p) for p in team["players"]}
    lines = {"GK": range(0, 1), "DF": range(1, 9), "MF": range(9, 20), "FW": range(20, 28)}
    starters = [p for p in team["squad"]["positions"] if p["pos"] < POS_SUB]
    entry = lambda spid, value, ep=0: {"spid": spid, "teamid": me, "value": value, "reward": {"count": ep}}
    out = {line: [entry(p["spid"], round(min(9.9, 6 + (rating.get(p["spid"], 80) - 80) / 25), 1))
                  for p in starters if p["pos"] in r] for line, r in lines.items()}
    best = lambda line: max((p["spid"] for p in starters if p["pos"] in lines[line]),
                            key=lambda s: rating.get(s, 0), default=0)
    played = 2 * (LEAGUE_SIZE - 1)
    out.update(ratings=[entry(best("FW"), 7.5, 3000)], goals=[entry(best("FW"), max(1, goals // 2), 3000)],
               assists=[entry(best("MF"), max(1, goals // 3), 2000)], tackles=[entry(best("DF"), played * 3, 2000)],
               passes=[entry(best("MF"), 0.85, 1000)],  # pass success rate; the UI shows value*100 %
               saves=[entry(best("GK"), played * 2, 1000)])
    return out


def ep_l2_end(query, body):
    """PUT /league2/end (closing day): season result for LeagueCupLeagueRewardScreen
    {promotion{current,next}, rewards{coach,money.reward{total,detail}}, sponsorRewards,
    leaderRewards, seasonRewards{items}}. The season stays (the reward screen reads it) but
    is marked finished: back on the front, the closing day then has seasonoff -> ON_CLOSED
    -> the creation screen, whose POST /league2/calendar starts the next season."""
    st = _state(); lg = st.get("league2") or {}; league = lg.get("league")
    if not league:
        return _json({"result": False, "reason": "LEAGUE_FAIL"})
    me = my_teamid(st); stats = league["statistics"][str(me)]; rank = stats["rank"]
    cur = lg.get("difficultyselect", 10)
    if cur >= 20:  # ultimate: promote top 3, demote bottom 3
        nxt = min(24, cur + 1) if rank <= 3 else max(20, cur - 1) if rank > LEAGUE_SIZE - 3 else cur
    else:
        nxt = min(19, cur + 1) if rank <= 3 else cur
    mid = SPONSORS.get(str(lg.get("sponsor")), "2")
    progress, done = mission_progress(st, mid)
    with open(os.path.join(RESPONSES, "static_db_LEAGUE2REWARD.json")) as f:
        sponsor_ep = json.load(f)["sponsor"].get(str(int(mid) - 1), {}).get(str(cur), {}).get("count", 0) if done else 0
    leaders = season_leaders(st, me, rank, stats["goals"])
    leader_ep = sum(e["reward"]["count"] for k in ("ratings", "goals", "assists", "tackles", "passes", "saves")
                    for e in leaders[k])
    base_ep = (LEAGUE_SIZE - rank + 1) * 2000
    season_exp = (LEAGUE_SIZE - rank + 1) * 100
    cprev, ccur = gain_exp(my_coach(st), season_exp, coach_need, COACH_MAX_LEVEL)
    out = {"result": True, "promotion": {"current": cur, "next": nxt},
           # setRewardRankExp: coach.prev/current/reward.total; setRewardRankEP: money.prev +
           # reward.detail.season; the sponsor/leader scenes read detail.sponsor/.leader
           "rewards": {"coach": {"prev": cprev, "current": ccur, "reward": {"total": season_exp}},
                       "money": {"prev": coach_me(st, _default_slot(st))["money"]["ep"],
                                 "reward": {"total": base_ep + sponsor_ep + leader_ep,
                                            "detail": {"season": base_ep, "sponsor": sponsor_ep, "leader": leader_ep}}}},
           "sponsorRewards": {"sponsorid": str(lg.get("sponsor")), "level": 1, "achievement": done,
                              "missions": [{"mid": mid, "achievement": done, "progress": progress}]},
           "leaderRewards": leaders, "seasonRewards": {"items": []}}
    lg.update(difficultyselect=nxt, sponsoroffice={"season": {"status": "finished"}})
    add_ep(st, out["rewards"]["money"]["reward"]["total"])
    _save(st)
    out["resources"] = l2_resources(st)
    return _json(out)


def match_step(mid, body, step):
    """Apply step(match, request) to the live league match (state["match"]) and answer with
    the updated /match/<id> resource; MatchController re-runs processMatchStageResource."""
    req = json.loads(body or b"{}")
    st = _state(); match = st.get("match")
    if not match or match["id"] != mid:
        return _json({"result": False})
    step(match, req)
    _save(st)
    return _json({"result": True, "resources": [{"uri": f"/match/{mid}", "body": match}]})


def _side_ready(side):
    def step(match, req):
        if side in match["side"]:
            match["side"][side]["ready"] = bool(req.get("ready", True))
        if req.get("ready", True):
            match["stage"] = "playing"
    return step


def ep_match_ready(mid, side, query, body):
    """PUT /match/<id>/side/<side>/ready {ready, stage}: single-player, so the match starts
    (stage playing -> LeagueCupGameSettingScreen.onPlaying -> StartSingleMatch)."""
    return match_step(mid, body, _side_ready(side))


NEXT_STAGE = {"created": "squad", "squad": "setting"}


def _player_ready(match, req):
    if req.get("ready", True) and req.get("stage", match["stage"]) == match["stage"]:
        match["stage"] = NEXT_STAGE.get(match["stage"], match["stage"])


def ep_match_player_ready(mid, query, body):
    """PUT /match/<id>/player/ready {ready, stage}: created -> squad (ON_SQUADING: squad screen),
    squad -> setting (LeagueCupSquadSettingHandler.updateStage: game setting screen)."""
    return match_step(mid, body, _player_ready)


def match_reward(st, stats):
    """match.reward for PostMatchRewardDefaultScene.onRewardReceived (paths it reads) and its
    player list (PlayerRewardListItemRenderer: id, spid, prev, current, reward.detail.extra).
    Credits EP, coach exp and exp for every player of mine in matchStats (in-match spid = the
    card's player id, i.e. spid % 1e6). ponytail: flat exp by result + goals/assists."""
    me, opp = stats.get("myTeam", "home"), stats.get("opponentTeam", "away")
    g = lambda s: int((stats.get(s) or {}).get("score") or 0)
    won, drew = g(me) > g(opp), g(me) == g(opp)
    ep = 2000 if won else 1000 if drew else 500
    cexp, pexp = EXP_WIN if won else EXP_DRAW if drew else EXP_LOSS
    reward = lambda n: {"total": n, "detail": {"base": n, "extra": 0}}
    prev, cur = gain_exp(my_coach(st), cexp, coach_need, COACH_MAX_LEVEL)
    team, done, players = st["teams"].get(str(_default_slot(st))) or {"players": []}, set(), []
    for s in (stats.get(me) or {}).get("players") or []:
        pid = int(s.get("spid") or 0) % 1000000
        mine = [p for p in team["players"] if int(p["spid"]) % 1000000 == pid and iid(p) not in done]
        p = next((p for p in mine if in_squad(team, p)), mine[0] if mine else None)
        if p is None:
            continue
        n = pexp + 20 * int(s.get("goals") or 0) + 10 * int(s.get("assists") or 0)
        pprev, pcur = gain_exp(p, n, player_need, PLAYER_MAX_LEVEL)
        done.add(iid(p))
        players.append({"id": iid(p), "spid": p["spid"], "prev": pprev, "current": pcur, "reward": reward(n)})
    return {"matchRewardBackType": 0, "extra": {"selected": {}, "availables": []}, "selected": [],
            "coach": {"reward": reward(cexp), "prev": prev, "current": cur},
            "money": {"reward": reward(ep)},
            "players": players, "missions": [], "exptankinfo": {}}


def ep_match_get(mid, query, body):
    """GET /match/<id>: the post-match screens re-read result/reward from the match."""
    match = _state().get("match") or {}
    return _json(match if match.get("id") == mid else {})


def ep_match_end(mid, query, body):
    """PUT /match/<id>/end matchStats {home:{id, score, players...}, away:{...}, myTeam,
    opponentTeam, motm, ...}. It comes back as match.result (PostMatchResultDefaultScene
    .onResultReceived reads exactly these fields) plus a match.reward."""
    req = json.loads(body or b"{}")
    st = _state(); lg = st.get("league2") or {}; p = lg.get("pending")
    match = st.get("match") if (st.get("match") or {}).get("id") == mid else None
    out = {"result": True}
    if match is not None and match.get("stage") != "closed":  # credit once
        match["result"] = {k: v for k, v in req.items() if k != "_method"}
        match["reward"] = match_reward(st, req)
        add_ep(st, match["reward"]["money"]["reward"]["total"])
    cp = st.get("cup_pending") or {}
    if cp.get("id") == mid and cp.get("cupid") in (st.get("cups") or {}):
        h, a = req.get("home") or {}, req.get("away") or {}
        gh, ga = int(h.get("score") or 0), int(a.get("score") or 0)
        sh, sa = int(h.get("shootout") or 0), int(a.get("shootout") or 0)
        if str(h.get("id")) == str(cp["away"]):
            gh, ga, sh, sa = ga, gh, sa, sh
        st["cups"][cp["cupid"]]["_mine"] = (gh, ga, sh, sa)  # applied by PUT /cup/<id>/next
        st.pop("cup_pending")
    if p and p["id"] == mid:
        h, a = req.get("home") or {}, req.get("away") or {}
        gh, ga = int(h.get("score") or 0), int(a.get("score") or 0)
        if str(h.get("id")) == str(p["away"]):  # matchStats home/away are match sides
            gh, ga = ga, gh
        out["leagueparam"] = league_play_round(st, p["round"], (gh, ga))
        lg.pop("pending", None)
        out["resources"] = l2_resources(st)
    if match is not None:
        match["stage"] = "closed"
        out["resources"] = [{"uri": f"/match/{mid}", "body": match}] + out.get("resources", [])
    _save(st)
    return _json(out)


# --------------------------------------------------------------------------- #
# Single > Cup (PvE cups; also the "Eropa/Amerika Selatan" shortcut). CupProxy/CupController.
#   GET /cup/list -> metas the front screen lists; GET /cup/record -> my cup records.
# --------------------------------------------------------------------------- #
def cup_meta(metaid, name, trophy, quality, matchcount, teams, reward):
    """A /cup/list entry: CupFrontScreen.onCupDetailInternal + CupProxy.onCupListReceived
    fields. difficulty = minimum AI level index (0 amateur .. 4 legendary)."""
    # visible: Boolean (isVisibleValue indexes it per condition otherwise); joinable.allteams
    # skips the teamids/leagueids whitelist ("Tim ini tidak dapat bergabung")
    return {"metaid": metaid, "_id": str(metaid), "name": name, "type": "pve", "teamselect": "user", "visible": True,
            "difficulty": 0, "quality": quality, "matchcount": matchcount, "teamcount_desc": teams,
            "proviso_desc": "PVECUP_INFO_OPTION_NO", "trophy": trophy, "skin": str(metaid),
            # onCupDetailInternal sums bestrewards[-1][*].count where itemid == 1 (EP)
            "bestrewards": [[{"itemid": 1, "count": reward}]],
            "startat": "2020-01-01T00:00:00.000Z", "endat": "2030-12-31T00:00:00.000Z",
            "joinable": {"fee": 0, "igr": False, "vip": 0, "allteams": True, "dayofweek": [1, 1, 1, 1, 1, 1, 1]}}


CUPS = [cup_meta(1, "European Champions Cup", "66", 4, 6, 16, 60000),  # 3 group + QF/SF/F
        cup_meta(2, "South American Cup", "300000", 3, 6, 16, 40000)]


def ep_cup_list(query, body):
    return _json([] if "type=wcpve" in query else CUPS)  # wcpve = World Cup PvE list


def cup_pool(metaid, mine):
    """15 AI clubs: the strongest clubs in fifadb (cup 1) or the next tier (cup 2)."""
    with closing(_db()) as c:
        rows = c.execute("""SELECT p.teamid FROM players p JOIN permid m ON m.spid = p.spid AND m.licensed = 1
                            WHERE p.spid < 1000000 GROUP BY p.teamid HAVING COUNT(*) >= 16
                            ORDER BY MAX(p.overallrating) DESC LIMIT 60""").fetchall()
    ids = [r[0] for r in rows if r[0] != mine]
    return ids[:15] if metaid == 1 else ids[15:30]


def cup_team(st, teamid):
    me = my_teamid(st)
    return {"teamid": teamid, "coach": coach_id(_default_slot(st)) if teamid == me else None}


def new_cup(st, meta, difficulty):
    """A cup instance (CupProxy.updateCurrentCupData): 16 clubs, 4 round-robin groups of 4
    (3 rounds), then an 8-club knock-out. pairs[].legs[leg] = {homeidx, awayidx, score?,
    shootout?} index into that group's teams; rounds[].shuffle -> the draw screen."""
    me = my_teamid(st)
    teams = [me] + cup_pool(meta["metaid"], me)
    random.shuffle(teams)
    groups = [{"teams": [cup_team(st, t) for t in teams[g * 4:g * 4 + 4]],
               "rounds": [{"shuffle": r == 0, "pairs": [{"legs": [{"homeidx": h, "awayidx": a}]}
                                                         for h, a in league_round([0, 1, 2, 3], r)]}
                          for r in range(3)]} for g in range(4)]
    cupid = f"{int(time.time()):08x}{os.urandom(8).hex()}"
    cup = {"_id": cupid, "metaid": meta["metaid"], "name": meta["name"], "type": "pve",
           "difficulty": difficulty, "current": {"phase": 0, "round": 0, "leg": 0},
           "phases": [{"system": "round-robin", "name": "Fase grup", "maxrounds": 3, "groups": groups}],
           "coaches": [{"_id": coach_id(_default_slot(st)), "state": "playing"}],
           "teams": [cup_team(st, t) for t in teams]}
    cup_refresh_stats(cup)
    return cup


def cup_my_pair(cup):
    """(group, leg) of my fixture in the current round, or (None, None)."""
    cur = cup["current"]
    for g in cup["phases"][cur["phase"]]["groups"]:
        for pair in g["rounds"][cur["round"]]["pairs"]:
            leg = pair["legs"][cur["leg"]]
            idx = [leg.get("homeidx"), leg.get("awayidx")]
            if any(i is not None and g["teams"][i]["coach"] for i in idx):
                return g, leg
    return None, None


def group_table(g):
    """[(team index, points, goal diff, goals)] best first."""
    t = {i: [0, 0, 0] for i in range(len(g["teams"]))}
    for rnd in g["rounds"]:
        for pair in rnd["pairs"]:
            leg = pair["legs"][0]
            if "score" not in leg:
                continue
            gh, ga = leg["score"]["home"], leg["score"]["away"]
            for i, f, a in ((leg["homeidx"], gh, ga), (leg["awayidx"], ga, gh)):
                t[i][0] += 3 if f > a else 1 if f == a else 0
                t[i][1] += f - a; t[i][2] += f
    return sorted(t, key=lambda i: tuple(-v for v in t[i]))


def cup_refresh_stats(cup):
    """Per-team W/D/L over the whole cup + group points/rank on every team object (group
    tables, "Juara - <rank> (W/D/L)" next-match panel, "Grup C - Peringkat <rank>")."""
    wdl = {}
    for phase in cup["phases"]:
        for g in phase["groups"]:
            for rnd in g["rounds"]:
                for pair in rnd["pairs"]:
                    leg = pair["legs"][0]
                    if "score" not in leg:
                        continue
                    for i, f, a in ((leg["homeidx"], leg["score"]["home"], leg["score"]["away"]),
                                    (leg["awayidx"], leg["score"]["away"], leg["score"]["home"])):
                        r = wdl.setdefault(g["teams"][i]["teamid"], [0, 0, 0])
                        r[0 if f > a else 1 if f == a else 2] += 1
    for phase in cup["phases"]:
        for g in phase["groups"]:
            order = group_table(g) if phase["system"] == "round-robin" else range(len(g["teams"]))
            for rank, i in enumerate(order):
                t = g["teams"][i]; w, d, l = wdl.get(t["teamid"], [0, 0, 0])
                t.update(win=w, draw=d, lose=l, point=3 * w + d, rank=rank + 1)


def cup_play_round(st, cup, mine=None):
    """Play the current round (my fixture = `mine` (home, away) if given, the rest simulated),
    then advance current / build the knock-out / settle my state."""
    cur = cup["current"]; phase = cup["phases"][cur["phase"]]; ko = phase["system"] == "knock-out"
    rng = random.Random()
    for g in phase["groups"]:
        for pair in g["rounds"][cur["round"]]["pairs"]:
            leg = pair["legs"][cur["leg"]]
            if "score" in leg or leg.get("homeidx") is None:
                continue
            h, a = g["teams"][leg["homeidx"]], g["teams"][leg["awayidx"]]
            played = mine is not None and (h["coach"] or a["coach"])
            if played:
                gh, ga = mine[:2]
            else:
                gh, ga = sim_score(rng, team_strength(st, h["teamid"]), team_strength(st, a["teamid"]))
            leg["score"] = {"home": gh, "away": ga}
            if ko and gh == ga and played and len(mine) > 3 and mine[2] != mine[3]:  # shootout played in game
                leg["shootout"] = {"home": mine[2], "away": mine[3]}
            elif ko and gh == ga:  # penalties
                ph = rng.randint(3, 5); pa = ph + rng.choice((-1, 1)) if ph < 5 else ph - 1
                leg["shootout"] = {"home": ph, "away": pa}
    me_state = cup["coaches"][0]
    if not ko:
        if cur["round"] + 1 < len(phase["groups"][0]["rounds"]):
            cur["round"] += 1
            return
        # groups done: A1-B2, B1-A2, C1-D2, D1-C2
        tables = [group_table(g) for g in phase["groups"]]
        qual = []
        for x, y in ((0, 1), (2, 3)):
            qual += [phase["groups"][x]["teams"][tables[x][0]], phase["groups"][y]["teams"][tables[y][1]],
                     phase["groups"][y]["teams"][tables[y][0]], phase["groups"][x]["teams"][tables[x][1]]]
        cup["phases"].append({"system": "knock-out", "name": "Babak gugur", "maxrounds": 3, "groups": [{"teams": qual, "rounds": [
            {"shuffle": True, "pairs": [{"legs": [{"homeidx": i, "awayidx": i + 1}]} for i in range(0, 8, 2)]},
            {"pairs": [{"legs": [{}]} for _ in range(2)]}, {"pairs": [{"legs": [{}]}]}]}]})
        cup.update(current={"phase": cur["phase"] + 1, "round": 0, "leg": 0})
        if not any(t["coach"] for t in qual):
            me_state["state"] = "eliminated"
        return
    g = phase["groups"][0]
    winners = []
    for pair in g["rounds"][cur["round"]]["pairs"]:
        leg = pair["legs"][0]; s = leg.get("shootout") or leg["score"]
        winners.append(leg["homeidx"] if s["home"] > s["away"] else leg["awayidx"])
    my_idx = next(i for i, t in enumerate(g["teams"]) if t["coach"]) if any(t["coach"] for t in g["teams"]) else None
    if my_idx is not None and me_state["state"] == "playing" and my_idx not in winners and \
            any(my_idx in (p["legs"][0].get("homeidx"), p["legs"][0].get("awayidx")) for p in g["rounds"][cur["round"]]["pairs"]):
        me_state["state"] = "eliminated"
    if cur["round"] + 1 < len(g["rounds"]):
        for k, pair in enumerate(g["rounds"][cur["round"] + 1]["pairs"]):
            pair["legs"][0] = {"homeidx": winners[2 * k], "awayidx": winners[2 * k + 1]}
        cur["round"] += 1
    else:  # final played
        cup["finished"] = True
        if me_state["state"] == "playing":
            me_state["state"] = "won" if my_idx in winners else "eliminated"  # CupMainScreen._cupState


def cup_resources(cup):
    uri = f"/cup/{cup['_id']}"
    return [{"resource": uri, "uri": uri, "body": cup}]


def ep_cup_new(query, body):
    """POST /cup/new {metaid, difficulty, teamid, lineup}."""
    req = json.loads(body or b"{}")
    meta = next((m for m in CUPS if m["metaid"] == int(req.get("metaid") or 0)), None)
    if meta is None:
        return _json({"result": False, "reason": "invalid metaid"})
    st = _state()
    cup = new_cup(st, meta, int(req.get("difficulty") or 0))
    st.setdefault("cups", {})[cup["_id"]] = cup
    _save(st)
    return _json({"result": True, "resources": cup_resources(cup)})


def cup_step(cupid, fn):
    st = _state(); cup = (st.get("cups") or {}).get(cupid)
    if cup is None:
        return _json({"result": False, "reason": "not exist cupid"})
    out = fn(st, cup) or {}
    _save(st)
    return _json(dict({"result": True, "resources": cup_resources(cup)}, **out))


def ep_cup_resume(cupid, query, body):
    return cup_step(cupid, lambda st, cup: None)


def ep_cup_next(cupid, query, body):
    """PUT /cup/<id>/next: play out the current round (my result from the match just played,
    if any) and advance (CupController.onMatchEndReceived -> ON_CUP_SCHEDULE_NEXT_ROUND)."""
    def step(st, cup):
        if cup.get("finished"):
            return
        cup_play_round(st, cup, cup.pop("_mine", None))
        cup_refresh_stats(cup)
    return cup_step(cupid, step)


def cup_reward_ep(cup):
    """EP for an ended cup: the meta's prize for the champion, a share of it by the round
    reached otherwise (group exit 10%, QF 20%, SF 35%, final 50%); 0 while still playing."""
    state = cup["coaches"][0]["state"]
    meta = next((m for m in CUPS if m["metaid"] == cup["metaid"]), CUPS[0])
    prize = meta["bestrewards"][-1][0]["count"]
    if state == "won":
        return prize
    if state != "eliminated":
        return 0
    ko = cup["phases"][1]["groups"][0] if len(cup["phases"]) > 1 else None
    mine = next((i for i, t in enumerate(ko["teams"]) if t["coach"]), None) if ko else None
    if mine is None:
        return prize // 10
    played = sum(1 for r in ko["rounds"] for p in r["pairs"]
                 if mine in (p["legs"][0].get("homeidx"), p["legs"][0].get("awayidx")) and "score" in p["legs"][0])
    return prize * (20, 35, 50)[max(0, min(played, 3) - 1)] // 100


def ep_cup_leave(cupid, query, body):
    """PUT /cup/<id>/leave: give up, or collect an ended cup's reward. CupReward*Scene.onItemExpand
    reads body.rewards[]: itemid 1 = EP (count), anything else is an item card."""
    st = _state(); cup = (st.get("cups") or {}).pop(cupid, None)
    ep = cup_reward_ep(cup) if cup else 0
    add_ep(st, ep)
    _save(st)
    return _json({"result": True, "rewards": [{"itemid": 1, "count": ep}] if ep else []})


def ep_cup_match(cupid, query, body):
    """POST /cup/<id>/match -> my fixture of the current round as a /match/<id> resource."""
    st = _state(); cup = (st.get("cups") or {}).get(cupid)
    g, leg = cup_my_pair(cup) if cup else (None, None)
    if leg is None or "score" in leg:
        return _json({"result": False, "reason": "already played match"})
    me = coach_me(st, _default_slot(st)); teamid = me["teamid"]
    home = g["teams"][leg["homeidx"]]["teamid"]; away = g["teams"][leg["awayidx"]]["teamid"]
    opp = away if home == teamid else home
    user = {"id": me["id"], "name": me["name"], "levelinfo": me["levelinfo"]}
    match = new_match("Cup_PvE", match_side(st["teams"][str(_default_slot(st))], teamid, user, _tactics(st)[1]),
                      match_side(roster_team(opp), opp))
    gs = match["gamesettings"]
    gs["home"] = home == teamid
    gs["kit"] = ["home", "away"] if gs["home"] else ["away", "home"]
    gs["difficulty"] = cup.get("difficulty", 0)  # AI level 0..4
    if cup["phases"][cup["current"]["phase"]]["system"] == "knock-out":
        # native maps drawMode 1/2/3 to end rules 9/6/7 (0 = a draw stands). 2 plays extra time
        # (FIRST_EXTRA_HALF seen in game); a draw after it is settled by cup_play_round
        gs["drawMode"] = 2
    st["match"] = match
    st["cup_pending"] = {"id": match["id"], "cupid": cupid, "home": home, "away": away}
    _save(st)
    return _json({"result": True, "resources": [{"uri": f"/match/{match['id']}", "body": match}]})


def ep_cup_record(query, body):
    """GET /cup/record: CupProxy.onCupRecordResourceChanged takes resources[] entries whose
    `resource` is /cup/record and body._id is my coach id."""
    st = _state(); coach = coach_me(st, _default_slot(st))
    return _json({"result": True, "resources": [{"resource": "/cup/record", "uri": "/cup/record",
                  "body": {"_id": coach["id"], "recordsbymetaid": []}}]})


# --------------------------------------------------------------------------- #
# Market (Bursa transfer): an NPC market over the cards fifaaddict lists, at their Indonesia-
# server prices (card_price). Offer rows are {spid, expandme: "player", grade, price, count};
# the client fills staticdata (names, position, ratings) from its own DB via `expandme`.
# --------------------------------------------------------------------------- #
LINES = {"GK": range(0, 1), "DF": range(1, 9), "MF": range(9, 20), "FW": range(20, 28)}  # Enums.PLAYERPOSITION
MAX_PLAYERS = 200  # coach.limits.players (squad size; user: "max skuad naikan", was 60)
# Tim inti (player state "first": Formasi lists only these) holds 36 ("Tim inti x/36" in Skuad);
# cards beyond that wait in Tim Cadangan, which Tukar Scout trades from.
FIRST_TEAM_MAX = 36


def first_team_count(team):
    return sum(1 for p in team["players"] if not p.get("team2"))


def balance_first_team(team):
    """Tim inti over FIRST_TEAM_MAX (clubs from before the cap): the least valuable cards outside
    the 18 of the squad move to Tim Cadangan."""
    extra = first_team_count(team) - FIRST_TEAM_MAX
    spare = [p for p in team["players"] if not p.get("team2") and not in_squad(team, p)]
    for p in sorted(spare, key=lambda p: card_price(p["spid"], grade_of(p)))[:max(0, extra)]:
        p["team2"] = True


def market_offer(spid, grade=1):
    return {"spid": spid, "expandme": "player", "grade": grade, "price": card_price(spid, grade), "count": 1}


def ep_transfer_top(position, query, body):
    """GET /transfer/top/50[/position/<GK|DF|MF|FW>]: the 50 priciest tradeable cards."""
    rng = LINES.get(str(position or "").upper())
    where = f"AND p.preferredposition1 BETWEEN {rng.start} AND {rng.stop - 1}" if rng else ""
    with closing(_db()) as c:
        both = c.execute(f"""SELECT p.spid, {POSOVR}, p.overallrating FROM players p
                             WHERE 1 {where}""").fetchall()  # every Market card, licensed or not
    _OVR.update((s, (o, d)) for s, o, d in both)
    rows = [(s, o) for s, o, _ in both]
    fa = fa_prices()
    price = lambda s, ovr: fa.get(s) or price_formula(ovr)
    spids = [s for s, ovr in sorted((r for r in rows if on_market(r[0])), key=lambda r: (-price(*r), r[0]))[:50]]
    return _json([market_offer(s) for s in spids])


def ep_transfer_search(spid, query, body):
    """GET /transfer/search/<spid>: that card's offers (grades 1..3); none if not tradeable."""
    return _json([market_offer(int(spid), g) for g in (1, 2, 3)] if on_market(spid) else [])


def add_player(team, spid, grade=1):
    """A new card instance in my club (reserves); returns it."""
    with closing(_db()) as c:
        r = c.execute("SELECT playerid FROM players WHERE spid = ?", (int(spid),)).fetchone()
        name = gen_teams.jersey_name(c, r[0]) if r else ""
    used = {p.get("jersey") for p in team["players"]}
    p = {"_id": new_iid(), "spid": int(spid), "expandme": "player", "grade": int(grade),
         "jersey": next((n for n in range(12, 100) if n not in used), 99), "jerseyname": name}
    if first_team_count(team) >= FIRST_TEAM_MAX:
        p["team2"] = True  # Tim inti full
    team["players"].append(p)
    return p


def remove_player(team, p):
    """Drop a card instance from my club and from the squad, captain and kickers."""
    team["players"].remove(p)
    sq, pid = team["squad"], iid(p)
    sq["positions"] = [s for s in sq["positions"] if str(s.get("id") or s["spid"]) != pid]
    sq["kickers"] = [k for k in sq.get("kickers", []) if str(k) != pid]
    if str(sq.get("captain")) == pid:
        sq["captain"] = next((str(s.get("id") or s["spid"]) for s in sq["positions"] if s["pos"]), "")


def is_starter(team, p):
    return any(str(s.get("id") or s["spid"]) == iid(p) and s["pos"] < POS_SUB for s in team["squad"]["positions"])


def in_squad(team, p):
    """Starter or sub (the 18 on the team sheet); reserves are not."""
    return any(str(s.get("id") or s["spid"]) == iid(p) for s in team["squad"]["positions"])


def ep_transfer_purchase(spid, grade, price, query, body):
    """POST /transfer/purchase/<spid>/<grade>/<price>/purchase: pay the market price (not the
    path's) and add a new instance of the card to my players (reserves)."""
    spid, grade = int(spid), int(grade)
    st = _state(); slot = _default_slot(st); team = st["teams"][str(slot)]
    if not on_market(spid):
        return _json({"result": False, "reason": "NOT_EXIST_OFFER"})
    if len(team["players"]) >= MAX_PLAYERS:
        return _json({"result": False, "reason": "Exceed plyer limit"})
    cost = card_price(spid, grade)
    if coach_me(st, slot)["money"]["ep"] < cost:
        return _json({"result": False, "reason": "NOT_ENOUGH_EP"})
    add_ep(st, -cost)
    add_player(team, spid, grade)
    _save(st)
    return _json({"result": True, "resources": coach_me_resources(st, slot)})


# Selling: the NPC market buys at once. The offer shows as "sold" in Daftar jual saya until
# its EP is collected (price minus SALE_FEE). Asking more than the market price is refused,
# so buying back is never cheaper than selling.
SALE_FEE = 0.10
# transfer-fee discounts (TransferMarketProxy.modifyOffersFee: floor(factor * completefee.base)):
# PC warnet (IGR) 20%, VIP 30% -- fifaaddict's FO3 tax calculator; both always active here
NO_BOOST = {"igr": 0.2, "vip": 0.3, "event": 0, "item": 0, "discountlimit": 0}


def sale_net(o):
    fee = o["completefee"]["base"]
    return o["price"] - fee + int(NO_BOOST["igr"] * fee) + int(NO_BOOST["vip"] * fee)


def offer_list(st):
    """/transfer/my/offer/list (TransferMarketProxy.fetch*): offers[] {_id, status
    selling|sold|expired, issuedat, completefee.base (fee in EP), spid, grade, price}."""
    return {"offerexpireoffset": 86400000, "boost": NO_BOOST, "offers": st.get("offers", [])}


def offer_resources(st):
    return [{"uri": "/transfer/my/offer/list", "body": offer_list(st)}] + coach_me_resources(st, _default_slot(st))


def ep_transfer_price(spid, grade, query, body):
    """GET /transfer/price/<spid>/<grade>: the Jual tab's price band and default."""
    p = card_price(spid, int(grade))
    return _json({"lower": p * 8 // 10, "upper": p, "default": p, "lowest": p, "registrationFee": 0})


def ep_transfer_offer(query, body):
    """POST /transfer/offer {playerobjectid, price}: sell one of my players (not a starter)."""
    req = json.loads(body or b"{}")
    st = _state(); team = st["teams"][str(_default_slot(st))]
    pid = str(req.get("playerobjectid"))
    p = next((p for p in team["players"] if iid(p) == pid), None)
    price = int(req.get("price") or 0)
    if p is None or is_starter(team, p):
        return _json({"result": False, "reason": "TRANSFERMARKET_SELECT_SELLABLE_PLAYER"})
    if not 0 < price <= card_price(p["spid"], grade_of(p)):
        return _json({"result": False, "reason": "TRANSFERMARKET_LOWERLIMIT"})
    remove_player(team, p)
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    st.setdefault("offers", []).append({
        "_id": f"{int(time.time()):08x}{os.urandom(8).hex()}", "status": "sold", "issuedat": now, "soldat": now,
        "spid": p["spid"], "expandme": "player", "grade": grade_of(p), "price": price,
        "playerobjectid": pid, "completefee": {"base": int(price * SALE_FEE)}})
    _save(st)
    return _json({"result": True, "resources": offer_resources(st)})


def ep_offer_collect(offerid, coupon, query, body):
    """DELETE /transfer/my/offer/completed/<id|all>[/<coupon>]: collect sold offers' EP."""
    st = _state(); offers = st.get("offers", [])
    done = [o for o in offers if o["status"] == "sold" and offerid in ("all", o["_id"])]
    if not done:
        return _json({"result": False, "reason": "NOT_EXIST_OFFER"})
    ep = sum(sale_net(o) for o in done)
    add_ep(st, ep)
    st["offers"] = [o for o in offers if o not in done]
    _save(st)
    return _json({"result": True, "ep": ep, "resources": offer_resources(st)})


# --------------------------------------------------------------------------- #
# Tim > Manajemen pemain > Upgrade (enhancement v1, TeamManagementPlayerEnhancementOldTabPage
# + TeamManagementPlayerEnhancingPopupScreen): two cards of the same player, the lower-grade
# one (material) is consumed. Buy a second copy on the Market to have a pair.
# --------------------------------------------------------------------------- #
ENHANCE_RATE = (1.0, 1.0, 0.81, 0.64, 0.50, 0.26, 0.15, 0.07, 0.04, 0.02, 0)  # +g -> +(g+1) chance
ENHANCE_DROP_FROM = 5  # a failed upgrade from +5 or higher drops one grade


def enhance_pair(team, a, b):
    pa = next((p for p in team["players"] if iid(p) == str(a)), None)
    pb = next((p for p in team["players"] if iid(p) == str(b)), None)
    if pa is None or pb is None or pa is pb or pa["spid"] != pb["spid"] or grade_of(pa) >= 10:
        return None, None
    return pa, pb


def enhance_rate(target, material):
    """Base chance by target grade; a material of the same grade (or one less) raises it
    (ENHANCEMENT_SUCCESSPROB_BONUS_HIGH / NORMAL)."""
    tg, mg = grade_of(target), grade_of(material)
    return min(1.0, ENHANCE_RATE[tg] * (1.5 if mg >= tg else 1.25 if mg == tg - 1 else 1.0))


def enhance_cost(target):
    return max(1000, sig3(card_price(target["spid"], grade_of(target)) // 50))  # 2% of its value


def merge_into(team, keep, gone):
    """The consumed card's squad slot (starter > sub) passes to the kept card of the same
    player, so an upgrade never leaves a hole in the XI; then the consumed card goes."""
    sq, k, g = team["squad"], iid(keep), iid(gone)
    slot = {str(s.get("id") or s["spid"]): s for s in sq["positions"]}
    if g in slot and (k not in slot or slot[g]["pos"] < POS_SUB <= slot[k]["pos"]):
        if k in slot:
            sq["positions"].remove(slot[k])
        slot[g]["id"] = k
        if str(sq.get("captain")) == g:
            sq["captain"] = k
        sq["kickers"] = [k if str(x) == g else x for x in sq.get("kickers", [])]
    remove_player(team, gone)


def ep_enhance_preview(a, b, query, body):
    """GET /player/<target>/enhance/<material>/preview -> {grade, levelinfo, price, enhancepay}."""
    st = _state(); team = st["teams"][str(_default_slot(st))]
    pa, pb = enhance_pair(team, a, b)
    if pa is None:
        return _json({"result": False, "reason": "INVALID_PARAM"})
    g = grade_of(pa) + 1
    return _json({"result": True, "grade": g, "levelinfo": pa.get("levelinfo", {"level": 1, "exp": 0}),
                  "price": card_price(pa["spid"], g), "enhancepay": enhance_cost(pa),
                  "probability": round(enhance_rate(pa, pb) * 100)})


def ep_enhance(a, b, boost, guard, query, body):
    """PUT /player/<target>/enhance/<material>[/<boostitem>/<guarditem>] -> {success, grade,
    rgrade, rprice, attribPlus} (TeamManagementPlayerEnhancingPopupScreen.onEnhanceComplete)."""
    st = _state(); slot = _default_slot(st); team = st["teams"][str(slot)]
    pa, pb = enhance_pair(team, a, b)
    if pa is None:
        return _json({"result": False, "reason": "INVALID_PARAM"})
    cost = enhance_cost(pa)
    if coach_me(st, slot)["money"]["ep"] < cost:
        return _json({"result": False, "reason": "Invalid target ep price"})
    add_ep(st, -cost)
    before = grade_of(pa)
    success = random.random() < enhance_rate(pa, pb)
    after = before + 1 if success else before - 1 if before >= ENHANCE_DROP_FROM else before
    pa["grade"] = after
    merge_into(team, pa, pb)
    _save(st)
    return _json({"result": True, "success": success, "grade": after, "rgrade": after,
                  "rprice": card_price(pa["spid"], after), "attribPlus": GRADE_BOOST[after] - GRADE_BOOST[before],
                  "resources": coach_me_resources(st, slot)})


# --------------------------------------------------------------------------- #
# Tim > Manajemen pemain > Tukar (player trade v1, TeamManagementPlayerRecycleTabPage +
# TeamManagementPlayerRecyclePopupScreen). Give 2..10 reserves; clubs offer Market cards
# rated at least avg(card OVR) + playertrade_cardovrbonus[n] (the tab's "expected OVR"):
# playertrade_normal[n] offers plus one each for PC warnet and VIP (the popup has 5 slots).
# An offer is a card, a card plus EP, or EP only (no spid). Tables: static_db_STATICVALUES.json.
# --------------------------------------------------------------------------- #
_TRADE_POOL = {}


def static_values():
    with open(os.path.join(RESPONSES, "static_db_STATICVALUES.json"), encoding="utf-8") as f:
        return json.load(f)


def trade_grades(gsum):
    """PLAYERTRADE.playergrade[sum of the given cards' grades] -> {grade: weight}: +1 always,
    one more possible grade per 8 grade points traded (the tab shows its first..last key)."""
    return {str(g): 0.5 ** (g - 1) for g in range(1, min(10, 1 + int(gsum) // 8) + 1)}


def trade_pool():
    """{client rating: [(spid, teamid)]} of every Market card, any season (cached). It used to
    take only cards licensed for the ID region, which left out most old and special seasons
    (user: "trading munculkan semua playernya")."""
    if not _TRADE_POOL:
        with closing(_db()) as c:
            for s, t, o in c.execute(f"SELECT p.spid, p.teamid, {POSOVR} FROM players p"):
                if on_market(s):
                    _TRADE_POOL.setdefault(o, []).append((s, t))
    return _TRADE_POOL


def trade_props(give, rng):
    sv = static_values()
    lim, n = sv["playertrade_limit"], len(give)
    avg = sum(card_ovr(p["spid"]) for p in give) / n
    lo = int(min(max(avg + sv["playertrade_cardovrbonus"][str(n)], lim["CardMinValue"]), lim["CardMaxValue"]))
    total = sum(card_price(p["spid"], grade_of(p)) for p in give)
    weights = trade_grades(sum(grade_of(p) for p in give))
    pool = trade_pool()
    props = []
    for i, boost in enumerate([None] * int(sv["playertrade_normal"][str(n)]) + ["igr", "vip"]):
        ovr = lo
        while ovr < min(lo + 8, lim["CardMaxValue"]) and rng.random() < 0.55:  # sometimes better
            ovr += 1
        s, t = rng.choice(pool[min(pool, key=lambda r: (abs(r - ovr), -r))])
        if i == 1 and boost is None:  # a club buys the lot for EP
            props.append({"teamid": t, "ep": sig3(total * rng.uniform(0.9, 1.15))})
            continue
        g = int(rng.choices(list(weights), list(weights.values()))[0])
        offer = {"spid": s, "grade": g, "teamid": t, "price": card_price(s, g)}
        if i == 2 and boost is None:  # card plus a top-up
            offer["ep"] = sig3(total * rng.uniform(0.05, 0.2))
        if boost:
            offer["boost"] = boost  # always a PC warnet + VIP coach here
        props.append(offer)
    return props


def ep_trade_proposal(query, body):
    """POST /players/trade/proposal {players: [iid...]} -> {result, props}."""
    req = json.loads(body or b"{}")
    st = _state(); team = st["teams"][str(_default_slot(st))]
    ids = [str(i) for i in req.get("players") or []]
    give = [p for p in team["players"] if iid(p) in ids]
    if not 2 <= len(give) <= 10 or len(give) != len(set(ids)) or any(in_squad(team, p) for p in give):
        return _json({"result": False, "reason": "invalid playerid"})  # the tab lists reserves only
    props = trade_props(give, random.Random())
    st["trade"] = {"players": ids, "props": props}
    _save(st)
    return _json({"result": True, "props": props})


def ep_trade_contract(query, body):
    """POST /players/trade/contract {select: offer index}: the given players leave, the
    offer's card and/or EP arrive. With the trade3 feature on, the popup (shared with the
    Scout tab) posts /trade3/contract {slotnum, selectnum} instead, also for this tab."""
    req = json.loads(body or b"{}")
    st = _state(); slot = _default_slot(st); team = st["teams"][str(slot)]
    tr = st.get("trade") or {}
    props, ids = tr.get("props") or [], tr.get("players") or []
    sel = int(req.get("select", req.get("selectnum", -1)))
    give = [p for p in team["players"] if iid(p) in ids]
    if not 0 <= sel < len(props) or len(give) != len(ids):
        return _json({"result": False, "reason": "invalid playerid"})
    for p in give:
        remove_player(team, p)
    offer = props[sel]
    if "spid" in offer:
        add_player(team, offer["spid"], offer["grade"])
    add_ep(st, offer.get("ep", 0))
    st.pop("trade")
    _save(st)
    return _json({"result": True, "resources": coach_me_resources(st, slot)})


# --------------------------------------------------------------------------- #
# Tim > Manajemen pemain > Tukar #2 (trade3 "Scout": TeamManagementPlayerTrade3TabPage +
# ScouterController). Pick a scout, give 2..10 reserves: offers are Market cards worth about
# their total value (fifaaddict prices) times the scout's range. Scouts (coach/me `scouters`)
# differ in range and cooldown; meeting a scout's quest shifts the range up. Same popup and
# /trade3/contract as Tukar v1 (the shared popup runs in trade3 mode while the feature is on).
# --------------------------------------------------------------------------- #
NO_LIMIT = 9_000_000_000_000_000  # "unlimited": below 2^53, so exact as an AS3 Number
TRADE3_STATIC = {  # GET /trade3/staticvalues -> ScouterController.staticvalues
    # total EP given / reward cap; user: "buat tidak terbatas" (was 30 / 10 billion EP: the tab
    # warned "Hanya dapat memperoleh pemain maximum 10,000,000,000 EP")
    "playertrade3_limit": {"max_trade": NO_LIMIT, "max": NO_LIMIT},
    "vvip_playertrade3_limit": {"max_trade": NO_LIMIT, "max": NO_LIMIT},
    "playertrade3_normal": {str(n): 1 + (n >= 4) + (n >= 7) for n in range(2, 11)},  # offers by #players
    "playertrade3_effect_price": [10_000_000, 3_000_000, 1_000_000],  # popup premium effect tiers
}
SCOUTS = (  # (key = scoutid, string id, cooldown s, offer value x total, quest); always PC + VIP here
    # no cooldowns (user: "buat scout tidak ada reset"; were 1 day / 1 hour / 1 hour)
    ("default_A01", "STR_SCOUTER_KR_DEFAULT_A01", 0, (0.8, 1.1), None),
    ("default_B01", "STR_SCOUTER_KR_DEFAULT_B01", 0, (0.9, 1.3), {"require_value": 100000}),
    ("igr", "STR_SCOUTER_KR_IGR_A01", 0, (0.9, 1.4), {"require_grade": 2, "require_grade_count": 2}),
    ("vip", "STR_SCOUTER_KR_VIP_A01", 0, (1.0, 1.5), {"require_season": "88", "require_season_count": 2}),
)
QUEST_BONUS = 0.2  # quest met: the offer value range moves up by this x total
_T3_POOL = []


def w3c(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def coach_scouters(st, slot):
    """coach/me `scouters` (CoachProxy.tradeScouters): a scout on cooldown is locked and carries
    `cooltime` (W3C) for the countdown (ScouterController re-asks /trade3/info when it ends)."""
    used, now = next(c for c in st["coaches"] if c["slot"] == slot).get("scout_used", {}), time.time()
    out = []
    for key, sid, cool, _, quest in SCOUTS:
        until = used.get(key, 0) + cool
        out.append({"key": key, "scoutid": key, "scoutername": sid + "_NAME", "scouterdesc": sid + "_DESC",
                    "lock": until > now, **({"cooltime": w3c(until)} if until > now else {}),
                    **({"quest": quest} if quest else {})})
    return out


def quest_met(quest, give):
    """The tab's quest checks: total value, cards of grade >= G, cards of the listed seasons."""
    if not quest:
        return False
    if sum(card_price(p["spid"], grade_of(p)) for p in give) < quest.get("require_value", 0):
        return False
    if "require_grade" in quest and sum(grade_of(p) >= quest["require_grade"] for p in give) < quest["require_grade_count"]:
        return False
    seasons = {int(s) for s in str(quest.get("require_season", "")).split(",") if s}
    return not seasons or sum(int(p["spid"]) // 1000000 in seasons for p in give) >= quest["require_season_count"]


def trade3_pool():
    """[(price, spid, teamid, grade)] of Market cards at every grade +1..+10, cheapest first
    (cached). Grade +1 only capped an offer at the dearest +1 card (~20 billion EP), so a trade
    of ~71 billion got far less back (user: "pertukaran tidak sebanding dengan nilai yang diawal")."""
    if not _T3_POOL:
        rows = [r for o in trade_pool().values() for r in o]
        card_ovrs([s for s, _ in rows])  # one bulk query instead of one per unpriced card
        _T3_POOL.extend(sorted((card_price(s, g), s, t, g) for s, t in rows for g in range(1, 11)))
    return _T3_POOL


def trade3_props(give, scout, rng):
    """playertrade3_normal[n] cards worth total x the scout's range (capped at the reward max;
    the last of them adds an EP top-up) + PC warnet and VIP extras."""
    lo, hi = scout[3]
    if quest_met(scout[4], give):
        lo, hi = lo + QUEST_BONUS, hi + QUEST_BONUS
    total = sum(card_price(p["spid"], grade_of(p)) for p in give)
    pool, normal = trade3_pool(), TRADE3_STATIC["playertrade3_normal"][str(len(give))]
    props = []
    for i, boost in enumerate([None] * normal + ["igr", "vip"]):
        target = min(TRADE3_STATIC["playertrade3_limit"]["max"], total * rng.uniform(lo, hi))
        k = bisect.bisect_left(pool, (target,))
        price, s, t, gr = rng.choice(pool[max(0, k - 10):k + 10])  # one of the 20 cards nearest in value
        offer = {"spid": s, "grade": gr, "teamid": t, "price": price}
        if i == normal - 1 and normal > 1:
            offer["ep"] = sig3(max(1000, total * rng.uniform(0.05, 0.15)))
        if boost:
            offer["boost"] = boost
        props.append(offer)
    return props


def ep_trade3_proposal(query, body):
    """POST /trade3/proposal {id: scout key, slotnum, playerids: [{id, spid, grade, price}]}
    -> {result, props} (tab.onResponseRecycle opens the popup; it posts /trade3/contract)."""
    req = json.loads(body or b"{}")
    st = _state(); slot = _default_slot(st); team = st["teams"][str(slot)]; coach = my_coach(st)
    scout = next((s for s in SCOUTS if s[0] == req.get("id")), None)
    sent = {str(p.get("id")): p for p in req.get("playerids") or []}
    give = [p for p in team["players"] if iid(p) in sent]
    if (scout is None or not 2 <= len(give) <= 10 or len(give) != len(sent)
            or any(in_squad(team, p) or not p.get("team2") for p in give)):  # Tim Cadangan only
        return _json({"result": False, "reason": "invalid playerid"})
    if any(int(sent[iid(p)].get("price") or 0) != card_price(p["spid"], grade_of(p)) for p in give):
        return _json({"result": False, "reason": "TRADE3_NOTICE_PLAYERPRICE03"})  # client shows a recheck notice
    used = coach.setdefault("scout_used", {})
    if used.get(scout[0], 0) + scout[2] > time.time():
        return _json({"result": False, "reason": "TRADE2_ERROR_TOO_MANY_TRY"})  # still cooling down
    if sum(card_price(p["spid"], grade_of(p)) for p in give) >= TRADE3_STATIC["playertrade3_limit"]["max_trade"]:
        return _json({"result": False, "reason": "invalid playerid"})  # the tab blocks this first
    props = trade3_props(give, scout, random.Random())
    if scout[2]:
        used[scout[0]] = time.time()  # a negotiation uses the scout, picked or not
    st["trade"] = {"players": [iid(p) for p in give], "props": props}
    _save(st)
    return _json({"result": True, "props": props, "resources": coach_me_resources(st, slot)})


def ep_player_move(pid, where, query, body):
    """POST /player/<_id>/move/firstteam|secondteam (Tim > Manajemen pemain > Skuad): Tim inti <->
    Tim Cadangan. Players in the 18 of the squad stay in Tim inti."""
    st = _state(); slot = _default_slot(st); team = st["teams"][str(slot)]
    p = next((x for x in team["players"] if iid(x) == pid), None)
    if p is None or (where == "secondteam" and in_squad(team, p)):
        return _json({"result": False, "reason": "TRANSFERMARKET_SELL_ERROR_FIRSTTEAM"})
    if where == "firstteam" and p.get("team2") and first_team_count(team) >= FIRST_TEAM_MAX:
        return _json({"result": False, "reason": "FIRSTTEAM_REGISTRATION_FAIL_POPUP_TITLE"})  # Tim inti full
    if where == "secondteam":
        p["team2"] = True
    else:
        p.pop("team2", None)
    _save(st)
    return _json({"result": True, "resources": coach_me_resources(st, slot)})


def ep_trade3_info(query, body):
    """POST /trade3/info {}: the client only reads the pushed resources (scouters)."""
    st = _state()
    return _json({"result": True, "resources": coach_me_resources(st, _default_slot(st))})


# --------------------------------------------------------------------------- #
# Shop > Pemain sells player packs for EP; Item saya > Pemain opens them one card at a time
# (TeamManagementPlayerCardScreen: POST /item/<_id>/use -> {player}). Icons are the real FO3 ID
# shop images in the client's CDN cache (externalAssets/shop/<itemid>.png). A pack draws a
# Market card from its pool, cheaper cards more likely (weight price^-0.75: ~0.5% for a card
# worth 10x the pool median); its price is the expected card value at fifaaddict prices.
# --------------------------------------------------------------------------- #
POSITIONS = {"GK": (0, 0), "DF": (1, 8), "MF": (9, 19), "FW": (20, 27)}  # preferredposition1
PACKS = {  # itemid (= shop icon): (name, pool)
    28001: ("Premier League 65+", {"league": 13, "ovr": 65}),
    28002: ("Bundesliga 65+", {"league": 19, "ovr": 65}),
    28003: ("LaLiga 65+", {"league": 53, "ovr": 65}),
    28004: ("Serie A 65+", {"league": 31, "ovr": 65}),
    28005: ("Ligue 1 65+", {"league": 16, "ovr": 65}),
    28006: ("FW 60+", {"pos": "FW", "ovr": 60}),
    28007: ("MF 60+", {"pos": "MF", "ovr": 60}),
    28008: ("DF 60+", {"pos": "DF", "ovr": 60}),
    28009: ("GK 60+", {"pos": "GK", "ovr": 60}),
    8223600: ("FW 65+", {"pos": "FW", "ovr": 65}),
    8223601: ("MF 65+", {"pos": "MF", "ovr": 65}),
    8223602: ("DF 65+", {"pos": "DF", "ovr": 65}),
    8223603: ("GK 65+", {"pos": "GK", "ovr": 65}),
    8223508: ("FW NEW 70+ 2017", {"pos": "FW", "ovr": 70, "season": 88}),
    8223509: ("MF NEW 70+ 2017", {"pos": "MF", "ovr": 70, "season": 88}),
    8223510: ("DF NEW 70+ 2017", {"pos": "DF", "ovr": 70, "season": 88}),
    82214068: ("World Best 300 2017", {"top": 300, "season": 88}),
    82214180: ("World Best 50 2017", {"top": 50, "season": 88}),
    # This Season = 2017 (spid season 88 = the client's DEFAULT_SEASON_ID, badge '17)
    45011: ("This Season Top 10", {"top": 10, "season": 88}),
    45012: ("This Season Top 20", {"top": 20, "season": 88}),
    45013: ("This Season Top 30", {"top": 30, "season": 88}),  # (Top 50 = World Best 50 2017)
    45097: ("This Season Top 30 +8", {"top": 30, "season": 88, "grade": 8}),
    # older seasons; fifaaddict season codes: 2006..2011 = 6..11, 2014 = 14, 2015 = 15
    45004: ("06-11 Player Top 10", {"top": 10, "seasons": (6, 7, 8, 9, 10, 11)}),
    45006: ("06-11 Player Top 30", {"top": 30, "seasons": (6, 7, 8, 9, 10, 11)}),
    45007: ("06-11 Player Top 50", {"top": 50, "seasons": (6, 7, 8, 9, 10, 11)}),
    82214356: ("All '07", {"season": 7}),
    739: ("'08 Top 3", {"top": 3, "season": 8}),
    15031: ("'14 Top 30 +5", {"top": 30, "season": 14, "grade": 5}),
    16001: ("'15 Top 30 +5", {"top": 30, "season": 15, "grade": 5}),
    # special seasons: wc2010 = 47, ucl2010 = 69, wc2014 = 77, tots2014 = 53, 16ec = 35, lp = 23,
    # wb (World Best) = 91, wl (World Legend) = 93
    7622: ("World Cup '10 Top 30", {"top": 30, "season": 47}),
    7618: ("Europe '10 Top 30 +3", {"top": 30, "season": 69, "grade": 3}),
    880047: ("World Cup '14 Top 50", {"top": 50, "season": 77}),
    45020: ("Special Season '14 Top 30", {"top": 30, "season": 77}),
    45063: ("Special Season '14 Top 50 +5", {"top": 50, "season": 77, "grade": 5}),
    45026: ("Team of the Season '14 Top 20", {"top": 20, "season": 53}),
    45027: ("Team of the Season '14 Top 30", {"top": 30, "season": 53}),
    45028: ("Team of the Season '14 Top 50", {"top": 50, "season": 53}),
    45130: ("Team of the Season '14 All", {"season": 53}),
    8157: ("EC Top 10", {"top": 10, "season": 35}),
    82232124: ("EC Top 30 +3", {"top": 30, "season": 35, "grade": 3}),
    8135: ("Best EC 30 +5", {"top": 30, "season": 35, "grade": 5}),
    8329: ("LP Top 50 +3", {"top": 50, "season": 23, "grade": 3}),
    8334: ("World Best 5", {"top": 5, "season": 91}),
    8336: ("World Best 1", {"top": 1, "season": 91}),
    82231307: ("World Best +5", {"season": 91, "grade": 5}),
    8194: ("World Legend Top 30", {"top": 30, "season": 93}),
    8335: ("World Legend Top 3", {"top": 3, "season": 93}),
    # every season
    82214007: ("Top 300", {"top": 300}),
    8223562: ("Best 30", {"top": 30}),
    7675: ("All Season +8", {"grade": 8, "all": True}),  # user: "pack baru khusus all session grade 1-8"
}
# "All <season>" like All '07 for every season fifaaddict lists (user: "yang keluar cuma 07,
# saya mau semua session"): (fifaaddict label, card season codes, closest shop icon). Codes
# checked against fifaaddict's own listings (e.g. tco92 = 49, mufclegend = 55, cel = 31 + 33).
FA_SEASONS = (
    ("06", (6,), 45004), ("06U", (63,), 7618), ("06WC", (65,), 7622), ("08", (8,), 739), ("08E", (67,), 8157),
    ("09", (9,), 45006), ("10", (10,), 45007), ("10U", (69,), 7618), ("10WC", (47,), 7622), ("11", (11,), 45004),
    ("14", (14,), 15031), ("14WC", (77,), 880047), ("14T", (53,), 45026), ("15", (15,), 16001), ("16", (16,), 16001),
    ("17", (88,), 45011), ("16EC", (35,), 8157), ("16TH", (45,), 8545), ("WB", (91,), 8334), ("WL", (93,), 8194),
    ("UB", (28,), 8336), ("UL", (30,), 8335), ("EL", (27,), 8194), ("LP", (23,), 8329), ("CP", (22,), 8549),
    ("CC", (24,), 8547), ("17TH", (44,), 8545), ("TCO92", (49,), 8546), ("MUA", (55,), 8547), ("ML", (71,), 8549),
    ("TL", (51,), 8549), ("VL", (61,), 8549), ("CL", (89,), 8549), ("KL02", (95,), 8546), ("CEL", (31, 33), 8335),
    ("U23", (29,), 8545), ("KXI", (97,), 8546), ("MUL", (48,), 8547), ("VS", (73,), 2000006),
)
PACKS.update({92000000 + codes[0]: (f"All {label}", {"seasons": codes, "icon": icon, "all": True})
              for label, codes, icon in FA_SEASONS})
PACKS[82214356] = ("All 07", {"season": 7, "all": True})  # the original All '07: every '07 card too
PACK_BUNCHES = (1, 10)  # buy 1 or 10 (5% off)
_PACK_POOL = {}


def pack_pool(itemid):
    """[(spid, weight)] of the Market cards a pack can give (cached)."""
    if itemid not in _PACK_POOL:
        f = PACKS[itemid][1]
        lo, hi = POSITIONS.get(f.get("pos"), (0, 27))
        seasons = f.get("seasons") or ((f["season"],) if "season" in f else None)
        # "65+" = the rating the card shows once opened (POSOVR base + its grade's boost, as on fifaaddict)
        # "all" packs take every Market card; the others only cards licensed for the ID region
        # (permid), which leaves e.g. 37 of the 395 '06 cards
        with closing(_db()) as c:
            rows = c.execute(f"""SELECT p.spid, {POSOVR} AS ovr FROM players p LEFT JOIN permid m ON m.spid = p.spid
                                 WHERE ovr >= ?1 AND p.preferredposition1 BETWEEN ?2 AND ?3
                                 AND (?4 IS NULL OR p.leagueid = ?4) AND (?5 OR m.licensed = 1)""",
                             (f.get("ovr", 0) - GRADE_BOOST[1], lo, hi, f.get("league"), bool(f.get("all")))).fetchall()
        rows = sorted(((s, o) for s, o in rows if on_market(s) and (not seasons or s // 1000000 in seasons)),
                      key=lambda r: -r[1])[:f.get("top")]
        # a price far below the pool's (odd fifaaddict data, e.g. a 120k World Best card among 20M+
        # ones) would win almost every draw: weigh anything under a tenth of the median as that tenth
        prices = [card_price(s) for s, _ in rows]
        floor = sorted(prices)[len(prices) // 2] / 10
        _PACK_POOL[itemid] = [(s, max(p, floor) ** -0.75) for (s, _), p in zip(rows, prices)]
    return _PACK_POOL[itemid]


def pack_grades(itemid):
    """[(grade, weight)]: a "+N" pack's card comes at a random grade +1..+N, lower grades more
    likely (weight N-g+1: for +5 that is 33/27/20/13/7%); other packs give +1."""
    n = PACKS[itemid][1].get("grade", 1)
    return [(g, n - g + 1) for g in range(1, n + 1)]


def pack_grade(itemid):
    grades = pack_grades(itemid)
    return random.choices([g for g, _ in grades], [w for _, w in grades])[0]


@functools.lru_cache(maxsize=None)
def pack_price(itemid):
    pool, grades = pack_pool(itemid), pack_grades(itemid)
    gw = sum(w for _, w in grades)
    return sig3(sum(card_price(s, g) * w * gwt for s, w in pool for g, gwt in grades) / sum(w for _, w in pool) / gw)


def warm_packs():
    """Build every pack pool and price and the trade pools once (~20 s) so the first Shop or
    Tukar request does not stall the client."""
    for itemid in PACKS:
        pack_price(itemid)
    trade3_pool()


SHOP_FREE = True  # user: "shop jadi 0 semua" -> every Shop product costs 0 EP


def shop_price(value, n):
    return 0 if SHOP_FREE else sig3(value * n * (0.95 if n > 1 else 1))


def item_def(itemid):
    """Item staticdata (POST /static/cache/item, shop products, coach items)."""
    if itemid == EP_ITEM:
        return EP_ITEM_DEF
    if itemid in ITEMS:
        return use_item_def(itemid)
    if itemid in PACKAGES or itemid in MONTHLY:
        return package_def(itemid)
    name, f = PACKS[itemid]
    return {"itemid": itemid, "productid": itemid, "name": name, "category": "PLAYERPACK",
            "type": "LEAGUE" if "league" in f else "POSITION" if "pos" in f else "CUSTOM",
            "unit": "COUNT", "itemicon": f.get("icon", itemid), "currency": "ep",
            **({"bundleicon": f["icon"]} if "icon" in f else {}),  # several packs share one icon
            "bunches": [{"bunch": n, "price": shop_price(pack_price(itemid), n)} for n in PACK_BUNCHES],
            **({"draw": {"year": 2017}} if f.get("season") == 88 else {})}


# --------------------------------------------------------------------------- #
# Shop items besides player packs (icons = the real FO3 ID shop images in the client cache).
# Category USE (Shop > Dapat dikonsumsi; Item saya > Dapat dikonsumsi): "Gunakan" opens the
# item's screen (GeneralInventoryTileItemRenderer.useUseItem by staticdata.type), which posts
# /item/<_id>/use (ItemUtil.postItemUse):
#   EP_LOTTERY (TeamManagementEPCardScreen) {id} -> {result, ep}
#   ITEM_LOTTERY (TeamManagementRandomCardScreen) {id} -> {result, ep} | {result, item:{itemid, count}}
#   COACHNAME_CHANGE {name} -> {result} (the name check first posts /coach/reservename {name})
#   TEAM_CHANGE (nation/league/club steppers, /static/info/team/summary/<id>) {teamid} -> {result}
# PACKAGE (Shop > Paket) bundles drop their items into the inventory on purchase; MILEAGE
# (MP Shop) sells copies of some items. All free while SHOP_FREE.
# --------------------------------------------------------------------------- #
EP_ITEM = 1  # externalAssets/shop/1.png = EP coin: EP in Bag entries and package contents
EP_ITEM_DEF = {"itemid": EP_ITEM, "productid": EP_ITEM, "name": "EP", "category": "EP", "type": "EP",
               "unit": "EP", "itemicon": EP_ITEM}
ITEMS = {  # itemid: (name, icon, type, effect, description)
    501: ("Lucky EP", 501, "EP_LOTTERY", {"ep": (10_000, 1_000_000)}, "Buka kartu: dapat 10.000 - 1.000.000 EP."),
    50029: ("Lucky EP max 20.000.000", "ID50029", "EP_LOTTERY", {"ep": (100_000, 20_000_000)},
            "Buka kartu: dapat 100.000 - 20.000.000 EP."),
    50028: ("Lucky EP max 50.000.000", "ID50028", "EP_LOTTERY", {"ep": (200_000, 50_000_000)},
            "Buka kartu: dapat 200.000 - 50.000.000 EP."),
    908: ("Jutawan EP Lottery", 908, "EP_LOTTERY", {"ep": (1_000_000, 10_000_000)}, "Undian EP: 1 - 10 juta EP."),
    50019: ("EP +200.000", "ID50019", "EP_LOTTERY", {"ep": (200_000, 200_000)}, "Langsung dapat 200.000 EP."),
    50020: ("EP +500.000", "ID50020", "EP_LOTTERY", {"ep": (500_000, 500_000)}, "Langsung dapat 500.000 EP."),
    50021: ("EP +1.000.000", "ID50021", "EP_LOTTERY", {"ep": (1_000_000, 1_000_000)}, "Langsung dapat 1.000.000 EP."),
    50840: ("Kantong EP", "ID50840", "EP_LOTTERY", {"ep": (500_000, 5_000_000)}, "Isi 500.000 - 5.000.000 EP."),
    50841: ("Peti EP", "ID50841", "EP_LOTTERY", {"ep": (5_000_000, 30_000_000)}, "Isi 5 - 30 juta EP."),
    119082304: ("Mesin Jackpot EP", 119082304, "EP_LOTTERY", {"ep": (100_000, 100_000_000)},
                "Jackpot: 100.000 - 100.000.000 EP."),
    # random boxes: one prize from the table, (weight, EP amount | pack itemid)
    50121: ("Kotak Platinum", "ID50121GP", "ITEM_LOTTERY",
            {"prizes": [(10, 8334), (5, 8194), (25, 82214007), (30, 45011), (30, ("ep", 10_000_000))]},
            "Hadiah acak: World Best 5, World Legend Top 30, Top 300, This Season Top 10 atau 10 juta EP."),
    50122: ("Kotak Emas", "ID50122GP", "ITEM_LOTTERY",
            {"prizes": [(30, 82214180), (40, 45012), (30, ("ep", 3_000_000))]},
            "Hadiah acak: World Best 50 2017, This Season Top 20 atau 3 juta EP."),
    50123: ("Kotak Perak", "ID50123GP", "ITEM_LOTTERY",
            {"prizes": [(25, 8223600), (25, 8223601), (25, 8223602), (25, ("ep", 1_000_000))]},
            "Hadiah acak: pak FW/MF/DF 65+ atau 1 juta EP."),
    50124: ("Kotak Perunggu", "ID50124GP", "ITEM_LOTTERY",
            {"prizes": [(25, 28006), (25, 28007), (25, 28008), (25, ("ep", 300_000))]},
            "Hadiah acak: pak FW/MF/DF 60+ atau 300.000 EP."),
    50125: ("Kotak Metal", "ID50125GP", "ITEM_LOTTERY",
            {"prizes": [(50, 82214356), (50, ("ep", 100_000))]}, "Hadiah acak: pak All '07 atau 100.000 EP."),
    200130000: ("Kotak Hadiah Perak", 200130000, "ITEM_LOTTERY",
                {"prizes": [(40, ("ep", 200_000)), (30, 28009), (30, 82214068)]},
                "Hadiah acak: 200.000 EP, pak GK 60+ atau World Best 300 2017."),
    200130001: ("Kotak Hadiah Emas", 200130001, "ITEM_LOTTERY",
                {"prizes": [(40, ("ep", 1_000_000)), (30, 45007), (30, 45013)]},
                "Hadiah acak: 1 juta EP, 06-11 Player Top 50 atau This Season Top 30."),
    3002: ("Ganti nama manager", 3002, "COACHNAME_CHANGE", {}, "Ganti nama manager Anda."),
    3001: ("Ganti tim", 3001, "TEAM_CHANGE", {}, "Pindah ke klub lain (logo, seragam, liga); pemain tetap."),
}
PACKAGES = {  # itemid: (name, icon, [(itemid, count)], description); bought = contents to Item saya
    801: ("Paket Starter", 801, [(28006, 1), (28007, 1), (28008, 1), (28009, 1), (50020, 2)],
          "Pak FW/MF/DF/GK 60+ dan 2x EP +500.000."),
    50880: ("Kotak Terima Kasih", "ID50880", [(50021, 1), (50122, 1), (501, 3)],
            "EP +1.000.000, Kotak Emas dan 3x Lucky EP."),
    50881: ("Kotak World Best", "ID50881", [(8334, 1), (82214180, 3), (82214068, 5)],
            "World Best 5, 3x World Best 50 2017, 5x World Best 300 2017."),
    50882: ("Kotak Legenda 06-11", "ID50882", [(45004, 1), (45006, 2), (45007, 3)],
            "06-11 Player Top 10, 2x Top 30, 3x Top 50."),
    118012902: ("Kotak Upgrade", 118012902, [(15031, 1), (7618, 1), (82232124, 1)],
                "'14 Top 30 +5, Europe '10 Top 30 +3, EC Top 30 +3."),
    80000001: ("VIP Premium", "vipterbaru", [(50028, 1), (50121, 1), (50021, 5)],
               "Lucky EP max 50 juta, Kotak Platinum, 5x EP +1.000.000."),
}
PACKAGES.update({  # Serial product steps (CHAINED below), bought in order
    70000001: ("Paket Berantai 1/3", 200130000, [(501, 3), (28007, 1)], "Tahap 1: 3x Lucky EP dan pak MF 60+."),
    70000002: ("Paket Berantai 2/3", 200130001, [(50122, 1), (50021, 1)], "Tahap 2: Kotak Emas dan EP +1.000.000."),
    70000003: ("Paket Berantai 3/3", "ID50121GP", [(50121, 1), (8334, 1)], "Tahap 3: Kotak Platinum dan World Best 5."),
})
MP_SHOP = (501, 50123, 50122, 45013, 45006, 3002)  # MP Shop (shopcategory MILEAGE) copies
MP_SHOPID = 900_000_000  # MP Shop entries: shopid = MP_SHOPID + itemid (productid stays itemid)
# Serial product (ShopChainedPackageTabPage, ShopController.currentChainedItemCheck): one entry
# {shopcategory CHAINED, step, chained: [later steps]}; the client shows step 0 = the entry
# itself, step k = chained[k-1]; remainedperaccount 0 marks a bought step ("complete").
CHAINED = {70000001: (70000001, 70000002, 70000003)}  # first step's itemid: all steps
# Special Bonus (ShopSpecialTabPage): GET /specifictarget {issell, targets:[{stid, endtime}]} and
# GET /shop/list/STSHOP = [entries with those stids] (ShopController keeps the targeted ones).
SPECIAL = (8335, 8194, 50841, 50881)
STSHOPID = 910_000_000
# Paket Bulanan (ShopMonthlyPackageTabPage: shopcategory MONTHLYPACK; ShopMonthlyPackageProxy):
# items now, then dailyitems every day for daily_period days; GET /monthlypack = my packages
# [{productid, product, expireat, rewardedat}], POST /monthlypack (the proxy, once a day while
# active) drops the day's items into the Bag as type "monthlypack".
MONTHLY = {  # itemid: (name, icon, days, items now, daily items, description)
    81000001: ("Paket Bulanan EP", "ID50840", 30, [(50021, 1)], [(EP_ITEM, 200_000)],
               "Langsung EP +1.000.000, lalu 200.000 EP tiap hari selama 30 hari."),
    81000002: ("Paket Bulanan Pak", 200130001, 30, [(8223601, 1)], [(28007, 1), (EP_ITEM, 50_000)],
               "Langsung pak MF 65+, lalu pak MF 60+ dan 50.000 EP tiap hari selama 30 hari."),
    81000003: ("Paket Bulanan Premium", "ID50841", 30, [(50121, 1)], [(45013, 1), (EP_ITEM, 500_000)],
               "Langsung Kotak Platinum, lalu This Season Top 30 dan 500.000 EP tiap hari selama 30 hari."),
}


def use_item_def(itemid):
    name, icon, typ, eff, desc = ITEMS[itemid]
    return {"itemid": itemid, "productid": itemid, "name": name, "category": "USE", "type": typ, "unit": "COUNT",
            "itemicon": icon, "bundleicon": icon, "currency": "ep", "stackable": True, "description": desc,
            # TeamManagementEPCardScreen.onQuery sizes the coin counter from count.max
            **({"count": {"min": eff["ep"][0], "max": eff["ep"][1]}} if "ep" in eff else {}),
            "bunches": [{"bunch": n, "price": 0} for n in PACK_BUNCHES]}


def package_def(itemid):
    if itemid in MONTHLY:
        name, icon, days, now, daily, desc = MONTHLY[itemid]
        contents, extra = now, {"daily_period": days, "monthly_platform": "all",
                                "dailyitems": [{"itemid": i, "count": n, "staticdata": item_def(i)} for i, n in daily]}
    else:
        name, icon, contents, desc = PACKAGES[itemid]
        extra = {}
    return {"itemid": itemid, "productid": itemid, "name": name, "category": "PACKAGE", "type": "ITEM",
            "unit": "COUNT", "itemicon": icon, "bundleicon": icon, "currency": "ep", "description": desc,
            "bunches": [{"bunch": 1, "price": 0}], **extra,
            "items": [{"itemid": i, "count": n, "staticdata": item_def(i)} for i, n in contents]}


SHOP_NEW = (45011, 45004, 50121, 50881, 501)  # Rekomendasi: "new" order
SHOP_BEST = (82214180, 8223600, 28001, 28003, 8223601)  # Rekomendasi: Terlaris rank


def shop_entry(itemid, category="PLAYERPACK", shopid=None):
    d = item_def(itemid)
    rank = {k: t.index(itemid) + 1 for k, t in (("new", SHOP_NEW), ("best", SHOP_BEST)) if itemid in t and not shopid}
    icon = d.get("bundleicon", itemid)
    items = d["items"] if category == "PACKAGE" else [{"itemid": itemid, "count": 1, "staticdata": d}]
    return {"_id": str(shopid or itemid), "shopid": shopid or itemid, "shopcategory": category, "forceenable": True,
            "visible": True, **rank, "salestart": "2014-01-01T00:00:00.000Z", "saleend": "2035-01-01T00:00:00.000Z",
            "product": {"itemid": itemid, "staticdata": {**d, "bundleicon": icon, "items": items}}}


CHAIN_STEPS = {i: (first, k) for first, steps in CHAINED.items() for k, i in enumerate(steps)}


def chained_entry(st, first):
    """A Serial product at my progress: step = steps bought (capped at the last)."""
    steps, done = CHAINED[first], my_coach(st).get("chain", {}).get(str(first), 0)
    parts = [dict(shop_entry(i, "CHAINED"), remainedperaccount=0 if k < done else 1, buy=k < done)
             for k, i in enumerate(steps)]
    return {**parts[0], "step": min(done, len(steps) - 1), "chained": parts[1:]}


def shop_list(st):
    """GET /shop/list: every Shop tab's products (ShopController filters them by shopcategory)."""
    return ([shop_entry(i) for i in PACKS] + [shop_entry(i, "USE") for i in ITEMS]
            + [shop_entry(i, "PACKAGE") for i in PACKAGES if i not in CHAIN_STEPS]
            + [shop_entry(i, "MILEAGE", MP_SHOPID + i) for i in MP_SHOP]
            + [shop_entry(i, "MONTHLYPACK") for i in MONTHLY]
            + [chained_entry(st, first) for first in CHAINED])


def product_category(itemid):
    return "PLAYERPACK" if itemid in PACKS else "USE" if itemid in ITEMS else "PACKAGE"


def ep_specifictarget(query, body):
    """GET /specifictarget: Special Bonus is open for me (ShopController.getSpecialShopInfo)."""
    return _json({"issell": True, "targets": [{"stid": STSHOPID + i, "endtime": "2035-01-01T00:00:00.000Z"}
                                              for i in SPECIAL]})


def ep_stshop(query, body):
    """GET /shop/list/STSHOP: the Special Bonus products."""
    return _json([dict(shop_entry(i, product_category(i), STSHOPID + i), stid=STSHOPID + i) for i in SPECIAL])


def monthly_list(st):
    """GET /monthlypack body: my active monthly packages."""
    return [{"_id": f"{i:024x}", "productid": i, "product": {"itemid": i, "staticdata": item_def(i)},
             "purchasedat": w3c(m["since"]), "expireat": w3c(m["expire"]), "rewardedat": w3c(m["rewarded"])}
            for i, m in ((int(k), v) for k, v in my_coach(st).get("monthly", {}).items()) if m["expire"] > time.time()]


def ep_monthlypack(query, body):
    """GET /monthlypack: my packages; POST: today's daily items of each (once a day) to the Bag."""
    st = _state()
    if body:  # POST (ShopMonthlyPackageProxy.requestReward)
        today = today_wib()
        for k, m in my_coach(st).get("monthly", {}).items():
            if m["expire"] > time.time() and today_wib(m["rewarded"]) != today:
                name, _, _, _, daily, _ = MONTHLY[int(k)]
                e = bag_add(st, f"{name} - hadiah harian", [{"itemid": i, "count": n} for i, n in daily],
                            msg=f"Hadiah harian {name}.")
                e["type"] = "monthlypack"
                m["rewarded"] = time.time()
        _save(st)
        return _json({"result": True, "resources": [{"uri": "/monthlypack", "body": monthly_list(st)},
                                                    {"uri": "/bag", "body": bag_list(st)}]})
    return _json(monthly_list(st))


def inventory_add(coach, itemid, count):
    inv = coach.setdefault("items", [])
    it = next((i for i in inv if i["itemid"] == itemid), None)
    if it is None:
        it = {"_id": new_iid(), "itemid": itemid, "remains": 0}
        inv.append(it)
    it["remains"] += count
    return it


def my_coach(st):
    return next(c for c in st["coaches"] if c["slot"] == _default_slot(st))


def coach_items(st, slot):
    """/coach/me items: one stack per pack ({_id, itemid, remains}); expireable marks an owned
    item (GeneralInventoryTileItemRenderer: Gunakan button instead of Beli)."""
    coach = next(c for c in st["coaches"] if c["slot"] == slot)
    return [{"_id": i["_id"], "id": i["_id"], "itemid": i["itemid"], "remains": i["remains"], "count": i["remains"],
             "expireable": True, "expired": False, "staticdata": item_def(i["itemid"])}
            for i in coach.get("items", []) if i["itemid"] in PACKS or i["itemid"] in ITEMS]


def ep_shop_purchase(query, body):
    """POST /shop/purchase {shopid, productid, bunch: price option index}: pay EP; packs and
    items go to Item saya, a package's contents too."""
    req = json.loads(body or b"{}")
    itemid = int(req.get("productid") or 0) or int(req.get("shopid") or 0) % MP_SHOPID % STSHOPID
    if not any(itemid in t for t in (PACKS, ITEMS, PACKAGES, MONTHLY)):
        return _json({"result": False, "reason": "NOT_EXIST_PRODUCT"})
    bunches = item_def(itemid)["bunches"]
    b = bunches[min(max(int(req.get("bunch") or 0), 0), len(bunches) - 1)]
    st = _state(); slot = _default_slot(st); coach = my_coach(st)
    if coach_me(st, slot)["money"]["ep"] < b["price"]:
        return _json({"result": False, "reason": "NOT_ENOUGH_EP"})
    add_ep(st, -b["price"])
    contents = PACKAGES[itemid][2] if itemid in PACKAGES else MONTHLY[itemid][3] if itemid in MONTHLY else [(itemid, 1)]
    for i, n in contents:
        if i == EP_ITEM:
            add_ep(st, n * b["bunch"])
        else:
            inventory_add(coach, i, n * b["bunch"])
    if itemid in MONTHLY:  # a month of daily items (renewing extends it)
        m = coach.setdefault("monthly", {}).setdefault(str(itemid), {"since": time.time(), "expire": time.time(), "rewarded": 0})
        m["expire"] = max(m["expire"], time.time()) + MONTHLY[itemid][2] * 86400
    if itemid in CHAIN_STEPS:
        first, k = CHAIN_STEPS[itemid]
        chain = coach.setdefault("chain", {})
        chain[str(first)] = max(chain.get(str(first), 0), k + 1)
    _save(st)
    return _json({"result": True, "resources": coach_me_resources(st, slot)
                  + ([{"uri": "/monthlypack", "body": monthly_list(st)}] if itemid in MONTHLY else [])})


def use_item(st, slot, coach, it, req):
    """/item/<_id>/use for a USE item (see ITEMS): its effect, one used up."""
    _, _, typ, eff, _ = ITEMS[it["itemid"]]
    out = {"result": True}
    if typ == "EP_LOTTERY":  # lower amounts more likely
        lo, hi = eff["ep"]
        out["ep"] = lo if lo == hi else max(lo, round(random.triangular(lo, hi, lo), -4))
        add_ep(st, out["ep"])
    elif typ == "ITEM_LOTTERY":
        prize = random.choices([p for _, p in eff["prizes"]], [w for w, _ in eff["prizes"]])[0]
        if isinstance(prize, tuple):
            out["ep"] = prize[1]
            add_ep(st, prize[1])
        else:
            inventory_add(coach, prize, 1)
            out["item"] = {"itemid": prize, "count": 1, "staticdata": item_def(prize)}
    elif typ == "COACHNAME_CHANGE":
        name = str(req.get("name") or "").strip()
        if not 2 <= len(name) <= 12:
            return {"result": False, "reason": "COACH_CREATION_ERROR_COACH_TOO_SHORT"}
        coach["name"] = name
    elif typ == "TEAM_CHANGE":
        teamid = int(req.get("teamid") or 0)
        if not roster(teamid)["team"]["players"]:
            return {"result": False, "reason": "ITEM_USE_ERROR"}
        coach["teamid"] = teamid
    it["remains"] -= 1
    if not it["remains"]:
        coach["items"].remove(it)
    return out


def ep_item_use(item_id, query, body):
    """POST /item/<_id>/use: open one pack -> {result, player: {spid, grade, price}}; USE items
    see use_item."""
    st = _state(); slot = _default_slot(st); coach = my_coach(st); team = st["teams"][str(slot)]
    it = next((i for i in coach.get("items", []) if i["_id"] == item_id and i["remains"] > 0), None)
    if it is not None and it["itemid"] in ITEMS:
        out = use_item(st, slot, coach, it, json.loads(body or b"{}"))
        if out["result"]:
            _save(st)
        return _json({**out, "resources": coach_me_resources(st, slot)})
    if it is None or it["itemid"] not in PACKS:
        return _json({"result": False, "reason": "INVALID_ITEM"})
    if len(team["players"]) >= MAX_PLAYERS:
        return _json({"result": False, "reason": "Exceed plyer limit"})  # client shows "squad full"
    pool = pack_pool(it["itemid"])
    spid = random.choices([s for s, _ in pool], [w for _, w in pool])[0]
    g = pack_grade(it["itemid"])
    p = add_player(team, spid, g)
    it["remains"] -= 1
    if not it["remains"]:
        coach["items"].remove(it)
    _save(st)
    return _json({"result": True, "player": {"_id": p["_id"], "spid": spid, "grade": g, "price": card_price(spid, g)},
                  "resources": coach_me_resources(st, slot)})


def ep_static_item(query, body):
    """POST /static/cache/item {itemids} -> {itemid: staticdata} (ItemUtil cache)."""
    ids = []
    for i in json.loads(body or b"{}").get("itemids") or []:
        try:
            ids.append(int(i))
        except (TypeError, ValueError):
            pass
    return _json({str(i): EP_ITEM_DEF if i == EP_ITEM else item_def(i) for i in ids
                  if i == EP_ITEM or i in PACKS or i in ITEMS or i in PACKAGES})


# --------------------------------------------------------------------------- #
# Daily login reward: the first login of a day (WIB, at PUT /coach/<slot>/default) drops that
# day's reward into the Bag (Item saya > Items, ItemBagTabPage: GET /bag; "Terima" = POST
# /bag/<id>/unpack, "Terima semua" = POST /bag/unpackall {reqUnpackItems}). 7-day cycle on a
# consecutive-login streak; the table is gamedata/daily_rewards.json (admin page edits it).
# Bag entries are event packs (ItemUtil.isEventItempackData: no product, type "event",
# items [{itemid, count}] expanded via /static/cache/item, players [{spid, grade}]).
# --------------------------------------------------------------------------- #
DAILY_FILE = os.path.join(HERE, "gamedata", "daily_rewards.json")
DAILY_DEFAULT = [{"ep": 10000}, {"ep": 20000}, {"pack": 28007}, {"ep": 30000}, {"pack": 28006},
                 {"ep": 50000}, {"ep": 100000, "pack": 82214068}]  # day 1..7
WIB = 7 * 3600


def daily_table():
    try:
        with open(DAILY_FILE, encoding="utf-8") as f:
            return json.load(f) or DAILY_DEFAULT
    except (OSError, ValueError):
        return DAILY_DEFAULT


def today_wib(ts=None):
    return time.strftime("%Y-%m-%d", time.gmtime((time.time() if ts is None else ts) + WIB))


def reward_items(reward):
    return ([{"itemid": EP_ITEM, "count": int(reward["ep"])}] if reward.get("ep") else []) + \
           ([{"itemid": int(reward["pack"]), "count": int(reward.get("count") or 1)}] if reward.get("pack") else [])


def bag_add(st, title, items=(), players=(), msg=""):
    entry = {"_id": new_iid(), "title": title, "msg": msg, "items": list(items), "players": list(players),
             "createdat": int(time.time())}
    my_coach(st).setdefault("bag", []).append(entry)
    return entry


def daily_login(st, now=None):
    """Grant today's reward once per WIB day; returns the new bag entry or None."""
    now = time.time() if now is None else now
    d = my_coach(st).setdefault("daily", {"last": "", "streak": 0, "total": 0})
    if d["last"] == today_wib(now):
        return None
    d["streak"] = d["streak"] + 1 if d["last"] == today_wib(now - 86400) else 1
    d["total"] += 1
    d["last"] = today_wib(now)
    table, n = daily_table(), d["streak"]
    return bag_add(st, f"Hadiah login harian - hari ke-{n}", reward_items(table[(n - 1) % len(table)]),
                   msg=f"Login {n} hari berturut-turut. Login lagi besok untuk hadiah hari ke-{n + 1}.")


def bag_list(st):
    out = []
    for e in my_coach(st).get("bag", []):
        icon = next((PACKS.get(i["itemid"], ("", {}))[1].get("icon", i["itemid"])
                     for i in e["items"] if i["itemid"] != EP_ITEM), EP_ITEM)
        # ShopItemDescription.MakeEventItemDesc tooltip: msg = text, date (W3C) = "Tanggal diterima"
        out.append({"_id": e["_id"], "id": e["_id"], "type": e.get("type", "event"), "title": e["title"],
                    "msg": e.get("msg") or "Hadiah dari server.", "date": w3c(e["createdat"]),
                    "items": e["items"], "players": e["players"], "bundleicon": icon})
    return out


def bag_apply(st, entry):
    """Credit a bag entry: EP, packs and items to Item saya, players to the club."""
    coach = my_coach(st); team = st["teams"][str(_default_slot(st))]
    for it in entry["items"]:
        if it["itemid"] == EP_ITEM:
            add_ep(st, it["count"])
        elif it["itemid"] in PACKS or it["itemid"] in ITEMS:
            inventory_add(coach, it["itemid"], it["count"])
    for p in entry["players"]:
        add_player(team, p["spid"], p.get("grade", 1))
    coach["bag"].remove(entry)


def bag_reply(st):
    return _json({"result": True, "overdueitems": [],
                  "resources": coach_me_resources(st, _default_slot(st)) + [{"uri": "/bag", "body": bag_list(st)}]})


def ep_bag_unpack(bid, query, body):
    """POST /bag/<id>/unpack: take one bag entry."""
    st = _state()
    entry = next((e for e in my_coach(st).get("bag", []) if e["_id"] == bid), None)
    if entry is None:
        return _json({"result": False, "reason": "ITEM_USE_ERROR"})
    if len(st["teams"][str(_default_slot(st))]["players"]) + len(entry["players"]) > MAX_PLAYERS:
        return _json({"result": False, "reason": "TRANSFERMARKET_FULLSLOT_DESC"})
    bag_apply(st, entry)
    _save(st)
    return bag_reply(st)


def ep_bag_unpackall(query, body):
    """POST /bag/unpackall {reqUnpackItems: [id...]}: take them all (club slots permitting)."""
    ids = set(map(str, json.loads(body or b"{}").get("reqUnpackItems") or []))
    st = _state(); team = st["teams"][str(_default_slot(st))]
    for e in [e for e in my_coach(st).get("bag", []) if e["_id"] in ids]:
        if len(team["players"]) + len(e["players"]) <= MAX_PLAYERS:
            bag_apply(st, e)
    _save(st)
    return bag_reply(st)


# Path-pattern routes (ids in the path); checked after the exact ENDPOINTS.
ROUTES = [
    (re.compile(r"/item/(\w+)/use"), ep_item_use),
    (re.compile(r"/bag/(\w+)/unpack"), ep_bag_unpack),
    (re.compile(r"/coach/(\w+)/postponed"), ep_coach_postponed),
    (re.compile(r"/static/info/authentic/team/(licensed|all)/(\d+)"), ep_authentic_team),
    # Ganti tim screen: a club's summary for its TeamInfoNew (name/crest id, ovr, teamgrade)
    (re.compile(r"/static/info/team/summary/(\d+)"), lambda t, q, b: _json({**roster(int(t))["summary"], "id": int(t)})),
    (re.compile(r"/player/(\w+)/move/(firstteam|secondteam)"), ep_player_move),
    (re.compile(r"/player/(\w+)/enhance/(\w+)/preview"), ep_enhance_preview),
    (re.compile(r"/player/(\w+)/enhance/(\w+)(?:/(-?\w+)/(-?\w+))?"), ep_enhance),
    (re.compile(r"/transfer/top/50(?:/position/(\w+))?"), ep_transfer_top),
    (re.compile(r"/transfer/search/(\d+)"), ep_transfer_search),
    (re.compile(r"/transfer/purchase/(\d+)/(\d+)/(\d+)/purchase"), ep_transfer_purchase),
    (re.compile(r"/transfer/price/(\d+)/(\d+)"), ep_transfer_price),
    (re.compile(r"/transfer/my/offer/completed/(\w+)(?:/(\w+))?"), ep_offer_collect),
    (re.compile(r"/match/([0-9a-f]+)/player/ready"), ep_match_player_ready),
    (re.compile(r"/match/([0-9a-f]+)/side/(\w+)/ready"), ep_match_ready),
    (re.compile(r"/match/([0-9a-f]+)/end"), ep_match_end),
    (re.compile(r"/match/([0-9a-f]{24})"), ep_match_get),
    (re.compile(r"/league2/sponsor/(\d+)"), ep_l2_sponsor),
    (re.compile(r"/cup/([0-9a-f]{24})/match"), ep_cup_match),
    (re.compile(r"/cup/([0-9a-f]{24})/next"), ep_cup_next),
    (re.compile(r"/cup/([0-9a-f]{24})/(?:resume|test)"), ep_cup_resume),
    (re.compile(r"/cup/([0-9a-f]{24})/leave"), ep_cup_leave),
    (re.compile(r"/cup/([0-9a-f]{24})"), lambda cupid, q, b: _json((_state().get("cups") or {}).get(cupid, {}))),
]


def route(path):
    for pat, fn in ROUTES:
        m = pat.fullmatch(path)
        if m:
            return lambda q, b, g=m.groups(): fn(*g, q, b)


ENDPOINTS = {
    "/info/server/config": ep_server_config,
    "/info/server/locale": ep_locale,
    "/info/server/time": ep_server_time,
    "/game/my/ipaddress": ep_my_ip,
    "/publisher/garena/sign": ep_garena_sign,
    "/publisher/nexon/2ndauth": ep_nexon_2ndauth,
    "/telemetry/gameconnection": ep_telemetry,
    "/shop/cash/charge/url": ep_shop_url,
    "/shop/cash/history/url": ep_shop_url,
}
ENDPOINTS["/coaches"] = ep_coaches
ENDPOINTS["/coach/me"] = ep_coach_me
ENDPOINTS["/coach/me/onready"] = ep_coach_onready
for _slot in (0, 1):  # two coach slots
    ENDPOINTS[f"/coach/{_slot}"] = lambda q, b, s=_slot: ep_create_coach(s, q, b)
    ENDPOINTS[f"/coach/{_slot}/new/step/3"] = lambda q, b, s=_slot: ep_new_team(s, q, b)
    ENDPOINTS[f"/coach/{_slot}/default"] = lambda q, b, s=_slot: ep_default_coach(s, q, b)
ENDPOINTS["/account"] = ep_account
ENDPOINTS["/account/option"] = ep_account_option
ENDPOINTS["/coach/me/squad"] = ep_coach_squad
ENDPOINTS["/match/campaign"] = ep_match_campaign
ENDPOINTS["/game/my/tactic/all"] = ep_tactic_all
for _i in range(TACTIC_SLOTS):
    ENDPOINTS[f"/game/my/tactic/{_i}"] = lambda q, b, i=_i: ep_tactic(i, q, b)
ENDPOINTS.update({
    "/league2/me": ep_l2_me, "/league2/reward": ep_l2_reward, "/league2/calendar": ep_l2_calendar,
    "/league2/match": ep_l2_match, "/league2/match/sim": lambda q, b: ep_l2_match(q, b, sim=True),
    "/league2/sleep": ep_l2_sleep, "/league2/end": ep_l2_end, "/league2/abandon": ep_l2_finish,
    "/league2/back": ep_telemetry, "/static/db/SORTTEAM": ep_sortteam,
    "/static/db/SORTNATION": ep_sortnation, "/static/db/SORTLEAGUE": ep_sortleague,
    "/league2/sponsor/achievement": ep_l2_achievement,
    "/cup/list": ep_cup_list, "/cup/record": ep_cup_record, "/cup/new": ep_cup_new,
    "/transfer/offer": ep_transfer_offer, "/transfer/my/offer/list": lambda q, b: _json(offer_list(_state())),
    "/transfer/my/boostfactor": lambda q, b: _json(NO_BOOST),
    "/players/trade/proposal": ep_trade_proposal, "/players/trade/contract": ep_trade_contract,
    "/trade3/contract": ep_trade_contract, "/trade3/proposal": ep_trade3_proposal,
    "/trade3/staticvalues": lambda q, b: _json(TRADE3_STATIC), "/trade3/info": ep_trade3_info,
    "/trade3/notification": lambda q, b: _json({"result": True}),
    "/static/db/PLAYERTRADE": lambda q, b: _json({"playergrade": {str(s): trade_grades(s) for s in range(2, 101)}}),
    "/static/db/EXPPLAYER": lambda q, b: _json(EXPPLAYER), "/static/db/EXPCOACH": lambda q, b: _json(EXPCOACH),
    "/static/db/STATICVALUES/max_coachlevel": lambda q, b: _json(COACH_MAX_LEVEL),
    "/shop/list": lambda q, b: _json(shop_list(_state())), "/shop/list/STSHOP": ep_stshop,
    "/specifictarget": ep_specifictarget, "/monthlypack": ep_monthlypack,
    "/shop/purchase": ep_shop_purchase, "/static/cache/item": ep_static_item,
    "/bag": lambda q, b: _json(bag_list(_state())), "/bag/unpackall": ep_bag_unpackall,
    "/players/trade/proposal/resend": lambda q, b: _json({"result": False, "reason": "NOT_SUPPORTED_REQUEST"}),
})


RESPONSES = os.path.join(HERE, "responses")


def from_file(path):
    """responses/<path, '/' -> '_'>.json overrides the handler; re-read every request so
    response shapes can be iterated against the live client without a restart."""
    f = os.path.join(RESPONSES, path.strip("/").replace("/", "_") + ".json")
    if os.path.isfile(f):
        with open(f, "rb") as fh:
            return ("application/json", fh.read())


class H(http.server.BaseHTTPRequestHandler):
    def _log(self, body):
        line = (f"{time.strftime('%H:%M:%S')} {self.command} {self.path} "
                f"from {self.client_address[0]} host={self.headers.get('Host')} "
                f"len={len(body)} body={body[:2000]!r}")  # UI error stack traces run long
        print(line, flush=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _respond(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if body[:2] == b"\x1f\x8b":  # the client gzips larger bodies (telemetry, /coach/me/squad)
            body = gzip.decompress(body)
        self._log(body)
        path = self.path.split("?")[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        # Unknown GETs get {}: a wrong shape in AS3 only raises a logged UI error, but []
        # where native code expects an object crashes the client (seen with
        # /static/db/PLAYER_EXTRA_GROWTH), and tables are id->row maps whose values get
        # iterated, so a stray "result": true becomes a bogus row. List endpoints get a
        # responses/ file with []. Unknown POSTs get {"result": true}.
        fallback = ep_telemetry if self.command == "POST" else ep_empty
        if path.startswith("/" + CDN_PREFIX):
            hit = cdn_file(path[len(CDN_PREFIX) + 1:])
            if hit is None:
                self.send_error(404)
                return
            ctype, data = hit
        else:
            try:
                with STATE_LOCK:
                    ctype, data = from_file(path) or (ENDPOINTS.get(path) or route(path) or fallback)(query, body)
            except Exception:
                # an empty reply is a curl error and the client quits (CLIENT_CLOSE); answer
                # something parseable and keep the traceback in the log instead
                import traceback
                self._log(traceback.format_exc().encode())
                ctype, data = _json({"result": False})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _respond
    do_POST = _respond
    do_PUT = _respond  # /game/my/ipaddress is a real PUT; 501 there made the client quit
    do_DELETE = _respond

    def log_message(self, *a):
        pass  # we do our own logging


if __name__ == "__main__":
    # Windows SO_REUSEADDR lets a second copy bind the same port and split the client's
    # requests between two servers; refuse instead (a second play.bat then just fails)
    http.server.ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), H)
    if TLS:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT)
        # Lazy handshake: a failed handshake (e.g. client rejects the cert) then
        # surfaces as a logged traceback in the handler thread instead of vanishing.
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    print(f"http config stub on 0.0.0.0:{PORT}{' (https)' if TLS else ''}, log -> {LOG}", flush=True)
    admin_port = int(os.environ.get("FO3_ADMIN_PORT", "8088"))  # 0 = no admin page
    if admin_port:
        import admin
        admin.start(sys.modules[__name__], admin_port)
        print(f"admin page on http://127.0.0.1:{admin_port}/", flush=True)
    threading.Thread(target=warm_packs, daemon=True, name="packs").start()
    srv.serve_forever()
