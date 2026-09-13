from datetime import datetime, timedelta
import random

AIRLINES = ['IndiGo', 'Air India', 'Vistara', 'SpiceJet']
ROUTES = [('DEL', 'BOM'), ('BOM', 'DEL'), ('BLR', 'DEL'), ('DEL', 'BLR')]
BASE_PRICES = {'DEL-BOM': 4500.0, 'BOM-DEL': 4400.0, 'BLR-DEL': 5200.0, 'DEL-BLR': 5100.0}

now = datetime.now()
sql_statements = ['USE aeroindex_db;\nINSERT INTO flight_prices (airline, flight_number, origin, destination, departure_time, price, scraped_at) VALUES\n']

values = []
for day_offset in range(30, -1, -1):
    scrape_date = now - timedelta(days=day_offset)
    for origin, dest in ROUTES:
        base_fare = BASE_PRICES.get(f'{origin}-{dest}', 4500.0)
        for airline in AIRLINES:
            price = round(base_fare * random.uniform(0.85, 1.15), 2)
            flight_num = f'{airline[:2].upper()}-{random.randint(100, 999)}'
            dep_time = (scrape_date + timedelta(days=7, hours=random.randint(6, 22))).strftime('%Y-%m-%d %H:%M:%S')
            scraped_str = scrape_date.strftime('%Y-%m-%d %H:%M:%S')
            values.append(f"('{airline}', '{flight_num}', '{origin}', '{dest}', '{dep_time}', {price}, '{scraped_str}')")

sql_statements.append(',\n'.join(values) + ';')

with open('seed.sql', 'w') as f:
    f.writelines(sql_statements)

print('seed.sql generated successfully!')
