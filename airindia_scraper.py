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
    """Dismiss OneTrust cookies, marketing dialogs, or overlay banners."""
    selectors = [
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept All')",
        "button:has-text('Accept all cookies')",
        "button:has-text('Accept')",
        "button[aria-label='Close']",
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


def ensure_one_way(page: Page):
    """Ensures Air India's trip type is set to One Way to prevent requiring a return date."""
    one_way_label = page.locator(
        "label.ai-radio-group__option:has(input[value='one-way']), "
        "label:has-text('One Way')"
    ).first

    if one_way_label.is_visible(timeout=5000):
        is_checked = page.locator("label.ai-radio-group__option--checked:has-text('One Way')").first.is_visible()
        if not is_checked:
            one_way_label.click(force=True)
            page.wait_for_timeout(600)


def select_airindia_station(page: Page, field_type: str, airport_code: str):
    """
    Selects origin or destination and explicitly clicks the option inside
    .ai-autocomplete-dropdown so Angular validates the form control.
    """
    field_class = f".ai-origin-destination__field--{field_type}"
    container = page.locator(field_class).first
    container.wait_for(state="visible", timeout=15000)

    # Click container to focus input
    container.click()
    page.wait_for_timeout(400)

    # Target the autocomplete input
    inp = container.locator("input.ai-autocomplete-input, input[role='combobox']").first
    inp.wait_for(state="visible", timeout=5000)
    inp.fill("")
    inp.press_sequentially(airport_code, delay=120)
    page.wait_for_timeout(1000)

    # Wait for the autocomplete overlay panel to appear in DOM
    page.wait_for_selector(
        ".ai-autocomplete-dropdown, .mat-mdc-autocomplete-panel",
        timeout=10000
    )

    # Click the matching airport option in the dropdown
    option = page.locator(
        f".ai-autocomplete-dropdown .ai-autocomplete-option:has-text('{airport_code}'), "
        f".mat-mdc-autocomplete-panel mat-option:has-text('{airport_code}'), "
        f"mat-option:has-text('{airport_code}')"
    ).first

    if option.is_visible(timeout=5000):
        option.click(force=True)
    else:
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(200)
        page.keyboard.press("Enter")

    page.wait_for_timeout(800)


def select_airindia_date(page: Page, days_ahead: int = 7):
    """
    Selects date from Air India's mat-calendar, then clicks the visible Confirm button.
    Filters out hidden modal-cancel buttons.
    """
    target_date = datetime.now() + timedelta(days=days_ahead)
    day_str = str(target_date.day)
    # Air India aria-label format: M/D/YYYY (e.g. 9/14/2026, 3/25/2025)
    aria_date = f"{target_date.month}/{target_date.day}/{target_date.year}"

    # 1. Open date picker if not already open
    calendar_visible = page.locator(".ai-date-picker__body-row").first.is_visible()
    if not calendar_visible:
        date_btn = page.locator(
            "button.ai-booking-widget__date-section, "
            ".ai-booking-widget__date-field"
        ).first
        date_btn.click()
        page.wait_for_timeout(800)

    # 2. Click the date cell in from-calendar
    cell = page.locator(
        f"mat-calendar[data-calendar-id='from-calendar'] button.mat-calendar-body-cell:not(.mat-calendar-body-disabled)[aria-label='{aria_date}']"
    ).first

    if not cell.is_visible():
        cell = page.locator(
            f"mat-calendar[data-calendar-id='from-calendar'] button.mat-calendar-body-cell:not(.mat-calendar-body-disabled):has(span.mat-calendar-body-cell-content:text-is('{day_str}'))"
        ).first

    if not cell.is_visible():
        cell = page.locator(
            f"mat-calendar[data-calendar-id='to-calendar'] button.mat-calendar-body-cell:not(.mat-calendar-body-disabled)[aria-label='{aria_date}']"
        ).first

    if not cell.is_visible():
        next_month_btn = page.locator("button.ai-date-picker__arrow--right, button.mat-calendar-next-button").first
        if next_month_btn.is_visible():
            next_month_btn.click()
            page.wait_for_timeout(600)
            cell = page.locator(
                f"mat-calendar button.mat-calendar-body-cell:not(.mat-calendar-body-disabled)[aria-label='{aria_date}']"
            ).first

    cell.wait_for(state="visible", timeout=10000)
    cell.click(force=True)
    page.wait_for_timeout(800)

    # 3. Click the VISIBLE Confirm button (ignoring any hidden modal-button-cancel)
    try:
        confirm_btn = page.locator(
            "ai-button button:not(.modal-button-cancel):has(span.ai-button__label:text-is('Confirm')):visible, "
            "button:not(.modal-button-cancel):has(span.ai-button__label:text-is('Confirm')):visible, "
            "span.ai-button__label:text-is('Confirm'):visible"
        ).first
        confirm_btn.wait_for(state="visible", timeout=6000)
        confirm_btn.click(force=True)
    except Exception:
        # Fallback: JavaScript scan for only visible Confirm button on screen
        page.evaluate("""
            () => {
                const candidates = Array.from(document.querySelectorAll('button, span.ai-button__label'));
                for (const el of candidates) {
                    if (el.textContent.trim() === 'Confirm' && el.offsetParent !== null && !el.classList.contains('modal-button-cancel')) {
                        (el.closest('button') || el).click();
                        return true;
                    }
                }
                return false;
            }
        """)

    page.wait_for_timeout(800)


def trigger_search(page: Page):
    """
    Clicks the Search button. If Angular form state still has disabled attribute,
    removes it via JavaScript and executes the click.
    """
    search_btn = page.locator(".ai-booking-widget__search-btn button").first
    search_btn.wait_for(state="visible", timeout=10000)

    # Self-healing: ensure disabled attribute is cleared, then click
    page.evaluate("""
        () => {
            const btn = document.querySelector('.ai-booking-widget__search-btn button');
            if (btn) {
                btn.removeAttribute('disabled');
                btn.classList.remove('ai-button--disabled');
                btn.click();
            }
        }
    """)

    page.wait_for_timeout(1500)


def run_scraper(origin: str, destination: str, days_ahead: int = 7, headless: Optional[bool] = None) -> List[Dict[str, Any]]:
    if headless is None:
        headless = get_default_headless()

    origin = origin.upper().strip()
    destination = destination.upper().strip()
    flights = []

    now = datetime.now()
    target_date = now + timedelta(days=days_ahead)
    date_iso = target_date.strftime("%Y-%m-%d")
    scraped_at = now.strftime("%Y-%m-%dT%H:%M:%S")
    timestamp_date = now.strftime("%Y-%m-%d")

    temp_dir = tempfile.mkdtemp(prefix="airindia_session_")

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=temp_dir,
                headless=headless,
                viewport={"width": 1920, "height": 1080} if headless else None,
                no_viewport=True if not headless else False,
                locale="en-IN",
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

            page.goto("https://www.airindia.com", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector(".ai-booking-widget__form-row", timeout=30000)
            page.wait_for_timeout(1000)
            dismiss_popups(page)

            # 1. Select One Way
            ensure_one_way(page)

            # 2. Select Stations
            select_airindia_station(page, "origin", origin)
            select_airindia_station(page, "destination", destination)

            # 3. Select Date and Confirm
            select_airindia_date(page, days_ahead)

            dismiss_popups(page)

            # 4. Click Search Button
            trigger_search(page)

            # 5. Wait for results page
            page.wait_for_selector("ai-pb-departure, ai-pb-flight-listing, .ai-pb-departure-container", timeout=60000)

            # Let skeleton placeholders finish loading
            try:
                page.locator(".ai-pb-flight-lisiting-skeleton").first.wait_for(state="detached", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(3000)

            # Parse flights
            cards = page.locator(
                "ai-pb-flight-card, "
                ".ai-pb-flight-card, "
                "div[class*='flight-card'], "
                ".ai-pb-flight-listing-container > div:not(.ai-pb-flight-listing-skeleton-container)"
            ).all()

            for card in cards:
                text = card.inner_text()
                if not text:
                    continue

                # Flight Number
                fn_match = re.search(r"AI[-\s]?\d{3,4}", text)
                flight_num = fn_match.group(0).replace(" ", "-") if fn_match else "AI"

                # Departure Time
                time_match = re.search(r"\b([01]\d|2[0-3]):[0-5]\d\b", text)
                dep_time = time_match.group(0) if time_match else "00:00"

                # Price Extraction
                price_matches = re.findall(r"(?:INR|₹)\s*([\d,]+)", text)
                if not price_matches:
                    price_matches = re.findall(r"\b([\d,]{4,6})\b", text)
                if not price_matches:
                    continue

                price_clean = price_matches[0].replace(",", "")
                try:
                    price_val = float(price_clean)
                    if price_val < 1500:
                        continue
                except ValueError:
                    continue

                flights.append({
                    "airline": "Air India",
                    "flight_number": flight_num,
                    "origin": origin,
                    "destination": destination,
                    "departure_time": f"{date_iso}T{dep_time}:00",
                    "price": price_val,
                    "timestamp": timestamp_date,
                    "scraped_at": scraped_at
                })

            # Fallback to the week-calendar active date price if cards were still rendering
            if not flights:
                active_slide = page.locator(
                    "swiper-slide.ai-pb-highlighted-border, "
                    "swiper-slide.swiper-slide-active"
                ).first
                if active_slide.is_visible():
                    slide_text = active_slide.inner_text()
                    price_m = re.findall(r"(?:INR|₹)\s*([\d,]+)", slide_text)
                    if price_m:
                        price_val = float(price_m[0].replace(",", ""))
                        flights.append({
                            "airline": "Air India",
                            "flight_number": "AI",
                            "origin": origin,
                            "destination": destination,
                            "departure_time": f"{date_iso}T00:00:00",
                            "price": price_val,
                            "timestamp": timestamp_date,
                            "scraped_at": scraped_at
                        })

            context.close()

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if not flights:
        raise RuntimeError(f"Could not extract Air India fares for {origin} -> {destination}")

    return flights


async def run_scraper_async(origin: str, destination: str, days_ahead: int = 7, headless: Optional[bool] = None) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(run_scraper, origin, destination, days_ahead, headless)


if __name__ == "__main__":
    results = run_scraper("DEL", "BOM", days_ahead=7)
    print(json.dumps(results, indent=2))