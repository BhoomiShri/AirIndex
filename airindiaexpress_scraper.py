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
    return os.getenv("SCRAPER_HEADLESS", "false").lower() in ("true", "1", "yes")


def select_station(page, field_id: str, input_id: str, airport_code: str):
    container = page.locator(f"#new-origin-destination-container-desktop #{field_id}").first
    container.wait_for(state="visible", timeout=20000)
    container.click()
    page.wait_for_timeout(500)

    inp = page.locator(f"input#{input_id}").first
    inp.wait_for(state="visible", timeout=10000)
    inp.fill(airport_code)
    page.wait_for_timeout(1000)

    item = page.locator(f".arrival-dropdown-holder button:has-text('{airport_code}')").first
    if not item.is_visible():
        raise RuntimeError(f"Airport code {airport_code} not found in dropdown.")
    item.click()
    page.wait_for_timeout(1000)


def select_date(page, days_ahead: int = 7):
    if not page.locator(".dialog-date-picker.open").is_visible():
        date_btn = page.locator("#new-date-selection-container-desktop #start-date-input-button").first
        date_btn.click()
        page.wait_for_timeout(1000)

    target_date = datetime.now() + timedelta(days=days_ahead)
    day_str = str(target_date.day)

    day_cell = page.locator(f".calendar-content .new-day.day:not(.disabled):has(.new-calender-day:text-is('{day_str}'))").first
    if not day_cell.is_visible():
        raise RuntimeError(f"Date for +{days_ahead} days ({day_str}) not available in calendar.")
    day_cell.click()
    page.wait_for_timeout(500)

    confirm_btn = page.locator("#calendar-confirm").first
    if confirm_btn.is_visible():
        confirm_btn.click()
        page.wait_for_timeout(500)


def run_scraper(origin: str, destination: str, days_ahead: int = 7, headless: Optional[bool] = None) -> List[Dict[str, Any]]:
    if headless is None:
        headless = get_default_headless()

    origin = origin.upper().strip()
    destination = destination.upper().strip()
    flights = []

    temp_dir = tempfile.mkdtemp(prefix="aix_session_")

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=temp_dir,
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--start-maximized"
                ],
                viewport={"width": 1920, "height": 1080} if headless else None,
                no_viewport=True if not headless else False,
            )

            page = context.pages[0] if context.pages else context.new_page()

            page.goto("https://www.airindiaexpress.com/home", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector(".new-flight-search-widget-container", timeout=30000)
            page.wait_for_timeout(1500)

            one_way_btn = page.locator("#One Way").first
            if one_way_btn.is_visible():
                one_way_btn.click()
                page.wait_for_timeout(500)

            select_station(page, "new-flight-search-origin-field-text", "basic-url-origin", origin)
            select_station(page, "new-flight-search-destination-field-text", "basic-url-destination", destination)

            select_date(page, days_ahead)

            search_btn = page.locator(".new-flight-search-widget-container .new-search-flight-button-container").first
            search_btn.click()

            page.wait_for_selector(".flight-details-wrapper", timeout=45000)
            cards = page.locator(".flight-details-wrapper").all()

            if not cards:
                raise RuntimeError(f"No flights found for {origin} -> {destination}")

            now = datetime.now()
            target_date = now + timedelta(days=days_ahead)
            scraped_at = now.strftime("%Y-%m-%dT%H:%M:%S")
            timestamp_date = now.strftime("%Y-%m-%d")

            for card in cards:
                num_elem = card.locator(".flight-det-number").first
                if num_elem.count() == 0:
                    continue
                flight_no = num_elem.inner_text().strip().replace(" ", "-")

                dep_time_elem = card.locator(".inner-flight-time").first
                dep_time = dep_time_elem.inner_text().strip() if dep_time_elem.count() > 0 else "00:00"
                departure_iso = f"{target_date.strftime('%Y-%m-%d')}T{dep_time}:00"

                price_elem = card.locator(".current-fare").first
                if price_elem.count() == 0:
                    continue
                raw_price = price_elem.inner_text()
                price_digits = re.sub(r"[^\d.]", "", raw_price)
                if not price_digits:
                    continue
                price_val = float(price_digits)

                flights.append({
                    "airline": "Air India Express",
                    "flight_number": flight_no,
                    "origin": origin,
                    "destination": destination,
                    "departure_time": departure_iso,
                    "price": price_val,
                    "timestamp": timestamp_date,
                    "scraped_at": scraped_at
                })

            context.close()

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if not flights:
        raise RuntimeError(f"Could not parse any valid fares for {origin} -> {destination}")

    return flights


async def run_scraper_async(origin: str, destination: str, days_ahead: int = 7, headless: Optional[bool] = None) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(run_scraper, origin, destination, days_ahead, headless)


if __name__ == "__main__":
    results = run_scraper("DEL", "BOM", days_ahead=7)
    print(json.dumps(results, indent=2))