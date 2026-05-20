import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "flight_deals.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_date       TEXT NOT NULL,
    origin              TEXT NOT NULL,
    destination         TEXT NOT NULL,
    trip                TEXT NOT NULL,
    n                   INTEGER NOT NULL,
    min_price           INTEGER NOT NULL,
    p25                 REAL,
    median              REAL,
    mean                REAL,
    cheapest_depart_at  TEXT,
    cheapest_return_at  TEXT,
    cheapest_airline    TEXT,
    cheapest_gate       TEXT,
    cheapest_link       TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (snapshot_date, origin, destination, trip)
);
"""


def connect(db_path=DB_PATH):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_snapshot(conn, row):
    """Keep the lowest minimum seen for the day: on conflict, replace the row
    only when the new run found a cheaper price."""
    conn.execute(
        """
        INSERT INTO snapshots
            (snapshot_date, origin, destination, trip, n, min_price,
             p25, median, mean, cheapest_depart_at, cheapest_return_at,
             cheapest_airline, cheapest_gate, cheapest_link)
        VALUES
            (:snapshot_date, :origin, :destination, :trip, :n, :min_price,
             :p25, :median, :mean, :cheapest_depart_at, :cheapest_return_at,
             :cheapest_airline, :cheapest_gate, :cheapest_link)
        ON CONFLICT(snapshot_date, origin, destination, trip) DO UPDATE SET
            n = excluded.n,
            min_price = excluded.min_price,
            p25 = excluded.p25,
            median = excluded.median,
            mean = excluded.mean,
            cheapest_depart_at = excluded.cheapest_depart_at,
            cheapest_return_at = excluded.cheapest_return_at,
            cheapest_airline = excluded.cheapest_airline,
            cheapest_gate = excluded.cheapest_gate,
            cheapest_link = excluded.cheapest_link,
            created_at = datetime('now')
        WHERE excluded.min_price < snapshots.min_price
        """,
        row,
    )
    conn.commit()


def historical_mins(conn, origin, destination, trip, before_date):
    """Daily cheapest prices recorded before `before_date`, oldest first."""
    rows = conn.execute(
        """
        SELECT min_price FROM snapshots
        WHERE origin = ? AND destination = ? AND trip = ?
          AND snapshot_date < ?
        ORDER BY snapshot_date
        """,
        (origin, destination, trip, before_date),
    ).fetchall()
    return [r["min_price"] for r in rows]
