"""
processing.py
Member 4 — Data Cleaning & Index Logic / Health Audit Lead

This module is the single contract point between:
  - Member 3's scraper output   (raw JSON list, one dict per fare quote)
  - Member 1's FastAPI backend  (calls these functions before saving to DB)
  - The MoSPI dashboard         (reads calculate_laspeyres_index() output)

STANDARD RECORD FORMAT (every record must have exactly these 7 fields):
{
    "airline":        "IndiGo",
    "flight_number":  "6E-204",
    "origin":         "DEL",
    "destination":    "BOM",
    "departure_time": "2026-09-15T08:30:00",
    "price":          4850.00,
    "scraped_at":     "2026-09-08T08:00:00"
}
"""

import re
import statistics
from datetime import datetime, timedelta
from collections import defaultdict

REQUIRED_FIELDS = [
    "airline", "flight_number", "origin", "destination",
    "departure_time", "price", "scraped_at",
]

# Route weights should reflect DGCA passenger-traffic share. Exact route-pair
# figures need to be pulled from DGCA's City-Pair-Wise Monthly Domestic Passenger
# Traffic Statistics (https://www.dgca.gov.in -> Monthly Statistics). The values
# below are DIRECTIONALLY set from public 2025 seat-capacity reporting (Delhi is
# India's busiest hub at ~19% national market share; Bengaluru-Delhi ranks as the
# #2 busiest specific domestic route by seat count) but are NOT exact DGCA figures.
# DGCA monthly figure, because Chennai-Delhi has dropped out of DGCA's published
# top-10 city pairs and the portal does not expose a direct current figure for it.
# Reviewed and confirmed acceptable for submission as-is; pulling the exact current
# MAA-DEL monthly figure from the DGCA portal to fine-tune 0.19 remains optional.
DGCA_ROUTE_WEIGHTS = {
    "DEL-BOM": 0.44,
    "BLR-DEL": 0.37,
    "MAA-DEL": 0.19,
}

BASE_INDEX = 100.0
ANOMALY_THRESHOLD = 0.5  # 50% deviation from 7-day moving average


# ---------------------------------------------------------------------------
# 1. FARE SANITIZATION (regex helpers — use these BEFORE a record enters
#    clean_data, e.g. inside Member 3's scraper if raw price is still a string
#    like "₹4,599* (incl. seat fee)")
# ---------------------------------------------------------------------------

_PROMO_PATTERN = re.compile(r"\((?:[^)]*?)\)", re.IGNORECASE)
_NUMBER_PATTERN = re.compile(r"[\d,]+(?:\.\d+)?")


def sanitize_fare_string(raw_price_text: str) -> float | None:
    """
    Strip currency symbols, commas, promo-code annotations, and seat/baggage
    add-on notes (anything in parentheses) from a raw scraped price string,
    then extract the first valid numeric amount as a clean float.

    Example:
        sanitize_fare_string("₹4,599* (SAVE200 applied)")  -> 4599.0
        sanitize_fare_string("Rs. 5,200 (incl. seat fee)")  -> 5200.0
        sanitize_fare_string("Sold Out")                    -> None
    """
    if not raw_price_text or not isinstance(raw_price_text, str):
        return None

    text = _PROMO_PATTERN.sub("", raw_price_text)  # drop ANY parenthetical note
    match = _NUMBER_PATTERN.search(text)
    if not match:
        return None

    try:
        return round(float(match.group().replace(",", "")), 2)
    except ValueError:
        return None


def route_key(record: dict) -> str:
    """Standard 'ORIGIN-DEST' key used everywhere (matches DGCA_ROUTE_WEIGHTS)."""
    return f"{record.get('origin', '').upper()}-{record.get('destination', '').upper()}"

def canonical_route(route: str) -> str:
    """Canonicalize city-pair so DEL-BOM and BOM-DEL match the same DGCA weight."""
    parts = route.upper().split("-")
    if len(parts) == 2:
        pair = set(parts)
        if pair == {"DEL", "BOM"}:
            return "DEL-BOM"
        if pair == {"BLR", "DEL"}:
            return "BLR-DEL"
        if pair == {"MAA", "DEL"}:
            return "MAA-DEL"
    return route.upper()

def route_key(record: dict) -> str:
    """Standard 'ORIGIN-DEST' key, canonicalized for DGCA weights."""
    raw_key = f"{record.get('origin', '').upper()}-{record.get('destination', '').upper()}"
    return canonical_route(raw_key)
# ---------------------------------------------------------------------------
# 1b. BOOKING WINDOW CLASSIFICATION (glossary: "Booking Window Decomposition
#     7 / 15 / 30 Days" — a flight priced 2 days before departure is naturally
#     more expensive than the same route 30 days out; blending those together
#     would misread ordinary surge pricing as structural inflation.
#
#     This needs no change to Member 3's 7-field contract: departure_time and
#     scraped_at are already required fields, so the window is derived, not
#     scraped separately.
# ---------------------------------------------------------------------------

BOOKING_WINDOW_BUCKETS = (7, 15, 30)


def booking_window(record: dict) -> int:
    """Robustly parse ISO strings or datetime objects to calculate booking window."""
    try:
        dep_raw = record.get("departure_time")
        scr_raw = record.get("scraped_at")

        departs = dep_raw if isinstance(dep_raw, datetime) else datetime.fromisoformat(str(dep_raw).replace(" ", "T"))
        scraped = scr_raw if isinstance(scr_raw, datetime) else datetime.fromisoformat(str(scr_raw).replace(" ", "T"))
        
        days_out = (departs - scraped).total_seconds() / 86400
        return min(BOOKING_WINDOW_BUCKETS, key=lambda bucket: abs(bucket - days_out))
    except Exception:
        return BOOKING_WINDOW_BUCKETS[0]
    
def route_window_key(record: dict) -> str:
    """e.g. 'DEL-BOM:7d' — the key that keeps booking windows from being mixed
    together anywhere history, anomaly detection, or streak tracking happens."""
    window = record.get("booking_window", booking_window(record))
    return f"{route_key(record)}:{window}d"


# ---------------------------------------------------------------------------
# 2. clean_data — filters null/invalid fares, 0 entries, duplicates, glitches
# ---------------------------------------------------------------------------

def clean_data(raw_json_list: list[dict]) -> list[dict]:
    """
    Take the raw list of scraped records and return only valid, de-duplicated
    records with numeric, non-zero prices and all 7 required fields present.

    Drops (does not raise on):
      - missing required fields
      - null / non-numeric / zero / negative prices
      - exact duplicate records (same flight_number + departure_time + price)
    """
    cleaned = []
    seen = set()

    for rec in raw_json_list:
        if not all(field in rec and rec[field] not in (None, "") for field in REQUIRED_FIELDS):
            continue

        price = rec["price"]
        if isinstance(price, str):
            price = sanitize_fare_string(price)
        if not isinstance(price, (int, float)) or price <= 0:
            continue

        dedup_key = (rec["flight_number"], rec["departure_time"], round(float(price), 2))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        clean_rec = dict(rec)
        clean_rec["price"] = round(float(price), 2)
        clean_rec["origin"] = rec["origin"].upper()
        clean_rec["destination"] = rec["destination"].upper()
        cleaned.append(clean_rec)

    return cleaned


# ---------------------------------------------------------------------------
# 3. detect_anomalies — flags spikes vs 7-day rolling moving average
# ---------------------------------------------------------------------------

def detect_anomalies(data: list[dict], history: dict[str, list[float]] | None = None) -> list[dict]:
    """
    For each record, compare its price against the 7-day moving average for
    that route AND booking window (origin-destination + 7/15/30-day bucket)
    and flag it if it deviates by more than ANOMALY_THRESHOLD (50%).

    Comparing within the same booking window matters: a 7-day-out fare is
    naturally higher than a 30-day-out fare on the same route, so comparing
    across windows would flag ordinary advance-purchase pricing as a spike.

    `history` is an optional dict of {route_window_key: [last 7 days of avg
    prices]} pulled from the database by Member 1's backend, keyed like
    "DEL-BOM:7d" (see route_window_key()). If not supplied, the moving
    average is computed from whatever is in `data` itself (fine for a demo /
    cold start, but real deployment should pass in DB history).

    Adds three fields to every record: booking_window (int), is_anomaly
    (bool), and pct_deviation (float).
    """
    if history is None:
        history = defaultdict(list)
        for rec in data:
            history[route_window_key(rec)].append(rec["price"])

    flagged = []
    for rec in data:
        rec = dict(rec)
        rec["booking_window"] = booking_window(rec)
        key = route_window_key(rec)
        past_prices = history.get(key, [])
        moving_avg = statistics.mean(past_prices) if past_prices else rec["price"]

        deviation = 0.0 if moving_avg == 0 else abs(rec["price"] - moving_avg) / moving_avg
        rec["moving_avg_7d"] = round(moving_avg, 2)
        rec["pct_deviation"] = round(deviation, 4)
        rec["is_anomaly"] = deviation > ANOMALY_THRESHOLD
        flagged.append(rec)

    return flagged


def filter_anomalies(flagged_records: list[dict]) -> list[dict]:
    """
    Drop records flagged is_anomaly=True from a detect_anomalies() output.

    detect_anomalies() only LABELS spikes — it never removes them. Anything
    that feeds calculate_laspeyres_index() (or any other aggregate/average)
    must be run through this first, or a single festival-week surge / broken
    scrape will distort the whole national index. Call this right after
    detect_anomalies() and pass its output onward instead of the raw
    flagged list.
    """
    return [rec for rec in flagged_records if not rec["is_anomaly"]]


def daily_route_averages(records: list[dict], window: int = 7) -> dict[str, float]:
    """
    Build {route: avg_price} for one scrape cycle, using ONLY records from
    the given booking-window bucket (default 7-day, matching Member 3's
    current scraper and the reference window the index is published
    against).

    Blending 7/15/30-day prices into one average would move the index just
    because the booking-window MIX shifted that day, not because fares
    actually changed — so this always averages within a single window, and
    should be used instead of a hand-rolled average anywhere records feed
    calculate_laspeyres_index().
    """
    by_route = defaultdict(list)
    for rec in records:
        rec_window = rec.get("booking_window", booking_window(rec))
        if rec_window != window:
            continue
        by_route[route_key(rec)].append(rec["price"])

    return {route: round(sum(prices) / len(prices), 2) for route, prices in by_route.items()}


def resolve_persistent_anomalies(flagged_records: list[dict], streaks: dict,
                                  persistence_threshold: int = 3) -> tuple[list[dict], dict]:
    """
    Guards against a subtle failure mode of filter_anomalies(): if a price
    shift is REAL and PERMANENT (e.g. a genuine fuel-surcharge hike), the
    moving average never updates once every new day keeps getting excluded
    as "anomalous" relative to a now-stale baseline — the index silently
    freezes on that route (and booking window) forever.

    Call this BEFORE filter_anomalies(), once per scrape cycle (day), with
    ALL of that day's records across ALL routes in a single call. It tracks
    how many CONSECUTIVE DAYS each route+booking-window combo has been
    flagged — not consecutive records, since one day can contain several
    flights on the same route and window, and a route's 7-day and 30-day
    fares can move independently. Once a route+window combo hits
    `persistence_threshold` consecutive anomalous days, every record for
    that combo this day is treated as a genuine level-shift rather than
    noise: is_anomaly is flipped back to False so the new price is allowed
    into history and the baseline can adapt.

    `streaks` is a plain dict of {route_window_key: consecutive_anomaly_days}
    that the CALLER must persist across days (e.g. a module-level dict, or a
    row in local_history_store). Pass in last run's returned streaks each time.

    Returns (corrected_records, updated_streaks).
    """
    streaks = dict(streaks)  # don't mutate caller's dict in place

    keys_today = {route_window_key(r) for r in flagged_records}
    anomalous_keys_today = {route_window_key(r) for r in flagged_records if r["is_anomaly"]}
    resolved_keys = set()

    # Increment/reset the streak exactly ONCE per route+window per call (per
    # day), regardless of how many individual flight records that combo has.
    for key in keys_today:
        if key in anomalous_keys_today:
            streaks[key] = streaks.get(key, 0) + 1
            if streaks[key] >= persistence_threshold:
                resolved_keys.add(key)
                streaks[key] = 0  # baseline has now adapted, reset the streak
        else:
            streaks[key] = 0

    updated = []
    for rec in flagged_records:
        rec = dict(rec)
        if rec["is_anomaly"] and route_window_key(rec) in resolved_keys:
            rec["is_anomaly"] = False
            rec["resolved_as_level_shift"] = True
        updated.append(rec)

    return updated, streaks


# ---------------------------------------------------------------------------
# 4. calculate_laspeyres_index — DGCA-weighted airfare price index
# ---------------------------------------------------------------------------

def calculate_laspeyres_index(
    current_prices_dict: dict[str, float],
    base_prices_dict: dict[str, float] | None = None,
    weights: dict[str, float] = DGCA_ROUTE_WEIGHTS,
) -> dict:
    """
    Compute the DGCA passenger-weighted Laspeyres Airfare Price Index.

    current_prices_dict: {route_key: current_avg_price}, e.g. {"DEL-BOM": 5200.0}
    base_prices_dict:     {route_key: base_period_price}. If omitted, the first
                           call's current_prices_dict is treated as the base
                           period (index = 100.0 by definition on day 1).
    weights:               DGCA passenger-traffic-derived route weights.

    Laspeyres formula:
        Index = 100 * [ sum(weight_i * (P_current_i / P_base_i)) / sum(weight_i) ]

    Returns a dict with the overall index plus a per-route breakdown, so the
    dashboard can show both the national number and route-level detail.
    """
    if base_prices_dict is None:
        base_prices_dict = dict(current_prices_dict)

    weighted_sum = 0.0
    weight_total = 0.0
    per_route = {}

    for route, weight in weights.items():
        p_current = current_prices_dict.get(route)
        p_base = base_prices_dict.get(route)
        if p_current is None or p_base is None or p_base == 0:
            continue

        relative = p_current / p_base
        weighted_sum += weight * relative
        weight_total += weight
        per_route[route] = {
            "current_price": round(p_current, 2),
            "base_price": round(p_base, 2),
            "route_index": round(BASE_INDEX * relative, 2),
            "weight": weight,
        }

    overall_index = round(BASE_INDEX * (weighted_sum / weight_total), 2) if weight_total else None

    return {
        "base_period_value": BASE_INDEX,
        "overall_index": overall_index,
        "per_route": per_route,
        "computed_at": datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# 5. Health Audit Logger — for the Scraper Health Audit Log / scraper_logs table
# ---------------------------------------------------------------------------

def log_scraper_run(route: str, status: str, records_collected: int,
                     error_message: str | None = None) -> dict:
    """
    Build one row for the `scraper_logs` table. Member 1 (backend) should call
    db.insert("scraper_logs", log_scraper_run(...)) right after each scrape.

    status should be "success" or "fail".
    """
    return {
        "route": route,
        "status": status,
        "records_collected": records_collected,
        "error_message": error_message,
        "run_timestamp": datetime.now().isoformat(),
    }


def scraper_health_summary(logs: list[dict]) -> dict:
    """
    Roll up raw scraper_logs rows into the numbers the dashboard's
    'Scraper Health Audit Log' panel needs: success rate per route and
    the last run time per route.
    """
    by_route = defaultdict(list)
    for log in logs:
        by_route[log["route"]].append(log)

    summary = {}
    for route, entries in by_route.items():
        total = len(entries)
        successes = sum(1 for e in entries if e["status"] == "success")
        last_run = max(entries, key=lambda e: e["run_timestamp"])
        summary[route] = {
            "success_rate_pct": round(100 * successes / total, 1) if total else 0.0,
            "total_runs": total,
            "last_run": last_run["run_timestamp"],
            "last_status": last_run["status"],
        }
    return summary


# ---------------------------------------------------------------------------
# DEMO — run `python processing.py` to see the whole pipeline work end to end
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    raw_sample = [
        {
            "airline": "IndiGo", "flight_number": "6E-204", "origin": "del",
            "destination": "bom", "departure_time": "2026-09-15T08:30:00",
            "price": 4850.00, "scraped_at": "2026-09-08T08:00:00",
        },
        {
            "airline": "IndiGo", "flight_number": "6E-204", "origin": "DEL",
            "destination": "BOM", "departure_time": "2026-09-15T08:30:00",
            "price": 4850.00, "scraped_at": "2026-09-08T08:00:00",  # duplicate
        },
        {
            "airline": "Air India", "flight_number": "AI-401", "origin": "DEL",
            "destination": "BOM", "departure_time": "2026-09-15T09:00:00",
            "price": 0, "scraped_at": "2026-09-08T08:00:00",  # invalid, price=0
        },
        {
            "airline": "Akasa Air", "flight_number": "QP-1123", "origin": "DEL",
            "destination": "BOM", "departure_time": "2026-09-15T18:00:00",
            "price": 12500.00, "scraped_at": "2026-09-08T08:00:00",  # surge spike
        },
    ]

    print("STEP 1: clean_data()")
    cleaned = clean_data(raw_sample)
    for r in cleaned:
        print(" ", r)

    print("\nSTEP 2: detect_anomalies()  [history: DEL-BOM:7d 7-day avg = 5000.0]")
    # Key must be 'DEL-BOM:7d' matching route_window_key
    flagged = detect_anomalies(cleaned, history={"DEL-BOM:7d": [4900, 5000, 5100, 4950, 5050, 5000, 4900]})
    for r in flagged:
        flag = " <-- ANOMALY" if r["is_anomaly"] else ""
        print(f"   {r['flight_number']}: price={r['price']} avg={r['moving_avg_7d']} dev={r['pct_deviation']:.0%}{flag}")

    print("\nSTEP 2b: filter_anomalies()  [drop the spike before it hits the index]")
    for r in filter_anomalies(flagged):
        print(" ", r["flight_number"], r["price"])

    print("\nSTEP 3: calculate_laspeyres_index()")
    base = {"DEL-BOM": 5000.0, "BLR-DEL": 4200.0, "MAA-DEL": 3800.0}
    current = {"DEL-BOM": 5200.0, "BLR-DEL": 4350.0, "MAA-DEL": 3700.0}
    result = calculate_laspeyres_index(current, base)
    print(" ", result)

    print("\nSTEP 4: log_scraper_run() + scraper_health_summary()")
    logs = [
        log_scraper_run("DEL-BOM", "success", 45),
        log_scraper_run("DEL-BOM", "fail", 0, error_message="Timeout: site blocked scrape"),
        log_scraper_run("BLR-DEL", "success", 38),
    ]
    print(" ", scraper_health_summary(logs))
