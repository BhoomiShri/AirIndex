import asyncio
import time

from fastapi import FastAPI, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool

from processing import (
    clean_data,
    detect_anomalies,
    resolve_persistent_anomalies,
    filter_anomalies,
    daily_route_averages,
    calculate_laspeyres_index,
)

import db

from report_generator import generate_statistical_report


app = FastAPI(
    title="AeroIndex MoSPI Backend",
    description="Real-time airfare analytics pipeline and DGCA-weighted Laspeyres Index engine.",
    version="1.0.0"
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


ANOMALY_STREAKS = {}

METRICS_CACHE = {
    "timestamp": 0,
    "data": None
}

CACHE_TTL_SECONDS = 60


def invalidate_metrics_cache():
    global METRICS_CACHE
    METRICS_CACHE["data"] = None
    METRICS_CACHE["timestamp"] = 0


# ============================================================
# METRICS
# ============================================================

@app.get("/api/v1/metrics/latest")
async def get_latest_metrics():

    global ANOMALY_STREAKS
    global METRICS_CACHE

    now = time.time()

    # Return cached result if still valid
    if (
        METRICS_CACHE["data"] is not None
        and (now - METRICS_CACHE["timestamp"] < CACHE_TTL_SECONDS)
    ):
        print("[CACHE HIT] Serving metrics from memory")
        return METRICS_CACHE["data"]

    try:

        print("[METRICS] Fetching raw records from DB...")

        t0 = time.time()

        raw_records = await asyncio.to_thread(
            db.get_latest_records,
            100
        )

        print(
            f"[METRICS] DB records fetched in "
            f"{time.time() - t0:.2f}s "
            f"(Count: {len(raw_records) if raw_records else 0})"
        )

        print("[METRICS] Fetching historical prices...")

        history_data = await asyncio.to_thread(
            db.get_historical_prices,
            7
        )

        print("[METRICS] Running processing logic...")

        cleaned = (
            clean_data(raw_records)
            if raw_records
            else []
        )

        flagged = (
            detect_anomalies(
                cleaned,
                history=history_data
            )
            if cleaned
            else []
        )

        # In get_latest_metrics():
        if flagged:
            # Pass a copy of streaks so GET requests don't mutate persistent streak state
            flagged, _ = resolve_persistent_anomalies(
                flagged_records=flagged,
                streaks=dict(ANOMALY_STREAKS),
                persistence_threshold=3
            )

        valid_records = filter_anomalies(flagged) if flagged else []

        current_avg_prices = (
            daily_route_averages(valid_records, window=7)
            if valid_records
            else {}
        )

        base_prices = {
            "DEL-BOM": 4500.0,
            "BLR-DEL": 5200.0,
            "MAA-DEL": 3800.0
        }

        index_result = calculate_laspeyres_index(
            current_prices_dict=current_avg_prices,
            base_prices_dict=base_prices
        )

        overall_idx = index_result.get("overall_index")
        response_data = {
            "status": "success",
            "is_simulation": getattr(db, "USE_SIMULATION_MODE", False),
            "total_records": len(flagged),
            "valid_records_count": len(valid_records),
            # Use 'or 100.0' because if overall_index is None, .get() returns None
            "airfare_index": overall_idx if overall_idx is not None else 100.0,
            "index_breakdown": index_result.get("per_route", {}),
            "data": flagged
        }

        METRICS_CACHE["timestamp"] = now
        METRICS_CACHE["data"] = response_data

        print(
            f"[METRICS] Completed in "
            f"{time.time() - t0:.2f}s"
        )

        return response_data

    except Exception as e:

        print(
            f"[ERROR] Failed to calculate metrics: {e}"
        )

        return {
            "status": "error",
            "message": str(e),
            "total_records": 0,
            "valid_records_count": 0,
            "airfare_index": 100.0,
            "index_breakdown": {},
            "data": []
        }


# ============================================================
# DIRECT FLIGHT SEARCH
# ============================================================

@app.get("/api/flights/search")
async def search_flights(
    origin: str = Query(
        "DEL",
        min_length=3,
        max_length=3
    ),

    destination: str = Query(
        "BOM",
        min_length=3,
        max_length=3
    ),

    days_ahead: int = Query(
        7,
        ge=1,
        le=30
    )
):

    from master_scraper import scrape_all_flights

    origin = origin.upper()
    destination = destination.upper()

    print(
        f"[SEARCH] Live search triggered: "
        f"{origin}-{destination}"
    )

    result = await asyncio.to_thread(
        scrape_all_flights,
        origin=origin,
        destination=destination,
        days_ahead=days_ahead,
        headless=True
    )

    raw_flights = (
        result.get("flights", [])
        if isinstance(result, dict)
        else []
    )

    scraper_status = (
        result.get("status", "failed")
        if isinstance(result, dict)
        else "failed"
    )

    source = (
        result.get("source", "EaseMyTrip")
        if isinstance(result, dict)
        else "EaseMyTrip"
    )

    print(
        f"[SEARCH] Scraper status: "
        f"{scraper_status}"
    )

    print(
        f"[SEARCH] Raw records: "
        f"{len(raw_flights)}"
    )

    # Do not process fake/empty data as live data
    if not raw_flights:

        return {
            "status": "failed",
            "source": source,
            "route": f"{origin}-{destination}",
            "message": (
                "Live scraper returned 0 records. "
                "No database update was performed."
            ),
            "flights": [],
            "total_cleaned_records": 0
        }

    cleaned_flights = clean_data(
        raw_flights
    )

    print(
        f"[SEARCH] Cleaned records: "
        f"{len(cleaned_flights)}"
    )

    if cleaned_flights:

        db.save_flight_records(
            cleaned_flights
        )

        invalidate_metrics_cache()

        print(
            "[SEARCH] Live records saved to database."
        )

    return {
        "status": "live",
        "source": source,
        "route": f"{origin}-{destination}",
        "flights": cleaned_flights,
        "total_cleaned_records": len(
            cleaned_flights
        )
    }


# ============================================================
# LIVE SCRAPE PIPELINE
# ============================================================

SCRAPE_STATUS = {
    "status": "idle",
    "route": None,
    "source": None,
    "records_found": 0,
    "records_saved": 0,
    "message": "No scrape has been triggered yet."
}


def execute_live_scrape_pipeline(origin: str = "DEL", destination: str = "BOM"):
    """Run Playwright scraper -> cleaning -> MySQL."""

    global SCRAPE_STATUS

    origin = origin.upper()
    destination = destination.upper()
    route = f"{origin}-{destination}"

    SCRAPE_STATUS.update({
        "status": "running",
        "route": route,
        "source": None,
        "records_found": 0,
        "records_saved": 0,
        "message": f"Live scrape running for {route}..."
    })

    print(f"[SCRAPER PIPELINE] Starting LIVE scrape: {route}")

    try:
        from master_scraper import scrape_all_flights

        result = scrape_all_flights(
            origin=origin,
            destination=destination,
            days_ahead=7,
            headless=True
        )

        result = result if isinstance(result, dict) else {}
        source = result.get("source", "EaseMyTrip")
        raw_flights = result.get("flights", [])

        SCRAPE_STATUS["source"] = source
        SCRAPE_STATUS["records_found"] = len(raw_flights)

        print(f"[SCRAPER PIPELINE] Source: {source}")
        print(f"[SCRAPER PIPELINE] Raw flights: {len(raw_flights)}")

        # Never save empty/failed live scraping as data.
        if not raw_flights:
            SCRAPE_STATUS.update({
                "status": "failed",
                "records_saved": 0,
                "message": (
                    f"Live scraper returned 0 records for {route}. "
                    "Database was not modified."
                )
            })
            print("[SCRAPER PIPELINE] LIVE SCRAPE FAILED - 0 records returned.")
            return

        # CLEAN
        cleaned = clean_data(raw_flights)
        print(f"[SCRAPER PIPELINE] Cleaned records: {len(cleaned)}")

        # SAVE
        if cleaned:
            db.save_flight_records(cleaned)
            invalidate_metrics_cache()

            SCRAPE_STATUS.update({
                "status": "success",
                "records_saved": len(cleaned),
                "message": (
                    f"Live scrape completed for {route}. "
                    f"{len(cleaned)} records saved to MySQL."
                )
            })
            print("[SCRAPER PIPELINE] LIVE records saved to database.")
        else:
            SCRAPE_STATUS.update({
                "status": "failed",
                "records_saved": 0,
                "message": (
                    f"Cleaning removed all scraped records for {route}. "
                    "Database was not modified."
                )
            })

    except Exception as e:
        SCRAPE_STATUS.update({
            "status": "failed",
            "records_saved": 0,
            "message": f"{type(e).__name__}: {e}"
        })
        print(f"[SCRAPER PIPELINE ERROR] {type(e).__name__}: {e}")


# ============================================================
# TRIGGER LIVE SCRAPE
# ============================================================

@app.post("/api/v1/scrape/trigger")
async def trigger_scrape(
    background_tasks: BackgroundTasks,
    origin: str = "DEL",
    destination: str = "BOM"
):
    """Start the real scraper without blocking the frontend."""

    origin = origin.upper()
    destination = destination.upper()
    route = f"{origin}-{destination}"

    if SCRAPE_STATUS["status"] == "running":
        return {
            "status": "already_running",
            "route": SCRAPE_STATUS["route"],
            "message": "A live scrape is already running."
        }

    print(f"[API] Trigger received for {route}")

    background_tasks.add_task(
        execute_live_scrape_pipeline,
        origin,
        destination
    )

    return {
        "status": "started",
        "route": route,
        "source": "EaseMyTrip",
        "message": (
            f"Live scrape initiated for {route}. "
            "Poll /api/v1/scrape/status for the result."
        )
    }


# ============================================================
# SCRAPE STATUS
# ============================================================

@app.get("/api/v1/scrape/status")
async def get_scrape_status():
    """Frontend polls this while the live scraper is running."""

    return {
        "status": "success",
        "scrape": SCRAPE_STATUS
    }


# ============================================================
# API INFO
# ============================================================

@app.get("/api/v1")
async def api_info():
    return {
        "name": "AeroIndex API",
        "version": "1.0.0",
        "endpoints": {
            "metrics": "GET /api/v1/metrics/latest",
            "trigger_scrape": "POST /api/v1/scrape/trigger",
            "scrape_status": "GET /api/v1/scrape/status",
            "flight_search": "GET /api/flights/search",
            "health": "GET /api/health"
        }
    }

# ============================================================
# HISTORICAL CHART DATA
# ============================================================

@app.get("/api/v1/history")
async def get_history(
    days_back: int = Query(
        30,
        ge=1,
        le=365
    )
):
    """
    Return historical airfare time-series data for frontend charts.
    """

    try:
        history = await asyncio.to_thread(
            db.get_historical_chart_data,
            days_back
        )

        return {
            "status": "success",
            "days_back": days_back,
            "data": history
        }

    except Exception as e:
        print(f"[HISTORY ERROR] {type(e).__name__}: {e}")

        return {
            "status": "error",
            "message": str(e),
            "data": []
        }
# ============================================================
# STATISTICAL REPORT
# ============================================================

@app.post("/api/v1/report/generate")
async def generate_report(
    days_back: int = Query(
        30,
        ge=1,
        le=365
    )
):
    """
    Generate the AeroIndex Statistical Report from database records.

    This connects the frontend report button to report_generator.py.
    """
    try:
        print(
            f"[REPORT] Generating statistical report "
            f"for last {days_back} days..."
        )

        records = await asyncio.to_thread(
            db.get_report_records,
            days_back
        )

        if not records:
            return {
                "status": "error",
                "message": f"No database records found for the last {days_back} days."
            }

        print(f"[REPORT] Found {len(records)} records.")

        report_path = await asyncio.to_thread(
            generate_statistical_report,
            records
        )

        return {
            "status": "success",
            "message": "Statistical report generated successfully.",
            "report_path": str(report_path)
        }

    except Exception as e:
        print(
            f"[REPORT ERROR] Failed to generate report: "
            f"{type(e).__name__}: {e}"
        )

        return {
            "status": "error",
            "message": str(e)
        }


@app.get("/api/v1/report/latest")
async def get_latest_report():
    """
    Return the most recently generated statistical report path.
    """
    try:
        from pathlib import Path

        reports_dir = Path("reports")

        if not reports_dir.exists():
            return {
                "status": "not_found",
                "message": "No reports have been generated yet."
            }

        reports = sorted(
            reports_dir.glob("AeroIndex_Statistical_Report_*.pdf"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )

        if not reports:
            return {
                "status": "not_found",
                "message": "No reports have been generated yet."
            }

        return {
            "status": "success",
            "report_path": str(reports[0])
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/api/health")
async def health_check():

    return {
        "status": "ok",
        "service": "AeroIndex Backend",
        "scraper": "Playwright",
        "database": "MySQL"
    }
