"""
All stats for the 2v2 Mayhem customs (GRCX, RikiPlancha12, BabyGolem, ragnarneves).
Replaces winrates.py + fun_stats.py. Reads customs_archive.json (written by lcu_archive.py).

Requirements: Python 3.9+, `pip install openpyxl`
              optional: `requests` (only to refresh champion/augment names), `tzdata` (Windows timezones)
Usage (in the folder with customs_archive.json):
    python stats.py                       # games from 2026-11-08 onward (post-script)
    python stats.py --all                 # every game in the archive, including the first 20
    python stats.py --since 2026-11-15    # only games on/after this local date
    python stats.py --min-duration 600 --min-champ-games 3 --min-augment-games 2
    python stats.py --no-fetch            # never touch the network

Output: console report + stats.xlsx (Summary tab with every TL;DR, then one tab per table)

Method notes:
  * All four of you play every game, so a pooled win rate is 50% by construction; breakdowns are per player.
  * Session = games less than --session-gap minutes apart (end of one to start of next).
  * Hours are --tz local time (default Europe/Lisbon) from game start. KDA = (K + A) / max(D, 1).
  * Time dead is ESTIMATED (the client does not store it); time alive is exact.
  * Champion / augment names come from caches and are only re-downloaded when an ID is missing.
"""
import argparse
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean


PLAYERS = ["GRCX", "BabyGolem", "RikiPlancha12", "ragnarneves"]
LOOKUP = {p.lower(): p for p in PLAYERS}
SIDE = {100: "Blue", 200: "Red"}
CHAMP_CACHE, AUG_CACHE = "champions_cache.json", "augments_cache.json"
DDRAGON = "https://ddragon.leagueoflegends.com"
AUG_URL = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/cherry-augments.json"
BASE_RESPAWN = [10, 10, 12, 12, 14, 16, 20, 25, 28, 32.5, 35, 37.5, 40, 42.5, 45, 47.5, 50, 52.5]
SECTIONS, ARGS = [], None


# ----------------------------------------------------------------------------- helpers
def canon(name):
    n = (name or "?").split("#")[0]
    return LOOKUP.get(n.lower(), n)


def order(p):
    return PLAYERS.index(p) if p in PLAYERS else 99


def div(a, b):
    return a / b if b else 0.0


def pct(x):
    return f"{x * 100:.1f}%"


def mmss(sec):
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def fetch_json(url):
    import requests
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def champion_names(need, fetch):
    cache = {int(k): v for k, v in (read_json(CHAMP_CACHE) or {}).items()}
    if fetch and need - cache.keys():
        try:
            ver = fetch_json(f"{DDRAGON}/api/versions.json")[0]
            data = fetch_json(f"{DDRAGON}/cdn/{ver}/data/en_US/champion.json")["data"]
            cache = {int(c["key"]): c["name"] for c in data.values()}
            write_json(CHAMP_CACHE, cache)
        except Exception as e:
            print(f"(could not refresh champion names: {e})")
    return cache


def augment_names(need, fetch):
    raw = read_json(AUG_CACHE) or []

    def parse(data):
        if isinstance(data, dict):
            data = data.get("augments") or data.get("data") or list(data.values())
        out = {}
        for a in data:
            if isinstance(a, dict) and "id" in a:
                name = ((a.get("nameTRA") or a.get("name") or "").strip() or a.get("augmentNameId") or f"Augment {a['id']}")
                out[int(a["id"])] = (name, re.sub(r"^k(?=[A-Z])", "", str(a.get("rarity", ""))))
        return out

    names = parse(raw)
    if fetch and need - names.keys():
        try:
            raw = fetch_json(AUG_URL)
            write_json(AUG_CACHE, raw)
            names = parse(raw)
        except Exception as e:
            print(f"(could not refresh augment names: {e})")
    return names


def get_tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        print(f"(timezone '{name}' unavailable, using UTC; `pip install tzdata` on Windows)")
        return timezone.utc


def est_dead(deaths, level, dur):
    """Estimated seconds spent dead: deaths spread evenly, level grows linearly, standard respawn timers."""
    def inc(m):
        if m <= 15:
            return 0.0
        if m <= 30:
            return int(2 * (m - 15)) * 0.00425
        if m <= 45:
            return 0.1275 + int(2 * (m - 30)) * 0.003
        return min(0.5, 0.2175 + int(2 * (m - 45)) * 0.0145)
    total = 0.0
    for i in range(deaths):
        f = (i + 0.5) / deaths
        lv = min(18, max(1, round(1 + (max(level, 1) - 1) * f)))
        total += BASE_RESPAWN[lv - 1] * (1 + inc(dur * f / 60))
    return min(total, dur)


# ----------------------------------------------------------------------------- loading
def load_games(path, tz, min_dur, since, gap_min):
    raw = read_json(path)
    if raw is None:
        raise SystemExit(f"Could not read {path}. Run lcu_archive.py first and keep this file next to stats.py.")
    games = []
    for g in sorted(raw, key=lambda g: g.get("gameCreation", 0)):
        parts, dur = g.get("participants", []), g.get("gameDuration") or 0
        if len(parts) != 4 or dur < min_dur:
            continue
        start = datetime.fromtimestamp(g["gameCreation"] / 1000, tz)
        if since and start.date() < since:
            continue
        ident = {i["participantId"]: i.get("player", {}) for i in g.get("participantIdentities", [])}
        fb = {t["teamId"]: bool(t.get("firstBlood")) for t in g.get("teams", [])}
        recs = []
        for p in parts:
            st, pl = p.get("stats", {}), ident.get(p["participantId"], {})
            d = st.get("deaths", 0)
            recs.append({
                "gid": g["gameId"], "start": g["gameCreation"] / 1000, "dt": start, "dur": dur,
                "player": canon(pl.get("gameName") or pl.get("summonerName")), "cid": p.get("championId"),
                "team": p["teamId"], "side": SIDE.get(p["teamId"], str(p["teamId"])), "win": bool(st.get("win")),
                "k": st.get("kills", 0), "d": d, "a": st.get("assists", 0),
                "dmg": st.get("totalDamageDealtToChampions", 0), "taken": st.get("totalDamageTaken", 0),
                "mit": st.get("damageSelfMitigated", 0), "gold": st.get("goldEarned", 0),
                "lvl": st.get("champLevel", 1), "alive": st.get("longestTimeSpentLiving", 0),
                "multi": [st.get(k, 0) for k in ("doubleKills", "tripleKills", "quadraKills", "pentaKills")],
                "fbk": bool(st.get("firstBloodKill")), "fba": bool(st.get("firstBloodAssist")),
                "team_fb": fb.get(p["teamId"], False),
                "augs": [st[k] for k in (f"playerAugment{i}" for i in range(1, 7)) if st.get(k)],
            })
        for r in recs:
            mates = [m for m in recs if m["team"] == r["team"]]
            r["opps"] = [m for m in recs if m["team"] != r["team"]]
            for key in ("k", "d", "dmg", "taken"):
                r["team_" + key] = sum(m[key] for m in mates)
            r["share"] = div(r["d"], r["team_d"])
            r["kda"] = (r["k"] + r["a"]) / max(r["d"], 1)
            r["dead"] = est_dead(r["d"], r["lvl"], dur)
        games.append(recs)
    sess, n, prev_end = 0, 0, None
    for recs in games:
        if prev_end is None or (recs[0]["start"] - prev_end) / 60 > gap_min:
            sess, n = sess + 1, 0
        n += 1
        prev_end = recs[0]["start"] + recs[0]["dur"]
        for r in recs:
            r["session"], r["sess_n"] = sess, n
    return games


def by_player(games):
    d = defaultdict(list)
    for recs in games:
        for r in recs:
            d[r["player"]].append(r)
    return dict(sorted(d.items(), key=lambda x: order(x[0])))


class Tally:
    def __init__(self):
        self.w = self.l = 0
        self.sw, self.sl = {"Blue": 0, "Red": 0}, {"Blue": 0, "Red": 0}

    def add(self, win, side=None):
        self.w, self.l = self.w + win, self.l + (not win)
        if side in self.sw:
            (self.sw if win else self.sl)[side] += 1

    n = property(lambda s: s.w + s.l)
    rate = property(lambda s: div(s.w, s.w + s.l))

    def wl(self):
        return f"{self.w}-{self.l}"

    def side(self, s):
        g = self.sw[s] + self.sl[s]
        return self.sw[s] / g if g else None


def tally(recs):
    t = Tally()
    for r in recs:
        t.add(r["win"], r["side"])
    return t


def section(title, tldr, cols, rows):
    SECTIONS.append({"title": title, "tldr": tldr, "cols": cols, "rows": rows})


def team_groups(recs):
    teams = defaultdict(list)
    for r in recs:
        teams[r["team"]].append(r)
    return teams.values()


def duo_key(mem):
    return " + ".join(sorted((m["player"] for m in mem), key=order))


# ----------------------------------------------------------------------------- sections
def sec_overview(games, pp):
    cols = [("Games", "int"), ("Wins", "int"), ("Losses", "int"), ("Win rate", "pct"),
            ("Blue", "pct"), ("Blue W-L", "text"), ("Red", "pct"), ("Red W-L", "text")]

    def row(name, t):
        return [name, t.n, t.w, t.l, t.rate, t.side("Blue"), f"{t.sw['Blue']}-{t.sl['Blue']}",
                t.side("Red"), f"{t.sw['Red']}-{t.sl['Red']}"]

    ps = sorted(((p, tally(r)) for p, r in pp.items()), key=lambda x: (-x[1].rate, order(x[0])))
    n = len(games)
    se = 100 * 0.5 / max(n, 1) ** 0.5
    section("Player win rates",
            f"{ps[0][0]} leads at {pct(ps[0][1].rate)}, {ps[-1][0]} trails at {pct(ps[-1][1].rate)}, "
            f"on {n} games. One standard error is about {se:.0f} points.",
            [("Player", "text")] + cols, [row(p, t) for p, t in ps])

    duos, champs = defaultdict(Tally), defaultdict(Tally)
    for recs in games:
        for mem in team_groups(recs):
            duos[duo_key(mem)].add(mem[0]["win"], mem[0]["side"])
    for recs in pp.values():
        for r in recs:
            champs[r["champ"]].add(r["win"], r["side"])
    ds = sorted(duos.items(), key=lambda x: (-x[1].rate, -x[1].n))
    section("Duo win rates",
            f"Best pairing: {ds[0][0]} at {pct(ds[0][1].rate)} ({ds[0][1].wl()}). Worst: {ds[-1][0]} at "
            f"{pct(ds[-1][1].rate)} ({ds[-1][1].wl()}). Every duo has few games, so none of this is settled.",
            [("Duo", "text")] + cols, [row(d, t) for d, t in ds])
    cs = sorted(champs.items(), key=lambda x: (-x[1].n, -x[1].rate, x[0]))
    top = sorted((c for c in cs if c[1].n >= ARGS.min_champ_games), key=lambda x: (-x[1].rate, -x[1].n, x[0]))
    txt = ", ".join(f"{c} {pct(t.rate)} ({t.wl()})" for c, t in top[:3]) or "none yet"
    section("Champion win rates",
            f"{len(champs)} distinct champions in {n * 4} player-games. Best with >= {ARGS.min_champ_games} games: {txt}.",
            [("Champion", "text")] + cols, [row(c, t) for c, t in cs])


def bucket_table(label, buckets, pp):
    cols, rows, tl = [(label, "text"), ("Games", "int")], [], {}
    for p in pp:
        cols += [(f"{p} W-L", "text"), (f"{p} %", "pct")]
    for name, pred in buckets:
        gids, cells, tl[name] = set(), [], {}
        for p in pp:
            sel = [r for r in pp[p] if pred(r)]
            t = tl[name][p] = tally(sel)
            gids |= {r["gid"] for r in sel}
            cells += [t.wl() if t.n else "-", t.rate if t.n else None]
        rows.append([name, len(gids)] + cells)
    return cols, rows, tl


def sec_tilt(pp):
    cols = [("Player", "text"), ("After win: games", "int"), ("After win %", "pct"),
            ("After loss: games", "int"), ("After loss %", "pct"), ("Delta (loss - win)", "pct")]
    rows, aw, al = [], Tally(), Tally()
    for p, recs in pp.items():
        tw, tl = Tally(), Tally()
        for prev, cur in zip(recs, recs[1:]):
            if cur["session"] == prev["session"]:
                (tw if prev["win"] else tl).add(cur["win"])
        aw.w, aw.l, al.w, al.l = aw.w + tw.w, aw.l + tw.l, al.w + tl.w, al.l + tl.l
        rows.append([p, tw.n, tw.rate if tw.n else None, tl.n, tl.rate if tl.n else None,
                     tl.rate - tw.rate if tw.n and tl.n else None])
    rows.append(["All players", aw.n, aw.rate if aw.n else None, al.n, al.rate if al.n else None,
                 al.rate - aw.rate if aw.n and al.n else None])
    diff = (al.rate - aw.rate) * 100
    noise = 2 * 100 * (0.25 / max(aw.n, 1) + 0.25 / max(al.n, 1)) ** 0.5
    if diff < -5:
        verdict = "Losses are followed by more losses" + (", but within sampling noise." if abs(diff) < noise else ". Tilt is measurable.")
    elif diff > 5:
        verdict = "Results after a loss are better than after a win. Tilt is absent."
    else:
        verdict = "The previous result has no meaningful effect. Tilt is not supported by the data."
    section("Tilt tracker", f"After a loss: {pct(al.rate)} ({al.n} games). After a win: {pct(aw.rate)} ({aw.n} games). {verdict}", cols, rows)


def sec_session(pp):
    buckets = [(f"Game {i}", lambda r, i=i: r["sess_n"] == i) for i in range(1, 6)] + [("Game 6+", lambda r: r["sess_n"] >= 6)]
    cols, rows, _ = bucket_table("Session game", buckets, pp)
    sessions = len({r["session"] for rs in pp.values() for r in rs})
    longest = max(r["sess_n"] for rs in pp.values() for r in rs)
    drops = []
    for p, rs in pp.items():
        e, l = tally([r for r in rs if r["sess_n"] <= 3]), tally([r for r in rs if r["sess_n"] >= 4])
        if e.n and l.n:
            drops.append((l.rate - e.rate, p, e.rate, l.rate))
    extra = f" Biggest drop from games 1-3 to 4+: {min(drops)[1]} ({pct(min(drops)[2])} to {pct(min(drops)[3])})." if drops else ""
    section("Session fatigue", f"{sessions} session{'s' if sessions != 1 else ''}, longest {longest} games. Late-session samples are small and biased.{extra}", cols, rows)


def sec_hour(pp):
    hours = sorted({r["dt"].hour for rs in pp.values() for r in rs})
    hours = [h for h in hours if h >= 12] + [h for h in hours if h < 12]
    cols, rows, _ = bucket_table("Hour (start)", [(f"{h:02d}:00", lambda r, h=h: r["dt"].hour == h) for h in hours], pp)
    late = lambda r: r["dt"].hour < 6
    gids = lambda f: len({r["gid"] for rs in pp.values() for r in rs if f(r)})
    section("Time of day",
            f"{gids(late)} of {gids(lambda r: True)} games began between 00:00 and 05:59.", cols, rows)


def sec_length(games, pp):
    buckets = [("Short (< 12 min)", lambda r: r["dur"] < 720), ("Mid (12-20 min)", lambda r: 720 <= r["dur"] < 1200),
               ("Long (20+ min)", lambda r: r["dur"] >= 1200)]
    cols, rows, tl = bucket_table("Game length", buckets, pp)
    extra = ""
    for label, key in (("short", "Short (< 12 min)"), ("long", "Long (20+ min)")):
        ok = [(p, t) for p, t in tl[key].items() if t.n]
        if ok:
            b = max(ok, key=lambda x: x[1].rate)
            extra += f" Best in {label} games: {b[0]} ({pct(b[1].rate)})."
    section("Game length buckets", f"Short: {rows[0][1]} games, mid: {rows[1][1]}, long: {rows[2][1]}.{extra}", cols, rows)

    cols = [("Player", "text"), ("Avg win length", "text"), ("Avg loss length", "text"), ("Loss - win (s)", "int"),
            ("Shortest win", "text"), ("Longest game", "text"), ("Longest game result", "text")]
    rows, diffs = [], []
    for p, rs in pp.items():
        w, l = [r for r in rs if r["win"]], [r for r in rs if not r["win"]]
        aw, al = (mean(r["dur"] for r in w) if w else None), (mean(r["dur"] for r in l) if l else None)
        lg = max(rs, key=lambda r: r["dur"])
        rows.append([p, mmss(aw) if w else "-", mmss(al) if l else "-", round(al - aw) if w and l else None,
                     mmss(min(r["dur"] for r in w)) if w else "-", mmss(lg["dur"]), "Win" if lg["win"] else "Loss"])
        if w and l:
            diffs.append((al - aw, p))
    avg_all = mean(g[0]["dur"] for g in games)
    sw, lg = min(games, key=lambda g: g[0]["dur"]), max(games, key=lambda g: g[0]["dur"])
    win = lambda g: " + ".join(sorted((r["player"] for r in g if r["win"]), key=order))
    d = max(diffs) if diffs else None
    section("Stomp vs throw",
            f"Average game {mmss(avg_all)}; shortest {mmss(sw[0]['dur'])} (won by {win(sw)}), longest {mmss(lg[0]['dur'])} "
            f"(won by {win(lg)})." + (f" Widest loss-vs-win length gap: {d[1]} ({d[0]:+.0f} s)." if d else ""), cols, rows)


def sec_multikill_fb(games, pp):
    cols = [("Player", "text"), ("Doubles", "int"), ("Triples", "int"), ("Quadras", "int"), ("Pentas", "int"),
            ("Total", "int"), ("Per game", "f2")]
    rows = []
    for p, rs in pp.items():
        s = [sum(r["multi"][i] for r in rs) for i in range(4)]
        rows.append([p] + s + [sum(s), div(sum(s), len(rs))])
    tot = [sum(r[i] for r in rows) for i in range(1, 6)]
    rows.append(["Group"] + tot + [div(tot[4], sum(len(v) for v in pp.values()))])
    lead = max(rows[:-1], key=lambda r: r[5])
    section("Multikills", f"{lead[0]} leads with {lead[5]} multikills. Group: {tot[0]} doubles, {tot[1]} triples, {tot[2]} quadras, {tot[3]} pentas.", cols, rows)

    cols = [("Player", "text"), ("Games", "int"), ("FB kills", "int"), ("FB kill rate", "pct"), ("FB assists", "int"),
            ("Team got FB", "int"), ("Win % with FB", "pct"), ("Win % without FB", "pct")]
    rows = []
    for p, rs in pp.items():
        w, wo = [r for r in rs if r["team_fb"]], [r for r in rs if not r["team_fb"]]
        rows.append([p, len(rs), sum(r["fbk"] for r in rs), div(sum(r["fbk"] for r in rs), len(rs)),
                     sum(r["fba"] for r in rs), len(w), tally(w).rate if w else None, tally(wo).rate if wo else None])
    tf, tn = Tally(), Tally()
    for recs in games:
        for mem in team_groups(recs):
            (tf if mem[0]["team_fb"] else tn).add(mem[0]["win"])
    rows.append(["Team-level", tf.n + tn.n, None, None, None, tf.n, tf.rate if tf.n else None, tn.rate if tn.n else None])
    top = max(rows[:-1], key=lambda r: r[2])
    section("First blood",
            f"Teams with first blood won {pct(tf.rate)} ({tf.w}-{tf.l}); without it {pct(tn.rate)} ({tn.w}-{tn.l}). "
            f"Most first-blood kills: {top[0]} ({top[2]}).", cols, rows)


def sec_combat(pp):
    cols = [("Player", "text"), ("Games", "int"), ("Minutes", "f1"), ("Kills/min", "f2"), ("Deaths/min", "f2"),
            ("Assists/min", "f2"), ("KDA", "f2"), ("Assists per kill", "f2"), ("Damage / death", "int"),
            ("Damage / gold", "f2"), ("Damage / kill", "int"), ("Avg K/D/A", "text")]
    rows = []
    for p, rs in pp.items():
        m = sum(r["dur"] for r in rs) / 60
        k, d, a, dmg, g = (sum(r[x] for r in rs) for x in ("k", "d", "a", "dmg", "gold"))
        rows.append([p, len(rs), m, div(k, m), div(d, m), div(a, m), (k + a) / max(d, 1), div(a, k),
                     div(dmg, max(d, 1)), div(dmg, g), div(dmg, k) if k else None,
                     f"{k / len(rs):.1f}/{d / len(rs):.1f}/{a / len(rs):.1f}"])
    hk, hd, ld = max(rows, key=lambda r: r[3]), max(rows, key=lambda r: r[4]), min(rows, key=lambda r: r[4])
    ks = min((r for r in rows if r[10]), key=lambda r: r[10], default=None)
    section("Combat profile",
            f"Highest kill pace: {hk[0]} ({hk[3]:.2f}/min). Highest death pace: {hd[0]} ({hd[4]:.2f}/min), lowest: {ld[0]} ({ld[4]:.2f}/min)."
            + (f" Lowest damage per kill (kill-steal index): {ks[0]} ({ks[10]:,.0f})." if ks else ""), cols, rows)


def sec_team_share(pp):
    cols = [("Player", "text"), ("Avg damage share", "pct"), ("Avg kill participation", "pct"), ("Damage/game", "int"),
            ("Taken/game", "int"), ("Taken/min", "int"), ("Taken/death", "int"), ("Mitigated/game", "int"),
            ("Share of team damage taken", "pct"), ("Verdict", "text")]
    rows = []
    for p, rs in pp.items():
        share = mean(div(r["dmg"], r["team_dmg"]) for r in rs)
        kp = mean(div(r["k"] + r["a"], r["team_k"]) for r in rs if r["team_k"]) if any(r["team_k"] for r in rs) else None
        taken, mins, deaths = sum(r["taken"] for r in rs), sum(r["dur"] for r in rs) / 60, sum(r["d"] for r in rs)
        rows.append([p, share, kp, mean(r["dmg"] for r in rs), taken / len(rs), taken / mins, div(taken, deaths) if deaths else None,
                     mean(r["mit"] for r in rs), mean(div(r["taken"], r["team_taken"]) for r in rs),
                     "Carrying" if share >= 0.55 else "Being backpacked" if share <= 0.45 else "Even split"])
    rows.sort(key=lambda r: -r[1])
    bag = max(rows, key=lambda r: r[4])
    section("Team share", f"{rows[0][0]} takes the largest damage share ({pct(rows[0][1])}); {bag[0]} is the biggest punching bag ({bag[4]:,.0f} damage taken per game).", cols, rows)


def sec_pool(pp):
    mg = ARGS.min_champ_games
    cols = [("Player", "text"), ("Most played", "text"), ("Games on it", "int"), ("Win % on it", "pct"), ("Unique champs", "int"),
            ("Games", "int"), ("Unique / games", "pct"), (f"Best (min {mg})", "text"), (f"Worst (min {mg})", "text")]
    rows = []
    for p, rs in pp.items():
        cs = defaultdict(list)
        for r in rs:
            cs[r["champ"]].append(r)
        mp = max(cs.items(), key=lambda x: (len(x[1]), x[0]))
        elig = [(c, tally(v)) for c, v in cs.items() if len(v) >= mg]
        bt = wt = "-"
        if elig:
            b, w = max(elig, key=lambda x: (x[1].rate, x[1].n)), min(elig, key=lambda x: (x[1].rate, -x[1].n))
            bt = f"{b[0]} {pct(b[1].rate)} ({b[1].wl()})"
            wt = f"{w[0]} {pct(w[1].rate)} ({w[1].wl()})" if len(elig) > 1 else "-"
        rows.append([p, mp[0], len(mp[1]), tally(mp[1]).rate, len(cs), len(rs), len(cs) / len(rs), bt, wt])
    wide, narrow = max(rows, key=lambda r: r[6]), min(rows, key=lambda r: r[6])
    section("Champion pool", f"Widest pool: {wide[0]} ({wide[4]} champions in {wide[5]} games). Narrowest: {narrow[0]} (favourite {narrow[1]} x{narrow[2]}).", cols, rows)

    cs = defaultdict(list)
    for rs in pp.values():
        for r in rs:
            cs[r["champ"]].append(r)
    cols = [("Champion", "text"), ("Games", "int"), ("Minutes", "f1"), ("Deaths", "int"), ("Deaths/min", "f2"),
            ("Win rate", "pct"), ("Played by", "text")]
    rows = []
    for c, v in cs.items():
        if len(v) >= mg:
            m, d = sum(r["dur"] for r in v) / 60, sum(r["d"] for r in v)
            rows.append([c, len(v), m, d, div(d, m), tally(v).rate, ", ".join(sorted({r["player"] for r in v}, key=order))])
    rows.sort(key=lambda r: -r[4])
    txt = (f"Highest deaths per minute: {rows[0][0]} ({rows[0][4]:.2f}), then " + ", ".join(f"{r[0]} ({r[4]:.2f})" for r in rows[1:3])
           + f". Min {mg} games.") if rows else f"No champion has {mg}+ games yet."
    section("Liability index", txt, cols, rows)


def sec_records(games, pp):
    allr = [r for rs in pp.values() for r in rs]
    when = lambda r: r["dt"].strftime("%Y-%m-%d %H:%M")
    kdas = lambda r: f"{r['k']}/{r['d']}/{r['a']}"
    wins, losses = [r for r in allr if r["win"]], [r for r in allr if not r["win"]]
    recs = [("Highest single-game damage", max(allr, key=lambda r: r["dmg"]), "dmg"),
            ("Most kills in one game", max(allr, key=lambda r: r["k"]), "k"),
            ("Most deaths in one game", max(allr, key=lambda r: r["d"]), "d"),
            ("Most damage taken in one game", max(allr, key=lambda r: r["taken"]), "taken")]
    if wins:
        recs += [("Lowest-damage win", min(wins, key=lambda r: r["dmg"]), "dmg"),
                 ("Lowest-KDA win", min(wins, key=lambda r: (r["kda"], -r["d"])), "kda")]
    if losses:
        recs.append(("Highest-damage loss", max(losses, key=lambda r: r["dmg"]), "dmg"))
    rows = [[lab, r["player"], f"{r[k]:.2f}" if k == "kda" else f"{r[k]:,}", kdas(r), r["champ"], mmss(r["dur"]), when(r)]
            for lab, r, k in recs]
    section("Records", "Single-game extremes across all four players (ties go to the earliest game).",
            [("Record", "text"), ("Player", "text"), ("Value", "text"), ("K/D/A", "text"), ("Champion", "text"),
             ("Length", "text"), ("Date", "text")], rows)

    if losses:
        top = sorted(losses, key=lambda r: (-r["d"], -r["share"]))[:5]
        section("Worst defeats",
                f"Most deaths in a loss: {top[0]['player']} on {top[0]['champ']}, {top[0]['d']} deaths "
                f"({pct(top[0]['share'])} of the team's deaths).",
                [("Rank", "int"), ("Player", "text"), ("Champion", "text"), ("Deaths", "int"), ("Team death share", "pct"),
                 ("K/D/A", "text"), ("Length", "text"), ("Date", "text")],
                [[i + 1, r["player"], r["champ"], r["d"], r["share"], kdas(r), mmss(r["dur"]), when(r)] for i, r in enumerate(top)])

    rows = []
    for p, rs in pp.items():
        w = [r for r in rs if r["win"]]
        dw = sum(r["d"] == 0 for r in w)
        rows.append([p, dw, len(w), div(dw, len(w)), sum(r["d"] == 0 for r in rs)])
    tot = sum(r[1] for r in rows)
    section("Clean execution",
            f"{tot} deathless wins across {len(allr)} player-games." + (f" {max(rows, key=lambda r: r[1])[0]} holds the most." if tot else ""),
            [("Player", "text"), ("Deathless wins", "int"), ("Total wins", "int"), ("% of wins deathless", "pct"),
             ("Deathless games (any result)", "int")], rows)


def sec_time_dead(pp):
    cols = [("Player", "text"), ("Games", "int"), ("Playtime (min)", "f1"), ("Deaths", "int"), ("Est. time dead (min)", "f1"),
            ("Est. % of playtime dead", "pct"), ("Longest alive (min)", "f1"), ("Avg longest alive (min)", "f1"), ("Record game", "text")]
    rows = []
    for p, rs in pp.items():
        play, dead = sum(r["dur"] for r in rs), sum(r["dead"] for r in rs)
        best = max(rs, key=lambda r: r["alive"])
        rows.append([p, len(rs), play / 60, sum(r["d"] for r in rs), dead / 60, div(dead, play), best["alive"] / 60,
                     mean(r["alive"] for r in rs) / 60, f"{best['dt']:%Y-%m-%d} {best['champ']}"])
    rows.sort(key=lambda r: -r[5])
    section("Time dead", f"{rows[0][0]} spends the most estimated time dead ({pct(rows[0][5])} of playtime). Dead time is estimated; alive time is exact.", cols, rows)


def sec_h2h_streaks(games, pp):
    names, t = list(pp), defaultdict(lambda: [0, 0])
    for recs in games:
        for a in recs:
            for b in a["opps"]:
                t[(a["player"], b["player"])][0 if a["win"] else 1] += 1
    rows = []
    for a in names:
        cells, rated = [], []
        for b in names:
            w, l = t.get((a, b), [0, 0])
            if a == b or not w + l:
                cells.append("-")
                continue
            cells.append(f"{w / (w + l) * 100:.1f}% ({w}-{l})")
            rated.append((w / (w + l), w + l, b))
        rows.append([a] + cells + [min(rated, key=lambda x: (x[0], -x[1]))[2] if rated else "-",
                                   max(rated, key=lambda x: (x[0], x[1]))[2] if rated else "-"])
    section("Head to head", "Row player's win rate (W-L) in games where the column player was on the OTHER team.",
            [("Player vs", "text")] + [(n, "text") for n in names] + [("Nemesis", "text"), ("Favourite victim", "text")], rows)

    def streaks(results):
        bw = bl = run = 0
        last = None
        for w in results:
            run = run + 1 if w == last else 1
            last = w
            bw, bl = (max(bw, run), bl) if w else (bw, max(bl, run))
        return bw, bl, (f"{'W' if last else 'L'}{run}" if results else "-")

    per, duo = defaultdict(list), defaultdict(list)
    for recs in games:
        for r in recs:
            per[r["player"]].append(r["win"])
        for mem in team_groups(recs):
            duo[duo_key(mem)].append(mem[0]["win"])
    rows = [[n, len(per[n]), *streaks(per[n])] for n in names]
    rows += [[k, len(v), *streaks(v)] for k, v in sorted(duo.items(), key=lambda x: -len(x[1]))]
    best = max(rows[:len(names)], key=lambda r: r[2])
    section("Streaks", f"Longest win streak: {best[0]} ({best[2]}). Streaks run across sessions, in chronological order.",
            [("Player / Duo", "text"), ("Games", "int"), ("Longest win streak", "int"), ("Longest loss streak", "int"),
             ("Current streak", "text")], rows)


def sec_augments(pp, aug_names):
    t = defaultdict(Tally)
    for rs in pp.values():
        for r in rs:
            for a in set(r["augs"]):
                t[a].add(r["win"])
    rows = [[*aug_names.get(a, (f"Augment #{a}", "")), x.n, x.w, x.l, x.rate] for a, x in t.items()]
    rows.sort(key=lambda r: (-r[2], -r[5], r[0]))
    mg = ARGS.min_augment_games
    pool = [r for r in rows if r[2] >= mg]
    best = sorted(pool, key=lambda r: (-r[5], -r[2], r[0]))[:3]
    worst = sorted(pool, key=lambda r: (r[5], -r[2], r[0]))[:3]
    fmt = lambda lst: ", ".join(f"{r[0]} {pct(r[5])} ({r[3]}-{r[4]})" for r in lst) or "none"
    section("Augments",
            f"{len(rows)} distinct augments picked. Best with >= {mg} games: {fmt(best)}. Worst: {fmt(worst)}.",
            [("Augment", "text"), ("Rarity", "text"), ("Games", "int"), ("Wins", "int"), ("Losses", "int"), ("Win rate", "pct")], rows)


# ----------------------------------------------------------------------------- output
def fmt(v, kind):
    if v is None:
        return "-"
    return {"pct": lambda: f"{v * 100:.1f}%", "int": lambda: f"{v:,.0f}", "f1": lambda: f"{v:,.1f}",
            "f2": lambda: f"{v:.2f}"}.get(kind, lambda: str(v))()


def print_sections():
    for s in SECTIONS:
        print(f"\n=== {s['title'].upper()} ===\nTL;DR: {s['tldr']}\n")
        cells = [[fmt(v, k) for v, (_, k) in zip(row, s["cols"])] for row in s["rows"]]
        widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, (h, _) in enumerate(s["cols"])]
        line = lambda vals: "  ".join(v.ljust(w) if i == 0 else v.rjust(w) for i, (v, w) in enumerate(zip(vals, widths)))
        print(line([h for h, _ in s["cols"]]))
        for r in cells[:25]:
            print(line(r))
        if len(cells) > 25:
            print(f"... {len(cells) - 25} more rows in the xlsx")

def _clean(v):
    """Torna valores JSON-safe e arredonda floats."""
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    return v


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def export_to_web(games, pp, an):
    """Exporta TODAS as estatisticas (secoes completas + perfil por jogador + historico de jogos) para stats_export.json."""
    export_data = {
        "totalGames": len(games),
        "totalPlayerGames": sum(len(v) for v in pp.values()),
        "firstGame": games[0][0]["dt"].isoformat(),
        "lastGame": games[-1][0]["dt"].isoformat(),
        "timezone": ARGS.tz,
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "filters": {
            "since": None if ARGS.all else ARGS.since,
            "minDuration": ARGS.min_duration,
            "minChampGames": ARGS.min_champ_games,
            "minAugmentGames": ARGS.min_augment_games,
            "sessionGap": ARGS.session_gap,
        },
        "players": {},
        "sections": [],
        "games": [],
    }

    # ---- perfil agregado por jogador (KPIs, graficos, scorecard)
    for p, rs in pp.items():
        t = tally(rs)
        gp = len(rs)
        mins = sum(r["dur"] for r in rs) / 60
        k, d, a = (sum(r[x] for r in rs) for x in ("k", "d", "a"))
        dmg, taken, gold = (sum(r[x] for r in rs) for x in ("dmg", "taken", "gold"))
        multi = [sum(r["multi"][i] for r in rs) for i in range(4)]
        wins = [r for r in rs if r["win"]]
        dead = sum(r["dead"] for r in rs)
        play = sum(r["dur"] for r in rs)
        best_alive = max(rs, key=lambda r: r["alive"])

        champs = defaultdict(Tally)
        for r in rs:
            champs[r["champ"]].add(r["win"])
        champ_list = [
            {"champion": c, "games": x.n, "wins": x.w, "losses": x.l, "winRate": x.rate * 100}
            for c, x in sorted(champs.items(), key=lambda x: (-x[1].n, -x[1].rate, x[0]))
        ]
        kp_vals = [div(r["k"] + r["a"], r["team_k"]) for r in rs if r["team_k"]]

        export_data["players"][p.lower()] = {
            "displayName": p,
            "games": gp,
            "wins": t.w,
            "losses": t.l,
            "winRate": t.rate * 100,
            "blueWins": t.sw["Blue"],
            "blueLosses": t.sl["Blue"],
            "redWins": t.sw["Red"],
            "redLosses": t.sl["Red"],
            "totalKills": k, "totalDeaths": d, "totalAssists": a,
            "avgKills": div(k, gp), "avgDeaths": div(d, gp), "avgAssists": div(a, gp),
            "kda": (k + a) / max(d, 1),
            "killsPerMin": div(k, mins), "deathsPerMin": div(d, mins), "assistsPerMin": div(a, mins),
            "avgDamage": div(dmg, gp), "avgTaken": div(taken, gp), "avgGold": div(gold, gp),
            "avgMitigated": mean(r["mit"] for r in rs),
            "damagePerMin": div(dmg, mins), "goldPerMin": div(gold, mins),
            "damagePerDeath": div(dmg, max(d, 1)), "damagePerGold": div(dmg, gold),
            "damageShare": mean(div(r["dmg"], r["team_dmg"]) for r in rs) * 100,
            "killParticipation": (mean(kp_vals) * 100) if kp_vals else None,
            "takenShare": mean(div(r["taken"], r["team_taken"]) for r in rs) * 100,
            "deathShare": mean(r["share"] for r in rs) * 100,
            "avgLevel": mean(r["lvl"] for r in rs),
            "playtimeMin": mins,
            "avgGameMin": mins / gp,
            "multikills": {"double": multi[0], "triple": multi[1], "quadra": multi[2], "penta": multi[3]},
            "firstBloodKills": sum(r["fbk"] for r in rs),
            "firstBloodAssists": sum(r["fba"] for r in rs),
            "deathlessGames": sum(r["d"] == 0 for r in rs),
            "deathlessWins": sum(r["d"] == 0 for r in wins),
            "estDeadMin": dead / 60,
            "estDeadPct": div(dead, play) * 100,
            "longestAliveMin": best_alive["alive"] / 60,
            "avgLongestAliveMin": mean(r["alive"] for r in rs) / 60,
            "uniqueChampions": len(champs),
            "champions": champ_list,
        }

    # ---- TODAS as seccoes do relatorio (as mesmas que vao para o xlsx / consola)
    for s in SECTIONS:
        export_data["sections"].append({
            "id": _slug(s["title"]),
            "title": s["title"],
            "tldr": s["tldr"],
            "columns": [{"label": h, "kind": kind} for h, kind in s["cols"]],
            "rows": _clean(s["rows"]),
        })

    # ---- historico completo de jogos (do mais recente para o mais antigo)
    for recs in reversed(games):
        first = recs[0]
        winner = next((r["side"] for r in recs if r["win"]), None)
        export_data["games"].append({
            "id": first["gid"],
            "date": first["dt"].isoformat(),
            "durationSec": first["dur"],
            "session": first["session"],
            "sessionGame": first["sess_n"],
            "winnerSide": winner,
            "teamFirstBlood": [s for s in ("Blue", "Red") if any(r["team_fb"] and r["side"] == s for r in recs)],
            "players": [
                {
                    "name": r["player"], "champion": r["champ"], "side": r["side"], "win": r["win"],
                    "kills": r["k"], "deaths": r["d"], "assists": r["a"], "kda": round(r["kda"], 2),
                    "damage": r["dmg"], "taken": r["taken"], "mitigated": r["mit"], "gold": r["gold"],
                    "level": r["lvl"], "longestAliveSec": r["alive"],
                    "estDeadSec": round(r["dead"], 1),
                    "multikills": r["multi"],
                    "firstBloodKill": r["fbk"], "firstBloodAssist": r["fba"],
                    "augments": [an.get(x, (f"Augment #{x}", ""))[0] for x in r["augs"]],
                }
                for r in sorted(recs, key=lambda r: (r["team"], order(r["player"])))
            ],
        })

    with open("stats_export.json", "w", encoding="utf-8") as f:
        json.dump(_clean(export_data), f, ensure_ascii=False, indent=1, default=str)
    print(f"Ficheiro 'stats_export.json' gerado: {len(export_data['sections'])} seccoes, "
          f"{len(export_data['players'])} jogadores, {len(export_data['games'])} jogos.")


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default="customs_archive.json")
    ap.add_argument("--since", default="2026-11-08", help="first local date counted (YYYY-MM-DD); default skips the first 20 games")
    ap.add_argument("--all", action="store_true", help="include every game in the archive")
    ap.add_argument("--min-duration", type=int, default=0, help="ignore games shorter than this many seconds")
    ap.add_argument("--min-champ-games", type=int, default=2)
    ap.add_argument("--min-augment-games", type=int, default=3)
    ap.add_argument("--session-gap", type=int, default=60, help="minutes between games that start a new session")
    ap.add_argument("--tz", default="Europe/Lisbon")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--out", default="stats.xlsx")
    ARGS = ap.parse_args()

    since = None if ARGS.all else datetime.strptime(ARGS.since, "%Y-%m-%d").date()
    games = load_games(ARGS.archive, get_tz(ARGS.tz), ARGS.min_duration, since, ARGS.session_gap)
    if not games:
        raise SystemExit("No games match. Try --all or an earlier --since date.")
    fetch = not ARGS.no_fetch
    cn = champion_names({r["cid"] for g in games for r in g}, fetch)
    an = augment_names({a for g in games for r in g for a in r["augs"]}, fetch)
    for g in games:
        for r in g:
            r["champ"] = cn.get(r["cid"], f"Champion {r['cid']}")
    pp = by_player(games)
    print(f"\n{len(games)} games analysed ({games[0][0]['dt']:%Y-%m-%d} to {games[-1][0]['dt']:%Y-%m-%d}, {ARGS.tz})")

    sec_overview(games, pp)
    sec_tilt(pp)
    sec_session(pp)
    sec_hour(pp)
    sec_length(games, pp)
    sec_multikill_fb(games, pp)
    sec_combat(pp)
    sec_team_share(pp)
    sec_pool(pp)
    sec_records(games, pp)
    sec_time_dead(pp)
    sec_h2h_streaks(games, pp)
    sec_augments(pp, an)
    print_sections()
    export_to_web(games, pp, an)

if __name__ == "__main__":
    main()