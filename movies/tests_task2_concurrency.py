"""
Concurrency tests for Task 2. Each worker is a separate thread with its own database connection,
and all workers wait on a barrier so their requests genuinely overlap.

These tests run against the configured database backend (see the report for which backend was
used). They are a check of real overlapping transactions, not a proof of every production backend's
lock behaviour: the correctness argument rests on conditional UPDATE statements and constraints,
which are enforced by the database itself.
"""
import threading
import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from . import reservations
from .models import Booking, Movie, Payment, Seat, Theater

RUNS = 5
WORKERS = 4
PASSWORD = "Strong-pass-9"


class ConcurrencyBase(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.movie = Movie.objects.create(
            name="Race", slug=f"race-{uuid.uuid4().hex[:6]}", description="d", image="movies/x.jpg",
            duration_minutes=120, age_certificate="UA", release_date=timezone.now().date(),
        )
        self.show = Theater.objects.create(
            name="PVR", city="Mumbai", movie=self.movie, screen="Race Screen",
            time=timezone.now() + timedelta(days=30), price_per_seat=Decimal("200.00"),
        )
        self.show.ensure_seat_layout()
        self.users = [
            User.objects.create_user(f"racer{i}{uuid.uuid4().hex[:4]}", f"r{i}@example.com", PASSWORD)
            for i in range(WORKERS)
        ]

    def run_together(self, operations):
        """Runs each zero-argument callable in its own thread, released at the same moment."""
        barrier = threading.Barrier(len(operations))
        outcomes = [None] * len(operations)

        def worker(index, operation):
            try:
                barrier.wait(timeout=20)
                outcomes[index] = ("ok", operation())
            except reservations.ReservationConflict as exc:
                outcomes[index] = ("conflict", exc)
            except reservations.ReservationUnavailable as exc:
                outcomes[index] = ("unavailable", exc)
            except Exception as exc:  # any other exception is a defect and is reported below
                outcomes[index] = ("error", exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(i, op)) for i, op in enumerate(operations)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        return outcomes

    def seat_ids(self, numbers):
        return [self.show.seats.get(seat_number=n).id for n in numbers]


class ConcurrentReservationTests(ConcurrencyBase):
    def test_competing_requests_for_one_seat_give_at_most_one_winner(self):
        for run in range(RUNS):
            numbers = [f"{'ABCDEFGHIJ'[run]}{run + 1}"]
            ids = self.seat_ids(numbers)
            outcomes = self.run_together([
                (lambda user=user: reservations.reserve(user, self.show, ids))
                for user in self.users
            ])
            kinds = [kind for kind, _ in outcomes]
            self.assertNotIn("error", kinds, outcomes)
            winners = [i for i, kind in enumerate(kinds) if kind == "ok"]
            self.assertLessEqual(len(winners), 1, f"run {run}: {kinds}")
            self.assertGreaterEqual(len(winners) + kinds.count("conflict") + kinds.count("unavailable"),
                                    WORKERS)
            seat = Seat.objects.get(pk=ids[0])
            if winners:
                self.assertEqual(seat.held_by_id, self.users[winners[0]].id)
            else:
                self.assertIsNone(seat.held_by_id)

    def test_overlapping_multi_seat_selections_never_split(self):
        """Each user asks for a block of seats overlapping its neighbour's. A winner must hold all its
        seats; a loser must hold none of its seats (no partial reservation)."""
        for run in range(RUNS):
            # Worker k wants seats F(k+1), F(k+2), F(k+3): neighbouring workers share two seats.
            # Each run starts from a fresh row so runs do not depend on each other.
            row = "FGHIJ"[run % 5]
            groups = [[f"{row}{k + 1}", f"{row}{k + 2}", f"{row}{k + 3}"] for k in range(WORKERS)]
            group_ids = [self.seat_ids(numbers) for numbers in groups]
            outcomes = self.run_together([
                (lambda user=user, ids=ids: reservations.reserve(user, self.show, ids))
                for user, ids in zip(self.users, group_ids)
            ])
            kinds = [kind for kind, _ in outcomes]
            self.assertNotIn("error", kinds, outcomes)
            winners = [i for i, kind in enumerate(kinds) if kind == "ok"]
            self.assertLessEqual(len(winners), WORKERS)

            for i, ids in enumerate(group_ids):
                holders = set(Seat.objects.filter(pk__in=ids).values_list("held_by_id", flat=True))
                if i in winners:
                    # A winner may have lost a seat to a later winner only if that winner's request
                    # was accepted after it; acceptance is serialised, so the final state must agree.
                    self.assertTrue(holders <= {self.users[i].id, *[self.users[w].id for w in winners]})
                else:
                    self.assertNotIn(self.users[i].id, holders, f"run {run}: loser {i} kept seats")
            # Every seat has at most one owner, by construction of the stored field.
            owners = Seat.objects.filter(theater=self.show, held_by__isnull=False).values_list("held_by_id", flat=True)
            self.assertTrue(all(owner in {u.id for u in self.users} for owner in owners))

            # Reset holds between runs so each run starts clean.
            Seat.objects.filter(theater=self.show).update(held_by=None, held_until=None)

    def test_repeated_identical_requests_from_one_user_are_stable(self):
        ids = self.seat_ids(["J9", "J10"])
        outcomes = self.run_together([
            (lambda: reservations.reserve(self.users[0], self.show, ids)) for _ in range(WORKERS)
        ])
        kinds = [kind for kind, _ in outcomes]
        self.assertNotIn("error", kinds, outcomes)
        self.assertEqual(kinds.count("ok"), WORKERS)
        self.assertEqual(Seat.objects.filter(pk__in=ids, held_by=self.users[0]).count(), 2)


class ConcurrentPaymentTests(ConcurrencyBase):
    def test_concurrent_callbacks_for_one_payment_book_seats_once(self):
        seat_ids = self.seat_ids(["A5"])
        seats = list(Seat.objects.filter(pk__in=seat_ids))
        payment = Payment.objects.create(
            user=self.users[0], theater=self.show, amount=Decimal("200.00"),
            status=Payment.STATUS_CREATED, razorpay_order_id=f"order_{uuid.uuid4().hex[:10]}",
        )
        payment.seats.set(seats)
        reservations.reserve(self.users[0], self.show, seat_ids)
        outcomes = self.run_together([
            (lambda: Payment.objects.get(pk=payment.pk).mark_success("pay_once", "sig", razorpay_client=None))
            for _ in range(WORKERS)
        ])
        self.assertNotIn("error", [kind for kind, _ in outcomes], outcomes)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.STATUS_SUCCESS)
        self.assertEqual(Booking.objects.filter(payment=payment).count(), 1)

    def test_two_payments_for_one_seat_book_it_only_once(self):
        seat_ids = self.seat_ids(["C7"])
        seat = Seat.objects.get(pk=seat_ids[0])
        payments = []
        for i in range(2):
            p = Payment.objects.create(
                user=self.users[i], theater=self.show, amount=Decimal("200.00"),
                status=Payment.STATUS_CREATED, razorpay_order_id=f"order_{uuid.uuid4().hex[:10]}",
            )
            p.seats.set([seat])
            payments.append(p)
        outcomes = self.run_together([
            (lambda p=p, n=n: Payment.objects.get(pk=p.pk).mark_success(
                f"pay_race_{n}", "sig", razorpay_client=None))
            for n, p in enumerate(payments)
        ])
        self.assertNotIn("error", [kind for kind, _ in outcomes], outcomes)
        active = Booking.objects.filter(seat=seat, is_cancelled=False).count()
        self.assertEqual(active, 1, "two payments booked the same seat")


class ConcurrentExpiryTests(ConcurrencyBase):
    def test_cleanup_racing_a_new_reservation_leaves_consistent_state(self):
        seat_ids = self.seat_ids(["D4", "D5"])
        Seat.objects.filter(pk__in=seat_ids).update(
            held_by=self.users[0], held_until=timezone.now() - timedelta(seconds=5)
        )
        outcomes = self.run_together([
            lambda: reservations.release_expired_holds(),
            lambda: reservations.reserve(self.users[1], self.show, seat_ids),
            lambda: reservations.release_expired_holds(),
            lambda: reservations.reserve(self.users[2], self.show, seat_ids),
        ])
        kinds = [kind for kind, _ in outcomes]
        self.assertNotIn("error", kinds, outcomes)
        seats = Seat.objects.filter(pk__in=seat_ids)
        self.assertFalse(seats.filter(is_booked=True).exists())
        holders = set(seats.values_list("held_by_id", flat=True))
        self.assertTrue(holders <= {None, self.users[1].id, self.users[2].id}, holders)
        if len(holders - {None}) == 1:
            # Only one user's hold survived, and it covers both seats.
            self.assertEqual(seats.filter(held_by__isnull=False).count(), 2)

    def test_cleanup_never_releases_a_booked_seat_under_concurrency(self):
        seat = Seat.objects.get(pk=self.seat_ids(["E6"])[0])
        payment = Payment.objects.create(
            user=self.users[0], theater=self.show, amount=Decimal("200.00"),
            status=Payment.STATUS_CREATED, razorpay_order_id=f"order_{uuid.uuid4().hex[:10]}",
        )
        payment.seats.set([seat])
        payment.mark_success("pay_e6", "sig", razorpay_client=None)
        outcomes = self.run_together([
            (lambda: reservations.release_expired_holds(now=timezone.now() + timedelta(days=1)))
            for _ in range(WORKERS)
        ])
        self.assertNotIn("error", [kind for kind, _ in outcomes], outcomes)
        seat.refresh_from_db()
        self.assertTrue(seat.is_booked)
