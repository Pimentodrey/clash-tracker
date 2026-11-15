"""
Collecteur Clash Tracker — lancé toutes les heures par GitHub Actions.
Récupère le clan, ses membres, la guerre, les classements France,
et écrit docs/data/latest.json + docs/data/history.json.

Variables d'environnement :
  CR_TOKEN   (obligatoire) clé API developer.clashroyale.com, IP autorisée 45.79.218.79
  CR_BASE    (optionnel)   défaut https://proxy.royaleapi.dev/v1
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")
BASE = os.environ.get("CR_BASE", "https://proxy.royaleapi.dev/v1").rstrip("/")
TOKEN = os.environ.get("CR_TOKEN", "").strip()


def tag(t):
    return "#" + t.strip().upper().lstrip("#").replace("O", "0")


def enc(t):
    return urllib.parse.quote(tag(t), safe="")


def api(path, tries=3):
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            if e.code in (429, 500, 502, 503) and i < tries - 1:
                time.sleep(2 + 3 * i)
                continue
            raise RuntimeError(f"{path} -> HTTP {e.code} {body}")
        except urllib.error.URLError as e:
            if i < tries - 1:
                time.sleep(2 + 3 * i)
                continue
            raise RuntimeError(f"{path} -> {e}")


def safe(path, default=None):
    try:
        return api(path)
    except Exception as e:
        print("  (ignoré)", e, file=sys.stderr)
        return default


def summarize_battles(log, me_tag):
    """Résumé compact des 25 derniers combats d'un joueur."""
    out = {"w": 0, "l": 0, "last": None, "war": 0, "modes": {}, "decks": {}, "recent": []}
    for b in log or []:
        team, opp = (b.get("team") or [{}])[0], (b.get("opponent") or [{}])[0]
        res = "w" if team.get("crowns", 0) > opp.get("crowns", 0) else "l" if team.get("crowns", 0) < opp.get("crowns", 0) else "d"
        if res == "w":
            out["w"] += 1
        elif res == "l":
            out["l"] += 1
        out["last"] = out["last"] or b.get("battleTime")
        mode = (b.get("gameMode") or {}).get("name") or b.get("type")
        out["modes"][mode] = out["modes"].get(mode, 0) + 1
        if "riverRace" in (b.get("type") or "") or "boatBattle" in (b.get("type") or ""):
            out["war"] += 1
        names = sorted(c["name"] for c in team.get("cards", []))
        if names:
            k = " · ".join(names)
            d = out["decks"].setdefault(k, {"n": 0, "w": 0})
            d["n"] += 1
            d["w"] += res == "w"
        if len(out["recent"]) < 10:
            out["recent"].append({"t": b.get("battleTime"), "m": mode, "r": res, "c": f'{team.get("crowns", 0)}-{opp.get("crowns", 0)}',
                                  "tc": team.get("trophyChange"), "o": opp.get("name")})
    out["decks"] = sorted(({"deck": k, **v} for k, v in out["decks"].items()), key=lambda d: -d["n"])[:3]
    return out


def summarize_player(p):
    cards = p.get("cards") or []
    top = max([c.get("maxLevel", 0) for c in cards] or [0])
    lvl = lambda c: c.get("level", 0) + (top - c.get("maxLevel", top))
    levels = [lvl(c) for c in cards]
    deck = p.get("currentDeck") or []
    return {
        "expLevel": p.get("expLevel"), "trophies": p.get("trophies"), "bestTrophies": p.get("bestTrophies"),
        "wins": p.get("wins"), "losses": p.get("losses"), "threeCrownWins": p.get("threeCrownWins"),
        "warDayWins": p.get("warDayWins"), "totalDonations": p.get("totalDonations"),
        "arena": (p.get("arena") or {}).get("name"), "fav": (p.get("currentFavouriteCard") or {}).get("name"),
        "cards": len(cards), "maxed": sum(c.get("level", 0) >= c.get("maxLevel", 99) for c in cards),
        "avgLevel": round(sum(levels) / len(levels), 1) if levels else None,
        "deck": [{"n": c.get("name"), "l": lvl(c), "e": c.get("elixirCost"), "i": (c.get("iconUrls") or {}).get("medium"),
                  "evo": bool(c.get("evolutionLevel"))} for c in deck],
    }


def main():
    if not TOKEN:
        sys.exit("CR_TOKEN manquant : ajoute ta clé dans Settings > Secrets and variables > Actions.")
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    clan_tag = tag(cfg["clan_tag"])
    now = datetime.now(timezone.utc)
    print("Clan", clan_tag)

    clan = api(f"/clans/{enc(clan_tag)}")
    race = safe(f"/clans/{enc(clan_tag)}/currentriverrace")
    racelog = safe(f"/clans/{enc(clan_tag)}/riverracelog", {"items": []})

    # Classements France
    country = cfg.get("country_code", "FR")
    locs = safe("/locations?limit=300", {"items": []})
    loc = next((l for l in locs.get("items", []) if l.get("countryCode") == country), None)
    rank_clans = rank_war = []
    if loc:
        rank_clans = (safe(f"/locations/{loc['id']}/rankings/clans?limit=200", {"items": []}) or {}).get("items", [])
        rank_war = (safe(f"/locations/{loc['id']}/rankings/clanwars?limit=200", {"items": []}) or {}).get("items", [])

    # Profil + journal de chaque membre (en parallèle, gentiment)
    members = clan.get("memberList") or []
    def fetch_member(m):
        p = safe(f"/players/{enc(m['tag'])}")
        bl = safe(f"/players/{enc(m['tag'])}/battlelog", [])
        return m["tag"], (summarize_player(p) if p else None), summarize_battles(bl, m["tag"])
    with ThreadPoolExecutor(max_workers=5) as ex:
        details = {t: {"profile": pr, "battles": bt} for t, pr, bt in ex.map(fetch_member, members)}

    def slim_rank(items):
        return [{"tag": c.get("tag"), "name": c.get("name"), "rank": c.get("rank"), "prev": c.get("previousRank"),
                 "score": c.get("clanScore"), "members": c.get("members")} for c in items]

    def slim_race_log(items):
        out = []
        for it in items:
            st = next((s for s in it.get("standings", []) if (s.get("clan") or {}).get("tag") == clan_tag), None)
            if not st:
                continue
            c = st["clan"]
            out.append({"season": it.get("seasonId"), "week": it.get("sectionIndex"), "date": it.get("createdDate"),
                        "rank": st.get("rank"), "trophyChange": st.get("trophyChange"), "fame": c.get("fame"),
                        "participants": [{"tag": p["tag"], "name": p.get("name"), "fame": p.get("fame", 0),
                                          "decks": p.get("decksUsed", 0)} for p in c.get("participants", [])]})
        return out

    latest = {
        "updated": now.isoformat(),
        "country": loc and {"id": loc["id"], "name": loc.get("name")},
        "clan": {k: clan.get(k) for k in ("tag", "name", "description", "type", "clanScore", "clanWarTrophies",
                                          "requiredTrophies", "donationsPerWeek", "members")},
        "members": [{**{k: m.get(k) for k in ("tag", "name", "role", "lastSeen", "expLevel", "trophies", "clanRank",
                                                "previousClanRank", "donations", "donationsReceived")},
                     **details.get(m["tag"], {})} for m in members],
        "race": race and {
            "periodType": race.get("periodType"), "sectionIndex": race.get("sectionIndex"),
            "clan": {"fame": (race.get("clan") or {}).get("fame"),
                     "participants": [{"tag": p["tag"], "name": p.get("name"), "fame": p.get("fame", 0),
                                       "decks": p.get("decksUsed", 0), "today": p.get("decksUsedToday", 0),
                                       "boat": p.get("boatAttacks", 0)} for p in (race.get("clan") or {}).get("participants", [])]},
            "clans": [{"tag": c.get("tag"), "name": c.get("name"), "fame": c.get("fame", 0)} for c in race.get("clans", [])],
        },
        "raceLog": slim_race_log(racelog.get("items", [])),
        "rankClans": slim_rank(rank_clans),
        "rankWar": slim_rank(rank_war),
    }
    os.makedirs(DATA, exist_ok=True)
    json.dump(latest, open(os.path.join(DATA, "latest.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))

    # Historique : un relevé par heure sur 7 jours, puis un par jour
    hpath = os.path.join(DATA, "history.json")
    try:
        history = json.load(open(hpath, encoding="utf-8"))
    except Exception:
        history = []
    fr_rank = next((c["rank"] for c in rank_clans if c.get("tag") == clan_tag), None)
    fr_war = next((c["rank"] for c in rank_war if c.get("tag") == clan_tag), None)
    parts = {p["tag"]: p for p in (latest["race"] or {}).get("clan", {}).get("participants", [])} if latest["race"] else {}
    snap = {
        "t": int(now.timestamp()),
        "c": {"s": clan.get("clanScore"), "w": clan.get("clanWarTrophies"), "m": clan.get("members"),
              "d": clan.get("donationsPerWeek"), "fr": fr_rank, "frw": fr_war,
              "fame": (latest["race"] or {}).get("clan", {}).get("fame")},
        "m": {m["tag"]: [m["name"], m.get("trophies"), m.get("donations"), parts.get(m["tag"], {}).get("fame", 0)] for m in members},
    }
    history.append(snap)
    cutoff = snap["t"] - 7 * 86400
    kept, seen_days = [], set()
    for s in reversed(history):
        if s["t"] >= cutoff:
            kept.append(s)
        else:
            day = datetime.fromtimestamp(s["t"], timezone.utc).strftime("%Y-%m-%d")
            if day not in seen_days:
                seen_days.add(day)
                kept.append(s)
    history = list(reversed(kept))[-2000:]
    json.dump(history, open(hpath, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"OK : {len(members)} membres, rang France {fr_rank}, guerre {fr_war}, {len(history)} relevés")


if __name__ == "__main__":
    main()
