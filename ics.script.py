#!/usr/bin/env python3
"""
Build one ICS file containing currently scheduled 2026 playoff games for:
- NHL
- AHL
- USHL
- OHL
- QMJHL
- WHL

Behavior:
- Includes only games with a concrete published start time.
- Skips games whose time is TBD.
- Writes one combined ICS file in UTC so calendar apps convert automatically.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

OUTPUT_FILE = "north_american_hockey_playoffs.ics"
DEFAULT_GAME_DURATION = timedelta(hours=3)
REQUEST_TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

TZ_ET = ZoneInfo("America/New_York")
TZ_CT = ZoneInfo("America/Chicago")

NHL_URL = "https://www.nhl.com/news/2026-stanley-cup-playoffs-first-round-schedule-television-results"
AHL_URL = "https://theahl.com/news/opening-schedules-2026-calder-cup-playoffs"
USHL_URL = "https://ushl.com/news/2026/4/12/mens-ice-hockey-dates-times-set-for-conference-semifinals.aspx"

OHL_SCHEDULE_URL = "https://chl.ca/ohl/schedule/217/293/"
QMJHL_SCHEDULE_URL = "https://chl.ca/lhjmq/en/schedule/217/293/"
WHL_SCHEDULE_URL = "https://chl.ca/whl/schedule/217/293/"

NOW_UTC = datetime.now(timezone.utc)


@dataclass
class GameEvent:
    league: str
    round_name: str
    away: str
    home: str
    start_utc: datetime
    end_utc: datetime
    source_url: str
    location: Optional[str] = None
    notes: Optional[str] = None

    @property
    def uid(self) -> str:
        raw = (
            f"{self.league}|{self.round_name}|{self.away}|{self.home}|"
            f"{self.start_utc.isoformat()}"
        )
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
        return f"{digest}@playoffs-2026"

    @property
    def summary(self) -> str:
        return f"{self.league} Playoffs — {self.away} at {self.home}"


def fetch(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def soup_text_lines(html_text: str) -> List[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    text = soup.get_text("\n")
    lines = [normalize_space(x) for x in text.splitlines()]
    return [x for x in lines if x]


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def format_ics_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def escape_ics_text(value: str) -> str:
    value = value.replace("\\", "\\\\")
    value = value.replace(";", r"\;")
    value = value.replace(",", r"\,")
    value = value.replace("\n", r"\n")
    return value


def parse_month_name(name: str) -> int:
    months = {
        "january": 1, "jan": 1,
        "february": 2, "feb": 2,
        "march": 3, "mar": 3,
        "april": 4, "apr": 4,
        "may": 5,
        "june": 6, "jun": 6,
        "july": 7, "jul": 7,
        "august": 8, "aug": 8,
        "september": 9, "sep": 9, "sept": 9,
        "october": 10, "oct": 10,
        "november": 11, "nov": 11,
        "december": 12, "dec": 12,
    }
    key = name.strip().lower().rstrip(".")
    if key not in months:
        raise ValueError(f"Unknown month name: {name}")
    return months[key]


def parse_time_12h(raw: str) -> tuple[int, int]:
    """
    Examples:
        7 p.m.
        7:30 p.m.
        10:10 p.m.
    """
    s = raw.strip().lower().replace(" ", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(a\.m\.|p\.m\.)$", s)
    if not m:
        raise ValueError(f"Unrecognized time: {raw}")
    hour = int(m.group(1))
    minute = int(m.group(2) or "0")
    ampm = m.group(3)
    if ampm == "a.m." and hour == 12:
        hour = 0
    elif ampm == "p.m." and hour != 12:
        hour += 12
    return hour, minute


def parse_date_with_tz(
    month_name: str,
    day_str: str,
    time_str: str,
    tz: ZoneInfo,
    year: int = 2026,
) -> datetime:
    month = parse_month_name(month_name)
    day = int(day_str)
    hour, minute = parse_time_12h(time_str)
    return datetime(year, month, day, hour, minute, tzinfo=tz)


def parse_ahl_time(raw: str) -> tuple[int, int]:
    """
    AHL format is often bare clock time like 7:00, 4:00, 9:05, 10:00
    and the page says 'All times Eastern'. These are game times, so
    we interpret them as afternoon/evening local times:
      1:00-11:59 -> PM
      12:xx      -> 12:xx
    """
    m = re.match(r"^(\d{1,2}):(\d{2})$", raw.strip())
    if not m:
        raise ValueError(f"Unrecognized AHL time: {raw}")
    hour = int(m.group(1))
    minute = int(m.group(2))

    if 1 <= hour <= 11:
        hour += 12

    return hour, minute


def parse_ahl_datetime(
    month_abbr: str,
    day_str: str,
    time_str: str,
    year: int = 2026,
) -> datetime:
    month = parse_month_name(month_abbr)
    day = int(day_str)
    hour, minute = parse_ahl_time(time_str)
    return datetime(year, month, day, hour, minute, tzinfo=TZ_ET)


def parse_ushl_datetime(date_str: str, time_str: str, tz_abbr: str, year: int = 2026) -> datetime:
    """
    Accepts both:
    - 7:05 p.m.
    - 7:05
    """
    date_str = normalize_space(date_str)
    time_str = normalize_space(time_str)

    dm = re.search(r"([A-Za-z]+)\s+(\d{1,2})$", date_str)
    if not dm:
        raise ValueError(f"Unrecognized USHL date: {date_str}")

    month_name, day_str = dm.group(1), dm.group(2)
    month = parse_month_name(month_name)
    day = int(day_str)
    tz = TZ_ET if tz_abbr.upper() == "ET" else TZ_CT

    m_simple = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
    if m_simple:
        hour = int(m_simple.group(1))
        minute = int(m_simple.group(2))
        if 1 <= hour <= 11:
            hour += 12
        return datetime(year, month, day, hour, minute, tzinfo=tz)

    hour, minute = parse_time_12h(time_str)
    return datetime(year, month, day, hour, minute, tzinfo=tz)


def extract_json_array_after_key(text: str, key: str) -> list[dict]:
    idx = text.find(key)
    if idx == -1:
        raise ValueError(f"Key not found: {key}")

    start = text.find("[", idx)
    if start == -1:
        raise ValueError(f"Opening [ not found after key: {key}")

    depth = 0
    end = None
    in_str = False
    escape = False

    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end is None:
        raise ValueError(f"Closing ] not found after key: {key}")

    raw = text[start:end]
    return json.loads(raw)


def iter_future(events: Iterable[GameEvent]) -> List[GameEvent]:
    return sorted(
        [e for e in events if e.start_utc >= NOW_UTC],
        key=lambda e: (e.start_utc, e.league, e.home, e.away),
    )


def fetch_nhl_article_lines(html_text: str) -> List[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    container = (
        soup.find("article")
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup
    )
    text = container.get_text("\n")
    lines = [normalize_space(x.replace("\xa0", " ")) for x in text.splitlines()]
    return [x for x in lines if x]


def repair_nhl_source_text(value: str) -> str:
    """
    Tolerate malformed source text like:
      'April 30n TBD'
    """
    value = re.sub(r"([A-Za-z]+)\s+(\d{1,2})[A-Za-z]+\s+TBD", r"\1 \2 TBD", value)
    value = value.replace(" ,", ",")
    return normalize_space(value)


def fetch_nhl_events() -> List[GameEvent]:
    html_text = fetch(NHL_URL)
    soup = BeautifulSoup(html_text, "html.parser")

    container = (
        soup.find("article")
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
    )

    if not container:
        raise RuntimeError("NHL: Could not locate article content")

    lines = [
        normalize_space(x.replace("\xa0", " ").replace("Â", ""))
        for x in container.get_text("\n").splitlines()
    ]
    lines = [x for x in lines if x]

    events: List[GameEvent] = []
    current_series = "First Round"
    pending_game_num: Optional[int] = None

    series_re = re.compile(r"^(.+?)\s+vs\.\s+(.+?)$")
    game_header_re = re.compile(r"^\*?Game\s+(\d+):$", re.IGNORECASE)
    inline_game_re = re.compile(r"^\*?Game\s+(\d+):\s*(.+)$", re.IGNORECASE)

    detail_re = re.compile(
    r"^(?P<away>.+?)\s+at\s+(?P<home>.+?)\s*[-–]{1,2}\s+"
    r"(?P<time>\d{1,2}(?::\d{2})?\s*[ap]\.m\.)\s*ET,?\s*"
    r"(?:[A-Za-z]+,?\s*)?"  # optional weekday
    r"(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})"
    r"(?:\s+(?P<tbd>TBD))?"
    r"(?:\s*\((?P<tv>.*?)\))?$",
    re.IGNORECASE,
)

    print("\n=== NHL DEBUG START ===")

    for line in lines:
        print(line)

        line = normalize_space(line)

        # Series header
        if " vs. " in line and len(line) < 120 and not line.startswith("Complete coverage of"):
            current_series = line
            pending_game_num = None
            continue

        # Case 1: "Game 2:"
        mh = game_header_re.match(line)
        if mh:
            pending_game_num = int(mh.group(1))
            continue

        # Case 2: "Game 2: Boston at Buffalo, ..."
        mi = inline_game_re.match(line)
        game_num = None
        detail_line = None

        if mi:
            game_num = int(mi.group(1))
            detail_line = normalize_space(mi.group(2))
            pending_game_num = None
        elif pending_game_num is not None:
            # next line after "Game X:"
            game_num = pending_game_num
            detail_line = line
            pending_game_num = None
        else:
            continue

        if not detail_line:
            continue

        # Skip completed results
        if " at " not in detail_line:
            print(f"SKIP RESULT/OTHER: {detail_line}")
            continue

        # Skip TBD games
        if "TBD" in detail_line:
            print(f"SKIP TBD: {detail_line}")
            continue

        dm = detail_re.match(detail_line)
        if not dm:
            print(f"FAILED PARSE: {detail_line}")
            continue

        away = normalize_space(dm.group("away"))
        home = normalize_space(dm.group("home"))
        time_str = dm.group("time")
        month_name = dm.group("month")
        day_str = dm.group("day")

        if not time_str:
            print(f"NO TIME FOUND: {detail_line}")
            continue

        # Source typo defense, e.g. April 30n
        month_name = re.sub(r"[^A-Za-z]", "", month_name)

        try:
            local_dt = parse_date_with_tz(month_name, day_str, time_str, TZ_ET, 2026)
            start_utc = local_dt.astimezone(timezone.utc)

            print(f"PARSED OK: Game {game_num}: {away} at {home} -> {start_utc.isoformat()}")

            events.append(
                GameEvent(
                    league="NHL",
                    round_name=f"Stanley Cup Playoffs — {current_series}",
                    away=away,
                    home=home,
                    start_utc=start_utc,
                    end_utc=start_utc + DEFAULT_GAME_DURATION,
                    source_url=NHL_URL,
                    notes=f"Game {game_num}. Source times published by NHL page.",
                )
            )
        except Exception as exc:
            print(f"PARSE ERROR: {detail_line} :: {exc}")

    print("=== NHL DEBUG END ===\n")

    return iter_future(events)


def fetch_ahl_events() -> List[GameEvent]:
    html_text = fetch(AHL_URL)
    lines = soup_text_lines(html_text)

    events: List[GameEvent] = []
    current_round = ""
    current_series = ""

    round_line_re = re.compile(r"^(Atlantic|North|Central|Pacific)\s+Division\s+(First Round|Semifinals).*$")
    series_line_re = re.compile(r"^[A-Z]\d[-–].+?\s+vs\.\s+.+$")
    game_re = re.compile(
    r"^Game\s+\d+\s+-\s+[A-Za-z]{3}\.,\s+([A-Za-z]{3})\.?\s+(\d{1,2})\s+-\s+(.+?)\s+at\s+(.+?),\s+(\d{1,2}:\d{2})$"
)

    for line in lines:
        line = line.lstrip("^*").strip()

        rm = round_line_re.match(line)
        if rm:
            current_round = normalize_space(line)
            current_series = ""
            continue

        if "winner" in line.lower() or "/" in line:
            continue

        if series_line_re.match(line):
            current_series = normalize_space(line)
            continue

        gm = game_re.match(line)
        if not gm:
            continue

        month_abbr = gm.group(1)
        day_str = gm.group(2)
        away = normalize_space(gm.group(3))
        home = normalize_space(gm.group(4))
        time_str = gm.group(5)

        try:
            local_dt = parse_ahl_datetime(month_abbr, day_str, time_str, year=2026)
        except Exception as exc:
            print(f"[WARN] AHL parse failed for line: {line} :: {exc}", file=sys.stderr)
            continue

        start_utc = local_dt.astimezone(timezone.utc)

        events.append(
            GameEvent(
                league="AHL",
                round_name=f"Calder Cup Playoffs — {current_round}",
                away=away,
                home=home,
                start_utc=start_utc,
                end_utc=start_utc + DEFAULT_GAME_DURATION,
                source_url=AHL_URL,
                notes=current_series or "Source times published in ET.",
            )
        )

    return iter_future(events)


def fetch_ushl_events() -> List[GameEvent]:
    html_text = fetch(USHL_URL)
    lines = soup_text_lines(html_text)

    events: List[GameEvent] = []
    current_conf = ""

    game_re = re.compile(
        r"^Game\s+\d+:\s+#\d+\s+(.+?)\s+at\s+#\d+\s+(.+?)\s+-\s+"
        r"([A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2}),\s+(.+?)\s+(ET|CT)\*?$"
    )

    for line in lines:
        if line in ("Eastern Conference", "Western Conference"):
            current_conf = line
            continue

        if "If necessary" in line:
            continue

        gm = game_re.match(line)
        if not gm:
            continue

        away = normalize_space(gm.group(1))
        home = normalize_space(gm.group(2))
        date_part = normalize_space(gm.group(3))
        time_part = normalize_space(gm.group(4))
        tz_abbr = gm.group(5)

        try:
            local_dt = parse_ushl_datetime(date_part, time_part, tz_abbr, year=2026)
        except Exception as exc:
            print(f"[WARN] USHL parse failed for line: {line} :: {exc}", file=sys.stderr)
            continue

        start_utc = local_dt.astimezone(timezone.utc)

        events.append(
            GameEvent(
                league="USHL",
                round_name=f"Clark Cup Playoffs — {current_conf} Semifinal",
                away=away,
                home=home,
                start_utc=start_utc,
                end_utc=start_utc + DEFAULT_GAME_DURATION,
                source_url=USHL_URL,
                notes="Conference semifinals.",
            )
        )

    return iter_future(events)


def fetch_chl_schedule_events(league_name: str, url: str) -> List[GameEvent]:
    html_text = fetch(url)
    scoreboard = extract_json_array_after_key(html_text, '"scoreboard":')

    events: List[GameEvent] = []

    for item in scoreboard:
        try:
            status = normalize_space(str(item.get("GameStatusStringLong", "")))
            if status.lower().startswith("final"):
                continue

            dt_iso = item.get("GameDateISO8601")
            if not dt_iso:
                continue

            start = datetime.fromisoformat(dt_iso)
            if start.tzinfo is None:
                continue

            start_utc = start.astimezone(timezone.utc)
            if start_utc < NOW_UTC:
                continue

            away = normalize_space(
                item.get("VisitorLongName")
                or f"{item.get('VisitorCity', '')} {item.get('VisitorNickname', '')}"
            )
            home = normalize_space(
                item.get("HomeLongName")
                or f"{item.get('HomeCity', '')} {item.get('HomeNickname', '')}"
            )

            if not away or not home:
                continue

            venue_name = normalize_space(item.get("venue_name", ""))
            venue_loc = normalize_space(item.get("venue_location", ""))
            location = ", ".join([x for x in [venue_name, venue_loc] if x])

            round_name = "Playoffs"
            maybe_letter = item.get("game_letter") or ""
            maybe_type = item.get("game_type") or ""
            if maybe_type:
                round_name = f"Playoffs — {maybe_type}"
            elif maybe_letter:
                round_name = f"Playoffs — {maybe_letter}"

            events.append(
                GameEvent(
                    league=league_name,
                    round_name=round_name,
                    away=away,
                    home=home,
                    start_utc=start_utc,
                    end_utc=start_utc + DEFAULT_GAME_DURATION,
                    source_url=url,
                    location=location or None,
                    notes=status or None,
                )
            )
        except Exception as exc:
            print(f"[WARN] Skipping {league_name} item due to parse error: {exc}", file=sys.stderr)

    return iter_future(events)


def build_ics(events: List[GameEvent]) -> str:
    dtstamp = format_ics_dt(datetime.now(timezone.utc))

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//OpenAI//North American Hockey Playoffs 2026//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:North American Hockey Playoffs 2026",
        "X-WR-TIMEZONE:UTC",
    ]

    for event in events:
        desc_parts = [event.round_name]
        if event.notes:
            desc_parts.append(event.notes)
        desc_parts.append(f"Source: {event.source_url}")
        description = "\n".join(desc_parts)

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{event.uid}",
                f"DTSTAMP:{dtstamp}",
                f"DTSTART:{format_ics_dt(event.start_utc)}",
                f"DTEND:{format_ics_dt(event.end_utc)}",
                f"SUMMARY:{escape_ics_text(event.summary)}",
                f"DESCRIPTION:{escape_ics_text(description)}",
            ]
        )

        if event.location:
            lines.append(f"LOCATION:{escape_ics_text(event.location)}")

        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"

import subprocess

def push_to_github(file_path: str):
    try:
        subprocess.run(["git", "add", file_path], check=True)

        # Commit only if there are changes
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True
        )

        if not result.stdout.strip():
            print("[INFO] No changes to commit")
            return

        subprocess.run(
            ["git", "commit", "-m", "Auto-update hockey playoff schedule"],
            check=True
        )

        subprocess.run(["git", "push"], check=True)

        print("[INFO] Successfully pushed to GitHub")

    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Git push failed: {e}")


def main() -> int:
    all_events: List[GameEvent] = []

    parsers = [
        ("NHL", fetch_nhl_events),
        ("AHL", fetch_ahl_events),
        ("USHL", fetch_ushl_events),
        ("OHL", lambda: fetch_chl_schedule_events("OHL", OHL_SCHEDULE_URL)),
        ("QMJHL", lambda: fetch_chl_schedule_events("QMJHL", QMJHL_SCHEDULE_URL)),
        ("WHL", lambda: fetch_chl_schedule_events("WHL", WHL_SCHEDULE_URL)),
    ]

    from datetime import datetime
   
    for name, parser in parsers:
        try:
            events = parser()
            print(f"[INFO] {name}: {len(events)} upcoming scheduled games found")
            all_events.extend(events)
        except Exception as exc:
            print(f"[ERROR] {name}: {exc}", file=sys.stderr)

    unique: dict[str, GameEvent] = {}
    for event in all_events:
        unique[event.uid] = event

    final_events = sorted(unique.values(), key=lambda e: (e.start_utc, e.league, e.home, e.away))

    if not final_events:
        print("[WARN] No events found. Check source pages or selectors.", file=sys.stderr)
    else:
        print(f"[INFO] Total events: {len(final_events)}")

    print(f"[RUN] {datetime.now().isoformat()}")
    ics_text = build_ics(final_events)

    with open(OUTPUT_FILE, "w", encoding="utf-8", newline="") as f:
        f.write(ics_text)

    print(f"[INFO] Wrote {OUTPUT_FILE}")
    # push_to_github(OUTPUT_FILE)
    return 0    

if __name__ == "__main__":
    raise SystemExit(main())