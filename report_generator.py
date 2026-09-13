"""
AeroIndex Detailed Statistical Report Generator

Generates a PDF report containing:

1. Executive Summary
2. Airfare Price Index
3. Inflation / Deflation
4. Anomaly Analysis
5. Booking Window Analysis
6. Route-wise Analysis
7. Airline-wise Analysis
8. Statistical Indicators
9. Policy Insights
10. Methodology
11. Data Quality Audit

This is an AeroIndex analytical report.
It is NOT an official Government of India / NSO publication.
"""

import os
import statistics
from datetime import datetime, timedelta
from collections import defaultdict

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
    KeepTogether,
)

from processing import clean_data


# ---------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------

REPORT_DIR = "reports"

BASE_INDEX = 100.0

ANOMALY_THRESHOLD = 0.50  # 50%

# Confirmed / project-defined DGCA route weights currently available
DGCA_ROUTE_WEIGHTS = {
    "DEL-BOM": 0.44,
    "BLR-DEL": 0.37,
    "MAA-DEL": 0.19,
}


# ---------------------------------------------------------------------
# GENERAL HELPERS
# ---------------------------------------------------------------------

def money(value):
    if value is None:
        return "N/A"

    return f"INR {float(value):,.2f}"


def number(value):
    if value is None:
        return "N/A"

    return f"{float(value):,.2f}"


def percentage(value):
    if value is None:
        return "N/A"

    return f"{float(value):+.2f}%"


def safe_average(values):
    values = [float(x) for x in values if x is not None]

    if not values:
        return None

    return statistics.mean(values)


def safe_median(values):
    values = [float(x) for x in values if x is not None]

    if not values:
        return None

    return statistics.median(values)


def safe_stdev(values):
    values = [float(x) for x in values if x is not None]

    if len(values) < 2:
        return 0.0

    return statistics.stdev(values)


def percentile(values, p):
    """
    Simple linear percentile.
    """

    values = sorted(
        float(x)
        for x in values
        if x is not None
    )

    if not values:
        return None

    if len(values) == 1:
        return values[0]

    position = (len(values) - 1) * p

    lower = int(position)
    upper = min(lower + 1, len(values))

    fraction = position - lower

    return (
        values[lower]
        + (values[upper] - values[lower]) * fraction
    )


def route_name(record):
    return (
        f"{str(record.get('origin', '')).upper()}-"
        f"{str(record.get('destination', '')).upper()}"
    )


def canonical_route(route):
    """
    Treat DEL-BOM and BOM-DEL as the same city-pair
    when applying passenger-volume weights.
    """

    try:
        origin, destination = route.split("-")

        pair = {origin, destination}

        if pair == {"DEL", "BOM"}:
            return "DEL-BOM"

        if pair == {"BLR", "DEL"}:
            return "BLR-DEL"

        if pair == {"MAA", "DEL"}:
            return "MAA-DEL"

    except Exception:
        pass

    return route


def parse_datetime(value):
    if not value:
        return None

    try:
        text = str(value).replace("T", " ")

        return datetime.fromisoformat(text[:19])

    except Exception:
        return None


# ---------------------------------------------------------------------
# BOOKING WINDOW
# ---------------------------------------------------------------------

def get_booking_window(record):
    """
    Categorize observations into approximately 7, 15 and 30 day
    advance-booking windows.

    This follows the project's 7/15/30-day booking-window concept.
    """

    departure = parse_datetime(record.get("departure_time"))
    scraped = parse_datetime(record.get("scraped_at"))

    if not departure or not scraped:
        return "Unknown"

    days = (departure - scraped).total_seconds() / 86400

    if days <= 11:
        return "7-day"

    if days <= 22:
        return "15-day"

    return "30-day"


# ---------------------------------------------------------------------
# CLEAN DATA
# ---------------------------------------------------------------------

def prepare_records(records):
    """
    Clean records using the project's existing processing layer.
    """

    cleaned = clean_data(records)

    prepared = []

    for record in cleaned:

        try:
            price = float(record["price"])

            if price <= 0:
                continue

            item = dict(record)

            item["price"] = price
            item["route"] = route_name(item)
            item["canonical_route"] = canonical_route(item["route"])
            item["booking_window"] = get_booking_window(item)

            dt = parse_datetime(item.get("scraped_at"))

            if dt:
                item["scraped_datetime"] = dt

            prepared.append(item)

        except Exception:
            continue

    return prepared


# ---------------------------------------------------------------------
# ANOMALY DETECTION
# ---------------------------------------------------------------------

def calculate_anomalies(records):
    """
    Detect observations that are more than 50% away from the
    previous 7 days of observations for the same route and
    booking window.

    We deliberately require previous observations.
    We do not fabricate a baseline when historical data is insufficient.
    """

    groups = defaultdict(list)

    for record in records:

        key = (
            record["route"],
            record["booking_window"]
        )

        groups[key].append(record)

    anomaly_records = []

    for key, group in groups.items():

        group.sort(
            key=lambda x: x.get(
                "scraped_datetime",
                datetime.min
            )
        )

        for record in group:

            current_time = record.get("scraped_datetime")

            if not current_time:
                continue

            window_start = current_time - timedelta(days=7)

            previous_prices = []

            for previous in group:

                previous_time = previous.get(
                    "scraped_datetime"
                )

                if not previous_time:
                    continue

                if (
                    window_start <= previous_time < current_time
                ):
                    previous_prices.append(
                        previous["price"]
                    )

            # Need historical observations for a meaningful baseline
            if len(previous_prices) < 3:
                record["is_anomaly"] = False
                record["moving_average_7d"] = None
                record["pct_deviation"] = None
                continue

            moving_average = statistics.mean(
                previous_prices
            )

            deviation = (
                (record["price"] - moving_average)
                / moving_average
            )

            record["moving_average_7d"] = moving_average
            record["pct_deviation"] = deviation
            record["is_anomaly"] = (
                abs(deviation) > ANOMALY_THRESHOLD
            )

            if record["is_anomaly"]:
                anomaly_records.append(record)

    return anomaly_records


# ---------------------------------------------------------------------
# AIRFARE PRICE INDEX
# ---------------------------------------------------------------------

def calculate_index(records):
    """
    Calculate a Laspeyres-style Airfare Price Index.

    Base period:
        earliest 20% of observations by date

    Current period:
        latest 20% of observations by date

    For each route:
        route index = current average / base average * 100

    National-style index:
        DGCA passenger weights are applied to supported city pairs.
    """

    dated = [
        r for r in records
        if r.get("scraped_datetime")
    ]

    if not dated:
        return {
            "overall_index": None,
            "base_date": None,
            "current_date": None,
            "route_results": [],
            "weight_coverage": 0.0,
        }

    dates = sorted(
        r["scraped_datetime"]
        for r in dated
    )

    earliest_date = dates[0]
    latest_date = dates[-1]

    total_span = (
        latest_date - earliest_date
    ).total_seconds()

    # If the data only covers a very short period,
    # use first/last day observations.
    if total_span <= 86400:
        base_records = [
            r for r in dated
            if r["scraped_datetime"].date()
            == earliest_date.date()
        ]

        current_records = [
            r for r in dated
            if r["scraped_datetime"].date()
            == latest_date.date()
        ]

    else:

        base_cutoff = earliest_date + timedelta(
            seconds=total_span * 0.20
        )

        current_cutoff = latest_date - timedelta(
            seconds=total_span * 0.20
        )

        base_records = [
            r for r in dated
            if r["scraped_datetime"] <= base_cutoff
        ]

        current_records = [
            r for r in dated
            if r["scraped_datetime"] >= current_cutoff
        ]

    base_prices = defaultdict(list)
    current_prices = defaultdict(list)

    for record in base_records:
        base_prices[
            record["canonical_route"]
        ].append(record["price"])

    for record in current_records:
        current_prices[
            record["canonical_route"]
        ].append(record["price"])

    route_results = []

    weighted_ratios = []
    total_supported_weight = 0.0

    all_routes = sorted(
        set(base_prices.keys())
        | set(current_prices.keys())
    )

    for route in all_routes:

        if route not in base_prices:
            continue

        if route not in current_prices:
            continue

        base_average = safe_average(
            base_prices[route]
        )

        current_average = safe_average(
            current_prices[route]
        )

        if not base_average or not current_average:
            continue

        route_index = (
            current_average
            / base_average
        ) * BASE_INDEX

        weight = DGCA_ROUTE_WEIGHTS.get(route)

        if weight is not None:

            weighted_ratios.append(
                weight * (
                    current_average
                    / base_average
                )
            )

            total_supported_weight += weight

        route_results.append({
            "route": route,
            "base_average": base_average,
            "current_average": current_average,
            "index": route_index,
            "weight": weight,
            "base_observations": len(base_prices[route]),
            "current_observations": len(current_prices[route]),
        })

    if total_supported_weight > 0:

        overall_index = (
            sum(weighted_ratios)
            / total_supported_weight
        ) * BASE_INDEX

        weight_coverage = (
            total_supported_weight
            / sum(DGCA_ROUTE_WEIGHTS.values())
        ) * 100

    else:

        overall_index = None
        weight_coverage = 0.0

    return {
        "overall_index": overall_index,
        "base_date": earliest_date,
        "current_date": latest_date,
        "route_results": route_results,
        "weight_coverage": weight_coverage,
    }


# ---------------------------------------------------------------------
# TABLE STYLE
# ---------------------------------------------------------------------

def make_table(data, widths=None):

    table = Table(
        data,
        colWidths=widths,
        repeatRows=1,
        hAlign="LEFT",
    )

    table.setStyle(
        TableStyle([
            (
                "BACKGROUND",
                (0, 0),
                (-1, 0),
                colors.HexColor("#1f4e78"),
            ),
            (
                "TEXTCOLOR",
                (0, 0),
                (-1, 0),
                colors.white,
            ),
            (
                "FONTNAME",
                (0, 0),
                (-1, 0),
                "Helvetica-Bold",
            ),
            (
                "FONTSIZE",
                (0, 0),
                (-1, -1),
                8,
            ),
            (
                "GRID",
                (0, 0),
                (-1, -1),
                0.4,
                colors.grey,
            ),
            (
                "VALIGN",
                (0, 0),
                (-1, -1),
                "MIDDLE",
            ),
            (
                "ROWBACKGROUNDS",
                (0, 1),
                (-1, -1),
                [
                    colors.white,
                    colors.HexColor("#f3f6f9"),
                ],
            ),
            (
                "LEFTPADDING",
                (0, 0),
                (-1, -1),
                5,
            ),
            (
                "RIGHTPADDING",
                (0, 0),
                (-1, -1),
                5,
            ),
            (
                "TOPPADDING",
                (0, 0),
                (-1, -1),
                5,
            ),
            (
                "BOTTOMPADDING",
                (0, 0),
                (-1, -1),
                5,
            ),
        ])
    )

    return table


# ---------------------------------------------------------------------
# REPORT GENERATOR
# ---------------------------------------------------------------------

def generate_statistical_report(records):

    if not records:
        raise ValueError(
            "No database records available for report generation."
        )

    os.makedirs(REPORT_DIR, exist_ok=True)

    prepared = prepare_records(records)

    if not prepared:
        raise ValueError(
            "No valid airfare records remained after cleaning."
        )

    anomalies = calculate_anomalies(prepared)

    index_data = calculate_index(prepared)

    prices = [
        r["price"]
        for r in prepared
    ]

    # ---------------------------------------------------------------
    # OVERALL STATISTICS
    # ---------------------------------------------------------------

    avg_price = safe_average(prices)
    median_price = safe_median(prices)
    stdev_price = safe_stdev(prices)

    min_price = min(prices)
    max_price = max(prices)

    p25 = percentile(prices, 0.25)
    p75 = percentile(prices, 0.75)

    iqr = p75 - p25

    cv = (
        (stdev_price / avg_price) * 100
        if avg_price
        else 0
    )

    anomaly_percentage = (
        len(anomalies) / len(prepared)
    ) * 100

    # ---------------------------------------------------------------
    # ROUTE STATISTICS
    # ---------------------------------------------------------------

    route_groups = defaultdict(list)

    for record in prepared:
        route_groups[
            record["route"]
        ].append(record["price"])

    route_rows = [
        [
            "Route",
            "Observations",
            "Average Fare",
            "Minimum",
            "Maximum",
            "Std Dev",
        ]
    ]

    for route in sorted(route_groups):

        values = route_groups[route]

        route_rows.append([
            route,
            str(len(values)),
            money(safe_average(values)),
            money(min(values)),
            money(max(values)),
            money(safe_stdev(values)),
        ])

    # ---------------------------------------------------------------
    # AIRLINE STATISTICS
    # ---------------------------------------------------------------

    airline_groups = defaultdict(list)

    for record in prepared:
        airline_groups[
            record.get("airline", "Unknown")
        ].append(record["price"])

    airline_rows = [
        [
            "Airline",
            "Observations",
            "Average Fare",
            "Minimum",
            "Maximum",
        ]
    ]

    for airline in sorted(airline_groups):

        values = airline_groups[airline]

        airline_rows.append([
            airline,
            str(len(values)),
            money(safe_average(values)),
            money(min(values)),
            money(max(values)),
        ])

    # ---------------------------------------------------------------
    # BOOKING WINDOW STATISTICS
    # ---------------------------------------------------------------

    window_groups = defaultdict(list)

    for record in prepared:
        window_groups[
            record["booking_window"]
        ].append(record["price"])

    window_rows = [
        [
            "Booking Window",
            "Observations",
            "Average Fare",
            "Premium vs Overall",
        ]
    ]

    for window in ["7-day", "15-day", "30-day", "Unknown"]:

        if window not in window_groups:
            continue

        values = window_groups[window]

        window_average = safe_average(values)

        premium = (
            (
                window_average
                - avg_price
            )
            / avg_price
        ) * 100 if avg_price else 0

        window_rows.append([
            window,
            str(len(values)),
            money(window_average),
            percentage(premium),
        ])

    # ---------------------------------------------------------------
    # ANOMALY TABLE
    # ---------------------------------------------------------------

    anomaly_rows = [
        [
            "Date",
            "Route",
            "Flight",
            "Window",
            "Observed",
            "7D Avg",
            "Deviation",
        ]
    ]

    for record in sorted(
        anomalies,
        key=lambda x: x.get(
            "scraped_datetime",
            datetime.min
        ),
        reverse=True,
    )[:50]:

        dt = record.get("scraped_datetime")

        anomaly_rows.append([
            dt.strftime("%Y-%m-%d")
            if dt else "N/A",

            record["route"],

            record.get(
                "flight_number",
                "N/A"
            ),

            record["booking_window"],

            money(record["price"]),

            money(
                record.get(
                    "moving_average_7d"
                )
            ),

            percentage(
                record.get(
                    "pct_deviation"
                ) * 100
                if record.get("pct_deviation")
                is not None
                else None
            ),
        ])

    # ---------------------------------------------------------------
    # INDEX TABLE
    # ---------------------------------------------------------------

    index_rows = [
        [
            "Route",
            "DGCA Weight",
            "Base Fare",
            "Current Fare",
            "Route Index",
        ]
    ]

    for item in index_data["route_results"]:

        weight_text = (
            f"{item['weight']:.2f}"
            if item["weight"] is not None
            else "N/A"
        )

        index_rows.append([
            item["route"],
            weight_text,
            money(item["base_average"]),
            money(item["current_average"]),
            number(item["index"]),
        ])

    # ---------------------------------------------------------------
    # POLICY INSIGHTS
    # ---------------------------------------------------------------

    insights = []

    if index_data["overall_index"] is not None:

        index_value = index_data["overall_index"]

        if index_value > 100:

            inflation = index_value - 100

            insights.append(
                f"The current Airfare Price Index is "
                f"{index_value:.2f}, indicating approximately "
                f"{inflation:.2f}% increase relative to the "
                f"defined base period."
            )

        elif index_value < 100:

            decrease = 100 - index_value

            insights.append(
                f"The current Airfare Price Index is "
                f"{index_value:.2f}, indicating approximately "
                f"{decrease:.2f}% decrease relative to the "
                f"defined base period."
            )

        else:

            insights.append(
                "The current Airfare Price Index is approximately "
                "100, indicating little aggregate movement "
                "relative to the defined base period."
            )

    if anomalies:

        insights.append(
            f"{len(anomalies)} observation(s), representing "
            f"{anomaly_percentage:.2f}% of the cleaned dataset, "
            f"were flagged as potential fare anomalies."
        )

    else:

        insights.append(
            "No observations exceeded the configured "
            "50% anomaly threshold with sufficient historical "
            "baseline data."
        )

    if window_groups:

        highest_window = max(
            (
                (
                    safe_average(values),
                    window
                )
                for window, values
                in window_groups.items()
                if values
            ),
            default=(None, None)
        )

        if highest_window[1]:

            insights.append(
                f"The highest average observed fare occurred "
                f"in the {highest_window[1]} booking-window group."
            )

    if cv > 20:

        insights.append(
            f"Fare dispersion is relatively high, with a "
            f"coefficient of variation of {cv:.2f}%, indicating "
            f"substantial variation across observed fares."
        )

    # ---------------------------------------------------------------
    # PDF
    # ---------------------------------------------------------------

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    output_path = os.path.join(
        REPORT_DIR,
        f"AeroIndex_Detailed_Statistical_Report_{timestamp}.pdf"
    )

    document = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title="AeroIndex Detailed Statistical Report",
        author="AeroIndex",
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        fontSize=22,
        leading=26,
        alignment=TA_CENTER,
        spaceAfter=8,
    )

    subtitle_style = ParagraphStyle(
        "Subtitle",
        parent=styles["Normal"],
        fontSize=10,
        alignment=TA_CENTER,
        textColor=colors.grey,
        spaceAfter=15,
    )

    heading_style = ParagraphStyle(
        "Heading",
        parent=styles["Heading2"],
        fontSize=14,
        leading=18,
        spaceBefore=8,
        spaceAfter=8,
        textColor=colors.HexColor("#1f4e78"),
    )

    body_style = ParagraphStyle(
        "Body",
        parent=styles["BodyText"],
        fontSize=9,
        leading=13,
        spaceAfter=6,
    )

    small_style = ParagraphStyle(
        "Small",
        parent=styles["BodyText"],
        fontSize=8,
        leading=11,
        textColor=colors.grey,
    )

    story = []

    # ---------------------------------------------------------------
    # TITLE
    # ---------------------------------------------------------------

    story.append(
        Paragraph(
            "AEROINDEX",
            title_style
        )
    )

    story.append(
        Paragraph(
            "DETAILED AIRFARE STATISTICAL REPORT",
            title_style
        )
    )

    story.append(
        Paragraph(
            "NSO-aligned analytical reporting format",
            subtitle_style
        )
    )

    story.append(
        Paragraph(
            f"Report generated: "
            f"{datetime.now().strftime('%d %B %Y, %H:%M IST')}",
            subtitle_style
        )
    )

    # ---------------------------------------------------------------
    # 1. EXECUTIVE SUMMARY
    # ---------------------------------------------------------------

    story.append(
        Paragraph(
            "1. Executive Summary",
            heading_style
        )
    )

    summary_rows = [
        ["Indicator", "Value"],

        [
            "Valid observations",
            f"{len(prepared):,}"
        ],

        [
            "Routes covered",
            f"{len(route_groups):,}"
        ],

        [
            "Airlines covered",
            f"{len(airline_groups):,}"
        ],

        [
            "Average observed fare",
            money(avg_price)
        ],

        [
            "Median observed fare",
            money(median_price)
        ],

        [
            "Lowest observed fare",
            money(min_price)
        ],

        [
            "Highest observed fare",
            money(max_price)
        ],

        [
            "Potential anomalies",
            f"{len(anomalies):,} ({anomaly_percentage:.2f}%)"
        ],

        [
            "Airfare Price Index",
            number(index_data["overall_index"])
        ],
    ]

    story.append(
        make_table(
            summary_rows,
            widths=[
                85 * mm,
                80 * mm,
            ],
        )
    )

    story.append(Spacer(1, 8))

    # ---------------------------------------------------------------
    # 2. AIRFARE PRICE INDEX
    # ---------------------------------------------------------------

    story.append(
        Paragraph(
            "2. Airfare Price Index",
            heading_style
        )
    )

    if index_data["overall_index"] is not None:

        index_value = index_data["overall_index"]

        change = index_value - 100

        story.append(
            Paragraph(
                f"<b>Current Airfare Price Index:</b> "
                f"{index_value:.2f}",
                body_style
            )
        )

        story.append(
            Paragraph(
                f"<b>Base Index:</b> 100.00",
                body_style
            )
        )

        story.append(
            Paragraph(
                f"<b>Estimated movement from base:</b> "
                f"{change:+.2f}%",
                body_style
            )
        )

        story.append(
            Paragraph(
                f"<b>DGCA weight coverage:</b> "
                f"{index_data['weight_coverage']:.2f}%",
                body_style
            )
        )

        story.append(
            Paragraph(
                "The index is calculated using a Laspeyres-style "
                "price-relative approach. Route-level current "
                "prices are compared with the defined base-period "
                "prices and supported routes are aggregated using "
                "the available DGCA passenger-volume weights.",
                body_style
            )
        )

        story.append(
            make_table(
                index_rows,
                widths=[
                    30 * mm,
                    25 * mm,
                    35 * mm,
                    35 * mm,
                    30 * mm,
                ],
            )
        )

    else:

        story.append(
            Paragraph(
                "Insufficient historical observations were "
                "available to calculate a comparable Airfare "
                "Price Index.",
                body_style
            )
        )

    # ---------------------------------------------------------------
    # 3. ANOMALY ANALYSIS
    # ---------------------------------------------------------------

    story.append(
        Paragraph(
            "3. Anomaly & Surge Analysis",
            heading_style
        )
    )

    story.append(
        Paragraph(
            f"The report uses a {ANOMALY_THRESHOLD * 100:.0f}% "
            "deviation threshold against the preceding seven days "
            "of observations for the same route and booking window. "
            "Observations without sufficient historical baseline "
            "are not artificially classified as anomalies.",
            body_style
        )
    )

    if len(anomaly_rows) > 1:

        story.append(
            make_table(
                anomaly_rows,
                widths=[
                    22 * mm,
                    22 * mm,
                    22 * mm,
                    20 * mm,
                    27 * mm,
                    27 * mm,
                    25 * mm,
                ],
            )
        )

    else:

        story.append(
            Paragraph(
                "No qualifying anomalies were detected.",
                body_style
            )
        )

    # ---------------------------------------------------------------
    # 4. BOOKING WINDOW
    # ---------------------------------------------------------------

    story.append(
        Paragraph(
            "4. Booking Window Analysis",
            heading_style
        )
    )

    story.append(
        Paragraph(
            "Observed fares are decomposed into approximately "
            "7-day, 15-day and 30-day advance-booking groups. "
            "This allows the platform to distinguish booking-lead "
            "effects from broader airfare movements.",
            body_style
        )
    )

    if len(window_rows) > 1:

        story.append(
            make_table(
                window_rows,
                widths=[
                    35 * mm,
                    35 * mm,
                    45 * mm,
                    45 * mm,
                ],
            )
        )

    # ---------------------------------------------------------------
    # 5. ROUTE ANALYSIS
    # ---------------------------------------------------------------

    story.append(PageBreak())

    story.append(
        Paragraph(
            "5. Route-wise Fare Analysis",
            heading_style
        )
    )

    story.append(
        make_table(
            route_rows,
            widths=[
                25 * mm,  # Route
                25 * mm,  # Observations
                33 * mm,  # Average Fare
                32 * mm,  # Minimum
                33 * mm,  # Maximum
                32 * mm,  # Std Dev
            ],
        )
    )

    # ---------------------------------------------------------------
    # 6. AIRLINE ANALYSIS
    # ---------------------------------------------------------------

    story.append(
        Paragraph(
            "6. Airline-wise Fare Analysis",
            heading_style
        )
    )

    story.append(
        make_table(
            airline_rows,
            widths=[
                44 * mm,  # Airline
                34 * mm,  # Observations
                34 * mm,  # Average Fare
                34 * mm,  # Minimum
                34 * mm,  # Maximum
            ],
        )
    )

    # ---------------------------------------------------------------
    # 7. STATISTICAL INDICATORS
    # ---------------------------------------------------------------

    story.append(
        Paragraph(
            "7. Statistical Indicators",
            heading_style
        )
    )

    statistical_rows = [
        ["Indicator", "Value"],

        ["Mean", money(avg_price)],

        ["Median", money(median_price)],

        ["Standard deviation", money(stdev_price)],

        ["Coefficient of variation", percentage(cv)],

        ["25th percentile (P25)", money(p25)],

        ["75th percentile (P75)", money(p75)],

        ["Interquartile range", money(iqr)],

        ["Minimum", money(min_price)],

        ["Maximum", money(max_price)],
    ]

    story.append(
        make_table(
            statistical_rows,
            widths=[
                90 * mm,
                75 * mm,
            ],
        )
    )

    # ---------------------------------------------------------------
    # 8. POLICY INSIGHTS
    # ---------------------------------------------------------------

    story.append(
        Paragraph(
            "8. Policy-oriented Insights",
            heading_style
        )
    )

    for insight in insights:

        story.append(
            Paragraph(
                "• " + insight,
                body_style
            )
        )

    story.append(
        Paragraph(
            "These observations are analytical indicators "
            "generated from the available airfare observations. "
            "They should be interpreted together with coverage, "
            "sample composition, route weights and data-quality "
            "information.",
            body_style
        )
    )

    # ---------------------------------------------------------------
    # 9. DATA QUALITY
    # ---------------------------------------------------------------

    story.append(
        Paragraph(
            "9. Data Quality & Audit",
            heading_style
        )
    )

    duplicate_count = len(records) - len(prepared)

    quality_rows = [
        ["Audit Item", "Result"],

        [
            "Raw database observations",
            f"{len(records):,}"
        ],

        [
            "Valid cleaned observations",
            f"{len(prepared):,}"
        ],

        [
            "Records removed during cleaning",
            f"{duplicate_count:,}"
        ],

        [
            "Potential anomalies",
            f"{len(anomalies):,}"
        ],

        [
            "Routes represented",
            f"{len(route_groups):,}"
        ],

        [
            "Airlines represented",
            f"{len(airline_groups):,}"
        ],

        [
            "DGCA-supported route weight coverage",
            f"{index_data['weight_coverage']:.2f}%"
        ],
    ]

    story.append(
        make_table(
            quality_rows,
            widths=[
                90 * mm,
                75 * mm,
            ],
        )
    )

    # ---------------------------------------------------------------
    # 10. METHODOLOGY
    # ---------------------------------------------------------------

    story.append(
        Paragraph(
            "10. Methodology",
            heading_style
        )
    )

    methodology = [
        "1. Flight-price observations are retrieved from the AeroIndex MySQL database.",

        "2. Invalid or unusable price observations are removed using the existing data-cleaning layer.",

        "3. Each observation is categorized into an approximate 7-day, 15-day or 30-day advance-booking window.",

        "4. Potential anomalies are evaluated against the preceding seven days for the same route and booking window.",

        "5. Route-level price relatives are calculated as Current Average Fare / Base Average Fare × 100.",

        "6. The aggregate Airfare Price Index uses available DGCA passenger-volume weights for supported city pairs.",

        "7. Descriptive statistics include mean, median, standard deviation, coefficient of variation, percentiles and interquartile range.",
    ]

    for item in methodology:

        story.append(
            Paragraph(
                item,
                body_style
            )
        )

    # ---------------------------------------------------------------
    # DISCLAIMER
    # ---------------------------------------------------------------

    story.append(Spacer(1, 15))

    story.append(
        Paragraph(
            "<b>Important Disclaimer:</b> This is an analytical "
            "report generated by the AeroIndex platform. It is "
            "not an official Government of India, MoSPI or NSO "
            "publication. DGCA weighting coverage depends on the "
            "route weights available to the system.",
            small_style
        )
    )

    # ---------------------------------------------------------------
    # BUILD PDF
    # ---------------------------------------------------------------

    document.build(story)

    return output_path
