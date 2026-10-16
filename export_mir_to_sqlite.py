import sqlite3
import pandas as pd
import psycopg2
import json
import uuid
import decimal
import datetime

POSTGRES_DB = "mir_backend"
POSTGRES_USER = "mir_user"
POSTGRES_PASSWORD = "Sahil1200secure"
POSTGRES_HOST = "localhost"
POSTGRES_PORT = 5432

SQLITE_FILE = "mir_backend.sqlite"


def make_sqlite_safe(value):
    """
    Convert PostgreSQL/Python values into values SQLite can store.
    """

    if value is None:
        return None

    # PostgreSQL JSON / JSONB
    if isinstance(value, dict):
        return json.dumps(value, default=str, ensure_ascii=False)

    # PostgreSQL arrays / JSON arrays
    if isinstance(value, (list, tuple, set)):
        return json.dumps(list(value), default=str, ensure_ascii=False)

    # UUID
    if isinstance(value, uuid.UUID):
        return str(value)

    # Decimal / NUMERIC
    if isinstance(value, decimal.Decimal):
        return str(value)

    # Dates and timestamps
    if isinstance(
        value,
        (
            datetime.datetime,
            datetime.date,
            datetime.time
        )
    ):
        return value.isoformat()

    # BYTEA may sometimes appear as memoryview
    if isinstance(value, memoryview):
        return bytes(value)

    # Values SQLite already understands
    if isinstance(value, (str, int, float, bytes)):
        return value

    # Fallback for unusual PostgreSQL types
    return str(value)


print("Connecting to PostgreSQL...")

pg = psycopg2.connect(
    dbname=POSTGRES_DB,
    user=POSTGRES_USER,
    password=POSTGRES_PASSWORD,
    host=POSTGRES_HOST,
    port=POSTGRES_PORT
)

print("Connected.")

sqlite_conn = sqlite3.connect(SQLITE_FILE)

cur = pg.cursor()

cur.execute("""
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_type = 'BASE TABLE'
ORDER BY table_name
""")

tables = [row[0] for row in cur.fetchall()]

print(f"Found {len(tables)} tables")


for table in tables:

    print(f"\nExporting {table}")

    first = True
    total = 0

    query = f'SELECT * FROM "{table}"'

    for chunk in pd.read_sql_query(
        query,
        pg,
        chunksize=10000
    ):

        # Convert PostgreSQL-specific object values
        for column in chunk.columns:

            if chunk[column].dtype == "object":

                chunk[column] = chunk[column].map(
                    make_sqlite_safe
                )

        chunk.to_sql(
            table,
            sqlite_conn,
            if_exists="replace" if first else "append",
            index=False
        )

        first = False
        total += len(chunk)

        print(f"  {total} rows exported")

    # Handle completely empty tables
    if first:
        empty_df = pd.read_sql_query(
            f'SELECT * FROM "{table}" LIMIT 0',
            pg
        )

        empty_df.to_sql(
            table,
            sqlite_conn,
            if_exists="replace",
            index=False
        )

        print("  0 rows exported")


print("\nExport complete.")

cur.close()
pg.close()
sqlite_conn.close()

print(f"Created: {SQLITE_FILE}")
