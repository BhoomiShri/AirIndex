import os
import time
import json
import asyncio
import logging
import threading
from typing import List, Dict, Any, Optional

# Import synchronous scraper functions directly
from airindia_scraper import run_scraper as scrape_airindia
from airindiaexpress_scraper import run_scraper as scrape_airindiaexpress
from indigo_scraper import run_scraper as scrape_indigo
from vistara_scraper import run_scraper as scrape_vistara

# Safely import database functions
try:
    from db import save_flights, log_scraper_run
except ImportError:
    save_flights = None
    log_scraper_run = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("master_scraper")

_db_lock = threading.Lock()


def _safe_save_flights(flights: List[Dict[str, Any]]) -> None:
    if not flights or save_flights is None:
        return
    try:
        with _db_lock:
            save_flights(flights)
    except Exception as err:
        logger.error(f"Database error in save_flights: {err}")


def _safe_log_scraper_run(**kwargs) -> None:
    if log_scraper_run is None:
        return
    try:
        with _db_lock:
            log_scraper_run(**kwargs)
    except Exception as err:
        logger.error(f"Database error in log_scraper_run: {err}")


async def _execute_single_scraper(
    scraper_name: str,
    scraper_func,
    origin: str,
    destination: str,
    days_ahead: int,
    headless: bool
) -> List[Dict[str, Any]]:
    """Runs a single scraper in a worker thread and safely records DB metrics."""
    start_time = time.time()
    try:
        logger.info(f"[{scraper_name}] Starting search for {origin} -> {destination}")
        
        # Offload synchronous Playwright scraper to thread so async loop remains responsive
        results = await asyncio.to_thread(
            scraper_func,
            origin=origin,
            destination=destination,
            days_ahead=days_ahead,
            headless=headless
        )

        duration = round(time.time() - start_time, 2)
        count = len(results) if isinstance(results, list) else 0

        _safe_save_flights(results)
        _safe_log_scraper_run(
            scraper_name=scraper_name,
            origin=origin,
            destination=destination,
            status="SUCCESS",
            records_count=count,
            duration_seconds=duration
        )

        logger.info(f"[{scraper_name}] Finished successfully in {duration}s. Scraped {count} flights.")
        return results or []

    except Exception as exc:
        duration = round(time.time() - start_time, 2)
        error_msg = str(exc)
        logger.error(f"[{scraper_name}] Failed after {duration}s: {error_msg}")

        _safe_log_scraper_run(
            scraper_name=scraper_name,
            origin=origin,
            destination=destination,
            status="FAILED",
            records_count=0,
            error_message=error_msg,
            duration_seconds=duration
        )
        return []


async def run_master_scraper_async(
    origin: str,
    destination: str,
    days_ahead: int = 7,
    headless: Optional[bool] = None
) -> Dict[str, Any]:
    """
    Executes each airline scraper sequentially.
    Sequential execution ensures:
      1. CDNs (Akamai) do not reset connections due to concurrent burst requests.
      2. Browser windows retain OS focus for keyboard navigation when running headed.
      3. Zero CPU/memory thrashing.
    """
    total_start = time.time()

    # Match the default of the individual files: headless=False unless explicitly requested
    if headless is None:
        headless = os.getenv("SCRAPER_HEADLESS", "false").lower() in ("true", "1", "yes")

    scrapers = [
        ("Air India", scrape_airindia),
        ("Air India Express", scrape_airindiaexpress),
        ("IndiGo", scrape_indigo),
        ("Vistara (AdaniOne)", scrape_vistara),
    ]

    all_flights: List[Dict[str, Any]] = []

    for idx, (name, func) in enumerate(scrapers):
        flights = await _execute_single_scraper(
            scraper_name=name,
            scraper_func=func,
            origin=origin,
            destination=destination,
            days_ahead=days_ahead,
            headless=headless
        )
        if flights:
            all_flights.extend(flights)

        # 2-second cooldown between scrapers to ensure previous Chromium instance
        # has completely shut down and released network sockets
        if idx < len(scrapers) - 1:
            await asyncio.sleep(2)

    total_duration = round(time.time() - total_start, 2)

    return {
        "status": "COMPLETED",
        "origin": origin,
        "destination": destination,
        "days_ahead": days_ahead,
        "total_flights_scraped": len(all_flights),
        "total_duration_seconds": total_duration,
        "data": all_flights
    }


def run_master_scraper(
    origin: str,
    destination: str,
    days_ahead: int = 7,
    headless: Optional[bool] = None
) -> Dict[str, Any]:
    """Synchronous entry point."""
    return asyncio.run(run_master_scraper_async(origin, destination, days_ahead, headless))


if __name__ == "__main__":
    output = run_master_scraper("DEL", "BOM", days_ahead=7)
    print(json.dumps(output, indent=2))