"""Sync player market prices from fifaaddict.com's FIFA Online 3 DB (Indonesia server).

    python tools/fa_sync.py [--cache DIR]

Walks the public listing fo3db.php?q=player&season=<code>&limit=500 (one request every 2 s;
a query that hits the 500-row cap is split by position, then age band) and writes
gamedata/fa_prices.json = {spid: Indonesia-server EP price at grade +1} for every listed card
that exists in our gamedata/fifadb.sqlite. Where the ID price is missing another server's is
scaled by its median ratio to ID; 0 = listed without any price (http_config uses a fitted
formula then). fifaaddict player ids are the client's spids (season * 1e6 + player id).
Pages are cached (default: %TEMP%/fa_cache) so a rerun resumes. Only listed cards are on the
Market (season-0 engine cards are not).
"""
import argparse, hashlib, json, os, re, sqlite3, sys, tempfile, time, urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://en.fifaaddict.com/fo3db.php?q=player"
SEASONS = ("2006,ucl2006,wc2006,2007,2008,eu2008,2009,2010,ucl2010,wc2010,2011,2014,wc2014,tots2014,2015,2016,"
           "2017,16ec,16th,wb,wl,ub,ul,el,lp,cp,cc,17th,tco92,mufclegend,ml,tl,vl,cl,kl2002,cel,u23,kxi,mul,vs").split(",")
POSITIONS = "st,cf,lf,rf,lw,rw,cam,cm,cdm,lm,rm,gk,cb,sw,lb,rb,lwb,rwb".split(",")
AGES = ["15-20", "21-22", "23-24", "25-26", "27-28", "29-30", "31-33", "34-50"]
CAP, DELAY = 500, 2.0
# Indonesia price = other server's price x median(ID / that server) over cards priced on both
SCALE = {"id": 1.0, "sg": 1.267, "th": 1.538, "vn": 1.408, "kr": 0.595}
ROW = re.compile(r'<tr id="player_id(\d+)" class="player-row">(.*?)</tr>', re.S)
PRICE = re.compile(r'stat_cell_price_(kr|th|vn|sg|id)">\s*(\d+)\s*<')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.join(tempfile.gettempdir(), "fa_cache"))
    args = ap.parse_args()
    os.makedirs(args.cache, exist_ok=True)
    last = [0.0]
    prices = {}

    def fetch(url):
        f = os.path.join(args.cache, hashlib.sha1(url.encode()).hexdigest() + ".html")
        if os.path.exists(f):
            return open(f, encoding="utf-8").read()
        time.sleep(max(0.0, DELAY - (time.time() - last[0])))
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (FO3 private server price sync)"})
                body = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
                break
            except OSError as e:
                print("retry", attempt, url, e, flush=True)
                time.sleep(10 * (attempt + 1))
        else:
            raise SystemExit("failed: " + url)
        last[0] = time.time()
        open(f, "w", encoding="utf-8").write(body)
        return body

    def walk(params, depth):
        url = BASE + "".join(f"&{k}={v}" for k, v in params) + f"&limit={CAP}"
        rows = ROW.findall(fetch(url))
        for spid, row in rows:
            p = {k: int(v) for k, v in PRICE.findall(row)}
            prices[int(spid)] = next((round(p[s] * f) for s, f in SCALE.items() if p.get(s, 0) > 0), 0)
        print(f"{len(rows):4d} {'  ' * depth}{dict(params)} total={len(prices)}", flush=True)
        if len(rows) >= CAP and depth < 2:
            for v in (POSITIONS if depth == 0 else AGES):
                walk(params + [("position" if depth == 0 else "age", v)], depth + 1)

    for code in SEASONS:
        walk([("season", code)], 0)
    with sqlite3.connect(os.path.join(HERE, "gamedata", "fifadb.sqlite")) as c:
        ours = {r[0] for r in c.execute("SELECT spid FROM players")}
    out = {str(s): p for s, p in sorted(prices.items()) if s in ours}
    with open(os.path.join(HERE, "gamedata", "fa_prices.json"), "w") as f:
        json.dump(out, f)
    print(f"wrote {len(out)} cards, {sum(1 for p in out.values() if p)} priced ({len(prices)} listed on fifaaddict)")


if __name__ == "__main__":
    sys.exit(main())
