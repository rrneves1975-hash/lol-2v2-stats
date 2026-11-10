"""
Saves new 2v2 custom games (GRCX, RikiPlancha12, BabyGolem, ragnarneves) into a permanent archive.
Run it after every session, with the League client open and logged in.

Requirements: Python 3, `pip install requests`
Usage:
    python lcu_archive.py
    python lcu_archive.py --lockfile "D:\\Riot Games\\League of Legends\\lockfile"

The client only serves your last 20 games, so run this before playing 20 more.
Writes customs_archive.json (only ever added to). Then run stats.py for the report.
"""
import argparse
import json
import os
import sys

import requests
import urllib3

urllib3.disable_warnings()

LOCKFILES = [r"C:\Riot Games\League of Legends\lockfile", r"D:\Riot Games\League of Legends\lockfile",
             r"E:\Riot Games\League of Legends\lockfile",
             "/Applications/League of Legends.app/Contents/LoL/lockfile"]
ARCHIVE = "customs_archive.json"
PLAYERS = {"grcx", "rikiplancha12", "babygolem", "ragnarneves"}


class LCU:
    def __init__(self, lockfile):
        with open(lockfile, encoding="utf-8") as f:
            _, _, port, password, _ = f.read().strip().split(":")
        self.base = f"https://127.0.0.1:{port}"
        self.s = requests.Session()
        self.s.auth, self.s.verify = ("riot", password), False

    def get(self, path, **params):
        r = self.s.get(self.base + path, params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code} {path} {r.text[:200]}")
        return r.json()


def is_custom(game):
    return game.get("gameType") == "CUSTOM_GAME" or game.get("queueId") == 0


def is_my_2v2(game):
    if len(game.get("participants", [])) != 4:
        return False
    names = set()
    for i in game.get("participantIdentities", []):
        pl = i.get("player", {})
        names |= {pl[k].lower() for k in ("gameName", "summonerName") if pl.get(k)}
    return PLAYERS <= names


def full_game(lcu, game):
    """List entries can lack participants; load the full game when they do."""
    if len(game.get("participants", [])) >= 4 and game.get("participantIdentities"):
        return game
    try:
        return lcu.get(f"/lol-match-history/v1/games/{game['gameId']}")
    except Exception as e:
        print(f"  could not load game {game.get('gameId')}: {e}")
        return game


def load_archive():
    if not os.path.exists(ARCHIVE):
        print("No archive yet, starting fresh.")
        return {}
    try:
        with open(ARCHIVE, encoding="utf-8") as f:
            return {g["gameId"]: g for g in json.load(f)}
    except Exception as e:  # never overwrite an archive we could not read
        sys.exit(f"Could not read {ARCHIVE} ({e}). Fix or restore it before running again; nothing was changed.")


def save_archive(games):
    tmp = ARCHIVE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(games, f, ensure_ascii=False)
    os.replace(tmp, ARCHIVE)  # atomic: the archive is never left half-written


def merge(archive, recent, lcu):
    """Adds new 2v2 customs from `recent` into `archive`. Returns the added games."""
    added = []
    for g in recent:
        if g["gameId"] in archive or not is_custom(g):
            continue
        g = full_game(lcu, g)
        if is_my_2v2(g):
            archive[g["gameId"]] = g
            added.append(g)
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lockfile", help="path to the League lockfile (auto-detected if omitted)")
    args = ap.parse_args()

    path = args.lockfile or next((p for p in LOCKFILES if os.path.exists(p)), None)
    if not path or not os.path.exists(path):
        sys.exit("Lockfile not found. Is the League client open? Use --lockfile to point to it.")
    lcu = LCU(path)
    me = lcu.get("/lol-summoner/v1/current-summoner")
    print(f"Logged in as {me.get('gameName') or me.get('displayName')}")

    archive = load_archive()
    before = len(archive)
    payload = lcu.get(f"/lol-match-history/v1/products/lol/{me['puuid']}/matches", begIndex=0, endIndex=20)
    recent = payload.get("games", [])
    recent = recent.get("games", []) if isinstance(recent, dict) else recent
    print(f"Client returned {len(recent)} recent games")

    known = {g["gameId"] for g in recent} & set(archive)
    added = merge(archive, recent, lcu)
    if before and len(recent) >= 20 and not known:
        print("WARNING: none of the client's 20 games were already archived. If you played more than 20 games "
              "since the last run, the oldest ones may be gone for good. Run this more often.")
    if added:
        save_archive(sorted(archive.values(), key=lambda g: g.get("gameCreation", 0)))
    print(f"New games added: {len(added)} (archive: {before} -> {len(archive)})")
    for g in sorted(added, key=lambda g: g.get("gameCreation", 0)):
        print(f"  {g.get('gameCreationDate')}  {g.get('gameDuration', 0) // 60} min")


if __name__ == "__main__":
    main()