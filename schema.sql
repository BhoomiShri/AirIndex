-- Step 1: Create Database
CREATE DATABASE IF NOT EXISTS aeroindex_db;
USE aeroindex_db;

-- Step 2: Create Main Table
CREATE TABLE IF NOT EXISTS flight_prices (
    id INT AUTO_INCREMENT PRIMARY KEY,
    airline VARCHAR(50) NOT NULL,
    flight_number VARCHAR(20) NOT NULL,
    origin VARCHAR(3) NOT NULL,
    destination VARCHAR(3) NOT NULL,
    departure_time DATETIME NOT NULL,
    price DECIMAL(10,2) NOT NULL,
    scraped_at DATETIME NOT NULL
);

-- Step 3: Create Composite Index for Speeding Up Route & Time Queries
CREATE INDEX idx_route_scraped
ON flight_prices(origin, destination, scraped_at);