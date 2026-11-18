"""Read-only connection health report for one child's WhatsApp connection.

Answers the recurring pilot question: "did we actually receive messages
every day, and were the degraded markings real outages?"

For a child (matched by name fragment or phone) prints:
1. User, guardian links, and current connection status.
2. Daily inbound/outbound message counts for the last N days, including
   days with zero messages (gap detection).
3. Connection-state episodes from ``whatsapp_connector.event_inbox``:
   how many disconnect/degraded flaps per day, their true duration
   (degraded -> next event), and the raw disconnect codes.
4. Ingestion lag per day (``created_at - timestamp``) — large lag indicates
   messages arriving late (offline catch-up), small lag means real-time.
5. Guard daily coverage stats.

All queries are SELECT-only. Usage:

    .venv/bin/python scripts/connection_health_report.py "מעין"
    .venv/bin/python scripts/connection_health_report.py "מעין" --days 30

Reads DATABASE_URL from .env.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

load_dotenv()


def _connect() -> psycopg.Connection:
    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
    return psycopg.connect(url, autocommit=True)


def _find_child(cur: psycopg.Cursor, query: str) -> tuple[str, str] | None:
    cur.execute(
        """
        SELECT id, first_name FROM users
        WHERE first_name ILIKE %s OR phone_number = %s
        ORDER BY created_at
        """,
        (f"%{query}%", query),
    )
    rows = cur.fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        print("Multiple users matched:")
        for user_id, name in rows:
            print(f"  {user_id}  {name}")
        sys.exit("Refusing to guess; pass a more specific name fragment.")
    return str(rows[0][0]), rows[0][1]


def _report(child_id: str, child_name: str, days: int) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.status, g.first_name, l.guardian_authorized_at
            FROM guardian_child_links l
            JOIN users g ON g.id = l.guardian_user_id
            WHERE l.child_user_id = %s
            """,
            (child_id,),
        )
        guardians = cur.fetchall()
        print(f"=== {child_name} ({child_id}) ===")
        for status, name, since in guardians:
            print(f"guardian: {name} ({status}, authorized {since:%Y-%m-%d})")

        cur.execute(
            """
            SELECT provider, connection_status, provider_raw_status,
                   last_authenticated_at, updated_at
            FROM whatsapp_connections WHERE user_id = %s
            """,
            (child_id,),
        )
        for provider, status, raw, auth, updated in cur.fetchall():
            print(
                f"connection: {provider} status={status} raw={raw} "
                f"auth={auth:%Y-%m-%d} updated={updated:%Y-%m-%d %H:%M}Z"
            )

        cur.execute(
            "SELECT id, provider_connection_id FROM whatsapp_connections"
            " WHERE user_id = %s",
            (child_id,),
        )
        conns = cur.fetchall()
        if not conns:
            sys.exit("No WhatsApp connection for this user.")
        provider_conn_id = str(conns[0][1])

        print(f"\n=== messages per local day (Asia/Jerusalem, last {days}) ===")
        cur.execute(
            """
            WITH days AS (
                SELECT generate_series(current_date - %s, current_date, '1 day') AS d
            )
            SELECT d.d::date,
                   coalesce(count(m.*) FILTER (WHERE m.direction = 'inbound'), 0),
                   coalesce(count(m.*) FILTER (WHERE m.direction = 'outbound'), 0),
                   round(coalesce(max(EXTRACT(EPOCH FROM (m.created_at - m.timestamp)))
                       FILTER (WHERE m.direction = 'inbound'), 0)::numeric / 60, 1)
            FROM days d
            LEFT JOIN messages m
              ON (m.timestamp AT TIME ZONE 'Asia/Jerusalem')::date = d.d
             AND m.user_id = %s
            GROUP BY d.d ORDER BY d.d
            """,
            (days - 1, child_id),
        )
        empty_days = []
        for day, inbound, outbound, max_lag_min in cur.fetchall():
            flag = ""
            if inbound == 0:
                flag = "  <-- NO INBOUND MESSAGES"
                empty_days.append(day)
            print(
                f"{day}  inbound={inbound:<4} outbound={outbound:<4} "
                f"max_ingest_lag={max_lag_min}min{flag}"
            )
        if not empty_days:
            print("=> messages received on every day in the window.")

        print("\n=== connection-state episodes (whatsapp_connector.event_inbox) ===")
        cur.execute(
            """
            WITH ev AS (
                SELECT payload->>'status' AS status,
                       payload->>'provider_raw_status' AS raw,
                       received_at,
                       LEAD(received_at) OVER (ORDER BY received_at) AS next_at
                FROM whatsapp_connector.event_inbox
                WHERE event_type = 'connection_state'
                  AND connection_id = %s
                  AND received_at >= now() - make_interval(days => %s)
            )
            SELECT (received_at AT TIME ZONE 'Asia/Jerusalem')::date AS local_date,
                   count(*) AS degraded_events,
                   round(max(EXTRACT(EPOCH FROM (next_at - received_at)))::numeric, 1),
                   round(sum(EXTRACT(EPOCH FROM (next_at - received_at)))::numeric, 1),
                   min(raw), max(raw)
            FROM ev
            WHERE status = 'degraded'
            GROUP BY 1 ORDER BY 1
            """,
            (provider_conn_id, days),
        )
        rows = cur.fetchall()
        if not rows:
            print("=> no degraded events in the window.")
        for day, events, longest, total, min_raw, max_raw in rows:
            print(
                f"{day}  degraded_events={events:<3} longest={longest}s "
                f"total={total}s raw={min_raw}"
                + (f"..{max_raw}" if max_raw != min_raw else "")
            )

        print("\n=== guard daily coverage ===")
        cur.execute(
            """
            SELECT local_date, coverage_status, coverage_health, messages_received
            FROM guard_daily_stats
            WHERE child_user_id = %s AND local_date >= current_date - %s
            ORDER BY local_date
            """,
            (child_id, days),
        )
        for day, coverage, health, received in cur.fetchall():
            print(f"{day}  {coverage:<10} health={health} messages={received}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("child", help="Child name fragment or full phone number")
    parser.add_argument("--days", type=int, default=14, help="Lookback window (default 14)")
    args = parser.parse_args()

    with _connect() as conn, conn.cursor() as cur:
        found = _find_child(cur, args.child)
    if found is None:
        sys.exit(f"No user matching {args.child!r}")
    _report(found[0], found[1], args.days)


if __name__ == "__main__":
    main()
