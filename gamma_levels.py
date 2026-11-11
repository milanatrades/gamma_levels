"""Gamma levels robot.

Downloads the delayed option chains of NDX and SPX from CBOE, computes
Call wall, Gamma 0 and Put wall, and writes them to levels.json.
Runs in GitHub Actions on a schedule (see .github/workflows/levels.yml).

Same formulas as the "Гамма-уровни NDX" calculator:
  GEX of a strike = gamma x open interest x 100 x spot^2 x 0.01, calls +, puts -
  Call wall = strike above spot (within 10%) with the largest call open interest
              (cw_g: the largest call GEX instead)
  Put wall  = strike below spot (within 10%) with the largest put open interest
              (pw_g: the largest put GEX instead)
  Gamma 0   = price where the total gamma of all options changes sign,
              gamma recomputed with Black-Scholes and each option's IV
Intraday sets (key "sets" of each index), expirations picked automatically:
  0dte = the nearest expiration that has not expired yet (today until 16:00 NY,
         then the next one)
  week = every expiration up to the Friday of that week
  For these sets the walls are the largest call / put GEX within +-5% of spot,
  Gamma 0 is computed from these options only, and "ladder" lists the strikes
  with the largest net GEX within +-3% of spot (strike, net GEX in $bn).
  The top-level cw / g0 / pw stay as before (all expirations).
Expected move (keys "em" and "em_iv" of each index), in index points:
  em    = price of the at-the-money straddle of the nearest expiration
          (call + put at the strike closest to spot, mid of bid/ask): the move
          the market pays for until that expiration
  em_iv = spot x IV of those two options / sqrt(252): the same for one full day
History (key "history" -> index -> New York date): snapshots of that day that
are never rewritten, so a chart can show each past day with its own levels:
  am   = the first run of the day (before London)
  pm   = the first run after the index traded past 09:30 New York
  last = the latest run of the day (updated until the day ends)
Only the Python standard library is used.
"""
import json
import math
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/_{}.json"
INDEXES = ["NDX", "SPX"]
OUT = "levels.json"
NY = ZoneInfo("America/New_York")
R, Q = 0.04, 0.008                 # rate and dividend yield for Black-Scholes
YEAR_MS = 365 * 24 * 3600 * 1000
SQ2PI = math.sqrt(2 * math.pi)
SYM = re.compile(r"(\d{6})([CP])(\d{8})$")
WALL_BAND = 0.10                   # walls are searched within +-10% of spot
GRID_LO, GRID_HI, GRID_N = 0.85, 1.15, 150
HISTORY_DAYS = 400                 # how many days of snapshots to keep
INTRA_BAND = 0.05                  # intraday walls: within +-5% of spot
LADDER_BAND, LADDER_N = 0.03, 6    # intraday ladder: top strikes within +-3%


def fetch(index):
    req = urllib.request.Request(URL.format(index), headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.cboe.com/",
    })
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # network hiccup: wait and retry
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"CBOE {index}: {last}")


def expiry_ms(yymmdd):
    y, m, d = 2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
    return datetime(y, m, d, 21, 0, tzinfo=timezone.utc).timestamp() * 1000   # 16:00 New York


def bs_gamma(s, k, t, iv):
    if not (iv > 0 and t > 0 and s > 0 and k > 0):
        return 0.0
    st = iv * math.sqrt(t)
    d1 = (math.log(s / k) + (R - Q + iv * iv / 2) * t) / st
    return math.exp(-Q * t) * math.exp(-d1 * d1 / 2) / SQ2PI / (s * st)


def parse_ts(ts):
    s = str(ts).strip().replace(" ", "T")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)          # CBOE timestamps are UTC
    return dt.timestamp() * 1000


def parse_ny(ts):
    """last_trade_time of the index: New York time when it carries no zone."""
    s = str(ts).strip().replace(" ", "T")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY)
    return dt.timestamp() * 1000


def compute(raw):
    d = raw.get("data", raw)
    spot = float(d.get("current_price") or d.get("close") or 0)
    if spot <= 0:
        raise RuntimeError("no index price in the file")
    now = parse_ts(raw.get("timestamp")) if raw.get("timestamp") else time.time() * 1000
    # when the index price was set: outside the session it is the last close, not the time of the download
    spot_ts = None
    try:
        if d.get("last_trade_time"):
            spot_ts = int(parse_ny(d["last_trade_time"]))
            if spot_ts > now + 3600e3:      # a bad zone guess would put it in the future
                spot_ts = None
    except Exception:
        spot_ts = None
    opts = []
    for x in d.get("options", []):
        m = SYM.search(str(x.get("option", "")))
        if not m:
            continue
        ex = expiry_ms(m.group(1))
        oi = float(x.get("open_interest") or 0)
        if ex < now - 3600e3 or oi <= 0:
            continue
        opts.append({
            "ex": ex,
            "k": int(m.group(3)) / 1000, "c": m.group(2) == "C", "oi": oi,
            "g": float(x.get("gamma") or 0), "iv": max(float(x.get("iv") or 0), 0.0),
            "t": max((ex - now) / YEAR_MS, 1 / 8760),
            "bid": float(x.get("bid") or 0), "ask": float(x.get("ask") or 0),
            "theo": float(x.get("theo") or 0),
        })
    if not opts:
        raise RuntimeError("no options with open interest")

    mult = 100 * spot * spot * 0.01
    by_k = {}
    for o in opts:
        g = o["g"] if o["g"] > 0 else bs_gamma(spot, o["k"], o["t"], o["iv"])
        v = g * o["oi"] * mult
        e = by_k.setdefault(o["k"], {"c": 0.0, "p": 0.0, "co": 0.0, "po": 0.0})
        if o["c"]:
            e["c"] += v; e["co"] += o["oi"]
        else:
            e["p"] -= v; e["po"] += o["oi"]

    def walls(use_oi):
        cw = pw = None; cmax = pmax = 0.0
        for k in sorted(by_k):
            if abs(k - spot) / spot > WALL_BAND:
                continue
            e = by_k[k]
            cv = e["co"] if use_oi else e["c"]
            pv = e["po"] if use_oi else -e["p"]
            if k >= spot and cv > cmax:
                cmax, cw = cv, k
            if k <= spot and pv > pmax:
                pmax, pw = pv, k
        return cw, pw

    cw, pw = walls(True)
    cw_g, pw_g = walls(False)

    with_iv = [o for o in opts if o["iv"] > 0]
    prof = []
    for i in range(GRID_N + 1):
        x = spot * (GRID_LO + (GRID_HI - GRID_LO) * i / GRID_N)
        tot = 0.0
        for o in with_iv:
            g = bs_gamma(x, o["k"], o["t"], o["iv"]) * o["oi"] * 100 * x * x * 0.01
            tot += g if o["c"] else -g
        prof.append((x, tot))
    g0, best = None, float("inf")
    for (a, ya), (b, yb) in zip(prof, prof[1:]):
        if (ya <= 0 < yb) or (ya >= 0 > yb):
            x = a + (b - a) * (0 - ya) / (yb - ya)
            if abs(x - spot) < best:
                best, g0 = abs(x - spot), x

    asof_ny = datetime.fromtimestamp(now / 1000, NY)
    num = lambda v: None if v is None else (int(v) if float(v).is_integer() else round(v, 2))
    return {
        "spot": round(spot, 2), "spot_ts": spot_ts, "asof": int(now), "date": asof_ny.strftime("%Y-%m-%d"),
        "cw": num(cw), "g0": None if g0 is None else round(g0), "pw": num(pw),
        "cw_g": num(cw_g), "pw_g": num(pw_g), "n": len(opts),
        "sets": intraday_sets(opts, spot, now),
        **expected_move(opts, spot, now),
    }


def expected_move(opts, spot, now):
    """At-the-money straddle of the nearest expiration: {"em", "em_iv", "em_k"} or {}."""
    alive = sorted({o["ex"] for o in opts if o["ex"] > now})
    if not alive:
        return {}
    sub = [o for o in opts if o["ex"] == alive[0]]
    calls = {o["k"]: o for o in sub if o["c"]}
    puts = {o["k"]: o for o in sub if not o["c"]}
    ks = sorted((k for k in calls if k in puts), key=lambda k: abs(k - spot))
    if not ks:
        return {}

    def mid(o):
        if o["bid"] > 0 and o["ask"] >= o["bid"]:
            return (o["bid"] + o["ask"]) / 2
        return o["theo"] if o["theo"] > 0 else None

    k = ks[0]
    c, p = mid(calls[k]), mid(puts[k])
    ivs = [o["iv"] for o in (calls[k], puts[k]) if o["iv"] > 0]
    out = {"em_k": int(k) if float(k).is_integer() else round(k, 2)}
    out["em"] = round(c + p, 1) if c is not None and p is not None else None
    out["em_iv"] = round(spot * (sum(ivs) / len(ivs)) / math.sqrt(252), 1) if ivs else None
    return out


def ny_date(ms):
    return datetime.fromtimestamp(ms / 1000, NY).date()


def level_set(opts, spot):
    """Walls by GEX within INTRA_BAND, walls by OI, Gamma 0 and the ladder for a subset of options."""
    mult = 100 * spot * spot * 0.01
    by_k = {}
    for o in opts:
        g = o["g"] if o["g"] > 0 else bs_gamma(spot, o["k"], o["t"], o["iv"])
        v = g * o["oi"] * mult
        e = by_k.setdefault(o["k"], {"c": 0.0, "p": 0.0, "co": 0.0, "po": 0.0})
        if o["c"]:
            e["c"] += v; e["co"] += o["oi"]
        else:
            e["p"] -= v; e["po"] += o["oi"]

    def best(key, above, sign=1):
        cand = [(sign * by_k[k][key], k) for k in by_k
                if abs(k - spot) / spot <= INTRA_BAND and ((k >= spot) if above else (k <= spot))]
        cand = [c for c in cand if c[0] > 0]
        return max(cand)[1] if cand else None

    with_iv = [o for o in opts if o["iv"] > 0]
    g0, bestd, prev = None, float("inf"), None
    for i in range(GRID_N + 1):
        x = spot * (0.95 + 0.10 * i / GRID_N)
        tot = 0.0
        for o in with_iv:
            g = bs_gamma(x, o["k"], o["t"], o["iv"]) * o["oi"]
            tot += g if o["c"] else -g
        if prev and ((prev[1] <= 0 < tot) or (prev[1] >= 0 > tot)):
            z = prev[0] + (x - prev[0]) * (0 - prev[1]) / (tot - prev[1])
            if abs(z - spot) < bestd:
                bestd, g0 = abs(z - spot), z
        prev = (x, tot)

    ladder = sorted(((k, e["c"] + e["p"]) for k, e in by_k.items() if abs(k - spot) / spot <= LADDER_BAND),
                    key=lambda kv: -abs(kv[1]))[:LADDER_N]
    num = lambda v: None if v is None else (int(v) if float(v).is_integer() else round(v, 2))
    return {
        "cw": num(best("c", True)), "pw": num(best("p", False, -1)),
        "g0": None if g0 is None else round(g0),
        "cw_oi": num(best("co", True)), "pw_oi": num(best("po", False)),
        "ladder": [[num(k), round(v / 1e9, 3)] for k, v in sorted(ladder)],
        "n": len(opts),
    }


def intraday_sets(opts, spot, now):
    alive = sorted({o["ex"] for o in opts if o["ex"] > now})
    if not alive:
        return {}
    first = alive[0]
    d = ny_date(first)
    fri = d.toordinal() + (4 - d.weekday()) % 7          # Friday of that week
    week_exp = [e for e in alive if ny_date(e).toordinal() <= fri]
    out = {}
    for name, exps in (("0dte", [first]), ("week", week_exp)):
        sub = [o for o in opts if o["ex"] in exps]
        if sub:
            r = level_set(sub, spot)
            r["exp"] = ny_date(exps[0]).isoformat()
            r["exp_last"] = ny_date(exps[-1]).isoformat()
            out[name] = r
    return out


def snapshot(r):
    """The part of an index result worth keeping for a past day."""
    keep = ("asof", "spot", "spot_ts", "cw", "g0", "pw", "cw_g", "pw_g", "em", "em_iv", "em_k")
    snap = {k: r.get(k) for k in keep}
    snap["sets"] = {name: {k: v.get(k) for k in ("cw", "g0", "pw", "exp", "exp_last")}
                    for name, v in (r.get("sets") or {}).items()}
    return snap


def add_history(out, index, r):
    hist = out.setdefault("history", {}).setdefault(index, {})
    day = r["date"]
    h = hist.setdefault(day, {})
    snap = snapshot(r)
    if "am" not in h:
        h["am"] = snap
    ts = r.get("spot_ts")
    if "pm" not in h and ts:
        t = datetime.fromtimestamp(ts / 1000, NY)
        if t.strftime("%Y-%m-%d") == day and (t.hour, t.minute) >= (9, 30):
            h["pm"] = snap
    h["last"] = snap
    for old in sorted(hist)[:-HISTORY_DAYS]:
        del hist[old]


def main():
    try:
        with open(OUT, encoding="utf-8") as f:
            out = json.load(f)
    except Exception:
        out = {}
    ok = 0
    for index in INDEXES:
        try:
            out[index] = compute(fetch(index))
            add_history(out, index, out[index])
            ok += 1
            print(index, out[index])
        except Exception as e:  # keep the previous levels of this index
            print(f"{index}: {e}", file=sys.stderr)
    if not ok:
        sys.exit("no index could be updated")
    out["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


if __name__ == "__main__":
    main()
