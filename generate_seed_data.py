"""
AeroIndex - Historical Demo Data Generator

Generates 30 days of synthetic historical airfare observations
for 7-day, 15-day and 30-day advance-booking windows.

IMPORTANT:
These are SEEDED / DEMO observations, NOT live scraped fares.
"""

from datetime import datetime, timedelta
import random

from db import get_db_connection


# ============================================================
# CONFIGURATION
# ============================================================

DAYS_OF_HISTORY = 30

BOOKING_WINDOWS = [7, 15, 30]

# Existing project routes
ROUTES = {
    "DEL-BOM": {
        "origin": "DEL",
        "destination": "BOM",
        "base_price": 4600,
    },
    "BOM-DEL": {
        "origin": "BOM",
        "destination": "DEL",
        "base_price": 4500,
    },
    "BLR-DEL": {
        "origin": "BLR",
        "destination": "DEL",
        "base_price": 5200,
    },
    "DEL-BLR": {
        "origin": "DEL",
        "destination": "BLR",
        "base_price": 5100,
    },
     "MAA-DEL": {
        "origin": "MAA",
        "destination": "DEL",
        "base_price": 3800,
    }
}


# Airline price adjustments.
# These are synthetic assumptions for demo data.
AIRLINES = {
    "IndiGo": {
        "code": "6E",
        "adjustment": 0,
    },
    "Air India": {
        "code": "AI",
        "adjustment": 250,
    },
    "Air India Express": {
        "code": "IX",
        "adjustment": -100,
    },
    "SpiceJet": {
        "code": "SG",
        "adjustment": -150,
    },
    "Vistara": {
        "code": "UK",
        "adjustment": 300,
    },
}


# Prices closer to departure are generally higher.
WINDOW_MULTIPLIER = {
    7: 1.15,
    15: 1.05,
    30: 0.94,
}


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def generate_flight_number(airline_code, route_index, airline_index):
    """
    Generate stable-looking synthetic flight numbers.
    """
    number = 100 + (route_index * 50) + (airline_index * 7)
    return f"{airline_code}-{number}"


def generate_price(base_price, airline_adjustment, booking_window, day_number):
    """
    Generate a realistic-looking synthetic fare.

    Components:
    - route base price
    - airline adjustment
    - booking-window effect
    - gradual market movement
    - small random variation
    """

    price = base_price

    # Airline-specific pricing
    price += airline_adjustment

    # Advance-booking effect
    price *= WINDOW_MULTIPLIER[booking_window]

    # Gradual 30-day market movement.
    # Creates a gentle trend instead of identical daily prices.
    trend = 1 + (day_number * 0.0025)
    price *= trend

    # Small daily/random market variation.
    variation = random.uniform(-0.045, 0.045)
    price *= (1 + variation)

    # Round to realistic rupee amount
    price = round(price / 10) * 10

    return float(price)


# ============================================================
# MAIN DATA GENERATION
# ============================================================

def generate_records():
    """
    Generate 30 days × 4 routes × 5 airlines × 3 windows
    = 1,800 synthetic observations.
    """

    random.seed(20260912)

    records = []

    # Use today's date as the end of the historical period.
    now = datetime.now().replace(
        hour=10,
        minute=0,
        second=0,
        microsecond=0
    )

    start_date = now - timedelta(days=DAYS_OF_HISTORY - 1)

    route_items = list(ROUTES.items())
    airline_items = list(AIRLINES.items())

    for day_number in range(DAYS_OF_HISTORY):

        scraped_at = start_date + timedelta(days=day_number)

        for route_index, (route_name, route_info) in enumerate(route_items):

            for airline_index, (airline_name, airline_info) in enumerate(
                airline_items
            ):

                flight_number = generate_flight_number(
                    airline_info["code"],
                    route_index,
                    airline_index
                )

                for booking_window in BOOKING_WINDOWS:

                    # Exact booking-window relationship:
                    #
                    # scraped_at → +7 days  → departure
                    # scraped_at → +15 days → departure
                    # scraped_at → +30 days → departure
                    #
                    # This makes the booking-window classification reliable.

                    departure_time = scraped_at + timedelta(
                        days=booking_window
                    )

                    price = generate_price(
                        base_price=route_info["base_price"],
                        airline_adjustment=airline_info["adjustment"],
                        booking_window=booking_window,
                        day_number=day_number,
                    )

                    records.append({
                        "airline": airline_name,
                        "flight_number": flight_number,
                        "origin": route_info["origin"],
                        "destination": route_info["destination"],
                        "departure_time": departure_time,
                        "price": price,
                        "scraped_at": scraped_at,
                    })

    return records


# ============================================================
# DATABASE INSERT
# ============================================================

def replace_seed_data(records):
    """
    Remove the existing generated flight-price data and replace it
    with the new 7/15/30-day historical demo dataset.

    IMPORTANT:
    Only run this while your flight_prices table contains seeded/demo
    data. Do NOT use this after you start collecting real live data.
    """

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            print("Deleting existing seeded flight-price records...")

            cursor.execute("DELETE FROM flight_prices")

            print(f"Inserting {len(records)} new historical records...")

            insert_query = """
                INSERT INTO flight_prices
                (
                    airline,
                    flight_number,
                    origin,
                    destination,
                    departure_time,
                    price,
                    scraped_at
                )
                VALUES
                (%s, %s, %s, %s, %s, %s, %s)
            """

            data = []

            for record in records:
                data.append((
                    record["airline"],
                    record["flight_number"],
                    record["origin"],
                    record["destination"],
                    record["departure_time"].strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    record["price"],
                    record["scraped_at"].strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                ))

            cursor.executemany(insert_query, data)

            conn.commit()

            print()
            print("Database successfully updated.")

    except Exception as e:

        conn.rollback()

        print()
        print("ERROR while inserting seed data:")
        print(e)

        raise

    finally:
        conn.close()


# ============================================================
# VERIFICATION
# ============================================================

def verify_data():
    """
    Verify how many observations exist for each booking window.
    """

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            query = """
                SELECT
                    COUNT(*) AS total_records,
                    MIN(scraped_at) AS earliest_scrape,
                    MAX(scraped_at) AS latest_scrape
                FROM flight_prices
            """

            cursor.execute(query)
            summary = cursor.fetchone()

            print()
            print("=" * 60)
            print("DATABASE VERIFICATION")
            print("=" * 60)

            print(f"Total records : {summary['total_records']}")
            print(f"Earliest scrape: {summary['earliest_scrape']}")
            print(f"Latest scrape  : {summary['latest_scrape']}")

            # Verify the actual booking-window difference.
            query = """
                SELECT
                    CASE
                        WHEN TIMESTAMPDIFF(
                            HOUR,
                            scraped_at,
                            departure_time
                        ) BETWEEN 144 AND 192
                            THEN '7-day'

                        WHEN TIMESTAMPDIFF(
                            HOUR,
                            scraped_at,
                            departure_time
                        ) BETWEEN 312 AND 408
                            THEN '15-day'

                        WHEN TIMESTAMPDIFF(
                            HOUR,
                            scraped_at,
                            departure_time
                        ) BETWEEN 672 AND 768
                            THEN '30-day'

                        ELSE 'Other'
                    END AS booking_window,
                    COUNT(*) AS records
                FROM flight_prices
                GROUP BY booking_window
                ORDER BY booking_window
            """

            cursor.execute(query)

            rows = cursor.fetchall()

            print()
            print("Booking-window distribution:")
            print()

            for row in rows:
                print(
                    f"  {row['booking_window']:>8} : "
                    f"{row['records']} records"
                )

            print("=" * 60)

    finally:
        conn.close()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    print("=" * 60)
    print("AEROINDEX HISTORICAL DATA GENERATOR")
    print("=" * 60)

    print()
    print("Generating 30 days of synthetic historical airfare data...")

    records = generate_records()

    print(f"Generated records: {len(records)}")

    print()
    print("Booking windows included:")
    print("  ✓ 7-day")
    print("  ✓ 15-day")
    print("  ✓ 30-day")

    print()
    print("WARNING:")
    print("This replaces the existing flight_prices data.")
    print("Use this only for your current seeded/demo dataset.")
    print()

    answer = input(
        "Type YES to replace the existing seeded data: "
    ).strip().upper()

    if answer != "YES":
        print()
        print("Operation cancelled.")
        print("No database changes were made.")
        exit()

    replace_seed_data(records)

    verify_data()

    print()
    print("=" * 60)
    print("DONE")
    print("=" * 60)
    print()
    print("These records are SEEDED HISTORICAL DATA.")
    print("They are NOT live scraped airfare observations.")
    print()