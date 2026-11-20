"""
Task 2 tests: seat inventory, atomic multi-seat holds, the 120-second hold lifecycle (controlled
time, no sleeping), owner-only modification, expiry cleanup, payment settlement, security and the
live seat map. Concurrency tests live in tests_task2_concurrency.py.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import Client, TestCase

from . import reservations
from .eligibility import user_has_completed_viewing
from .models import Booking, Movie, Payment, Seat, Theater

T0 = datetime(2030, 1, 1, 12, 0, tzinfo=dt_timezone.utc)   # controlled "now"
SHOW_START = T0 + timedelta(days=5)
PASSWORD = "Strong-pass-9"


def make_movie(slug="task2-movie"):
    return Movie.objects.create(
        name="Task 2 Movie", slug=f"{slug}-{uuid.uuid4().hex[:6]}", description="d",
        image="movies/x.jpg", duration_minutes=120, age_certificate="UA",
        release_date=datetime(2020, 1, 1).date(),
    )


def make_show(movie=None, screen=None, start=SHOW_START):
    movie = movie or make_movie()
    theater = Theater.objects.create(
        name="PVR", city="Mumbai", movie=movie, screen=screen or f"S{uuid.uuid4().hex[:6]}",
        time=start, price_per_seat=Decimal("200.00"),
    )
    theater.ensure_seat_layout()
    return theater


def make_payment(user, theater, seats, status=Payment.STATUS_CREATED):
    payment = Payment.objects.create(
        user=user, theater=theater, amount=Decimal("200.00") * len(seats), status=status,
        razorpay_order_id=f"order_{uuid.uuid4().hex[:12]}",
    )
    payment.seats.set(seats)
    return payment


class Task2Base(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(f"alice{uuid.uuid4().hex[:5]}", "a@example.com", PASSWORD)
        self.bob = User.objects.create_user(f"bob{uuid.uuid4().hex[:5]}", "b@example.com", PASSWORD)
        self.show = make_show()
        self.clock = patch("django.utils.timezone.now", return_value=T0)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def at(self, seconds):
        """Moves the controlled clock to T0 + seconds."""
        return T0 + timedelta(seconds=seconds)

    def seat(self, number, theater=None):
        return (theater or self.show).seats.get(seat_number=number)

    def reserve(self, user, seat_ids, theater=None, now=None):
        return reservations.reserve(user, theater or self.show, seat_ids, now=now or T0)


# ---------------------------------------------------------------------------
# A. Inventory
# ---------------------------------------------------------------------------

class InventoryTests(Task2Base):
    def test_each_show_gets_its_own_100_seat_inventory(self):
        self.assertEqual(self.show.seats.count(), 100)
        self.assertEqual(self.show.seats.filter(seat_number="A1").count(), 1)

    def test_same_seat_number_in_another_show_is_a_separate_seat(self):
        other = make_show(movie=self.show.movie)
        self.reserve(self.alice, [self.seat("A1").id])
        self.assertTrue(other.seats.get(seat_number="A1").held_by_id is None)
        self.reserve(self.bob, [self.seat("A1", theater=other).id], theater=other)
        self.assertEqual(self.seat("A1").held_by_id, self.alice.id)

    def test_booked_seat_in_one_show_does_not_block_another_show(self):
        other = make_show(movie=self.show.movie)
        seat = self.seat("B2")
        payment = make_payment(self.alice, self.show, [seat])
        payment.mark_success("pay_one", "sig", razorpay_client=None)
        self.assertTrue(self.seat("B2").is_booked)
        self.reserve(self.bob, [other.seats.get(seat_number="B2").id], theater=other)
        self.assertEqual(other.seats.get(seat_number="B2").held_by_id, self.bob.id)

    def test_duplicate_seat_number_in_one_show_is_refused_by_the_database(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Seat.objects.create(theater=self.show, seat_number="A1")

    def test_seat_from_another_show_is_rejected(self):
        other = make_show(movie=self.show.movie)
        with self.assertRaises(reservations.ReservationError):
            self.reserve(self.alice, [other.seats.get(seat_number="A1").id])


# ---------------------------------------------------------------------------
# B. Atomic multi-seat holds
# ---------------------------------------------------------------------------

class MultiSeatHoldTests(Task2Base):
    def test_several_seats_are_held_together(self):
        ids = [self.seat(n).id for n in ("A1", "A2", "A3")]
        result = self.reserve(self.alice, ids)
        self.assertEqual(sorted(result["held_seat_ids"]), sorted(ids))
        self.assertEqual(self.show.seats.filter(held_by=self.alice).count(), 3)

    def test_duplicate_seat_ids_in_one_request_are_rejected(self):
        a1 = self.seat("A1").id
        with self.assertRaises(reservations.ReservationError):
            self.reserve(self.alice, [a1, a1])

    def test_request_with_a_booked_seat_changes_nothing(self):
        payment = make_payment(self.bob, self.show, [self.seat("C3")])
        payment.mark_success("pay_b", "sig", razorpay_client=None)
        ids = [self.seat("A1").id, self.seat("C3").id]
        with self.assertRaises(reservations.ReservationConflict) as ctx:
            self.reserve(self.alice, ids)
        self.assertEqual([c["seat_number"] for c in ctx.exception.conflicts], ["C3"])
        self.assertFalse(self.show.seats.filter(held_by=self.alice).exists(),
                         "a partial hold was left behind")

    def test_request_with_a_seat_held_by_someone_else_changes_nothing(self):
        self.reserve(self.bob, [self.seat("D4").id])
        with self.assertRaises(reservations.ReservationConflict):
            self.reserve(self.alice, [self.seat("A1").id, self.seat("D4").id])
        self.assertFalse(self.show.seats.filter(held_by=self.alice).exists())
        self.assertEqual(self.seat("D4").held_by_id, self.bob.id)

    def test_too_many_seats_are_refused(self):
        ids = [self.seat(f"A{n}").id for n in range(1, 11)] + [self.seat("B1").id]
        with self.assertRaises(reservations.ReservationError):
            self.reserve(self.alice, ids)

    def test_non_integer_seat_ids_are_refused(self):
        for bad in (["A1"], [True], [0], [-3], "A1"):
            with self.assertRaises(reservations.ReservationError, msg=str(bad)):
                self.reserve(self.alice, bad)

    def test_a_show_that_has_started_cannot_be_reserved(self):
        with self.assertRaises(reservations.ReservationError):
            self.reserve(self.alice, [self.seat("A1").id], now=SHOW_START + timedelta(minutes=1))


# ---------------------------------------------------------------------------
# C. Hold lifetime: 120 seconds, exact boundaries, controlled time
# ---------------------------------------------------------------------------

class HoldLifetimeTests(Task2Base):
    def test_hold_expires_exactly_120_seconds_after_it_was_made(self):
        result = self.reserve(self.alice, [self.seat("A1").id], now=self.at(0))
        self.assertEqual(result["expires_in_seconds"], 120)
        self.assertEqual(self.seat("A1").held_until, self.at(120))

    def test_at_119_seconds_the_hold_still_blocks_other_users(self):
        self.reserve(self.alice, [self.seat("A1").id], now=self.at(0))
        with self.assertRaises(reservations.ReservationConflict):
            self.reserve(self.bob, [self.seat("A1").id], now=self.at(119))

    def test_at_exactly_120_seconds_the_seat_is_available_again(self):
        self.reserve(self.alice, [self.seat("A1").id], now=self.at(0))
        result = self.reserve(self.bob, [self.seat("A1").id], now=self.at(120))
        self.assertEqual(result["held_seat_ids"], [self.seat("A1").id])
        self.assertEqual(self.seat("A1").held_by_id, self.bob.id)

    def test_after_expiry_the_seat_is_available_to_another_user(self):
        self.reserve(self.alice, [self.seat("A1").id], now=self.at(0))
        self.reserve(self.bob, [self.seat("A1").id], now=self.at(600))
        self.assertEqual(self.seat("A1").held_by_id, self.bob.id)

    def test_seat_map_reports_expired_hold_as_available(self):
        self.reserve(self.alice, [self.seat("A1").id], now=self.at(0))
        states = {s["seat_number"]: s["status"] for s in
                  reservations.seat_states_for(self.bob, self.show, now=self.at(121))}
        self.assertEqual(states["A1"], "available")

    def test_owner_modification_refreshes_the_deadline_for_kept_seats(self):
        self.reserve(self.alice, [self.seat("A1").id, self.seat("A2").id], now=self.at(0))
        self.reserve(self.alice, [self.seat("A1").id, self.seat("A2").id, self.seat("A3").id], now=self.at(100))
        self.assertEqual(self.seat("A1").held_until, self.at(220))
        self.assertEqual(self.seat("A3").held_until, self.at(220))

    def test_polling_the_live_map_does_not_extend_a_hold(self):
        self.reserve(self.alice, [self.seat("A1").id], now=self.at(0))
        client = Client()
        client.force_login(self.alice)
        url = f"/movies/theater/{self.show.id}/seats/status/"
        for second in (30, 60, 90, 110):
            with patch("django.utils.timezone.now", return_value=self.at(second)):
                client.get(url)
        self.assertEqual(self.seat("A1").held_until, self.at(120))


# ---------------------------------------------------------------------------
# D. Owner-only modification and release
# ---------------------------------------------------------------------------

class ModificationTests(Task2Base):
    def test_owner_can_remove_a_seat_and_it_becomes_available(self):
        self.reserve(self.alice, [self.seat("A1").id, self.seat("A2").id])
        self.reserve(self.alice, [self.seat("A1").id])
        self.assertIsNone(self.seat("A2").held_by_id)
        self.assertEqual(self.seat("A1").held_by_id, self.alice.id)

    def test_deselecting_every_seat_releases_them_all(self):
        self.reserve(self.alice, [self.seat("A1").id, self.seat("A2").id])
        result = self.reserve(self.alice, [])
        self.assertEqual(result["held_seat_ids"], [])
        self.assertFalse(self.show.seats.filter(held_by=self.alice).exists())

    def test_failed_modification_keeps_the_previous_selection(self):
        self.reserve(self.alice, [self.seat("A1").id])
        self.reserve(self.bob, [self.seat("E5").id])
        with self.assertRaises(reservations.ReservationConflict):
            self.reserve(self.alice, [self.seat("A1").id, self.seat("E5").id])
        self.assertEqual(self.seat("A1").held_by_id, self.alice.id)

    def test_user_cannot_release_another_users_seat(self):
        self.reserve(self.alice, [self.seat("A1").id])
        result = reservations.release(self.bob, self.show, [self.seat("A1").id])
        self.assertEqual(result["released"], 0)
        self.assertEqual(self.seat("A1").held_by_id, self.alice.id)

    def test_release_endpoint_only_affects_the_requesting_owner(self):
        self.reserve(self.alice, [self.seat("A1").id])
        client = Client()
        client.force_login(self.bob)
        client.post(f"/movies/theater/{self.show.id}/seats/{self.seat('A1').id}/release/")
        self.assertEqual(self.seat("A1").held_by_id, self.alice.id)

    def test_user_cannot_modify_another_users_hold_through_the_api(self):
        self.reserve(self.alice, [self.seat("A1").id])
        client = Client()
        client.force_login(self.bob)
        response = client.post(
            f"/movies/theater/{self.show.id}/seats/reserve/",
            data=json.dumps({"seat_ids": [self.seat("A2").id]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seat("A1").held_by_id, self.alice.id)


# ---------------------------------------------------------------------------
# E. Cleanup and payment settlement
# ---------------------------------------------------------------------------

class CleanupTests(Task2Base):
    def test_cleanup_clears_only_expired_unbooked_holds(self):
        self.reserve(self.alice, [self.seat("A1").id], now=self.at(0))
        self.reserve(self.bob, [self.seat("A2").id], now=self.at(200))
        released = reservations.release_expired_holds(now=self.at(150))
        self.assertEqual(released, 1)
        self.assertIsNone(self.seat("A1").held_by_id)
        self.assertEqual(self.seat("A2").held_by_id, self.bob.id)

    def test_cleanup_never_releases_a_booked_seat(self):
        seat = self.seat("F6")
        payment = make_payment(self.alice, self.show, [seat])
        payment.mark_success("pay_c", "sig", razorpay_client=None)
        Seat.objects.filter(pk=seat.pk).update(held_by=self.alice, held_until=self.at(0))
        reservations.release_expired_holds(now=self.at(999))
        seat.refresh_from_db()
        self.assertTrue(seat.is_booked)

    def test_cleanup_is_idempotent(self):
        self.reserve(self.alice, [self.seat("A1").id], now=self.at(0))
        self.assertEqual(reservations.release_expired_holds(now=self.at(500)), 1)
        self.assertEqual(reservations.release_expired_holds(now=self.at(500)), 0)

    def test_management_command_runs(self):
        from io import StringIO
        from django.core.management import call_command
        self.reserve(self.alice, [self.seat("A1").id], now=self.at(0))
        out = StringIO()
        with patch("django.utils.timezone.now", return_value=self.at(500)):
            call_command("release_expired_holds", stdout=out)
        self.assertIn("Cleared 1", out.getvalue())


class PaymentSettlementTests(Task2Base):
    def test_successful_payment_confirms_seats_exactly_once(self):
        seats = [self.seat("A1"), self.seat("A2")]
        self.reserve(self.alice, [s.id for s in seats])
        payment = make_payment(self.alice, self.show, seats)
        payment.mark_success("pay_ok", "sig", razorpay_client=None)
        payment.mark_success("pay_ok", "sig", razorpay_client=None)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.STATUS_SUCCESS)
        self.assertEqual(Booking.objects.filter(payment=payment).count(), 2)
        self.assertTrue(all(Seat.objects.get(pk=s.pk).is_booked for s in seats))

    def test_confirmed_seats_are_not_held_any_more(self):
        seat = self.seat("A1")
        self.reserve(self.alice, [seat.id])
        payment = make_payment(self.alice, self.show, [seat])
        payment.mark_success("pay_h", "sig", razorpay_client=None)
        seat.refresh_from_db()
        self.assertTrue(seat.is_booked)
        self.assertIsNone(seat.held_by_id)

    def test_pending_payment_is_not_marked_successful(self):
        payment = make_payment(self.alice, self.show, [self.seat("A1")], status=Payment.STATUS_CREATED)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.STATUS_CREATED)
        self.assertFalse(Booking.objects.filter(payment=payment).exists())

    def test_late_success_refunds_when_another_user_now_holds_the_seat(self):
        seat = self.seat("A1")
        self.reserve(self.alice, [seat.id], now=self.at(0))
        payment = make_payment(self.alice, self.show, [seat])
        self.reserve(self.bob, [seat.id], now=self.at(200))   # alice's hold expired; bob holds it
        client = MagicMock()
        client.payment.refund.return_value = {"id": "rfnd_late"}
        with patch("django.utils.timezone.now", return_value=self.at(210)):
            payment.mark_success("pay_late", "sig", razorpay_client=client)
        payment.refresh_from_db()
        seat.refresh_from_db()
        self.assertEqual(payment.status, Payment.STATUS_REFUNDED)
        self.assertFalse(Booking.objects.filter(payment=payment).exists())
        self.assertFalse(seat.is_booked)
        self.assertEqual(seat.held_by_id, bob_id(self))

    def test_late_success_refunds_when_the_seat_is_already_booked(self):
        seat = self.seat("B1")
        first = make_payment(self.bob, self.show, [seat])
        first.mark_success("pay_first", "sig", razorpay_client=None)
        late = make_payment(self.alice, self.show, [seat])
        client = MagicMock()
        client.payment.refund.return_value = {"id": "rfnd_x"}
        late.mark_success("pay_second", "sig", razorpay_client=client)
        late.refresh_from_db()
        self.assertEqual(late.status, Payment.STATUS_REFUNDED)
        self.assertEqual(Booking.objects.filter(seat=seat, is_cancelled=False).count(), 1)

    def test_failed_payment_releases_the_owners_holds(self):
        seat = self.seat("A1")
        self.reserve(self.alice, [seat.id])
        payment = make_payment(self.alice, self.show, [seat])
        payment.mark_failed("card declined")
        seat.refresh_from_db()
        self.assertIsNone(seat.held_by_id)

    def test_temporary_hold_never_satisfies_review_eligibility(self):
        movie = self.show.movie
        self.reserve(self.alice, [self.seat("A1").id], now=self.at(0))
        make_payment(self.alice, self.show, [self.seat("A1")], status=Payment.STATUS_CREATED)
        with patch("django.utils.timezone.now", return_value=SHOW_START + timedelta(days=1)):
            self.assertFalse(user_has_completed_viewing(self.alice, movie))
        self.assertFalse(Booking.objects.filter(user=self.alice).exists())

    def test_completed_paid_booking_still_qualifies_for_reviews(self):
        movie = self.show.movie
        seat = self.seat("A1")
        payment = make_payment(self.alice, self.show, [seat])
        payment.mark_success("pay_rev", "sig", razorpay_client=None)
        with patch("django.utils.timezone.now", return_value=SHOW_START + timedelta(days=1)):
            self.assertTrue(user_has_completed_viewing(self.alice, movie))


def bob_id(test):
    return test.bob.id


# ---------------------------------------------------------------------------
# F. Endpoints: authentication, CSRF, malformed input, safe responses
# ---------------------------------------------------------------------------

class EndpointSecurityTests(Task2Base):
    def url(self):
        return f"/movies/theater/{self.show.id}/seats/reserve/"

    def test_anonymous_request_is_refused_and_changes_nothing(self):
        response = Client().post(self.url(), data=json.dumps({"seat_ids": [self.seat("A1").id]}),
                                 content_type="application/json")
        self.assertIn(response.status_code, (302, 401, 403))
        self.assertFalse(self.show.seats.filter(held_by__isnull=False).exists())

    def test_post_without_csrf_token_is_refused(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.alice)
        response = client.post(self.url(), data=json.dumps({"seat_ids": [self.seat("A1").id]}),
                               content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.show.seats.filter(held_by__isnull=False).exists())

    def test_malformed_json_returns_400_without_detail(self):
        client = Client()
        client.force_login(self.alice)
        response = client.post(self.url(), data="{not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Traceback", response.content.decode())

    def test_oversized_body_is_refused(self):
        client = Client()
        client.force_login(self.alice)
        response = client.post(self.url(), data=json.dumps({"seat_ids": [1] * 2000, "pad": "x" * 4000}),
                               content_type="application/json")
        self.assertEqual(response.status_code, 413)

    def test_conflict_response_is_409_with_safe_conflict_list(self):
        self.reserve(self.bob, [self.seat("A2").id])
        client = Client()
        client.force_login(self.alice)
        response = client.post(self.url(), data=json.dumps({"seat_ids": [self.seat("A2").id]}),
                               content_type="application/json")
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["conflicts"][0]["seat_number"], "A2")
        self.assertNotIn("held_by", json.dumps(body))  # other users' identities stay private

    def test_missing_show_returns_404(self):
        client = Client()
        client.force_login(self.alice)
        response = client.post("/movies/theater/999999/seats/reserve/",
                               data=json.dumps({"seat_ids": [1]}), content_type="application/json")
        self.assertEqual(response.status_code, 404)

    def test_successful_reservation_response_contains_server_expiry(self):
        client = Client()
        client.force_login(self.alice)
        response = client.post(self.url(), data=json.dumps({"seat_ids": [self.seat("A1").id]}),
                               content_type="application/json")
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["expires_in_seconds"], 120)
        self.assertEqual(body["held_until"], self.at(120).isoformat())

    def test_live_map_shows_own_expiry_but_not_others(self):
        self.reserve(self.alice, [self.seat("A1").id])
        self.reserve(self.bob, [self.seat("A2").id])
        states = {s["seat_number"]: s for s in reservations.seat_states_for(self.alice, self.show)}
        self.assertEqual(states["A1"]["status"], "held_by_you")
        self.assertIsNotNone(states["A1"]["held_until"])
        self.assertEqual(states["A2"]["status"], "held")
        self.assertIsNone(states["A2"]["held_until"])


class SeatMapAccessibilityTests(Task2Base):
    def test_seat_buttons_name_their_state_and_disable_unavailable_seats(self):
        self.reserve(self.alice, [self.seat("A1").id])
        self.reserve(self.bob, [self.seat("A2").id])
        payment = make_payment(self.bob, self.show, [self.seat("A3")])
        payment.mark_success("pay_a11y", "sig", razorpay_client=None)
        client = Client()
        client.force_login(self.alice)
        body = client.get(f"/movies/theater/{self.show.id}/seats/").content.decode()
        self.assertIn('aria-label="Seat A1, selected by you"', body)
        self.assertIn('aria-pressed="true"', body)
        self.assertIn('aria-label="Seat A2, held by another customer"', body)
        self.assertIn('aria-label="Seat A3, booked"', body)
        # Other people's holds and booked seats cannot be pressed.
        def opening_tag(label):
            start = body.index(f'aria-label="Seat {label},')
            end = body.index("</button>", start)
            return body[start:end]

        self.assertIn("disabled", opening_tag("A2"))
        self.assertIn("disabled", opening_tag("A3"))
        self.assertNotIn("disabled", opening_tag("A1"))


class TwoPhaseRefundTests(Task2Base):
    def test_refund_call_runs_outside_any_database_transaction(self):
        from django.db import connection
        seat = self.seat("A1")
        self.reserve(self.bob, [seat.id])  # bob holds it, so alice's payment must be refunded
        payment = make_payment(self.alice, self.show, [seat])
        # TestCase wraps each test in a transaction, so in_atomic_block is always True here. What matters
        # is whether the refund runs inside a transaction opened by mark_success itself. Each atomic
        # block nested inside another adds a savepoint, so compare the savepoint depth at the call with
        # the depth at the moment the provider is called.
        depth_at_call = len(connection.savepoint_ids)
        depths_at_refund = []

        def refund(payment_id, body):
            depths_at_refund.append(len(connection.savepoint_ids))
            return {"id": "rfnd_outside"}

        client = MagicMock()
        client.payment.refund.side_effect = refund
        payment.mark_success("pay_out", "sig", razorpay_client=client)
        self.assertEqual(depths_at_refund, [depth_at_call],
                         "the refund was issued inside a transaction opened by settlement")
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.STATUS_REFUNDED)
        self.assertEqual(payment.refund_id, "rfnd_outside")

    def test_crash_between_decision_and_refund_leaves_a_recoverable_payment(self):
        from io import StringIO
        from django.core.management import call_command
        seat = self.seat("A2")
        self.reserve(self.bob, [seat.id])
        payment = make_payment(self.alice, self.show, [seat])
        with patch.object(Payment, "finish_refund", side_effect=RuntimeError("worker died")):
            with self.assertRaises(RuntimeError):
                payment.mark_success("pay_crash", "sig", razorpay_client=None)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.STATUS_REFUND_PENDING)
        self.assertFalse(Booking.objects.filter(payment=payment).exists())

        client = MagicMock()
        client.payment.refund.return_value = {"id": "rfnd_recovered"}
        with patch("movies.views.get_razorpay_client", return_value=client):
            call_command("process_pending_refunds", stdout=StringIO())
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.STATUS_REFUNDED)
        self.assertEqual(client.payment.refund.call_count, 1)

    def test_a_refund_is_issued_only_once_even_if_finished_twice(self):
        seat = self.seat("A3")
        self.reserve(self.bob, [seat.id])
        payment = make_payment(self.alice, self.show, [seat])
        with patch.object(Payment, "finish_refund", side_effect=RuntimeError("worker died")):
            with self.assertRaises(RuntimeError):
                payment.mark_success("pay_twice", "sig", razorpay_client=None)
        client = MagicMock()
        client.payment.refund.return_value = {"id": "rfnd_once"}
        payment = Payment.objects.get(pk=payment.pk)
        payment.finish_refund(client)
        payment.finish_refund(client)
        payment.mark_success("pay_twice", "sig", razorpay_client=client)
        self.assertEqual(client.payment.refund.call_count, 1)

    def test_failure_callback_never_overwrites_a_pending_refund(self):
        seat = self.seat("A4")
        self.reserve(self.bob, [seat.id])
        payment = make_payment(self.alice, self.show, [seat])
        with patch.object(Payment, "finish_refund", side_effect=RuntimeError("worker died")):
            with self.assertRaises(RuntimeError):
                payment.mark_success("pay_keep", "sig", razorpay_client=None)
        Payment.objects.get(pk=payment.pk).mark_failed("late failure message")
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.STATUS_REFUND_PENDING)
