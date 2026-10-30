"""Build the starter-team responses from the client's own player DB (extract_fifadb.py).

StartupQuickCreationScreen reads /static/info/authentic/team/recommendation as an ARRAY of
    {teamid, summary:{numofselectableplayers}, team:{squad:{positions,captain,kickers}, players}}
and calls PlayerListUtil.createPlayerList(item.team, false). Players are sent as
{"spid", "expandme": "player"}: ResourceController.expandResource collects every object with
`expandme` and builds `staticdata` from the client's own DB (its `stats` is an Array with named
keys, which JSON cannot express). squad.positions = [{spid, pos}] with pos an index into
Enums.PLAYERPOSITION; players not in positions become reserves. The detail button reads
/static/info/authentic/team/all/<teamid> as {summary, team}. Rosters are the real teamid
links in fifadb (base cards preferred).

Usage: python gen_teams.py [teamid ...]   (default: clubs whose crest is in the game cache)
"""
import json, os, sqlite3, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "gamedata", "fifadb.sqlite")
OUT = os.path.join(HERE, "responses")
CRESTS = r"D:\FO3ID\32795\_cache\games\fo3id\externalAssets\crests\large"
DEFAULT_TEAMS = [241, 243, 1, 9, 45, 22]  # Barcelona, Real Madrid, Arsenal, Liverpool, Juventus, Dortmund

# 4-4-2: (Enums.PLAYERPOSITION index, pitch x, y). x/y are FIFA-style formation offsets (0..1,
# y from own goal); SquadScreenHandler reads them from squad.positions[i].starting[2..3] and
# team.formations[].offsets.
FORMATION = [
    (0, 0.5, 0.02),                                                                    # GK
    (3, 0.85, 0.22), (4, 0.62, 0.16), (6, 0.38, 0.16), (7, 0.15, 0.22),                # RB RCB LCB LB
    (12, 0.85, 0.5), (13, 0.62, 0.46), (15, 0.38, 0.46), (16, 0.15, 0.5),              # RM RCM LCM LM
    (24, 0.62, 0.8), (26, 0.38, 0.8),                                                  # RS LS
]
LINES = {"DF": range(1, 9), "MF": range(9, 20), "FW": range(20, 28)}  # Enums.PLAYERPOSITION
# preferredposition1 values that are natural for each slot (keeps LB/RB, LM/RM on their side)
FITS = {0: {0}, 3: {3, 2}, 4: {4, 5, 1}, 6: {6, 5, 1}, 7: {7, 8}, 12: {12, 23}, 13: {13, 14, 10, 9, 11, 18},
        15: {15, 14, 10, 11, 9, 18}, 16: {16, 27}, 24: {24, 25, 21, 20, 22, 23}, 26: {26, 25, 21, 22, 20, 27}}
FIT_BONUS = 4  # a natural player keeps the slot unless an out-of-position one rates 4+ higher
FORMATION_ID = 1
# starting = [attacking participation, defensive participation, x, y]; 1 = medium
PARTICIPATION = 1
SELECTABLE = 18
# summary text the client shows as-is (TeamStaticInfo: name, nation.name, rival.name); team and
# nation names are not in fifadb. league/nation ids come from the roster.
TEAM_INFO = {241: ("FC Barcelona", "Real Madrid"), 243: ("Real Madrid", "FC Barcelona"),
             1: ("Arsenal", "Tottenham Hotspur"), 9: ("Liverpool", "Everton"),
             45: ("Juventus", "Inter"), 22: ("Borussia Dortmund", "FC Schalke 04")}
NATION_NAMES = {45: "Spanyol", 14: "Inggris", 27: "Italia", 21: "Jerman"}
# shirt numbers per formation slot (MenuScreen kicker data reads player.jersey; 0 = no number)
NUMBERS = {0: 1, 3: 2, 4: 4, 6: 5, 7: 3, 12: 7, 13: 8, 15: 6, 16: 11, 24: 9, 26: 10}


def jersey_name(c, playerid):
    r = c.execute("SELECT jersey FROM namesind WHERE playerid = ?", (playerid,)).fetchone()
    return r[0] if r else ""


CURRENT_SEASON = 88  # spid // 1e6 of the "2017" cards (fifaaddict season badge y2088)


def team_grade(avg):
    """summary.teamgrade = "Rating klub" stars, 0..5 in halves (TeamInfoNew.setTeamGrade), from
    the XI's average client rating (2017 cards): 76 = 5 stars (Real Madrid), -0.5 per 1.75."""
    return max(0.5, min(5.0, round((avg - 59) / 1.75) / 2))


def _hc():
    """http_config (the client's ratings, ROLE_WEIGHTS), also while it runs as the server's
    __main__ (a plain import there would load a second copy)."""
    m = sys.modules.get("__main__")
    return m if hasattr(m, "ROLE_WEIGHTS") else __import__("http_config")


def team(c, teamid):
    hc = _hc()
    rows = c.execute("""SELECT p.* FROM players p JOIN permid m ON m.spid = p.spid AND m.licensed = 1
                        WHERE p.teamid = ? ORDER BY (p.spid / 1000000 != ?), (p.spid < 1000000), p.spid""",
                     (teamid, CURRENT_SEASON)).fetchall()
    best = {}
    # the club's current-season cards (as on fifaaddict); older seasons (former players, e.g.
    # a 2006 Ronaldinho at Barcelona) and season 0 (engine template cards, ratings 120+, not on
    # fifaaddict) only when the current squad is too small
    for r in rows:
        if r["spid"] // 1000000 == CURRENT_SEASON or len(best) < SELECTABLE:
            best.setdefault(r["playerid"], r)
    rows = sorted(best.values(), key=lambda r: -r["overallrating"])[:30]
    if len(rows) < 11:
        return None
    # XI: best (player, slot) pairs by the client's rating at that slot (the "Attr" the squad
    # screens show); goalkeepers only in goal. A slot left over takes the best remaining player.
    at = lambda r, pos: sum(r[k] * w for k, w in hc.ROLE_WEIGHTS[hc.POS_ROLE[pos]].items()) // 100
    pick = {}
    for _, i, pos in sorted(((at(r, pos) + FIT_BONUS * (r["preferredposition1"] in FITS[pos]), i, pos)
                             for i, r in enumerate(rows) for pos, _, _ in FORMATION
                             if (pos == 0) == (r["preferredposition1"] == 0)), reverse=True):
        if pos not in pick and i not in pick.values():
            pick[pos] = i
    for pos, _, _ in FORMATION:
        if pos not in pick:
            pick[pos] = max((i for i in range(len(rows)) if i not in pick.values()), key=lambda i: at(rows[i], pos))
    positions = [{"spid": rows[pick[pos]]["spid"], "pos": pos, "starting": [PARTICIPATION, PARTICIPATION, x, y]}
                 for pos, x, y in FORMATION]
    numbers = {p["spid"]: NUMBERS[p["pos"]] for p in positions}
    subs = iter(range(12, 99))
    players = [{"spid": r["spid"], "expandme": "player", "price": r["overallrating"],
                "jersey": numbers.get(r["spid"]) or next(subs), "jerseyname": jersey_name(c, r["playerid"])}
               for r in rows]
    # kickers is an Array indexed by Enums.KICKER_TYPES (leftcorner, rightcorner, freekick,
    # penalty, freekick2); an Object here fails "Type Coercion ... to Array"
    starters = [p["spid"] for p in positions if p["pos"]]
    squad = {"positions": positions, "captain": rows[0]["spid"], "kickers": starters[-5:], "formation": FORMATION_ID}
    formations = [{"formationid": FORMATION_ID, "formationname": "4-4-2",
                   "positions": [f[0] for f in FORMATION], "offsets": [{"x": f[1], "y": f[2]} for f in FORMATION]}]
    league = max(set(r["leagueid"] for r in rows), key=[r["leagueid"] for r in rows].count)
    nation = max(set(r["nationality"] for r in rows), key=[r["nationality"] for r in rows].count)
    name, rival = TEAM_INFO.get(teamid, ("", ""))
    # TeamInfoNew (club confirmation popup): TEAMNAME + crest by id, ovr.FW/MF/DF bars and
    # teamgrade stars; the same numbers as the lobby HUD (http_config.team_summary: ratings
    # at each player's own position)
    ovr = {p["spid"]: hc.card_ovr(p["spid"]) for p in positions}
    line = {k: round(sum(ovr[p["spid"]] for p in positions if p["pos"] in r) / sum(p["pos"] in r for p in positions))
            for k, r in LINES.items()}
    summary = {"teamid": teamid, "id": teamid, "numofselectableplayers": SELECTABLE, "name": name,
               "league": {"id": league}, "nation": {"id": nation, "name": NATION_NAMES.get(nation, "")},
               "rival": {"name": rival}, "ovr": line, "teamgrade": team_grade(sum(ovr.values()) / len(ovr))}
    return {"teamid": teamid, "summary": summary,
            "team": {"teamid": teamid, "squad": squad, "players": players, "formations": formations}}


def main():
    ids = [int(a) for a in sys.argv[1:]] or [t for t in DEFAULT_TEAMS
                                             if os.path.exists(os.path.join(CRESTS, f"s{t}.png"))]
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    items = [t for t in (team(c, i) for i in ids) if t]
    for t in items:
        with open(os.path.join(OUT, f"static_info_authentic_team_all_{t['teamid']}.json"), "w") as f:
            json.dump({"summary": t["summary"], "team": t["team"]}, f)
        print(t["teamid"], len(t["team"]["players"]), "players, captain", t["team"]["squad"]["captain"])
    with open(os.path.join(OUT, "static_info_authentic_team_recommendation.json"), "w") as f:
        json.dump(items, f)


if __name__ == "__main__":
    main()
