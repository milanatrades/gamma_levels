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


def compute(raw):
    d = raw.get("data", raw)
    spot = float(d.get("current_price") or d.get("close") or 0)
    if spot <= 0:
        raise RuntimeError("no index price in the file")
    now = parse_ts(raw.get("timestamp")) if raw.get("timestamp") else time.time() * 1000
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
            "k": int(m.group(3)) / 1000, "c": m.group(2) == "C", "oi": oi,
            "g": float(x.get("gamma") or 0), "iv": max(float(x.get("iv") or 0), 0.0),
            "t": max((ex - now) / YEAR_MS, 1 / 8760),
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
        "spot": round(spot, 2), "asof": int(now), "date": asof_ny.strftime("%Y-%m-%d"),
        "cw": num(cw), "g0": None if g0 is None else round(g0), "pw": num(pw),
        "cw_g": num(cw_g), "pw_g": num(pw_g), "n": len(opts),
    }


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
