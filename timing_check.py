import time
from datetime import timedelta
from django.db import connection
from django.utils import timezone

now = timezone.now()
start_day = (now - timedelta(days=7)).strftime("%Y-%m-%d")
end_day = now.strftime("%Y-%m-%d")

queries = {
    "plain booking count": (
        f"SELECT COUNT(*) FROM movies_booking "
        f"WHERE booked_at >= '{start_day}' AND booked_at < '{end_day}'"
    ),
    "booking trend (local day)": (
        f"SELECT django_datetime_cast_date(booked_at, 'Asia/Kolkata', 'UTC') AS d, COUNT(*) "
        f"FROM movies_booking WHERE booked_at >= '{start_day}' AND booked_at < '{end_day}' GROUP BY d"
    ),
    "peak hour (local hour)": (
        f"SELECT django_datetime_extract('hour', booked_at, 'Asia/Kolkata', 'UTC') AS h, COUNT(*) "
        f"FROM movies_booking WHERE booked_at >= '{start_day}' AND booked_at < '{end_day}' GROUP BY h"
    ),
    "payment revenue (local day)": (
        f"SELECT django_datetime_cast_date(created_at, 'Asia/Kolkata', 'UTC') AS d, SUM(amount) "
        f"FROM movies_payment WHERE status = 'success' AND created_at >= '{start_day}' AND created_at < '{end_day}' GROUP BY d"
    ),
}

c = connection.cursor()
for label, sql in queries.items():
    start = time.perf_counter()
    c.execute(sql)
    rows = c.fetchall()
    print(f"{label:<30} {round((time.perf_counter() - start) * 1000, 1):>8} ms  {len(rows)} rows")