import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from movies.models import Booking, Movie, Payment, Seat, Theater


class Command(BaseCommand):
    help = "Generate realistic bookings in the CURRENT database. Use only with DATABASE_URL pointing at a performance database."

    def add_arguments(self, parser):
        parser.add_argument("--bookings", type=int, default=100000)
        parser.add_argument("--seed", type=int, default=42)

    def handle(self, *args, **options):
        random.seed(options["seed"])
        target = options["bookings"]
        now = timezone.now()

        users = User.objects.bulk_create([
            User(username=f"perf_user_{i}", email=f"p{i}@example.com", date_joined=now - timedelta(days=random.randint(0, 365)))
            for i in range(2000)
        ])
        movies = Movie.objects.bulk_create([
            Movie(name=f"Perf Movie {i}", slug=f"perf-movie-{i}", description="d", image="movies/x.jpg",
                  duration_minutes=120, age_certificate="UA", release_date=now.date() - timedelta(days=random.randint(0, 900)))
            for i in range(20)
        ])
        theaters = []
        for i in range(1000):
            theaters.append(Theater(
                name=f"Perf Venue {i % 40}", city="Pune", movie=random.choice(movies), screen=f"S{i % 5}",
                time=now - timedelta(days=random.randint(0, 365), hours=random.randint(0, 23)),
                price_per_seat=Decimal(random.choice(["150.00", "200.00", "250.00", "300.00"])),
            ))
        theaters = Theater.objects.bulk_create(theaters)

        seats = []
        for theater in theaters:
            seats.extend(Seat(theater=theater, seat_number=f"{r}{n}") for r in "ABCDEFGHIJ" for n in range(1, 11))
        seats = Seat.objects.bulk_create(seats, batch_size=5000)
        seats = list(Seat.objects.values_list("id", "theater_id"))

        by_theater = {}
        for seat_id, theater_id in seats:
            by_theater.setdefault(theater_id, []).append(seat_id)
        for theater_id in by_theater:
            random.shuffle(by_theater[theater_id])

        created = 0
        batch = []
        pay_batch = []
        for theater in theaters:
            pool = by_theater.get(theater.id, [])
            for _ in range(min(len(pool), target // len(theaters) + 1)):
                if created >= target:
                    break
                payment_status = random.choices(
                    [Payment.STATUS_SUCCESS, Payment.STATUS_FAILED, Payment.STATUS_CREATED],
                    weights=[90, 7, 3],
                )[0]
                cancelled = payment_status == Payment.STATUS_SUCCESS and random.random() < 0.05
                booked_at = now - timedelta(days=random.randint(0, 365), minutes=random.randint(0, 1439))
                user = random.choice(users) if False else users[random.randrange(len(users))]
                payment = Payment(
                    user=user, theater=theater, amount=theater.price_per_seat, status=payment_status,
                    razorpay_order_id=f"perf_order_{created}",
                    razorpay_payment_id=f"perf_pay_{created}" if payment_status == Payment.STATUS_SUCCESS else None,
                )
                pay_batch.append((payment, booked_at, cancelled, pool.pop(), user, theater))
                created += 1
                if len(pay_batch) >= 2000:
                    self._flush(pay_batch)
                    pay_batch = []
        if pay_batch:
            self._flush(pay_batch)
            
        # auto_now_add overwrote booked_at on insert, so every booking got the insert time.
        # Spread the timestamps over the past 365 days with one UPDATE per row, in batches.
        from django.db import connection
        from django.utils import timezone as tz
        import random as _random
        spread_now = tz.now()
        booking_ids = list(Booking.objects.values_list("id", flat=True))
        with connection.cursor() as cur:
            for start in range(0, len(booking_ids), 5000):
                chunk = booking_ids[start:start + 5000]
                cases = []
                params = []
                for bid in chunk:
                    offset = timedelta(days=_random.randint(0, 364), minutes=_random.randint(0, 1439))
                    cases.append("WHEN %s THEN %s")
                    params.extend([bid, (spread_now - offset).isoformat()])
                placeholders = ",".join(["%s"] * len(chunk))
                cur.execute(
                    f"UPDATE movies_booking SET booked_at = CASE id {' '.join(cases)} END "
                    f"WHERE id IN ({placeholders})",
                    params + chunk,
                )
        self.stdout.write(self.style.SUCCESS(f"Created {created} bookings."))

    def _flush(self, pay_batch):
        payments = Payment.objects.bulk_create([row[0] for row in pay_batch], batch_size=2000)
        bookings = []
        for (payment, booked_at, cancelled, seat_id, user, theater), saved in zip(pay_batch, payments):
            bookings.append(Booking(
                user=user, seat_id=seat_id, movie=theater.movie, theater=theater, payment=saved,
                is_cancelled=cancelled, cancelled_at=booked_at if cancelled else None,
                refund_status=Booking.REFUND_REFUNDED if cancelled else Booking.REFUND_NONE,
                refund_amount=theater.price_per_seat if cancelled else None,
                booked_at=booked_at,
            ))
        Booking.objects.bulk_create(bookings, batch_size=2000)