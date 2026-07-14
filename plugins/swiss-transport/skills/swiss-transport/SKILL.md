---
name: swiss-transport
description: Look up real Swiss public-transport info — train/bus/tram/boat connections, live departure & arrival boards, and station lookup — via the keyless public search.ch timetable API. Use this whenever the user asks about getting around Switzerland by public transport: "next train from Bern to Zürich", "when does the S7 leave Genève", "departures from Lausanne gare", "how do I get from Zug to Locarno", SBB/CFF/FFS schedules, connections, platforms, or delays for any Swiss stop — even if they don't name a tool or the API. Prefer this over guessing timetables from memory; schedules change and only the live API is accurate.
---

# Swiss public transport (search.ch)

Answer Swiss public-transport questions with real, current data from the public
`timetable.search.ch` API (no key, JSON). Never invent timetables from memory —
Swiss schedules change and departures carry live delays; only the API is accurate.

All three tasks go through one bundled script — call it with Bash, don't hand-roll
URLs (it handles the fiddly date/time formatting the API demands):

```
scripts/sbb.py search <term>              # name -> stop id(s)
scripts/sbb.py board  <stop> [options]    # live departures / arrivals
scripts/sbb.py route  <from> <to> [opts]  # connections A -> B
```

The script is `scripts/sbb.py` in this skill's directory, e.g.
`python3 scripts/sbb.py route Bern "Zürich HB"`.

API text (weekday names, the route header, "stop not found" notices) defaults to
**English**. Switzerland is multilingual — if the user is writing in French,
German, or Italian, pass `--lang fr|de|it` so those strings match their language.

## Choosing the task

- **"When's the next … / departures from X / what leaves X"** → `board`.
- **"How do I get from A to B / connection / train from A to B [at TIME]"** → `route`.
- **Ambiguous or misspelled place, or you want a precise stop** → `search` first,
  then pass the returned `id` (ids disambiguate identically-named stops and are
  more reliable than names).

## board — departures / arrivals

```
scripts/sbb.py board "Bern" --limit 8
scripts/sbb.py board "8501120" --arrivals            # arrivals instead
scripts/sbb.py board "Lausanne" --types train tram   # filter vehicle types
```

Each line is `HH:MM (delay)  LINE -> destination  platform  operator`. Relay the
times, line, destination and platform; call out any non-empty delay explicitly.

## route — connections between two stops

```
scripts/sbb.py route "Genève" "Lausanne"
scripts/sbb.py route "Bern" "Locarno" --date 7/20/2026 --time 8:30 --num 3
scripts/sbb.py route "Zug" "Zürich HB" --time 18:00 --arrival   # arrive by 18:00
```

Date/time formatting is the one real trap: **`--date` is `M/D/YYYY`** (American)
and **`--time` is 24h `H:M`**. The user rarely phrases dates that way — convert
their intent ("next Monday", "8:30am", "tomorrow evening") into that shape.
Today's date is available in your context; use it to resolve relative dates.
Omit `--date`/`--time` entirely for "now". Add `--arrival` when the user gives a
time they must *arrive by* rather than depart at.

Each connection prints as a summary line
(`dep -> arr (duration, N chg) via LINE, LINE`) followed by one indented line per
ride showing board/alight stops, times and platforms. A "0 chg" trip is direct.

## Relaying results

- Lead with the direct answer (the next departure, or the best connection), then
  offer alternatives if returned.
- Always surface **delays and platform changes** — that's the value of live data.
- The script prints a `(source: …)` URL; you don't need to repeat it unless the
  user wants to open the search.ch page.
- If `connections` is empty the script prints the API's own note (e.g. stop not
  found) — pass that back and, if it looks like a typo, offer to `search` the name.

## Field-level details

For the full parameter list, response shapes, and fields the script doesn't print
(intermediate calling points, line colours, coordinates, reverse-geocoding by
lat/lon), read `references/api.md`.
