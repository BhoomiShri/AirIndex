import os
import re
import json
import shutil
import tempfile
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from playwright.sync_api import sync_playwright, Page


def get_default_headless() -> bool:
    return os.getenv("SCRAPER_HEADLESS", "false").lower() in ("true", "1", "yes")


def dismiss_popups(page: Page):
    """Dismiss cookie banners or promotional popovers if visible."""
    selectors = [
        "#cookie-close",
        "button:has-text('Accept')",
        "button:has-text('Accept All')",
        "button:has-text('OK')",
        "button.close",
        "[aria-label='Close']",
        ".modal-close",
        ".close-btn",
    ]
    for sel in selectors:
        try:
            elem = page.locator(sel).first
            if elem.is_visible(timeout=1000):
                elem.click()
                page.wait_for_timeout(300)
        except Exception:
            pass


def select_indigo_station(page: Page, field_type: str, airport_code: str):
    """
    Selects From or To using IndiGo's exact DOM classes:
    .search-widget-form-body__from and .search-widget-form-body__to
    """
    container_class = f".search-widget-form-body__{field_type}"
    container = page.locator(container_class).first
    container.wait_for(state="visible", timeout=15000)

    # Click the container to open the combobox/popover
    container.click()
    page.wait_for_timeout(500)

    # Target the combobox input inside this container
    station_input = container.locator("input").first
    if station_input.is_visible():
        station_input.fill("")
        station_input.press_sequentially(airport_code, delay=100)
    else:
        page.keyboard.type(airport_code, delay=100)

    page.wait_for_timeout(1000)

    # Click the matching airport suggestion from the popover
    suggestion = page.locator(
        f".popover__content :is(li, div, button):has-text('{airport_code}'), "
        f"div[role='combobox'] ~ div :is(li, div):has-text('{airport_code}'), "
        f":is(li, div.airport-card, div[role='option']):has-text('{airport_code}')"
    ).first

    if suggestion.is_visible(timeout=4000):
        suggestion.click()
    else:
        page.keyboard.press("Enter")

    page.wait_for_timeout(800)


def select_indigo_date(page: Page, days_ahead: int = 7):
    """Selects target date using .search-widget-form-body__departure and dismisses popover."""
    target_date = datetime.now() + timedelta(days=days_ahead)
    day_str = str(target_date.day)
    date_iso = target_date.strftime("%Y-%m-%d")

    # If calendar popover did not auto-open after destination selection, open it
    dep_field = page.locator(".search-widget-form-body__departure").first
    dep_field.wait_for(state="visible", timeout=10000)
    dep_field.click()
    page.wait_for_timeout(800)

    # Match target day cell inside calendar popover
    day_cell = page.locator(
        f"div[data-date='{date_iso}']:visible, "
        f"button[aria-label*='{date_iso}']:visible, "
        f".react-calendar__tile:not(:disabled):has-text('{day_str}'):visible, "
        f"div.calendar-day:not(.disabled):has-text('{day_str}'):visible"
    ).first

    if day_cell.is_visible(timeout=4000):
        day_cell.click()
    else:
        # Fallback to exact text match on visible day numbers
        page.locator(f"span:text-is('{day_str}'):visible").first.click()

    page.wait_for_timeout(600)

    # IMPORTANT: Close calendar popover so it doesn't intercept the Search button click
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)


def trigger_indigo_search(page: Page):
    """
    Clicks the enabled Search button:
    <button class="skyplus-button--filled skyplus-button--filled-primary skyplus-button--medium">Search</button>
    """
    # Ensure any active dropdown or popover overlay is dismissed
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)

    search_btn = page.locator(
        "button.skyplus-button--filled-primary:has-text('Search'), "
        "div.search-btn button, "
        "button[role='button']:has-text('Search')"
    ).first

    search_btn.wait_for(state="visible", timeout=10000)
    search_btn.scroll_into_view_if_needed()
    page.wait_for_timeout(300)

    # First native Playwright click
    search_btn.click()
    page.wait_for_timeout(800)

    # JavaScript fallback: dispatches mousedown, mouseup, click to trigger React state listeners
    page.evaluate("""
        () => {
            const btn = document.querySelector('button.skyplus-button--filled-primary, div.search-btn button');
            if (btn && !btn.disabled && btn.getAttribute('aria-disabled') !== 'true') {
                btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                btn.click();
            }
        }
    """)
    page.wait_for_timeout(2000)


def run_scraper(origin: str, destination: str, days_ahead: int = 7, headless: Optional[bool] = None) -> List[Dict[str, Any]]:
    if headless is None:
        headless = get_default_headless()

    origin = origin.upper().strip()
    destination = destination.upper().strip()
    flights = []

    target_date = datetime.now() + timedelta(days=days_ahead)
    date_iso = target_date.strftime("%Y-%m-%d")

    temp_dir = tempfile.mkdtemp(prefix="indigo_session_")

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=temp_dir,
                headless=headless,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="en-IN",
                viewport={"width": 1920, "height": 1080},
                ignore_default_args=["--enable-automation"],
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--start-maximized"
                ],
            )

            page = context.pages[0] if context.pages else context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            page.goto("https://www.goindigo.in", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector(".search-widget-form", timeout=30000)
            page.wait_for_timeout(1000)
            dismiss_popups(page)

            # Ensure One Way is active
            one_way_label = page.locator("label[for='radio-input-triptype-oneWay']").first
            if one_way_label.is_visible() and not one_way_label.locator("input").is_checked():
                one_way_label.click()
                page.wait_for_timeout(400)

            # Select Origin ("from") and Destination ("to")
            select_indigo_station(page, "from", origin)
            select_indigo_station(page, "to", destination)

            # Select Departure Date
            select_indigo_date(page, days_ahead)

            dismiss_popups(page)

            # Click Search button
            trigger_indigo_search(page)

            # Wait for search results container
            flight_locator = page.locator(
                ".srp__search-result-list__item, "
                "[data-testid='flight-card'], "
                ".flight-card, "
                ".flight-details, "
                "div[class*='flight-card'], "
                "div[class*='flightResult']"
            )
            flight_locator.first.wait_for(state="attached", timeout=45000)
            page.wait_for_timeout(3000)

            cards = flight_locator.all()
            if not cards:
                raise RuntimeError(f"No flights found for route {origin} -> {destination}")

            now = datetime.now()
            scraped_at = now.strftime("%Y-%m-%dT%H:%M:%S")
            timestamp_date = now.strftime("%Y-%m-%d")

            for card in cards:
                card_text = card.inner_text()
                if not card_text or "Sold Out" in card_text:
                    continue

                # Flight Number (e.g. 6E 204 or 6E-204)
                match_num = re.search(r"6E[-\s]?\d{3,4}", card_text)
                flight_no = match_num.group(0).replace(" ", "-") if match_num else "6E"

                # Departure Time (HH:MM)
                match_time = re.search(r"\b([01]\d|2[0-3]):[0-5]\d\b", card_text)
                dep_time_raw = match_time.group(0) if match_time else "00:00"
                departure_time_iso = f"{date_iso}T{dep_time_raw}:00"

                # Price extraction
                prices = re.findall(r"₹\s*([\d,]+)", card_text)
                if not prices:
                    prices = re.findall(r"\b([\d,]{4,6})\b", card_text)
                if not prices:
                    continue

                price_clean = prices[0].replace(",", "")
                try:
                    price_val = float(price_clean)
                    if price_val < 1000:
                        continue
                except ValueError:
                    continue

                flights.append({
                    "airline": "IndiGo",
                    "flight_number": flight_no,
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
        raise RuntimeError(f"No valid fares parsed for {origin} -> {destination}")

    return flights


async def run_scraper_async(origin: str, destination: str, days_ahead: int = 7, headless: Optional[bool] = None) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(run_scraper, origin, destination, days_ahead, headless)


if __name__ == "__main__":
    data = run_scraper("DEL", "BOM", days_ahead=7)
    print(json.dumps(data, indent=2))
