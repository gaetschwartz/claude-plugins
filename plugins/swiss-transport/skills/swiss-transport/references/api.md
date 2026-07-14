# search.ch timetable API — contract reference

Public Swiss timetable API. **No key, no auth**, JSON in/out. Base:

```
https://timetable.search.ch/api/<endpoint>.json?<params>
```

Only header that matters: `accept-language` (e.g. `de`, `fr`, `it`, `en`) changes
the language of `description`/message text. Covers all of Switzerland plus
cross-border links. Read this file only when you need a field the `sbb.py`
output doesn't surface, or when calling the endpoints by hand.

Three endpoints, one per task:

| Endpoint | Task | `sbb.py` subcommand |
|----------|------|---------------------|
| `completion` | resolve a name → stop id | `search` |
| `stationboard` | live departures / arrivals at a stop | `board` |
| `route` | connections between two stops | `route` |

---

## completion — station / place autocomplete

`GET /api/completion.json`

| Param | Example | Meaning |
|-------|---------|---------|
| `term` | `bern` | free text to match |
| `show_ids` | `1` | include the stop `id` in each result |
| `show_coordinates` | `1` | include `lat`/`lon`/`x`/`y` |
| `nofavorites` | `1` | don't bias by the caller's saved favourites |
| `latlon` | `46.94,7.44` | reverse-geocode: nearest stops to a point (instead of `term`) |

Response is a **JSON array** of objects:

```json
{"label":"Bern","id":"8507000","x":"600037","y":"199749",
 "lon":7.439123,"lat":46.948823,"iconclass":"sl-icon-type-train",
 "html":"<span class=\"sl-keyword\">Bern</span>"}
```

- `id` — the stop id (UIC/DIDOK). Prefer passing this id to `route`/`stationboard`
  when you have it; it disambiguates identically-named stops.
- `iconclass` — `sl-icon-type-<kind>` where kind ∈ `station`/`train`/`tram`/`bus`/`ship`/`cableway`/`adr` (address).
- `x`/`y` — Swiss LV03 grid; `lat`/`lon` — WGS84.

---

## stationboard — live departures / arrivals

`GET /api/stationboard.json`

| Param | Example | Meaning |
|-------|---------|---------|
| `stop` | `8507000` or `Bern` | stop id (preferred) or name |
| `limit` | `12` | max entries |
| `mode` | `departure` / `arrival` | board direction |
| `show_delays` | `1` | include real-time `dep_delay`/`arr_delay` |
| `show_tracks` | `1` | include `track` (platform) |
| `show_trackchanges` | `1` | flag platform changes |
| `show_subsequent_stops` | `1` | include `subsequent_stops[]` (downstream calling points) |
| `transportation_types` | `train,bus` | CSV filter; values `train,tram,bus,ship,cableway` |
| `date` / `time` | see route | board at a future moment instead of now |

Response:

```json
{"stop": {"id":"8507000","name":"Bern","type":"train,strain", ...},
 "connections": [
   {"time":"2026-07-14 18:52:00","type":"strain","line":"S7","*L":"7",
    "operator":"RBS","color":"039~fff~","type_name":"S-Bahn",
    "terminal":{"id":"8507063","name":"Worb Dorf", ...},
    "dep_delay":"+0","track":"23"} ]}
```

- `time` — scheduled departure/arrival, `YYYY-MM-DD HH:MM:SS`.
- `line` — public line label (e.g. `S7`, `IR 16`); `*L` is the raw line number,
  `*G` the raw category, `type`/`type_name` the vehicle class.
- `terminal` — final destination of the service (the "direction").
- `dep_delay`/`arr_delay` — `+N` minutes, `+0` when on time.
- `color` — `"BG~FG~"` hex pair for the line badge.

---

## route — connections A → B

`GET /api/route.json`

| Param | Example | Meaning |
|-------|---------|---------|
| `from` | `Bern` or `8507000` | origin stop name or id |
| `to` | `Zürich HB` or `8503000` | destination stop name or id |
| `date` | `7/14/2026` | **US format `M/D/YYYY`** — NOT ISO |
| `time` | `14:05` | 24h `H:M`; leading zeros optional (`8:30` ok) |
| `time_type` | `depart` / `arrival` | is `time` the departure or the desired arrival |
| `num` | `4` | number of later connections to return |
| `pre` | `0` | number of earlier connections to also return |
| `show_delays` | `1` | include real-time delays |
| `show_trackchanges` | `1` | flag platform changes |

> ⚠️ **Date/time formatting is the #1 correctness trap.** `date` is American
> `M/D/YYYY`, and `time` is `H:M` (24-hour). Passing ISO `2026-07-14` or `14:05:00`
> yields wrong or empty results silently. `sbb.py` takes the same `--date M/D/YYYY`
> `--time H:M` and passes them through, so let the user's natural date flow into
> that shape. Omit both to mean "now".

Response:

```json
{"count":1,"min_duration":3360,"max_duration":3360,
 "description":"Von Bern nach Zürich HB am Dienstag 14.07.2026",
 "connections":[ ... ],
 "points":[{"id":"8507000","text":"Bern", ...}, ...]}
```

Each **connection**:

```json
{"departure":"2026-07-14 19:02:00","arrival":"2026-07-14 19:58:00",
 "duration":3360, "dep_delay":"+0",
 "legs":[ ... ]}
```

- `duration` — seconds.
- `dep_delay` — `+N` minutes on the first leg.

**Legs** are the crux. A connection's `legs` array holds one entry per ride
**plus a trailing arrival marker**:

- A **ride leg** has a `line`/`type` and a `departure`, and carries where you get
  off in a nested **`exit`** object (`exit.arrival`, `exit.name`, `exit.track`).
  Key fields: `line`, `type`, `type_name`, `name` (boarding stop), `track`
  (boarding platform), `terminal` (service direction), `departure`, `stops[]`
  (intermediate calling points), `operator`, `fgcolor`/`bgcolor`.
- The **final leg** has no `line`/`type` — only `name` + `arrival`. It marks the
  destination; it is NOT a change.

So **number of changes = (count of legs with a line/type) − 1**, and a leg's
arrival time/platform come from its `exit`, not from a sibling leg. `sbb.py`
already applies exactly this rule — reach for the raw JSON only for fields it
doesn't print (intermediate `stops`, colours, coordinates, occupancy).

---

## Notes & limits

- No documented key or hard rate limit, but it is a courtesy-use public API:
  keep calls modest, `search` once to get an id, then reuse the id.
- Errors: a bad stop yields `200` with a `messages`/`connections:[]` payload
  (e.g. `"Haltestelle X nicht gefunden."`), not an HTTP error — check for empty
  `connections`.
- This is the same backend behind `fahrplan.search.ch`; each route response even
  includes a shareable `url` to the human page.
- The swift_travel app additionally uses opentransportdata.swiss TRIAS (XML, needs
  a personal key) for station autocomplete and geo.admin for addresses. Neither is
  needed here — `timetable.search.ch` alone covers search + board + route keyless.
