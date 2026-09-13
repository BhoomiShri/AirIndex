import os
import re
import json
import shutil
import tempfile
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from playwright.sync_api import sync_playwright


def get_default_headless() -> bool:
    return os.getenv("SCRAPER_HEADLESS", "true").lower() in ("true", "1", "yes")


def select_airport(page, input_id: str, airport_code: str):
    field = page.locator(f"input#{input_id}")
    field.wait_for(state="visible", timeout=20000)
    field.click()
    page.wait_for_timeout(1000)

    suggestion = page.locator(f"ul.airports_suggestions_list li.airport:has-text('{airport_code}')").first
    if suggestion.is_visible(timeout=5000):
        suggestion.click()
    else:
        field.fill(airport_code)
        page.wait_for_timeout(1000)
        page.locator(f"ul.airports_suggestions_list li.airport:has-text('{airport_code}')").first.click()

    page.wait_for_timeout(1000)


def select_departure_date(page, days_ahead: int = 7):
    date_input = page.locator("input#onward")
    if not page.locator(".react-datepicker").is_visible():
        date_input.click()
        page.wait_for_timeout(1000)

    page.wait_for_selector(".react-datepicker", timeout=10000)

    target_date = datetime.now() + timedelta(days=days_ahead)
    day_str = str(target_date.day)

    day_cells = page.locator(
        ".react-datepicker__day:not(.react-datepicker__day--disabled):not(.react-datepicker__day--outside-month)"
    )

    clicked = False
    for i in range(day_cells.count()):
        cell = day_cells.nth(i)
        date_num = cell.locator(".datepicker-date span").inner_text().strip()
        if date_num == day_str:
            cell.click()
            clicked = True
            break

    if not clicked:
        raise RuntimeError(f"Could not find available date for +{days_ahead} days in calendar.")

    page.wait_for_timeout(1000)


def run_scraper(origin: str, destination: str, days_ahead: int = 7, headless: Optional[bool] = None) -> List[Dict[str, Any]]:
    if headless is None:
        headless = get_default_headless()

    origin = origin.upper().strip()
    destination = destination.upper().strip()
    flights = []

    temp_dir = tempfile.mkdtemp(prefix="adanione_session_")

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=temp_dir,
                headless=headless,
                ignore_default_args=["--enable-automation"],
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--start-maximized",
                ],
                viewport={"width": 1920, "height": 1080} if headless else None,
                no_viewport=True if not headless else False,
            )

            page = context.pages[0] if context.pages else context.new_page()

            page.goto("https://www.adanione.com/domestic-airlines/vistara", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector(".flight-search-wrapper", timeout=30000)
            page.wait_for_timeout(2000)

            select_airport(page, "from", origin)
            select_airport(page, "to", destination)

            select_departure_date(page, days_ahead)

            search_btn = page.locator("button.adl-button:has-text('Search')").first
            search_btn.wait_for(state="visible", timeout=10000)
            search_btn.click()

            page.wait_for_selector(".result-item-desktop", timeout=45000)
            cards = page.locator(".result-item-desktop").all()

            if not cards:
                context.close()
                raise RuntimeError(f"No flights found for route {origin} -> {destination}")

            now = datetime.now()
            target_date = now + timedelta(days=days_ahead)
            scraped_at = now.strftime("%Y-%m-%dT%H:%M:%S")
            timestamp_date = now.strftime("%Y-%m-%d")

            for card in cards:
                airline_elem = card.locator("li.airline p.fm-rm span").first
                airline_name = airline_elem.inner_text().strip() if airline_elem.count() > 0 else "Unknown Airline"

                card_id = card.get_attribute("id") or ""
                flight_match = re.search(r"\^([A-Z0-9]{2})\^(\d{3,4})", card_id)
                if flight_match:
                    flight_num = f"{flight_match.group(1)}-{flight_match.group(2)}"
                else:
                    retail_match = re.search(r"fkretail([A-Z0-9]{2})(\d{3,4})", card_id)
                    if retail_match:
                        flight_num = f"{retail_match.group(1)}-{retail_match.group(2)}"
                    else:
                        flight_num = "N/A"

                dep_elem = card.locator("li.depart > p.flx > span").first
                dep_time = dep_elem.inner_text().strip() if dep_elem.count() > 0 else "00:00"
                departure_time_iso = f"{target_date.strftime('%Y-%m-%d')}T{dep_time}:00"

                price_elem = card.locator("li.price p.fm-rb span").first
                if price_elem.count() > 0:
                    raw_price = price_elem.inner_text()
                    digits = re.sub(r"[^\d.]", "", raw_price)
                    if not digits:
                        continue
                    price_val = float(digits)
                else:
                    continue

                flights.append({
                    "airline": airline_name,
                    "flight_number": flight_num,
                    "origin": origin,
                    "destination": destination,
                    "departure_time": departure_time_iso,
                    "price": price_val,
                    "timestamp": timestamp_date,
                    "scraped_at": scraped_at
                })

            context.close()

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if not flights:
        raise RuntimeError(f"No valid flight data extracted for {origin} -> {destination}")

    return flights


async def run_scraper_async(origin: str, destination: str, days_ahead: int = 7, headless: Optional[bool] = None) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(run_scraper, origin, destination, days_ahead, headless)


if __name__ == "__main__":
    results = run_scraper("DEL", "BOM", days_ahead=7)
    print(json.dumps(results, indent=2))