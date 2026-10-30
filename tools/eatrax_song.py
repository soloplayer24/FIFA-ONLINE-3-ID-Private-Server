"""Put a song first in the game's own menu music (EA Trax) playlist.
User: "masukan lagu ini sebagai starter game", then "bukan di mute tapi di masukan ke urutan".

How the client plays EA Trax (fifazf build 654420, found 2026-10-30):
  * data_0.big data/db/fifa_ng_db.db is an EA "DB\\0\\x08" database. Table eatrax (bBVN) = songs
    {songid, songname, artist, album, intro}; songplaylistlinks (JVoI) = {songid, playlistid,
    unlocked, artificialkey}. EATraxImpl (0x8F1B60) reads playlist 0 and uses songid as TrackID;
    it queues TrackID 1 first, then a Fisher-Yates shuffle of every unlocked song (0x8F3990).
  * data_1.big audiodata/eatrax/eatrax_01.sbr is a Harmony "SBle" bank with one DSET: 2 rows per
    TrackID; fields .OFF (u32 stream offsets + 4-bit row -> offset index), .SBS, .SID (base + u8
    per row) and TrackID (field id e8770f66, 1 + u8 per row). eatrax_01.sbs holds one EAAC SPS
    stream per track (EALayer3); the engine also decodes EAAC codec 2 (P6B0 = PCM16BE).

The song becomes TrackID 1 (the old TrackID 1 moves to a new last TrackID): its PCM16BE stream is
appended to the .sbs, the bank and the DB get the new rows, and the three files are appended to
their .big archives with the table entries re-pointed (tools/re/bigdir.py); the DB goes in
RefPack-packed (tools/re/refpack.py), as shipped. The first run saves
each archive's original table and length in backup/eatrax/; every run starts from those, and
--restore puts them back (cutting off the appended data).

Usage: python -I tools/eatrax_song.py [song.mp3] [--title T] [--artist A] [--album B] [--game DIR]
       python -I tools/eatrax_song.py --restore
Needs ffmpeg on PATH (decodes the song to 44.1 kHz stereo PCM).
"""
import argparse, json, os, struct, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "re"))
import bigdir
from eadb import Table, db_tables, crc, u32
from refpack import refpack

BACKUP = os.path.join(ROOT, "backup", "eatrax")
DB, SBR, SBS = "data/db/fifa_ng_db.db", "audiodata/eatrax/eatrax_01.sbr", "audiodata/eatrax/eatrax_01.sbs"
BIGS = {"data_0.big": (DB,), "data_1.big": (SBR, SBS)}
TRACKID = struct.pack("<I", 0xE8770F66)  # DSET field id of the TrackID column
BLOCK = 2048  # samples per SPS data block: 8 KiB of PCM, the size of the bank's own blocks


def originals(game):
    """Put each archive back in its shipped state (saved on first use) and return the shipped
    DB, SBR and SBS bytes."""
    os.makedirs(BACKUP, exist_ok=True)
    files = {}
    for big, names in BIGS.items():
        path = os.path.join(game, "data", big)
        head, meta = os.path.join(BACKUP, big + ".head"), os.path.join(BACKUP, big + ".json")
        if os.path.isfile(meta):
            with open(path, "r+b") as f:
                f.write(open(head, "rb").read())
                f.truncate(json.load(open(meta))["length"])
        else:
            open(head, "wb").write(bigdir.table(path))
            json.dump({"length": os.path.getsize(path)}, open(meta, "w"))
        for n in names:
            files[n] = bigdir.read(path, n)
    return files


# --- EA DB (format in tools/re/eadb.py) -------------------------------------------------------

def edit_db(db, title, artist, album):
    """The song takes songid 1 (intro); the old songid 1 moves to a new last songid."""
    base, names, raws = db_tables(db)
    i_tr, i_pl = names.index(b"bBVN"), names.index(b"JVoI")
    tr, pl = Table(raws[i_tr]), Table(raws[i_pl])
    old = tr.find("oriB", 1)
    new_id = max(tr.get(r, "oriB") for r in tr.rows) + 1
    mine = old
    for sn, value in (("VXEQ", title), ("mVGg", artist), ("iENE", album)):
        mine = tr.set(mine, sn, value)
    tr.rows = [mine if r == old else r for r in tr.rows] + [tr.set(tr.set(old, "oriB", new_id), "RlGL", 0)]
    link = pl.find("oriB", 1)
    key = max(pl.get(r, "JMld") for r in pl.rows) + 1
    pl.rows.append(pl.set(pl.set(link, "oriB", new_id), "JMld", key))
    raws[i_tr], raws[i_pl] = tr.raw(), pl.raw()
    out, pos = bytearray(db[:base]), 0
    for i, r in enumerate(raws):
        struct.pack_into("<I", out, 0x1C + 8 * i, pos)
        pos += len(r)
    struct.pack_into("<I", out, base - 4, crc(out[0x18:base - 4]))
    struct.pack_into("<I", out, 8, base + pos)
    struct.pack_into("<I", out, 0x14, crc(out[:0x14]))
    return bytes(out) + b"".join(raws), new_id


# --- sound bank -----------------------------------------------------------------------------

def bank_layout(sbr):
    """(dset, rows, descriptors {id: pos}, offsets) of the bank, asserting the layout this tool
    edits: rows 2k and 2k+1 = TrackID k+1 -> offset k."""
    assert sbr[:4] == b"SBle"
    dset = u32(sbr, u32(sbr, 0x18))
    assert sbr[dset:dset + 4] == b"TESD"
    rows, nf = u32(sbr, dset + 0x38), struct.unpack_from("<H", sbr, dset + 0x3C)[0]
    desc = {bytes(sbr[p:p + 4]): p for p in range(dset + 0x48, dset + 0x48 + 0x18 * nf, 0x18)}
    d_off, d_sid, d_tid = desc[b"FFO."], desc[b"DIS."], desc[TRACKID]
    n = struct.unpack_from("<Q", sbr, d_off + 8)[0]
    tab = u32(sbr, d_off + 0x10)
    offs = list(struct.unpack_from(f"<{n}I", sbr, tab))
    pairs = [k // 2 for k in range(rows)]
    assert rows == 2 * n and sbr[d_off + 5] == 3 and sbr[d_sid + 5] == sbr[d_tid + 5] == 2
    assert sbr[tab + 4 * n:tab + 5 * n] == bytes(0x11 * k for k in range(n))
    for d in (d_sid, d_tid):
        assert list(sbr[u32(sbr, d + 0x10):u32(sbr, d + 0x10) + rows]) == pairs
    return dset, rows, desc, offs


def edit_bank(sbr, new_offset):
    """TrackID 1 -> new_offset; the old TrackID 1 stream becomes a new last TrackID."""
    dset, rows, desc, offs = bank_layout(sbr)
    b, n = bytearray(sbr), len(offs) + 1
    tab = u32(b, desc[b"FFO."] + 0x10)
    out = b[:tab] + struct.pack(f"<{n}I", new_offset, *offs[1:], offs[0]) + bytes(0x11 * k for k in range(n))
    for d in (desc[b"DIS."], desc[TRACKID]):
        struct.pack_into("<I", out, d + 0x10, len(out))
        out += bytes(k // 2 for k in range(2 * n))
    out += bytes(-len(out) % 0x10)
    struct.pack_into("<I", out, dset + 0x38, 2 * n)
    struct.pack_into("<Q", out, desc[b"FFO."] + 8, n)
    # the TrackID key range in the DSET index: (count, min, max) = (n-1, 1, n-1) -> (n, 1, n)
    key = TRACKID + struct.pack("<III", n - 1, 1, n - 1)
    at = out.find(key, dset, dset + u32(out, dset + 4))
    assert at > 0 and out.find(key, at + 1, dset + u32(out, dset + 4)) < 0
    out[at:at + len(key)] = TRACKID + struct.pack("<III", n, 1, n)
    struct.pack_into("<I", out, 4, len(out))
    return bytes(out)


def sps_pcm(song):
    """EAAC SPS stream, codec 2 (PCM16BE), stereo 44.1 kHz: 'H' header block, 'D' blocks of
    interleaved samples, 'E' end block."""
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", song, "-f", "s16be", "-ac", "2", "-ar", "44100", "-"],
                         check=True, capture_output=True).stdout
    n = len(pcm) // 4
    out = bytearray(b"\x48\x00\x00\x0c" + struct.pack(">II", 1 << 28 | 2 << 24 | 1 << 18 | 44100, 1 << 30 | n))
    for s in range(0, n, BLOCK):
        k = min(BLOCK, n - s)
        out += b"\x44" + (8 + 4 * k).to_bytes(3, "big") + struct.pack(">I", k) + pcm[4 * s:4 * (s + k)]
    out += b"\x45\x00\x00\x04"
    return bytes(out + bytes(-len(out) % 0x20)), n


def stream_info(sbs, off):
    """(codec, num_samples) of the SPS stream at off, after checking its blocks add up."""
    assert sbs[off] == 0x48
    h1, h2 = struct.unpack_from(">II", sbs, off + 4)
    p, total = off + int.from_bytes(sbs[off + 1:off + 4], "big"), 0
    while sbs[p] != 0x45:
        assert sbs[p] == 0x44, hex(p)
        total += struct.unpack_from(">I", sbs, p + 4)[0]
        p += int.from_bytes(sbs[p + 1:p + 4], "big")
    assert total == h2 & 0x1FFFFFFF
    return h1 >> 24 & 0xF, total


def verify(game, title):
    """Read the three files back out of the archives and check the song is TrackID 1."""
    db = bigdir.read(os.path.join(game, "data", "data_0.big"), DB)
    sbr = bigdir.read(os.path.join(game, "data", "data_1.big"), SBR)
    sbs = bigdir.read(os.path.join(game, "data", "data_1.big"), SBS)
    _, names, raws = db_tables(db)
    tr, pl = Table(raws[names.index(b"bBVN")]), Table(raws[names.index(b"JVoI")])
    songs = {tr.get(r, "oriB"): (tr.get(r, "VXEQ"), tr.get(r, "RlGL")) for r in tr.rows}
    unlocked = {pl.get(r, "oriB") for r in pl.rows if pl.get(r, "gAPY") and pl.get(r, "DAJE") == 0}
    _, _, _, offs = bank_layout(sbr)
    assert songs[1] == (title, 1) and set(songs) <= unlocked and len(offs) == len(songs)
    infos = [stream_info(sbs, o) for o in offs]
    assert infos[0][0] == 2 and all(c == 6 for c, _ in infos[1:])
    for tid in sorted(songs):
        print(f"  TrackID {tid:2}: {songs[tid][0]:<24} {infos[tid - 1][1] / 44100:6.1f} s  codec {infos[tid - 1][0]}"
              + ("  (intro, plays first)" if tid == 1 else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("song", nargs="?", default=os.path.join(ROOT, "music", "starter.mp3"))
    ap.add_argument("--title", default="Time-Bomb")
    ap.add_argument("--artist", default="All Time Low")
    ap.add_argument("--album", default="All Time Low")
    ap.add_argument("--game", default=r"D:\FO3ID\32795")
    ap.add_argument("--restore", action="store_true", help="put the shipped archives back")
    a = ap.parse_args()
    files = originals(a.game)
    if a.restore:
        print("archives restored")
        return
    stream, n = sps_pcm(a.song)
    sbs = files[SBS] + bytes(-len(files[SBS]) % 0x20)
    db, new_id = edit_db(files[DB], a.title, a.artist, a.album)
    # the DB must stay RefPack-packed and smaller than its contents: stored or literal-only
    # entries hang the boot right after GET /account
    bigdir.patch(os.path.join(a.game, "data", "data_0.big"), {DB: (refpack(db), len(db))})
    bigdir.patch(os.path.join(a.game, "data", "data_1.big"), {SBR: edit_bank(files[SBR], len(sbs)), SBS: sbs + stream})
    print(f"{a.artist} - {a.title}: {n / 44100:.1f} s as TrackID 1, old TrackID 1 -> {new_id}")
    verify(a.game, a.title)


if __name__ == "__main__":
    main()
