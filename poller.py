#!/usr/bin/env python3
"""BookMyShow/District ticket watcher with exact-date JSON API detection."""

import json
import os
import re
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
STATE_PATH = Path(os.environ.get("STATE_PATH", ROOT / "state.json"))

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Origin": "https://in.bookmyshow.com",
    "Referer": "https://in.bookmyshow.com/",
}


def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def load_config():
    cfg = load_json(CONFIG_PATH, default={}) or {}
    env_map = {
        "TARGET_URL": "target_url",
        "THEATRE": "theatre",
        "MOVIE": "movie",
        "REQUESTED_DATE": "requested_date",
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
        "BMS_VENUE_CODE": "venue_code",
        "BMS_REGION_CODE": "region_code",
        "BMS_EVENT_CODE": "event_code",
    }
    for env_key, cfg_key in env_map.items():
        if os.environ.get(env_key):
            cfg[cfg_key] = os.environ[env_key]
    if os.environ.get("HEADERS_JSON"):
        cfg["headers"] = json.loads(os.environ["HEADERS_JSON"])

    if cfg.get("url_template") and cfg.get("requested_date"):
        cfg["target_url"] = cfg["url_template"].format(date=cfg["requested_date"])

    detector = cfg.get("detector", "api_json")
    required = ["target_url", "telegram_bot_token", "telegram_chat_id"]
    if detector in ("api_json", "imax_api_json", "bms_date", "venue_date"):
        required.append("requested_date")
    if detector in ("api_json", "imax_api_json"):
        required.extend(["venue_code", "region_code"])
    elif detector not in ("venue_date", "bms_date"):
        required.append("theatre")

    missing = [key for key in required if not cfg.get(key)]
    if missing:
        sys.exit(f"Missing required config: {', '.join(missing)}")
    return cfg


def send_telegram(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": False},
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok", False):
        raise RuntimeError("Telegram API did not confirm the message was sent")


def fetch(cfg):
    """Fetch target URL, optionally through ScraperAPI or a configured proxy."""
    headers = dict(DEFAULT_HEADERS)
    headers.update(cfg.get("headers", {}))
    scraper_key = os.environ.get("SCRAPERAPI_KEY")
    if scraper_key:
        api_url = "https://api.scraperapi.com/?" + urllib.parse.urlencode(
            {"api_key": scraper_key, "country_code": "in", "url": cfg["target_url"]}
        )
        response = requests.get(api_url, headers=headers, timeout=90)
        response.raise_for_status()
        return response.text

    proxy = os.environ.get("PROXY_URL")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    session = requests.Session()
    session.headers.update(headers)
    response = session.get(cfg["target_url"], timeout=45, proxies=proxies)
    response.raise_for_status()
    return response.text


def _walk(value):
    """Yield every dict nested inside a JSON object/list."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def is_available_api_json(response_text, cfg):
    """Require the exact requested date to be enabled and the target event to have showtimes."""
    try:
        payload = json.loads(response_text)
    except (json.JSONDecodeError, TypeError):
        print("API detector: response was not valid JSON; treating as unavailable.")
        return False

    requested = str(cfg["requested_date"])
    venue_code = str(cfg.get("venue_code", ""))
    region_code = str(cfg.get("region_code", ""))
    event_code = str(cfg.get("event_code", ""))
    movie_name = (cfg.get("movie") or "").strip().casefold()

    # If response metadata includes a venue/region, reject mismatches.
    for key, expected in (("VenueCode", venue_code), ("venueCode", venue_code),
                          ("RegionCode", region_code), ("regionCode", region_code)):
        actual = payload.get(key) if isinstance(payload, dict) else None
        if actual and expected and str(actual).casefold() != expected.casefold():
            print(f"API detector: {key} mismatch; treating as unavailable.")
            return False

    date_entries = []
    if isinstance(payload, dict):
        for key in ("ShowDatesArray", "showDatesArray", "show_dates", "dates"):
            if isinstance(payload.get(key), list):
                date_entries = payload[key]
                break
    date_entry = next(
        (item for item in date_entries if isinstance(item, dict) and
         str(item.get("DateCode", item.get("dateCode", item.get("Date", item.get("date", ""))))) == requested),
        None,
    )
    if date_entry is None:
        print(f"API detector: requested date {requested} not found in date list.")
        return False

    disabled = date_entry.get("isDisabled", date_entry.get("IsDisabled", False))
    if disabled is True or str(disabled).strip().lower() in ("true", "1", "yes"):
        print(f"API detector: requested date {requested} is still disabled.")
        return False

    # Only inspect the ShowDetails section for the exact requested date, not fallback dates.
    show_details = payload.get("ShowDetails", payload.get("showDetails", [])) if isinstance(payload, dict) else []
    if isinstance(show_details, dict):
        show_details = [show_details]
    exact_date_sections = []
    for section in show_details if isinstance(show_details, list) else []:
        if not isinstance(section, dict):
            continue
        section_date = str(section.get("Date", section.get("DateCode", section.get("date", section.get("dateCode", "")))))
        if section_date == requested:
            exact_date_sections.append(section)

    if not exact_date_sections:
        print(f"API detector: no show details for exact date {requested}; fallback dates are ignored.")
        return False

    for section in exact_date_sections:
        for item in _walk(section):
            code = str(item.get("EventCode", item.get("eventCode", "")))
            name = str(item.get("EventName", item.get("eventName", item.get("Name", item.get("name", ""))))).casefold()
            if event_code and code and code != event_code:
                continue
            matches_event = (bool(event_code) and code == event_code) or (bool(movie_name) and movie_name in name)
            if not matches_event:
                continue
            showtimes = item.get("ShowTimes", item.get("showTimes", item.get("Showtimes", item.get("showtimes"))))
            if isinstance(showtimes, list) and len(showtimes) > 0:
                print(f"API detector: found {len(showtimes)} showtimes for target event on {requested}.")
                return True
    print(f"API detector: no showtimes for the target event on {requested}.")
    return False




def is_available_imax_api_json(response_text, cfg):
    """Detect any IMAX screening in ShowDetails for the exact requested date."""
    try:
        payload = json.loads(response_text)
    except (json.JSONDecodeError, TypeError):
        preview = str(response_text)[:160].replace("\n", " ")
        print("IMAX API detector: response was not valid JSON; treating as unavailable.")
        print(f"IMAX API detector: response preview: {preview!r}")
        return False

    if not isinstance(payload, dict):
        print(f"IMAX API detector: expected a JSON object, got {type(payload).__name__}.")
        return False

    requested = str(cfg.get("requested_date", ""))
    print("IMAX API detector: JSON parsed successfully.")
    print("IMAX API detector: top-level keys:", list(payload.keys()))

    date_entries = payload.get("ShowDatesArray", payload.get("showDatesArray", []))
    if isinstance(date_entries, list):
        date_codes = []
        for entry in date_entries:
            if isinstance(entry, dict):
                date_code = entry.get(
                    "DateCode",
                    entry.get("dateCode", entry.get("Date", entry.get("date", ""))),
                )
                if date_code:
                    date_codes.append(str(date_code))
            elif isinstance(entry, (str, int)):
                date_codes.append(str(entry))
        print("IMAX API detector: dates listed by API:", date_codes)
        if requested not in date_codes:
            print(f"IMAX API detector: requested date {requested} is not listed by API.")
            # Continue checking ShowDetails: some responses include exact-date
            # details even when the date summary is incomplete.
    else:
        print("IMAX API detector: ShowDatesArray missing or not a list.")

    show_details = payload.get("ShowDetails", payload.get("showDetails", []))
    if isinstance(show_details, dict):
        show_details = [show_details]
    if not isinstance(show_details, list):
        print("IMAX API detector: ShowDetails missing or not a list.")
        return False

    exact_sections = []
    for section in show_details:
        if not isinstance(section, dict):
            continue
        section_date = str(
            section.get(
                "Date",
                section.get("DateCode", section.get("date", section.get("dateCode", ""))),
            )
        )
        print("IMAX API detector: ShowDetails date:", section_date)
        if section_date == requested:
            exact_sections.append(section)

    if not exact_sections:
        print(f"IMAX API detector: no ShowDetails for exact date {requested}; fallback dates ignored.")
        return False

    matches = []
    for section in exact_sections:
        for item in _walk(section):
            showtimes = item.get(
                "ShowTimes",
                item.get("showTimes", item.get("Showtimes", item.get("showtimes"))),
            )
            if not isinstance(showtimes, list) or not showtimes:
                continue

            event_name = str(
                item.get(
                    "EventName",
                    item.get("eventName", item.get("EventTitle", item.get("Name", item.get("name", "")))),
                )
            )
            dimension = str(item.get("EventDimension", item.get("eventDimension", "")))
            context = [event_name, dimension]

            for show in showtimes:
                if isinstance(show, dict):
                    for key in ("Attributes", "Attribute", "Format", "Dimension", "EventDimension", "ShowTime", "Time"):
                        value = show.get(key)
                        if value is not None:
                            context.append(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))

            if re.search(r"\bimax\b", " ".join(context), re.IGNORECASE):
                title = event_name or "Unknown movie"
                times = []
                for show in showtimes:
                    if isinstance(show, dict):
                        value = show.get("ShowTime", show.get("Time", show.get("showTime", show.get("time", ""))))
                        if value:
                            times.append(str(value))
                matches.append((title, times, len(showtimes)))

    if matches:
        for title, times, count in matches:
            time_text = ", ".join(times) if times else f"{count} showtime(s)"
            print(f"IMAX API detector: MATCH on {requested}: {title} | {time_text}")
        return True

    print(f"IMAX API detector: exact-date section found, but no IMAX showtimes on {requested}.")
    return False


def is_available_bms_date(page_text, cfg):
    tokens = re.findall(r"20\d{6}", page_text)
    if not tokens:
        return False
    counts = Counter(tokens)
    requested = str(cfg["requested_date"])
    return counts.most_common(1)[0][0] == requested and counts.get(requested, 0) >= cfg.get("min_references", 10)



def is_available_venue_date(page_text, cfg):
    """
    Check whether the exact requested venue/date page contains
    an IMAX screening with a showtime.
    """
    requested_date = str(cfg["requested_date"])
    codes = cfg.get("venue_codes") or [cfg.get("venue_code", "")]

    # Find the canonical URL to verify the exact venue and date.
    canonical_url = ""

    for match in re.finditer(r"<link\b[^>]*>", page_text, re.IGNORECASE):
        tag = match.group(0)

        rel = re.search(
            r"\brel\s*=\s*[\"']([^\"']+)[\"']",
            tag,
            re.IGNORECASE,
        )
        href = re.search(
            r"\bhref\s*=\s*[\"']([^\"']+)[\"']",
            tag,
            re.IGNORECASE,
        )

        if rel and href and rel.group(1).lower() == "canonical":
            canonical_url = href.group(1)
            break

    path = urllib.parse.urlparse(canonical_url).path

    exact_venue_date = any(
        re.search(
            rf"/buytickets/{re.escape(str(code))}/"
            rf"{re.escape(requested_date)}(?:/|$)",
            path,
            re.IGNORECASE,
        )
        for code in codes
        if code
    )

    if not exact_venue_date:
        print(
            f"IMAX detector: canonical venue/date mismatch. "
            f"Found path: {path or '(not found)'}"
        )
        return False

    # Normalize whitespace in the fetched page.
    normalized = re.sub(r"\s+", " ", page_text)

    # Look for an IMAX label with a nearby showtime.
    imax_pattern = re.compile(r"\bIMAX(?:\s+2D)?\b", re.IGNORECASE)

    time_pattern = re.compile(
        r"\b(?:0?[1-9]|1[0-2]):[0-5]\d\s*(?:AM|PM)\b",
        re.IGNORECASE,
    )

    for match in imax_pattern.finditer(normalized):
        start = max(0, match.start() - 500)
        end = min(len(normalized), match.end() + 500)

        nearby_text = normalized[start:end]

        if time_pattern.search(nearby_text):
            print(
                f"IMAX detector: found an IMAX label and a nearby "
                f"showtime for {requested_date}."
            )
            return True

    print(
        f"IMAX detector: no IMAX screening with a nearby showtime "
        f"was detected for {requested_date}."
    )
    return False


def is_available_generic(page_text, cfg):
    text = re.sub(r"\s+", " ", page_text).casefold()
    if re.sub(r"\s+", " ", cfg["theatre"]).strip().casefold() not in text:
        return False
    movie = cfg.get("movie")
    if movie and re.sub(r"\s+", " ", movie).strip().casefold() not in text:
        return False
    open_signals = cfg.get("open_signals", ["book tickets", "book now", "showtimes", "showtime", "select seats"])
    closed_signals = cfg.get("closed_signals", ["notify me", "coming soon"])
    has_open = any(signal.casefold() in text for signal in open_signals)
    only_closed = any(signal.casefold() in text for signal in closed_signals) and not has_open
    return has_open and not only_closed


def is_available(response_text, cfg):
    detector = cfg.get("detector", "api_json")
    if detector == "api_json":
        return is_available_api_json(response_text, cfg)
    if detector == "imax_api_json":
        return is_available_imax_api_json(response_text, cfg)
    if detector == "venue_date":
        return is_available_venue_date(response_text, cfg)
    if detector == "bms_date":
        return is_available_bms_date(response_text, cfg)
    return is_available_generic(response_text, cfg)


def main():
    cfg = load_config()

    # Manual Telegram test mode: sends one test message and exits before
    # fetching BookMyShow or reading/writing state.json.
    if os.environ.get("TELEGRAM_TEST_MODE", "").strip().lower() in ("1", "true", "yes", "on"):
        requested_date = str(cfg.get("requested_date", ""))
        pretty_date = (
            f"{requested_date[6:8]}-{requested_date[4:6]}-{requested_date[0:4]}"
            if len(requested_date) == 8 and requested_date.isdigit()
            else requested_date or "not configured"
        )
        venue = cfg.get("venue_label") or cfg.get("theatre") or cfg.get("venue_code", "target")
        message = (
            "✅ Telegram test successful!\\n\\n"
            "GitHub Actions can send messages to this chat.\\n\\n"
            f"Movie: {cfg.get('movie', 'Jailer 2 - Tamil')}\\n"
            f"Theatre: {venue}\\n"
            f"Target date: {pretty_date}\\n\\n"
            "This was a test only; booking status and state.json were not changed."
        )
        send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"], message)
        print("Telegram test message sent successfully. Booking check and state tracking were skipped.")
        return 0

    state = load_json(STATE_PATH, default={"available": False}) or {"available": False}
    label = f"{cfg.get('movie', 'movie')} @ {cfg.get('theatre', cfg.get('venue_code', 'target'))}"

    try:
        response_text = fetch(cfg)
    except requests.RequestException as exc:
        print(f"[{label}] fetch failed: {exc}")
        return 0

    requested_date = str(cfg.get("requested_date", ""))
    print(f"Requested date: {requested_date or '(not configured)'}")
    available = is_available(response_text, cfg)
    previous = bool(state.get("available", False))
    print(f"[{label}] available={available} (was {previous})")

    if available and not previous:
        pretty_date = f"{requested_date[6:8]}-{requested_date[4:6]}-{requested_date[0:4]}" if len(requested_date) == 8 else requested_date
        venue = cfg.get("venue_label") or cfg.get("theatre") or cfg.get("venue_code", "")
        message = (
            "🎬 IMAX BOOKING DETECTED!\n\n"
            "Screen format: IMAX\n"
            f"Theatre: {venue}\n"
            f"Date: {pretty_date}\n\n"
            f"Check available movies and showtimes here:\n"
            f"{cfg.get('booking_url', cfg['target_url'])}"
        )
        try:
            send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"], message)
            print(f"[{label}] Telegram notification sent.")
        except (requests.RequestException, RuntimeError) as exc:
            print(f"Telegram notification failed: {exc}")
            return 1

    if available != previous:
        state["available"] = available
        state["checked_at"] = int(time.time())
        state["requested_date"] = requested_date
        save_json(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
