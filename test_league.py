"""Self-check for the Single > Liga server logic in http_config.py (no client needed).

    python test_league.py

Runs a whole season through the handlers against a temporary copy of gamedata/state.json.
"""
import json, os, shutil, sys, tempfile, time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import http_config as hc


def body(resp):
    return json.loads(resp[1])


def main():
    # schedule: every club once per round, every pair twice, once at each ground
    teams = list(range(1, 11))
    rounds = [hc.league_round(teams, r) for r in range(2 * (len(teams) - 1))]
    for pairs in rounds:
        assert sorted(t for p in pairs for t in p) == teams, pairs
    meet = Counter(frozenset(p) for pairs in rounds for p in pairs)
    assert set(meet.values()) == {2} and len(meet) == 45, meet
    homes = Counter(p for pairs in rounds for p in pairs)
    assert set(homes.values()) == {1}, [p for p, n in homes.items() if n != 1]

    tmp = tempfile.mkdtemp()
    try:
        hc.STATE = os.path.join(tmp, "state.json")
        # a fresh account through the client's club creation (the live state may hold no manager)
        hc._save({"coaches": [], "defaultcoachid": "", "teams": {}})
        with open(os.path.join(hc.RESPONSES, "static_info_authentic_team_all_241.json")) as f:
            spids = [p["spid"] for p in json.load(f)["team"]["players"]]
        assert body(hc.ENDPOINTS["/coach/0/new/step/3"]("", json.dumps({"id": 241, "spids": spids}).encode()))["result"]
        assert body(hc.ENDPOINTS["/coach/0"]("", json.dumps({"id": 241, "coachName": "Tester"}).encode()))["result"]
        assert body(hc.ENDPOINTS["/coach/0/default"]("", b""))["result"]
        shown = hc.coach_me(hc._state(), 0)["team"]  # the client saves the squad it shows (11 + 7 subs)
        hc.ENDPOINTS["/coach/me/squad"]("", json.dumps({k: shown[k] for k in ("squad", "kickers", "captain")}).encode())
        st = hc._state()
        assert len(st["teams"]["0"]["squad"]["positions"]) == 18
        me = hc.my_teamid(st)
        # a new club's cards are +0: no market value, not sellable
        assert all(p["grade"] == 0 == p["price"] for p in hc.coach_me(st, 0)["team"]["players"])
        # levels start at 1; exp tables: player LV.1-23 (24 rows), coach 1-100
        assert hc.coach_me(st, 0)["levelinfo"] == {"level": 1, "exp": 0, "to": hc.coach_need(1)}
        assert len(body(hc.ENDPOINTS["/static/db/EXPPLAYER"]("", b""))) == hc.PLAYER_MAX_LEVEL + 1
        assert body(hc.ENDPOINTS["/static/db/STATICVALUES/max_coachlevel"]("", b"")) == hc.COACH_MAX_LEVEL
        # Tim > Formasi: the club's own formation first (Barcelona has none: 4-4-2(1)), then the
        # 33 common ones, each with 11 positions + offsets (needs the client's data_0.big)
        forms = hc.coach_me(st, 0)["team"]["formations"]
        if hc.formation_table()["common"]:
            assert len(forms) == 33 and forms[0]["formationname"] == "4-4-2(1)"
            assert all(len(f["positions"]) == 11 == len(f["offsets"]) and f["positions"][0] == 0 for f in forms)
            assert hc.coach_me(st, 0)["team"]["squad"]["formation"] == forms[0]["formationid"]
        res = next(p for p in st["teams"]["0"]["players"] if not hc.in_squad(st["teams"]["0"], p))
        assert not body(hc.ENDPOINTS["/transfer/offer"]("", json.dumps({"playerobjectid": hc.iid(res), "price": 1000}).encode()))["result"]
        # club picker ("Tim lain"): nation -> league -> club lists agree; a club without a starter
        # file (Man City) gets its squad built on the fly; starter files carry name id + stars
        nations, leagues = (body(hc.ENDPOINTS[f"/static/db/{t}"]("", b"")) for t in ("SORTNATION", "SORTLEAGUE"))
        clubs = body(hc.ENDPOINTS["/static/db/SORTTEAM"]("", b""))
        assert nations and all(clubs[str(lg)] for n in nations for lg in leagues[str(n)])
        city = body(hc.route("/static/info/authentic/team/licensed/10")("", b""))
        assert city["teamid"] == 10 == city["summary"]["id"] and len(city["team"]["squad"]["positions"]) == 11
        assert city["team"]["captain"] in {p["id"] for p in city["team"]["players"]}
        assert hc.roster(243)["summary"]["teamgrade"] == 5.0 and hc.roster(243)["summary"]["id"] == 243

        assert "league" not in body(hc.ep_l2_me("", b""))  # -> creation screen
        offer = body(hc.route("/league2/sponsor/12")("", b"{}"))
        assert offer["result"] and len(offer["sponsors"]) == 3
        created = body(hc.ep_l2_calendar("", json.dumps({"difficulty": 12, "sponsor": "1"}).encode()))
        league = created["resources"][0]["body"]["league"]
        assert league["teams"][0] == me and len(league["teams"]) == hc.LEAGUE_SIZE

        for rnd in range(league["maxround"] + 1):
            home, away = next(p for p in hc.league_round(league["teams"], rnd) if me in p)
            req = json.dumps({"matchtype": "league", "playtype": "nrm", "round": rnd, "home": home, "away": away}).encode()
            if rnd % 2:  # simulate
                r = body(hc.ep_l2_match("", req, sim=True))
            else:  # play: create, ready, end 2-1 for the home side
                m = body(hc.ep_l2_match("", req))["resources"][0]["body"]
                assert m["type"] == "League_2_league" and m["stage"] == "created"
                assert m["leagueparam"]["matchtype"] == "league" and m["side"]["left"]["ready"] is False
                for stage, nxt in (("created", "squad"), ("squad", "setting")):
                    pr = body(hc.route(f"/match/{m['id']}/player/ready")("", json.dumps({"ready": True, "stage": stage}).encode()))
                    assert pr["resources"][0]["body"]["stage"] == nxt, pr
                rd = body(hc.route(f"/match/{m['id']}/side/left/ready")("", b'{"ready": true, "stage": "setting"}'))
                assert rd["resources"][0]["body"]["stage"] == "playing"
                stats = {"home": {"id": home, "score": 2}, "away": {"id": away, "score": 1}}
                stats.update(myTeam="home" if home == me else "away", opponentTeam="away" if home == me else "home")
                cm = hc.coach_me(hc._state(), hc._default_slot(hc._state()))
                ep0, lv0 = cm["money"]["ep"], cm["levelinfo"]
                xi = [p for p in cm["team"]["squad"]["positions"] if p["pos"] < hc.POS_SUB]
                # in-match player ids are the card's player id (spid % 1e6)
                stats[stats["myTeam"]]["players"] = [{"spid": int(p["spid"]) % 1000000, "goals": int(i == 0)} for i, p in enumerate(xi)]
                r = body(hc.route(f"/match/{m['id']}/end")("", json.dumps(stats).encode()))
                body(hc.route(f"/match/{m['id']}/end")("", json.dumps(stats).encode()))  # a resend credits nothing
                assert hc.coach_me(hc._state(), hc._default_slot(hc._state()))["money"]["ep"] == ep0 + (2000 if home == me else 500)
                mine = next(x for x in r["leagueparam"]["leaguematches"] if me in (x["home"], x["away"]))
                assert mine["goals"] == {"home": 2, "away": 1}, mine
                got = body(hc.route(f"/match/{m['id']}")("", b""))
                assert got["result"]["home"]["score"] == 2 and got["stage"] == "closed"
                assert got["reward"]["money"]["reward"]["total"] == (2000 if home == me else 500)
                # exp: coach by result, each of my 11 players by result (+20 for a goal); kept
                rw, cexp = got["reward"], hc.EXP_WIN[0] if home == me else hc.EXP_LOSS[0]
                assert rw["coach"]["prev"] == lv0 and rw["coach"]["reward"]["total"] == cexp
                assert hc.coach_me(hc._state(), 0)["levelinfo"] == rw["coach"]["current"]
                assert len(rw["players"]) == 11 and rw["players"][0]["reward"]["total"] == rw["players"][1]["reward"]["total"] + 20
                shown = {p["id"]: p["levelinfo"] for p in hc.coach_me(hc._state(), 0)["team"]["players"]}
                assert all(shown[x["id"]] == x["current"] for x in rw["players"])
                assert all((x["current"]["level"], x["current"]["exp"]) > (x["prev"]["level"], x["prev"]["exp"]) for x in rw["players"])
            assert r["leagueparam"]["round"] == rnd and len(r["leagueparam"]["leaguematches"]) == hc.LEAGUE_SIZE // 2

        lg = body(hc.ep_l2_me("", b""))
        assert lg["laststatus"] == f"league:{league['maxround']}"
        s = lg["league"]["statistics"]
        assert sorted(v["rank"] for v in s.values()) == list(range(1, hc.LEAGUE_SIZE + 1))
        assert all(v["wins"] + v["draws"] + v["losses"] == 2 * (hc.LEAGUE_SIZE - 1) for v in s.values())
        assert sum(v["goals"] for v in s.values()) == sum(v["goalsconceded"] for v in s.values())

        ach = body(hc.ep_l2_achievement("", b"{}"))["missions"][0]
        assert ach["mid"] == hc.SPONSORS["1"] and ach["progress"][0] == s[str(me)]["rank"], ach
        assert ach["achievement"] == (s[str(me)]["rank"] <= hc.MISSION_TARGET[ach["mid"]])

        end = body(hc.ep_l2_end("", b"{}"))
        assert end["promotion"]["current"] == 12 and end["promotion"]["next"] == (13 if s[str(me)]["rank"] <= 3 else 12)
        assert sum(len(end["leaderRewards"][k]) for k in ("FW", "MF", "DF", "GK")) == 11
        assert all(len(end["leaderRewards"][k]) == 1 for k in ("ratings", "goals", "assists", "tackles", "passes", "saves"))
        assert end["rewards"]["money"]["reward"]["total"] == sum(end["rewards"]["money"]["reward"]["detail"].values())
        after = body(hc.ep_l2_me("", b""))
        assert after["sponsoroffice"]["season"]["status"] == "finished" and "league" in after
        assert after["difficultyselect"] == end["promotion"]["next"]
        body(hc.ep_l2_finish("", b"{}"))  # give up / reset
        assert "league" not in body(hc.ep_l2_me("", b""))
        # --- cup: groups of 4 then an 8-club knock-out; I win every match I play
        cup = body(hc.ep_cup_new("", b'{"metaid": 1, "difficulty": 2}'))["resources"][0]["body"]
        cid = cup["_id"]
        assert len(cup["teams"]) == 16 and cup["phases"][0]["system"] == "round-robin"
        played = 0
        for _ in range(10):
            g, leg = hc.cup_my_pair(hc._state()["cups"][cid])
            if leg is not None and "score" not in leg:
                m = body(hc.route(f"/cup/{cid}/match")("", b"{}"))["resources"][0]["body"]
                assert m["type"] == "Cup_PvE"
                mine_home = g["teams"][leg["homeidx"]]["coach"] is not None
                stats = {"home": {"id": m["side"]["left" if mine_home else "right"]["team"]["teamid"], "score": 3 if mine_home else 0},
                         "away": {"id": m["side"]["right" if mine_home else "left"]["team"]["teamid"], "score": 0 if mine_home else 3}}
                body(hc.route(f"/match/{m['id']}/end")("", json.dumps(stats).encode()))
                played += 1
            cup = body(hc.route(f"/cup/{cid}/next")("", b"{}"))["resources"][0]["body"]
            if cup.get("finished") or cup["coaches"][0]["state"] != "playing":
                break
        assert played == 6 and cup.get("finished") and cup["coaches"][0]["state"] == "won", (played, cup["coaches"])
        assert len(cup["phases"]) == 2 and all("score" in p["legs"][0] for r in cup["phases"][1]["groups"][0]["rounds"] for p in r["pairs"])
        assert {"metaid": 1, "cupid": cid} in hc.coach_me(hc._state(), hc._default_slot(hc._state()))["cups"]  # until reward taken
        ep = lambda: hc.coach_me(hc._state(), hc._default_slot(hc._state()))["money"]["ep"]
        ep0 = ep()
        lv = body(hc.route(f"/cup/{cid}/leave")("", b"{}"))
        assert lv["rewards"] == [{"itemid": 1, "count": 60000}], lv
        assert ep() == ep0 + 60000  # cup prize credited
        assert cid not in hc._state()["cups"]
        # --- market: top list, search, buy (EP checked, a new card instance in my players)
        top = body(hc.route("/transfer/top/50")("", b""))
        assert len(top) == 50 and top[0]["price"] >= top[-1]["price"] and top[0]["expandme"] == "player"
        assert all(o["spid"] for o in body(hc.route("/transfer/top/50/position/GK")("", b"")))
        assert body(hc.route("/transfer/search/158023")("", b"")) == []  # season-0 engine card: not tradeable
        buy = lambda o: body(hc.route(f"/transfer/purchase/{o['spid']}/{o['grade']}/{o['price']}/purchase")("", b"{}"))
        team = lambda: hc._state()["teams"][str(hc._default_slot(hc._state()))]
        copies = lambda spid: [p for p in team()["players"] if p["spid"] == spid]
        star = next((o for o in top if o["price"] > ep()), None)
        assert star is None or buy(star)["reason"] == "NOT_ENOUGH_EP"
        with hc.closing(hc._db()) as c:
            spid = next(r[0] for r in c.execute("SELECT spid FROM players WHERE overallrating BETWEEN 70 AND 75 ORDER BY spid")
                        if hc.on_market(r[0]) and not copies(r[0]) and hc.card_price(r[0]) * 3 < ep())
        offer = body(hc.route(f"/transfer/search/{spid}")("", b""))[0]
        ep0 = ep()
        assert buy(offer)["result"] and len(copies(spid)) == 1 and ep() == ep0 - offer["price"]
        assert buy(offer)["result"] and len(copies(spid)) == 2  # a second copy: an upgrade pair
        # --- upgrade (enhancement v1): +1 & +1 -> +2 at 100% (ENHANCE_RATE[1] = 1.0)
        a, b = (hc.iid(p) for p in copies(spid))
        pv = body(hc.route(f"/player/{a}/enhance/{b}/preview")("", b""))
        assert pv["grade"] == 2 and pv["enhancepay"] >= 1000 and pv["price"] == hc.card_price(spid, 2), pv
        ep1 = ep()
        up = body(hc.route(f"/player/{a}/enhance/{b}")("", b"{}"))
        assert up["success"] and up["grade"] == 2 and [hc.iid(p) for p in copies(spid)] == [a] and ep() == ep1 - pv["enhancepay"]
        assert hc.grade_of(copies(spid)[0]) == 2 and hc.player_ovr(copies(spid)[0]) == hc.card_ovr(spid) + hc.GRADE_BOOST[2]
        assert not body(hc.route(f"/player/{a}/enhance/{a}/preview")("", b""))["result"]  # needs two cards
        # --- selling: a reserve at <= market price; the NPC buys at once, EP minus fee on collect
        band = body(hc.route(f"/transfer/price/{spid}/2")("", b""))
        assert band["upper"] == hc.card_price(spid, 2) and band["lower"] < band["upper"]
        sell = lambda pid, price: body(hc.ep_transfer_offer("", json.dumps({"playerobjectid": str(pid), "price": price}).encode()))
        squad = team()["squad"]
        starter = next(str(s.get("id") or s["spid"]) for s in squad["positions"] if s["pos"] < hc.POS_SUB)
        assert not sell(starter, 1000)["result"] and not sell(a, band["upper"] + 1)["result"]
        assert sell(a, band["upper"])["result"] and not copies(spid)
        offers = body(hc.ENDPOINTS["/transfer/my/offer/list"]("", b""))["offers"]
        assert len(offers) == 1 and offers[0]["status"] == "sold"
        ep1 = ep()
        got = body(hc.route(f"/transfer/my/offer/completed/{offers[0]['_id']}")("", b"{}"))
        fee = offers[0]["completefee"]["base"]  # minus PC warnet 20% + VIP 30% fee discounts
        assert got["result"] and ep() == ep1 + band["upper"] - fee + int(0.2 * fee) + int(0.3 * fee)
        me = hc.coach_me(hc._state(), hc._default_slot(hc._state()))
        assert me["igr"] is True and me["vipDetail"] == 2 and me["vipExpireAt"] > time.time() * 1000
        assert body(hc.ENDPOINTS["/transfer/my/offer/list"]("", b""))["offers"] == []
        # --- trade (Tukar v1): 2..10 reserves -> offers rated >= avg OVR + bonus, PC + VIP extra
        post = lambda path, req: body(hc.ENDPOINTS[path]("", json.dumps(req).encode()))
        with hc.closing(hc._db()) as c:
            cheap = [r[0] for r in c.execute("SELECT spid FROM players WHERE overallrating BETWEEN 58 AND 64 ORDER BY spid")
                     if hc.on_market(r[0])][:6]
        for s in cheap:  # bought cards land in the reserves
            assert buy(body(hc.route(f"/transfer/search/{s}")("", b""))[0])["result"]
        reserves = [p for p in team()["players"] if not hc.in_squad(team(), p)][:4]
        ids = [hc.iid(p) for p in reserves]
        sub = next(str(s.get("id") or s["spid"]) for s in team()["squad"]["positions"] if s["pos"] >= hc.POS_SUB)
        assert not post("/players/trade/proposal", {"players": ids[:1]})["result"]  # needs 2+
        assert not post("/players/trade/proposal", {"players": [starter] + ids[:1]})["result"]
        assert not post("/players/trade/proposal", {"players": [sub] + ids[:1]})["result"]  # subs neither
        pr = post("/players/trade/proposal", {"players": ids})
        sv = hc.static_values()
        lo = min(int(sum(hc.card_ovr(p["spid"]) for p in reserves) / 4 + sv["playertrade_cardovrbonus"]["4"]),
                 sv["playertrade_limit"]["CardMaxValue"])
        assert pr["result"] and len(pr["props"]) == sv["playertrade_normal"]["4"] + 2, pr
        assert [o.get("boost") for o in pr["props"]][-2:] == ["igr", "vip"]
        assert all(hc.card_ovr(o["spid"]) >= lo and o["price"] == hc.card_price(o["spid"], o["grade"])
                   for o in pr["props"] if "spid" in o), (lo, pr)
        ep_only = next(i for i, o in enumerate(pr["props"]) if "spid" not in o)
        n0, ep1 = len(team()["players"]), ep()
        assert post("/players/trade/contract", {"select": ep_only})["result"]
        assert len(team()["players"]) == n0 - 4 and ep() == ep1 + pr["props"][ep_only]["ep"]
        assert not any(hc.iid(p) in ids for p in team()["players"])
        assert not post("/players/trade/contract", {"select": 0})["result"]  # proposal used up
        pr = post("/players/trade/proposal", {"players": [hc.iid(p) for p in team()["players"] if not hc.in_squad(team(), p)][:2]})
        n0, card = len(team()["players"]), pr["props"][0]
        assert post("/trade3/contract", {"selectnum": 0, "slotnum": 0})["result"] and len(team()["players"]) == n0 - 1
        assert (team()["players"][-1]["spid"], team()["players"][-1]["grade"]) == (card["spid"], card["grade"])
        pt = body(hc.ENDPOINTS["/static/db/PLAYERTRADE"]("", b""))
        assert list(pt["playergrade"]["2"]) == ["1"] and list(pt["playergrade"]["40"])[-1] == "6"
        # --- trade3 (Scout): offers worth about the total value; scouts cool down after use
        st = hc._state(); hc.my_coach(st).pop("scout_used", None); hc._save(st)
        scouts = lambda: {s["key"]: s for s in hc.coach_me(hc._state(), hc._default_slot(hc._state()))["scouters"]}
        assert set(scouts()) == {"default_A01", "default_B01", "igr", "vip"} and not any(s["lock"] for s in scouts().values())
        for s in cheap[:3]:
            assert buy(body(hc.route(f"/transfer/search/{s}")("", b""))[0])["result"]
        give3 = [p for p in team()["players"] if not hc.in_squad(team(), p)][-3:]
        pids = [{"id": hc.iid(p), "spid": p["spid"], "grade": hc.grade_of(p), "price": hc.card_price(p["spid"], hc.grade_of(p))}
                for p in give3]
        total = sum(x["price"] for x in pids)
        assert not post("/trade3/proposal", {"id": "vip", "slotnum": 3, "playerids": pids})["result"]  # still Tim inti
        move = lambda pid, where: body(hc.route(f"/player/{pid}/move/{where}")("", b"{}"))
        assert not move(starter, "secondteam")["result"]  # the 18 stay in Tim inti
        for x in pids:
            assert move(x["id"], "secondteam")["result"]
        players = {p["_id"]: p for p in hc.coach_me(hc._state(), hc._default_slot(hc._state()))["team"]["players"]}
        assert all("state" not in players[x["id"]] for x in pids) and players[starter]["state"] == "first"
        assert move(pids[0]["id"], "firstteam")["result"] and move(pids[0]["id"], "secondteam")["result"]
        pr = post("/trade3/proposal", {"id": "vip", "slotnum": 3, "playerids": pids})
        assert pr["result"] and len(pr["props"]) == hc.TRADE3_STATIC["playertrade3_normal"]["3"] + 2, pr
        assert all(total * 0.5 <= o["price"] <= total * 3 for o in pr["props"]), (total, pr["props"])
        # scouts have no cooldown: the same scout negotiates again at once
        assert not any(s["lock"] or "cooltime" in s for s in scouts().values())
        pr = post("/trade3/proposal", {"id": "vip", "slotnum": 3, "playerids": pids})
        assert pr["result"]
        stale = [dict(pids[0], price=pids[0]["price"] + 1)] + pids[1:]
        assert post("/trade3/proposal", {"id": "default_A01", "slotnum": 0, "playerids": stale})["reason"] == "TRADE3_NOTICE_PLAYERPRICE03"
        n0 = len(team()["players"])
        assert post("/trade3/contract", {"slotnum": 3, "selectnum": 0})["result"] and len(team()["players"]) == n0 - 2
        assert team()["players"][-1]["spid"] == pr["props"][0]["spid"]
        assert hc.quest_met({"require_grade": 1, "require_grade_count": 2}, give3) and not hc.quest_met(None, give3)
        assert body(hc.ENDPOINTS["/trade3/staticvalues"]("", b""))["playertrade3_normal"] == {
            "2": 1, "3": 1, "4": 2, "5": 2, "6": 2, "7": 3, "8": 3, "9": 3, "10": 3}
        # --- daily login reward: once per WIB day into the Bag; claim one ("Terima") / all
        hc.DAILY_FILE = os.path.join(tmp, "daily_rewards.json")  # default table; admin test writes here
        st = hc._state(); hc.my_coach(st).pop("daily", None); hc.my_coach(st)["bag"] = []
        t0, day, table = time.time(), 86400, hc.daily_table()
        e1 = hc.daily_login(st, t0)
        assert e1["items"] == hc.reward_items(table[0]) and hc.daily_login(st, t0 + 60) is None  # once a day
        e2 = hc.daily_login(st, t0 + day)
        assert hc.my_coach(st)["daily"]["streak"] == 2 and e2["items"] == hc.reward_items(table[1])
        e3 = hc.daily_login(st, t0 + 3 * day)
        assert hc.my_coach(st)["daily"] == {"last": hc.today_wib(t0 + 3 * day), "streak": 1, "total": 3}  # streak broken
        hc._save(st)
        bag = body(hc.ENDPOINTS["/bag"]("", b""))
        assert [b["title"] for b in bag] == [e1["title"], e2["title"], e3["title"]] and {b["type"] for b in bag} == {"event"}
        assert "hari ke-3" in bag[1]["msg"] and all(b["date"].endswith(".000Z") for b in bag)  # tooltip text + received date
        ep1 = ep()
        assert body(hc.route(f"/bag/{e1['_id']}/unpack")("", b"{}"))["result"] and ep() == ep1 + table[0].get("ep", 0)
        got = body(hc.ENDPOINTS["/bag/unpackall"]("", json.dumps({"reqUnpackItems": [e2["_id"], e3["_id"]]}).encode()))
        assert got["result"] and body(hc.ENDPOINTS["/bag"]("", b"")) == []
        assert not body(hc.route(f"/bag/{e1['_id']}/unpack")("", b"{}"))["result"]  # already taken
        assert list(body(hc.ENDPOINTS["/static/cache/item"]("", json.dumps({"itemids": [1]}).encode()))) == ["1"]
        st = hc._state(); hc.my_coach(st).pop("items", None); hc._save(st)  # packs from the rewards (shop test counts)
        # --- shop: player packs for EP (1 or 10); Item saya opens one card per use
        shop = body(hc.ENDPOINTS["/shop/list"]("", b""))
        cats = Counter(e["shopcategory"] for e in shop)
        assert cats == {"PLAYERPACK": len(hc.PACKS), "USE": len(hc.ITEMS), "PACKAGE": len(hc.PACKAGES) - len(hc.CHAIN_STEPS),
                        "MILEAGE": len(hc.MP_SHOP), "MONTHLYPACK": len(hc.MONTHLY), "CHAINED": len(hc.CHAINED)}
        assert all(b["price"] == 0 for e in shop for b in e["product"]["staticdata"]["bunches"])  # free Shop
        pid = 8223509  # MF NEW 70+ 2017
        bunches = next(e for e in shop if e["shopid"] == pid)["product"]["staticdata"]["bunches"]
        ep1 = ep()
        assert post("/shop/purchase", {"shopid": pid, "productid": pid, "bunch": 0})["result"] and ep() == ep1 - bunches[0]["price"]
        assert post("/shop/purchase", {"shopid": pid, "productid": pid, "bunch": 1})["result"] and ep() == ep1 - bunches[0]["price"] - bunches[1]["price"]
        items = lambda: hc.coach_me(hc._state(), hc._default_slot(hc._state()))["items"]
        assert [(i["itemid"], i["remains"], i["staticdata"]["category"]) for i in items()] == [(pid, 11, "PLAYERPACK")]
        n0 = len(team()["players"])
        got = body(hc.route(f"/item/{items()[0]['_id']}/use")("", b"{}"))
        assert got["result"] and len(team()["players"]) == n0 + 1 and team()["players"][-1]["spid"] == got["player"]["spid"]
        with hc.closing(hc._db()) as c:
            pos = c.execute("SELECT preferredposition1 FROM players WHERE spid = ?", (got["player"]["spid"],)).fetchone()[0]
        # "70+" = the rating shown at +1 (base + 5, fifaaddict's enchant table)
        assert hc.card_ovr(got["player"]["spid"]) + hc.GRADE_BOOST[1] >= 70 and 9 <= pos <= 19 and got["player"]["spid"] // 1000000 == 88
        assert hc.GRADE_BOOST[:6] == (0, 5, 6, 7, 8, 10) and hc.player_ovr({"spid": 91190871, "grade": 5}) == 93  # WB Neymar +5
        assert items()[0]["remains"] == 10
        # client rating = Enums.calcPosOvr at preferredposition1 (fifaaddict shows the same):
        # Messi 2017 RW 80, Belotti 2017 ST 77, Andre Gomes 2017 CM 65 (fifadb column: 85/82/70)
        assert [hc.card_ovr(s) for s in (88158023, 88208596, 88211575)] == [80, 77, 65]
        assert list(body(hc.ENDPOINTS["/static/cache/item"]("", json.dumps({"itemids": [pid, "x", 2]}).encode()))) == [str(pid)]
        # USE items: buy (free), then "Gunakan" -> /item/<_id>/use per type; a package fills Item saya
        def use(itemid, req=None):
            it = next(i for i in hc.coach_me(hc._state(), hc._default_slot(hc._state()))["items"] if i["itemid"] == itemid)
            return body(hc.route(f"/item/{it['_id']}/use")("", json.dumps(req or {"id": it["_id"]}).encode()))
        assert post("/shop/purchase", {"shopid": 50021, "productid": 50021, "bunch": 0})["result"]
        ep1 = ep(); assert use(50021)["ep"] == 1_000_000 and ep() == ep1 + 1_000_000  # EP +1.000.000
        assert post("/shop/purchase", {"shopid": 501, "productid": 501, "bunch": 1})["result"]  # 10 Lucky EP
        assert all(10_000 <= use(501)["ep"] <= 1_000_000 for _ in range(10))
        assert not any(i["itemid"] == 501 for i in items())  # used up
        assert post("/shop/purchase", {"shopid": 50122, "productid": 50122, "bunch": 1})["result"]  # 10 Kotak Emas
        for _ in range(10):
            got = use(50122)
            assert got["result"] and (got.get("ep") == 3_000_000 or got["item"]["itemid"] in (82214180, 45012))
        assert post("/shop/purchase", {"shopid": hc.MP_SHOPID + 3002, "productid": 3002, "bunch": 0})["result"]  # MP Shop
        assert use(3002, {"name": "Juragan"})["result"] and hc.my_coach(hc._state())["name"] == "Juragan"
        assert post("/shop/purchase", {"shopid": 3001, "productid": 3001, "bunch": 0})["result"]
        club = hc.my_teamid(hc._state())
        assert use(3001, {"teamid": 10})["result"] and hc.my_teamid(hc._state()) == 10  # Ganti tim
        assert body(hc.route("/static/info/team/summary/10")("", b""))["id"] == 10
        hc.my_coach(st := hc._state())["teamid"] = club; hc._save(st)
        n0 = {i["itemid"]: i["remains"] for i in items()}
        assert post("/shop/purchase", {"shopid": 50881, "productid": 50881, "bunch": 0})["result"]  # Kotak World Best
        n1 = {i["itemid"]: i["remains"] for i in items()}
        assert all(n1[i] - n0.get(i, 0) == n for i, n in hc.PACKAGES[50881][2]) and 50881 not in n1
        # Paket Bulanan: items now + one daily Bag entry per day; Serial product steps; Special Bonus
        ep1 = ep(); assert post("/shop/purchase", {"shopid": 81000001, "productid": 81000001, "bunch": 0})["result"]
        assert [m["productid"] for m in body(hc.ENDPOINTS["/monthlypack"]("", b""))] == [81000001]
        hc.my_coach(st := hc._state())["bag"] = []; hc._save(st)
        assert body(hc.ENDPOINTS["/monthlypack"]("", b"{}"))["result"]
        bag = body(hc.ENDPOINTS["/bag"]("", b""))
        assert [(b["type"], b["items"]) for b in bag] == [("monthlypack", [{"itemid": hc.EP_ITEM, "count": 200_000}])]
        hc.ENDPOINTS["/monthlypack"]("", b"{}"); assert len(body(hc.ENDPOINTS["/bag"]("", b""))) == 1  # once a day
        hc.my_coach(st := hc._state())["bag"] = []; hc._save(st)
        chain = lambda: next(e for e in body(hc.ENDPOINTS["/shop/list"]("", b"")) if e["shopcategory"] == "CHAINED")
        assert chain()["step"] == 0 and [c["shopid"] for c in chain()["chained"]] == [70000002, 70000003]
        assert post("/shop/purchase", {"shopid": 70000001, "productid": 70000001, "bunch": 0})["result"]
        assert chain()["step"] == 1 and chain()["remainedperaccount"] == 0
        targets = body(hc.ENDPOINTS["/specifictarget"]("", b""))
        special = body(hc.ENDPOINTS["/shop/list/STSHOP"]("", b""))
        assert targets["issell"] and {t["stid"] for t in targets["targets"]} == {e["stid"] for e in special}
        assert post("/shop/purchase", {"shopid": special[0]["shopid"], "productid": special[0]["product"]["itemid"], "bunch": 0})["result"]
        # "+N" packs give grade-N cards; 06-11 packs draw only from seasons 2006..2011
        assert len(hc.pack_pool(45007)) == 50 and {s // 1000000 for s, _ in hc.pack_pool(45007)} <= set(range(6, 12))
        wb = hc.pack_pool(82231307)  # one odd cheap card must not win most draws (it once won 80%)
        assert max(w for _, w in wb) / sum(w for _, w in wb) < 0.2
        gp = 15031  # '14 Top 30 +5
        assert hc.pack_price(gp) > 5 * sum(hc.card_price(s) * w for s, w in hc.pack_pool(gp)) / sum(w for _, w in hc.pack_pool(gp))
        assert hc.pack_grades(gp) == [(1, 5), (2, 4), (3, 3), (4, 2), (5, 1)] and hc.pack_grades(28007) == [(1, 1)]
        # prices follow the rating only (user: "fix harga yang tidak sesua statsnya")
        assert all(hc.stat_price(o) < hc.stat_price(o + 1) for o in range(40, 95))
        # All Season +8: any Market card of any season at +1..+8
        assert [g for g, _ in hc.pack_grades(7675)] == list(range(1, 9))
        assert len({s // 1000000 for s, _ in hc.pack_pool(7675)}) > 20
        st = hc._state(); hc.my_coach(st)["ep"] = 10 ** 10; hc._save(st)
        assert post("/shop/purchase", {"shopid": gp, "productid": gp, "bunch": 1})["result"]  # 10 packs
        opened = [body(hc.route(f"/item/{next(i for i in items() if i['itemid'] == gp)['_id']}/use")("", b"{}"))["player"]
                  for _ in range(10)]
        assert all(1 <= p["grade"] <= 5 and p["spid"] // 1000000 == 14 and p["price"] == hc.card_price(p["spid"], p["grade"])
                   for p in opened) and len({p["grade"] for p in opened}) > 1  # a random grade +1..+5
        assert team()["players"][-1]["grade"] == opened[-1]["grade"]
        # --- admin page API (admin.py)
        import admin, urllib.error, urllib.request
        admin.hc = hc
        A = lambda name, **p: admin.API[name](p)
        def refused(name, **p):
            try:
                A(name, **p)
            except admin.AdminError:
                return True
        assert A("ep", set=5000)["ep"] == 5000 and A("ep", add=-9000)["ep"] == 0 and A("ep", add=1500)["ep"] == ep() == 1500
        assert A("summary")["coach"]["ep"] == 1500 and A("summary")["players"] == len(team()["players"])
        assert any(r["spid"] == 88158023 and r["ovr"] == 80 for r in A("search", q="Messi")["rows"])
        roster = lambda: {p["id"]: p for p in A("players")["players"]}
        if len(team()["players"]) >= hc.MAX_PLAYERS:  # make room for one
            A("player_remove", id=next(i for i, p in roster().items() if p["role"] == "RES"))
        new = A("player_add", spid=88158023, grade=3)["id"]
        assert (roster()[new]["role"], roster()[new]["ovr"]) == ("RES", 80 + hc.GRADE_BOOST[3])
        A("player_grade", id=new, grade=5); A("player_team", id=new, team="second")
        assert (roster()[new]["grade"], roster()[new]["team2"]) == (5, True)
        A("player_team", id=new, team="first"); assert not roster()[new]["team2"]
        A("player_remove", id=new); assert new not in roster()
        main_id = next(i for i, p in roster().items() if p["role"] == "MAIN")
        assert refused("player_remove", id=main_id) and refused("player_team", id=main_id, team="second")
        assert refused("player_add", spid=1) and refused("player_grade", id="nope", grade=2) and refused("ep", set="x")
        A("give_pack", itemid=pid, count=2); assert items()[0]["remains"] == 12 and refused("give_pack", itemid=2)
        gift = A("bag_send", title="tes", ep=777, pack=pid, count=1, spid=88158023, grade=2)["id"]
        g = next(b for b in body(hc.ENDPOINTS["/bag"]("", b"")) if b["_id"] == gift)
        assert g["items"] == [{"itemid": hc.EP_ITEM, "count": 777}, {"itemid": pid, "count": 1}] and g["players"] == [{"spid": 88158023, "grade": 2}]
        ep1, n0 = ep(), len(team()["players"])
        assert body(hc.route(f"/bag/{gift}/unpack")("", b"{}"))["result"]
        assert ep() == ep1 + 777 and items()[0]["remains"] == 13 and len(team()["players"]) == n0 + 1
        assert refused("bag_send", title="kosong") and refused("bag_send", pack=2)
        tbl = [{"ep": 1234}, {"pack": pid, "count": 2}]
        assert A("daily_set", table=tbl)["table"] == tbl == hc.daily_table()
        assert refused("daily_set", table=[{"ep": 0}]) and refused("daily_set", table=[{"pack": 2}]) and refused("daily_set", table=[])
        A("daily_reset")
        assert A("daily_grant")["title"].endswith("hari ke-1") and A("daily_grant")["title"].endswith("hari ke-2")
        assert [b["items"] for b in body(hc.ENDPOINTS["/bag"]("", b""))] == [hc.reward_items(r) for r in tbl]
        st = hc._state(); hc.my_coach(st)["scout_used"] = {"vip": time.time()}; hc._save(st)
        assert [s["lock"] for s in A("summary")["scouts"]] == [False, False, False, False]  # no cooldowns
        A("scouts_reset"); assert not any(s["lock"] for s in A("summary")["scouts"])
        assert len(A("log", n=5)["lines"]) <= 5
        # HTTP: reads are GETs; a change needs POST + X-FO3-Admin (a cross-site form can't add it)
        srv = admin.start(hc, 0)
        url, web = f"http://127.0.0.1:{srv.server_address[1]}", urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            assert b"FO3ID admin" in web.open(url + "/").read()
            assert json.load(web.open(url + "/api/summary"))["coach"]["ep"] == ep()
            for req in (urllib.request.Request(url + "/api/ep?add=5"),
                        urllib.request.Request(url + "/api/ep", b'{"add": 5}', method="POST")):
                try:
                    web.open(req); raise AssertionError("change without POST + header")
                except urllib.error.HTTPError as e:
                    assert e.code == 403
            ep1, hdr = ep(), {"X-FO3-Admin": "1", "Content-Type": "application/json"}
            assert json.load(web.open(urllib.request.Request(url + "/api/ep", b'{"add": 5}', hdr, method="POST")))["ep"] == ep1 + 5 == ep()
            assert json.load(web.open(urllib.request.Request(url + "/api/player_grade", b'{"id": "nope", "grade": 2}', hdr, method="POST"))) == {
                "error": "pemain tidak ditemukan"}
        finally:
            srv.shutdown(); srv.server_close()
        # --- Tim inti holds 36: further cards go to Tim Cadangan (Tukar Scout's list); old clubs
        # over the cap are balanced at login; moving into a full Tim inti is refused
        st = hc._state(); t = st["teams"][str(hc._default_slot(st))]
        for p in t["players"]:
            p.pop("team2", None)
        while len(t["players"]) < 45:
            t["players"].append(dict(t["players"][-1], _id=hc.new_iid()))
        hc._save(st)
        assert body(hc.ENDPOINTS[f"/coach/{hc._default_slot(st)}/default"]("", b""))["result"]
        t = hc._state()["teams"][str(hc._default_slot(st))]
        assert hc.first_team_count(t) == hc.FIRST_TEAM_MAX and sum(bool(p.get("team2")) for p in t["players"]) == 9
        # the Skuad tab's move to Tim inti reads limits.firstTeam (missing = it never asks)
        assert hc.coach_me(hc._state(), hc._default_slot(hc._state()))["limits"]["firstTeam"] == hc.FIRST_TEAM_MAX
        assert all(not p.get("team2") for p in t["players"] if hc.in_squad(t, p))
        new = hc.add_player(t, 88158023)
        assert new["team2"]
        res = next(p for p in t["players"] if p.get("team2") and p is not new)
        hc._save(hc._state() | {"teams": {**hc._state()["teams"], str(hc._default_slot(st)): t}})
        assert not body(hc.route(f"/player/{hc.iid(res)}/move/firstteam")("", b"{}"))["result"]
        # --- delete a manager: PUT /coach/<slot>/postponed -> "postpone" until hideat (restorable with
        # DELETE /coach/<_id>/postponed), then the slot is free and the club data goes with it
        slot = hc._default_slot(hc._state()); cid = hc.coach_id(slot)
        coaches = lambda: body(hc.ENDPOINTS["/coaches"]("", b""))
        mine_c = lambda: next((x for x in coaches()["coachlist"] if x["_id"] == cid), None)
        postpone = lambda key, method: body(hc.route(f"/coach/{key}/postponed")("", json.dumps({"_method": method}).encode()))
        assert mine_c()["recoveryperiod"] == hc.COACH_RECOVERY and "hideat" not in mine_c()
        assert postpone(slot, "PUT")["result"]
        assert mine_c()["state"] == "postpone" and mine_c()["hideat"].endswith(".000Z") and coaches()["defaultcoachid"] == ""
        assert postpone(cid, "DELETE")["result"] and mine_c()["state"] == "active" and coaches()["defaultcoachid"] == cid
        assert postpone(slot, "PUT")["result"]
        st = hc._state(); hc.my_coach(st)["deleteat"] = time.time() - 1; st["offers"] = []; hc._save(st)  # restore time over
        assert mine_c() is None and coaches()["defaultcoachid"] == ""
        st = hc._state(); assert str(slot) not in st["teams"] and "offers" not in st and "cups" not in st
        assert postpone(cid, "DELETE") == {"result": False, "reason": "already removed"}
    finally:
        shutil.rmtree(tmp)
    print("league + cup + market + upgrade + trade + trade3 + daily + shop + admin + coach delete OK")


if __name__ == "__main__":
    main()
