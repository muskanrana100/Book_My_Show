# Admin Dashboard: Performance Report (Task 4)

## Admin access for evaluation

| Field | Value |
|---|---|
| Dashboard URL | `/movies/admin-dashboard/` |
| Admin site | `/admin/` |
| Username | `<FILL IN: evaluation admin username>` |
| Password | `<FILL IN: evaluation admin password>` |

Create a dedicated evaluation account rather than sharing a personal one:

```
python manage.py createsuperuser
```

Only users with `is_staff` can reach the dashboard and the CSV export. Anonymous users and regular users are redirected to the admin login.

## Dataset

- 100,000 bookings across 61 days (about 5% cancelled), created with `python manage.py seed_bookings --bookings 100000`.
- 100,000 successful payments, 1,000 showtimes, 20 movies, 200,000 seats.
- The database schema includes migration `0009`, which adds the partial unique constraint on active bookings per seat.

## Method

- Each endpoint is requested through Django's test client, with the same user, and timed as the median of 5 runs (3 for the CSV).
- Query count comes from Django's `CaptureQueriesContext`.
- Each run is in a separate process, on a copy of the database, so no run affects another.
- Environment: a Linux container running Python 3.12, Django 5.2.17, SQLite 3. Absolute times depend on the machine; the before/after comparison is what matters.
- Variance between runs was about 10% (for example, the optimized 30-day dashboard measured 1,054 ms in one run and 1,118 ms in another).

## Results

| Endpoint | Before | After | Change | Queries before → after |
|---|---|---|---|---|
| Dashboard, default 30 days | 2,244 ms | 1,118 ms | about 50% faster | 15 → 11 |
| Dashboard, 90-day range | 4,569 ms | 2,121 ms | about 54% faster | 15 → 11 |
| CSV export, 90 days (7.26 MB) | 4,261 ms | 1,938 ms | about 55% faster | 3 → 3 |

The CSV export is byte-identical before and after (7,261,365 bytes), which confirms the rewrite produces the same output.

## Where the time went

Profiling the dashboard showed the cost was not in the indexed lookups. It was in the grouping:

- Django's timezone-aware date functions (`TruncDate`, `TruncWeek`, `TruncMonth`, `TruncYear`, `ExtractHour`) run on SQLite as **Python callbacks, once per row**, so a `GROUP BY` on them runs one Python call per row.
- The original dashboard ran four separate revenue `GROUP BY`s (day, week, month, year). Each one scanned all payments and made a Python call per row. Together they took about 2.9 s of the 4.3 s total.
- The CSV export built a full Django model instance for each of 70,000+ rows, along with joined user, movie, theater and seat data for each one.

## Optimizations applied

1. **Single revenue pass.** One grouped query by day replaces the four. Week, month and year totals are sums of those day totals, computed in Python from at most about 366 rows. A test checks every week and month bucket against an independent ORM sum.
2. **`values_list` for the CSV.** Plain tuples replace model instances, which removes per-row object construction and the joined related objects.
3. **Filters that use the analytics indexes.** All date-range filters use `booked_at` and `created_at` directly, not computed expressions, so the planner can use the indexes.

## Indexing analysis

Indexes on the analytics columns (migration `0005` and the `Booking`/`Payment` `Meta.indexes`):

- `booking_booked_at_idx`, `booking_date_cancel_idx` on `movies_booking(booked_at, is_cancelled)`
- `booking_movie_date_idx`, `booking_theater_date_idx` on `movies_booking(movie/theater, booked_at)`
- `payment_created_at_idx`, `payment_date_status_idx` on `movies_payment(created_at[, status])`
- `unique_active_booking_per_seat`: partial unique index on `movies_booking(seat)` where `is_cancelled = 0`. This enforces the one-active-booking-per-seat rule at the database level.

Query plans confirm the indexes are used. For `booked_at` and `created_at` range filters:

| Table | Without analytics indexes | With analytics indexes |
|---|---|---|
| `movies_booking` | `SCAN movies_booking` | `SEARCH movies_booking USING COVERING INDEX booking_date_cancel_idx` |
| `movies_payment` | `SCAN movies_payment` | `SEARCH movies_payment USING INDEX payment_created_at_idx` |

**Honest finding on timing:** on SQLite, dropping the analytics indexes did not produce a consistent change in dashboard time (in separate runs it was slightly slower in one and slightly faster in another, both within run-to-run noise). The reason is that the dominant cost is the per-row Python date callbacks described above, which run whether or not the scan is indexed. The indexes are still the correct design, because they reduce rows scanned for any range query and are what a production database uses.

## Production recommendation (not measured here)

- **Use PostgreSQL in production** (`DATABASE_URL`). Its date functions run in the database engine, so the per-row Python cost disappears and the indexes above are used for the aggregates. I could not run PostgreSQL in this environment, so the speed-up on PostgreSQL is not measured in this report.
- For very large histories, add a daily rollup table (`revenue_by_day`, `bookings_by_day`) updated when payments and bookings are confirmed or cancelled. The dashboard then reads a few hundred rows regardless of booking volume.

## Data-quality fix found during this work

The seeder stored every booking and payment with the same timestamp. `auto_now_add` overwrote the intended values during `bulk_create`, and the seeder then copied the overwritten values back. All rows fell in a single two-hour window, which made trends and peak-hour charts meaningless. The seeder now writes the intended timestamps (`movies/management/commands/seed_bookings.py`). The bundled demo database was respread across the same 60-day span, with the superuser and all other rows left unchanged.
