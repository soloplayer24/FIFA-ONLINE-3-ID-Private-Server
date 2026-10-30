"""Fill in the player mini faces the client's cache lacks, from fifaaddict.com.
User: "update semua mini face yang belum ada ambil dari FIFAADDICT".

The client shows faces from its cache (_cache/games/fo3id/externalAssets/players, served by
http_config /cdn/): p<spid>.png for a season face, else p<player id>.png. fifaaddict serves
the same 128x128 PNGs at fo3img/players/p<player id>.png (byte-identical to the cached ones;
it has no per-season PNGs), so this adds the missing p<player id>.png files. Order: my club's
players, faces the game already asked for, then every Market card, dearest first. One request
every --delay seconds; existing files are never touched; gamedata/fa_faces.json records what
was added or failed so a rerun resumes, and --remove deletes only the added files.

Usage: python -I tools/fa_faces.py [--delay 0.5] [--limit N] [--remove]
"""
import argparse, json, os, re, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
ARGS, sys.argv = sys.argv[1:], sys.argv[:1]  # http_config reads a port from argv at import
import http_config as hc

FACES = os.path.join(hc.ASSETS, "externalAssets", "players")
MANIFEST = os.path.join(ROOT, "gamedata", "fa_faces.json")
URL = "https://fifaaddict.com/fo3img/players/p{}.png"


def wanted():
    """Player ids in download order (club, already requested, Market by price)."""
    st = hc._state()
    club = [int(p["spid"]) % 1000000 for t in st["teams"].values() for p in t["players"]]
    asked = []
    with open(hc.LOG, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.search(r"GET /cdn/externalAssets/players/p(\d+)\.png", line)
            if m:
                asked.append(int(m.group(1)) % 1000000)
    fa = hc.fa_prices()
    market = [s % 1000000 for s in sorted(fa, key=lambda s: -fa[s])]
    seen, out = set(), []
    for pid in club + asked + market:
        if pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--remove", action="store_true", help="delete the faces this tool added")
    a = ap.parse_args(ARGS)
    man = json.load(open(MANIFEST)) if os.path.isfile(MANIFEST) else {"added": [], "failed": {}}
    if a.remove:
        for pid in man["added"]:
            f = os.path.join(FACES, f"p{pid}.png")
            if os.path.isfile(f):
                os.remove(f)
        print(len(man["added"]), "faces removed")
        os.remove(MANIFEST)
        return
    todo = [pid for pid in wanted() if not os.path.isfile(os.path.join(FACES, f"p{pid}.png"))
            and str(pid) not in man["failed"]]
    if a.limit:
        todo = todo[:a.limit]
    print(len(todo), "faces to fetch", flush=True)
    done = 0
    for i, pid in enumerate(todo):
        time.sleep(a.delay)
        req = urllib.request.Request(URL.format(pid), headers={"User-Agent": "Mozilla/5.0 (FO3 private server face sync)"})
        try:
            body = urllib.request.urlopen(req, timeout=60).read()
            if not body.startswith(b"\x89PNG"):
                raise ValueError("not a PNG")
        except Exception as e:
            man["failed"][str(pid)] = str(getattr(e, "code", e))[:40]
        else:
            with open(os.path.join(FACES, f"p{pid}.png"), "wb") as f:
                f.write(body)
            man["added"].append(pid)
            done += 1
        if i % 50 == 49 or i == len(todo) - 1:
            os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
            with open(MANIFEST, "w") as f:
                json.dump(man, f)
            print(f"{i + 1}/{len(todo)} fetched {done}, failed {len(man['failed'])}", flush=True)
    print("done:", done, "added,", len(man["failed"]), "failed in total")


if __name__ == "__main__":
    main()
