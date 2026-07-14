#!/usr/bin/env python3
"""Query the public search.ch Swiss timetable API (timetable.search.ch).

No API key. Three subcommands map to the three public JSON endpoints:

    search  <term>            completion.json    -> resolve a name to a stop id
    board   <stop>            stationboard.json  -> live departures/arrivals
    route   <from> <to>       route.json         -> connections A->B

Output is compact human-readable text by default so it can be relayed
verbatim; pass --json to get the raw upstream payload instead.
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime

BASE = "https://timetable.search.ch/api"
UA = "claude-swiss-transport-skill/1.0 (+https://timetable.search.ch)"

# Language of the API's localized strings (`description`, "not found" messages,
# weekday names). Without it search.ch defaults to German. Set from --lang.
LANG = "en"


def _get(endpoint, params):
    # search.ch drops null/empty params server-side; strip them here for tidy URLs.
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{BASE}/{endpoint}.json?{query}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Accept-Language": LANG,
        "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"error: HTTP {e.code} from {endpoint} ({e.reason})")
    except urllib.error.URLError as e:
        sys.exit(f"error: could not reach timetable.search.ch: {e.reason}")
    if isinstance(data, dict) and data.get("messages") and endpoint == "route":
        # non-fatal upstream notices; surface but keep going
        pass
    return url, data


def _hhmm(iso):
    """'2026-07-14 19:02:00' -> '19:02'; pass through anything unexpected."""
    if not iso:
        return "--:--"
    try:
        return datetime.strptime(iso, "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
    except ValueError:
        return str(iso)[11:16] or str(iso)


def _delay(val):
    """search.ch delays look like '+3' / '+0' / None."""
    if val in (None, "", "+0", 0, "0"):
        return ""
    s = str(val)
    return f" ({s if s.startswith(('+', '-')) else '+' + s})"


def _dur(seconds):
    try:
        m = int(seconds) // 60
    except (TypeError, ValueError):
        return "?"
    return f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m}min"


# ---- search ---------------------------------------------------------------
def cmd_search(args):
    url, data = _get("completion", {
        "term": args.term,
        "show_ids": 1,
        "show_coordinates": 1,
        "nofavorites": 1,
    })
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if not data:
        print(f"No stops matching {args.term!r}.")
        return
    print(f"Stops matching {args.term!r}:")
    for item in data[:args.limit]:
        kind = (item.get("iconclass", "").replace("sl-icon-type-", "") or "?")
        sid = item.get("id", "")
        print(f"  {item.get('label', '?'):<34} id={sid:<10} [{kind}]")
    print(f"\n(source: {url})")


# ---- board ----------------------------------------------------------------
def cmd_board(args):
    url, data = _get("stationboard", {
        "stop": args.stop,
        "limit": args.limit,
        "mode": "arrival" if args.arrivals else "departure",
        "show_delays": 1,
        "show_tracks": 1,
        "show_trackchanges": 1,
        "transportation_types": ",".join(args.types) if args.types else None,
    })
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    stop = (data.get("stop") or {}).get("name", args.stop)
    conns = data.get("connections") or []
    kind = "Arrivals at" if args.arrivals else "Departures from"
    print(f"{kind} {stop} (next {len(conns)}):")
    for c in conns:
        t = _hhmm(c.get("time"))
        delay = _delay(c.get("dep_delay") or c.get("arr_delay"))
        line = c.get("line") or c.get("*L") or c.get("type", "")
        dest = (c.get("terminal") or {}).get("name", "?")
        track = c.get("track")
        track_s = f" pl.{track}" if track else ""
        op = c.get("operator", "")
        print(f"  {t}{delay:<7} {line:<7} -> {dest:<26}{track_s:<7} {op}")
    print(f"\n(source: {url})")


# ---- route ----------------------------------------------------------------
def cmd_route(args):
    params = {
        "from": args.origin,
        "to": args.destination,
        "num": args.num,
        "show_delays": 1,
        "show_trackchanges": 1,
    }
    if args.date:
        params["date"] = args.date       # expected M/D/YYYY
    if args.time:
        params["time"] = args.time       # expected H:M (24h)
    if args.arrival:
        params["time_type"] = "arrival"
    url, data = _get("route", params)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    conns = data.get("connections") or []
    if not conns:
        print(f"No connections {args.origin} -> {args.destination}.")
        for m in data.get("messages") or []:
            print(f"  note: {m}")
        return
    if data.get("description"):
        print(data["description"])
    for c in conns:
        dep, arr = _hhmm(c.get("departure")), _hhmm(c.get("arrival"))
        ddelay = _delay(c.get("dep_delay"))
        # A ride leg carries a line/type; the trailing leg (no line) is just the
        # final arrival marker, and each ride's alight info lives in its `exit`.
        rides = [l for l in (c.get("legs") or []) if l.get("line") or l.get("type")]
        changes = max(len(rides) - 1, 0)
        vias = " via " + ", ".join(l.get("line") or l.get("type") for l in rides) if rides else ""
        print(f"\n  {dep}{ddelay} -> {arr}  ({_dur(c.get('duration'))}, {changes} chg){vias}")
        for l in rides:
            exit_ = l.get("exit") or {}
            dtrack = f" pl.{l['track']}" if l.get("track") else ""
            atrack = f" pl.{exit_['track']}" if exit_.get("track") else ""
            line = l.get("line") or l.get("type", "")
            direction = f" (dir. {l['terminal']})" if l.get("terminal") else ""
            print(f"      {_hhmm(l.get('departure'))}{dtrack} {line:<8} "
                  f"{l.get('name', '?')} -> {exit_.get('name', arr)} "
                  f"{_hhmm(exit_.get('arrival'))}{atrack}{direction}")
    print(f"\n(source: {url})")


def main():
    # Shared options accepted either before or after the subcommand, so both
    # `sbb.py --lang fr route ...` and `sbb.py route ... --lang fr` work.
    # SUPPRESS defaults so a value given before the subcommand isn't clobbered by
    # the subparser copy's default (argparse writes both into one namespace).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print raw upstream JSON")
    common.add_argument("--lang", metavar="L", default=argparse.SUPPRESS,
                        help="language for API text (en/de/fr/it); default en")

    p = argparse.ArgumentParser(description="Query the public search.ch Swiss timetable API.",
                                parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", parents=[common], help="resolve a place name to stop id(s)")
    s.add_argument("term")
    s.add_argument("--limit", type=int, default=8)
    s.set_defaults(func=cmd_search)

    b = sub.add_parser("board", parents=[common], help="live departures/arrivals for a stop")
    b.add_argument("stop", help="stop name or id")
    b.add_argument("--arrivals", action="store_true", help="show arrivals instead of departures")
    b.add_argument("--limit", type=int, default=12)
    b.add_argument("--types", nargs="+", metavar="T",
                   help="filter: train tram bus ship cableway")
    b.set_defaults(func=cmd_board)

    r = sub.add_parser("route", parents=[common], help="connections from A to B")
    r.add_argument("origin")
    r.add_argument("destination")
    r.add_argument("--date", help="travel date, format M/D/YYYY (e.g. 7/14/2026)")
    r.add_argument("--time", help="travel time, 24h H:M (e.g. 8:30)")
    r.add_argument("--arrival", action="store_true",
                   help="treat --time as desired arrival instead of departure")
    r.add_argument("--num", type=int, default=4, help="number of connections")
    r.set_defaults(func=cmd_route)

    args = p.parse_args()
    global LANG
    LANG = getattr(args, "lang", "en")
    args.json = getattr(args, "json", False)
    args.func(args)


if __name__ == "__main__":
    main()
