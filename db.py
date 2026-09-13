import os
import json
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
import pymysql
import pymysql.cursors
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker
load_dotenv()
# --- CONFIGURATION & SIMULATION TOGGLE ---
USE_SIMULATION_MODE = False  # Set to True if MySQL is offline or for testingMYSQL_CONFIG = {
MYSQL_CONFIG ={
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "database": os.getenv("DB_NAME"),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD"),
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "2")),
    "cursorclass": pymysql.cursors.DictCursor,
}
logger = logging.getLogger("scraper_db")

# SQLAlchemy setup (for SQLite fallback or Scraper Logs)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./flights.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Flight(Base):
    __tablename__ = "flight_prices"  # Aligned with schema.sql

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    airline = Column(String(100), nullable=False, index=True)
    flight_number = Column(String(50), nullable=False, index=True)
    origin = Column(String(10), nullable=False, index=True)
    destination = Column(String(10), nullable=False, index=True)
    departure_time = Column(String(50), nullable=False)
    price = Column(Float, nullable=False)
    timestamp = Column(String(20), nullable=True)  # Made nullable=True to prevent crashes
    scraped_at = Column(String(50), nullable=False)


class ScraperLog(Base):
    __tablename__ = "scraper_logs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    scraper_name = Column(String(100), nullable=False, index=True)
    origin = Column(String(10), nullable=False)
    destination = Column(String(10), nullable=False)
    status = Column(String(20), nullable=False)  # SUCCESS, FAILED, PARTIAL
    records_count = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    duration_seconds = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)


# Auto-create tables on startup
Base.metadata.create_all(bind=engine)


def get_db_connection():
    """Establishes connection to MySQL database using PyMySQL."""
    return pymysql.connect(**MYSQL_CONFIG)


def get_latest_records(limit: int = 100) -> List[Dict[str, Any]]:
    if USE_SIMULATION_MODE:
        return _get_mock_fallback()

    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            query = """
                SELECT airline, flight_number, origin, destination, departure_time, price, scraped_at
                FROM flight_prices
                ORDER BY id DESC
                LIMIT %s
            """
            cursor.execute(query, (limit,))
            rows = cursor.fetchall()
            
            for row in rows:
                row["price"] = float(row["price"])
                row["departure_time"] = str(row["departure_time"])
                row["scraped_at"] = str(row["scraped_at"])
                
        conn.close()
        return rows
    except Exception as e:
        print(f"[Warning] MySQL fetch failed or timed out ({e}). Falling back to Simulation Mode.")
        return _get_mock_fallback()


def get_historical_prices(days_back: int = 7) -> Dict[str, List[float]]:
    if USE_SIMULATION_MODE:
        return {}

    history = {}
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            query = """
                SELECT UPPER(origin) AS origin, UPPER(destination) AS destination, 
                       departure_time, scraped_at, price
                FROM flight_prices
                WHERE scraped_at >= NOW() - INTERVAL %s DAY
            """
            cursor.execute(query, (days_back,))
            rows = cursor.fetchall()
            
            from processing import route_window_key
            for r in rows:
                r["price"] = float(r["price"])
                r["departure_time"] = str(r["departure_time"])
                r["scraped_at"] = str(r["scraped_at"])
                
                key = route_window_key(r)
                if key not in history:
                    history[key] = []
                history[key].append(r["price"])

        conn.close()
        return history
    except Exception as e:
        print(f"[Warning] Failed to fetch historical prices: {e}")
        return {}


def save_flight_records(records: List[Dict[str, Any]]):
    if not records or USE_SIMULATION_MODE:
        print("[DB] No records provided or running in simulation mode. Skipping save.")
        return
        
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            insert_query = """
                INSERT INTO flight_prices (airline, flight_number, origin, destination, departure_time, price, scraped_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            data_tuples = []
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            for r in records:
                dep_time = str(r.get("departure_time", "")).replace("T", " ")[:19]
                scraped_at = str(r.get("scraped_at", now_str)).replace("T", " ")[:19]
                
                if not scraped_at:
                    scraped_at = now_str

                data_tuples.append((
                    r.get("airline", "Unknown"),
                    r.get("flight_number", "N/A"),
                    r.get("origin", "DEL"),
                    r.get("destination", "BOM"),
                    dep_time,
                    float(r.get("price", 0.0)),
                    scraped_at
                ))

            cursor.executemany(insert_query, data_tuples)
            conn.commit()
            print(f"[DB SUCCESS] Successfully inserted {len(data_tuples)} records into MySQL.")
            
        conn.close()
    except Exception as e:
        print(f"[DB ERROR] Failed to save flight records to MySQL: {e}")


def _get_mock_fallback() -> List[Dict[str, Any]]:
    if os.path.exists("mock_data.json"):
        with open("mock_data.json", "r") as f:
            return json.load(f)
    return []


def get_report_records(days_back: int = 30) -> List[Dict[str, Any]]:
    if USE_SIMULATION_MODE:
        print("[REPORT] Simulation mode is enabled. No historical DB records.")
        return []

    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            query = """
                SELECT airline, flight_number, origin, destination, departure_time, price, scraped_at
                FROM flight_prices
                WHERE scraped_at >= NOW() - INTERVAL %s DAY
                ORDER BY scraped_at ASC
            """
            cursor.execute(query, (days_back,))
            rows = cursor.fetchall()
        conn.close()

        records = []
        for row in rows:
            try:
                row["price"] = float(row["price"])
                row["origin"] = str(row["origin"]).upper()
                row["destination"] = str(row["destination"]).upper()
                row["departure_time"] = str(row["departure_time"])
                row["scraped_at"] = str(row["scraped_at"])
                records.append(row)
            except (ValueError, TypeError):
                continue

        return records
    except Exception as e:
        print(f"[REPORT ERROR] Failed to fetch report records: {e}")
        return []


def get_historical_chart_data(days_back: int = 30) -> List[Dict[str, Any]]:
    if USE_SIMULATION_MODE:
        return []

    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            query = """
                SELECT
                    DATE(scraped_at) AS date,
                    airline,
                    UPPER(origin) AS origin,
                    UPPER(destination) AS destination,
                    AVG(price) AS average_price,
                    COUNT(*) AS records
                FROM flight_prices
                WHERE scraped_at >= NOW() - INTERVAL %s DAY
                GROUP BY DATE(scraped_at), airline, origin, destination
                ORDER BY date ASC, airline ASC
            """
            cursor.execute(query, (days_back,))
            rows = cursor.fetchall()
        conn.close()

        for row in rows:
            row["date"] = str(row["date"])
            row["average_price"] = round(float(row["average_price"]), 2)
            row["records"] = int(row["records"])

        return rows
    except Exception as e:
        print(f"[HISTORY ERROR] Failed to fetch chart data: {e}")
        return []



def log_scraper_run(
    scraper_name: str,
    origin: str,
    destination: str,
    status: str,
    records_count: int = 0,
    error_message: Optional[str] = None,
    duration_seconds: float = 0.0
):
    session = SessionLocal()
    try:
        log_entry = ScraperLog(
            scraper_name=scraper_name,
            origin=origin,
            destination=destination,
            status=status,
            records_count=records_count,
            error_message=error_message,
            duration_seconds=duration_seconds,
            created_at=datetime.utcnow()
        )
        session.add(log_entry)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Failed to log scraper run: {e}")
    finally:
        session.close()