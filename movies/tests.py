import hashlib
import csv
import hmac
import json
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import MagicMock, patch

import razorpay
from django.contrib.auth.models import User, Permission
from django.core import mail
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.db.models import Sum
from django.utils import timezone

from .models import Booking, Genre, Language, Movie, Payment, Review, Seat, Theater
from .queueing import queue_ticket_email
from .tasks import send_booking_ticket_email
from .ticket_service import generate_ticket_pdf, is_valid_verify_token, make_verify_token


PASSWORD = "pw-Strong-123"
WEBHOOK_SECRET = "whsec_test_123"


def fake_razorpay(refund_id="rfnd_test_1"):
    """Stand-in for the Razorpay SDK client."""
    client = MagicMock()
    client.payment.refund.return_value = {"id": refund_id}
    return client
def fake_razorpay_captured(order_id, amount_rupees, refund_id="rfnd_test_1"):
    """Fake client whose payment.fetch returns a captured record matching the order."""
    client = fake_razorpay(refund_id)
    client.payment.fetch.return_value = {
        "id": "pay_wh",
        "order_id": order_id,
        "status": "captured",
        "currency": "INR",
        "amount": int(Decimal(amount_rupees) * 100),
    }
    return client

class BaseBookingTestCase(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", PASSWORD)
        self.bob = User.objects.create_user("bob", "bob@example.com", PASSWORD)
        self.movie = Movie.objects.create(name="Test Movie", image="movies/x.jpg")
        self.future_theater = self._theater(days=1)
        self.seat_a1 = Seat.objects.create(theater=self.future_theater, seat_number="A1")
        self.seat_a2 = Seat.objects.create(theater=self.future_theater, seat_number="A2")

    def _theater(self, days, price="200"):
        return Theater.objects.create(
            name="PVR", city="Delhi", screen="Screen 1", movie=self.movie,
            time=timezone.now() + timedelta(days=days),
            price_per_seat=Decimal(price),
        )

    def _payment(self, user, order_id, seats, theater=None):
        theater = theater or self.future_theater
        payment = Payment.objects.create(
            user=user, theater=theater,
            amount=theater.price_per_seat * len(seats),
            razorpay_order_id=order_id,
        )
        payment.seats.set(seats)
        return payment

    def _paid_booking(self, user, seat, order_id):
        payment = self._payment(user, order_id, [seat])
        payment.mark_success(f"pay_{order_id}", "sig", razorpay_client=fake_razorpay())
        return Booking.objects.get(seat=seat, is_cancelled=False)


# ---------------------------------------------------------------------------
# Task 2 + Task 3: seat holds, settlement, refunds, duplicates
# ---------------------------------------------------------------------------

class PaymentSettlementTests(BaseBookingTestCase):
    def test_success_books_every_seat_once(self):
        payment = self._payment(self.alice, "order_ok", [self.seat_a1, self.seat_a2])
        payment.mark_success("pay_ok", "sig", razorpay_client=fake_razorpay())
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.STATUS_SUCCESS)
        self.assertEqual(Booking.objects.filter(payment=payment).count(), 2)
        self.assertEqual(Seat.objects.filter(is_booked=True).count(), 2)

    def test_duplicate_confirmation_never_duplicates_bookings(self):
        payment = self._payment(self.alice, "order_dup", [self.seat_a1])
        client = fake_razorpay()
        payment.mark_success("pay_dup", "sig", razorpay_client=client)
        payment.mark_success("pay_dup", "sig", razorpay_client=client)
        self.assertEqual(Booking.objects.filter(seat=self.seat_a1).count(), 1)
        client.payment.refund.assert_not_called()

    def test_late_payment_for_seat_taken_by_another_user_is_refunded(self):
        # Alice's hold expires, Bob takes the seat, then Alice's payment captures late.
        payment = self._payment(self.alice, "order_late", [self.seat_a1])
        self.seat_a1.held_by = self.bob
        self.seat_a1.held_until = timezone.now() + timedelta(minutes=2)
        self.seat_a1.save()

        client = fake_razorpay("rfnd_late")
        payment.mark_success("pay_late", "sig", razorpay_client=client)
        payment.refresh_from_db()

        self.assertEqual(payment.status, Payment.STATUS_REFUNDED)
        self.assertEqual(payment.refund_id, "rfnd_late")
        self.assertFalse(Booking.objects.filter(payment=payment).exists())
        self.seat_a1.refresh_from_db()
        self.assertEqual(self.seat_a1.held_by, self.bob)  # Bob's hold is untouched
        client.payment.refund.assert_called_once()

    def test_second_paid_order_for_booked_seat_is_refunded_not_kept(self):
        self._paid_booking(self.alice, self.seat_a1, "order_first")
        second = self._payment(self.bob, "order_second", [self.seat_a1])
        second.mark_success("pay_second", "sig", razorpay_client=fake_razorpay("rfnd_2"))
        second.refresh_from_db()
        self.assertEqual(second.status, Payment.STATUS_REFUNDED)
        self.assertEqual(Booking.objects.filter(seat=self.seat_a1, is_cancelled=False).count(), 1)

    def test_refund_failure_marks_payment_failed_for_manual_handling(self):
        payment = self._payment(self.alice, "order_rf", [self.seat_a1])
        self.seat_a1.is_booked = True
        self.seat_a1.save()
        client = fake_razorpay()
        client.payment.refund.side_effect = Exception("gateway down")
        payment.mark_success("pay_rf", "sig", razorpay_client=client)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.STATUS_FAILED)
        self.assertIn("manual refund", payment.failure_reason)

    def test_failed_payment_releases_its_own_hold(self):
        self.seat_a1.held_by = self.alice
        self.seat_a1.held_until = timezone.now() + timedelta(minutes=2)
        self.seat_a1.save()
        payment = self._payment(self.alice, "order_fail", [self.seat_a1])
        payment.mark_failed("card declined")
        self.seat_a1.refresh_from_db()
        self.assertIsNone(self.seat_a1.held_by)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.STATUS_FAILED)

    def test_failed_payment_never_releases_another_users_hold(self):
        # Alice's old hold expired and Bob now holds the seat.
        self.seat_a1.held_by = self.bob
        self.seat_a1.held_until = timezone.now() + timedelta(minutes=2)
        self.seat_a1.save()
        payment = self._payment(self.alice, "order_keep", [self.seat_a1])
        payment.mark_failed("timeout")
        self.seat_a1.refresh_from_db()
        self.assertEqual(self.seat_a1.held_by, self.bob)

    def test_cancelled_seat_can_be_sold_again(self):
        # Regression: Booking.seat used to be a OneToOneField, so one cancelled
        # booking blocked the seat forever (IntegrityError on the next sale).
        booking = self._paid_booking(self.alice, self.seat_a1, "order_resell")
        booking.cancel_and_refund(fake_razorpay("rfnd_resell"))
        self.seat_a1.refresh_from_db()
        self.assertFalse(self.seat_a1.is_booked)

        second = self._payment(self.bob, "order_resell_2", [self.seat_a1])
        second.mark_success("pay_resell_2", "sig", razorpay_client=fake_razorpay())
        second.refresh_from_db()
        self.assertEqual(second.status, Payment.STATUS_SUCCESS)
        self.assertEqual(Booking.objects.filter(seat=self.seat_a1, is_cancelled=False).count(), 1)


class HoldAndCreatePaymentTests(BaseBookingTestCase):
    def hold(self, user, seat):
        self.client.force_login(user)
        return self.client.post(reverse("hold_seat", kwargs={
            "theater_id": self.future_theater.id, "seat_id": seat.id}))

    def test_second_user_cannot_take_an_active_hold(self):
        self.assertEqual(self.hold(self.alice, self.seat_a1).status_code, 200)
        self.assertEqual(self.hold(self.bob, self.seat_a1).status_code, 409)

    def test_cannot_hold_seats_for_a_show_that_already_started(self):
        past = self._theater(days=-1)
        seat = Seat.objects.create(theater=past, seat_number="Z9")
        self.client.force_login(self.alice)
        response = self.client.post(reverse("hold_seat", kwargs={
            "theater_id": past.id, "seat_id": seat.id}))
        self.assertEqual(response.status_code, 400)

    @patch("movies.views.get_razorpay_client")
    def test_repeated_checkout_reuses_the_live_order(self, get_client):
        get_client.return_value = MagicMock()
        get_client.return_value.order.create.side_effect = [
            {"id": "order_one"}, {"id": "order_two"}]
        self.hold(self.alice, self.seat_a1)
        url = reverse("create_payment", kwargs={"theater_id": self.future_theater.id})
        first = self.client.post(url).json()
        second = self.client.post(url).json()
        # Only one provider order is created; the second checkout returns the same one.
        self.assertEqual(get_client.return_value.order.create.call_count, 1)
        self.assertEqual(first["order_id"], "order_one")
        self.assertEqual(second["order_id"], "order_one")
        self.assertEqual(Payment.objects.filter(user=self.alice, status=Payment.STATUS_CREATED).count(), 1)
    
    @patch("movies.views.get_razorpay_client")
    def test_razorpay_outage_returns_503_not_500(self, get_client):
        get_client.return_value = MagicMock()
        get_client.return_value.order.create.side_effect = Exception("down")
        self.hold(self.alice, self.seat_a1)
        with self.assertLogs("movies.views", level="ERROR"):
            response = self.client.post(reverse(
                "create_payment", kwargs={"theater_id": self.future_theater.id}))
        self.assertEqual(response.status_code, 503)


# ---------------------------------------------------------------------------
# Task 3: webhook and browser verification
# ---------------------------------------------------------------------------

@override_settings(RAZORPAY_WEBHOOK_SECRET=WEBHOOK_SECRET)
class WebhookTests(BaseBookingTestCase):
    def _post(self, event, order_id, payment_id="pay_wh", secret=WEBHOOK_SECRET):
        body = json.dumps({"event": event, "payload": {"payment": {"entity": {
            "order_id": order_id, "id": payment_id, "error_description": "boom"}}}}).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return Client().post(reverse("razorpay_webhook"), data=body,
                             content_type="application/json",
                             HTTP_X_RAZORPAY_SIGNATURE=signature)

    @override_settings(RAZORPAY_WEBHOOK_SECRET="")
    def test_refuses_everything_when_secret_not_configured(self):
        payment = self._payment(self.alice, "order_nosecret", [self.seat_a1])
        # Signed with an empty key, which is exactly what an attacker would do.
        body = b'{"event":"payment.captured"}'
        forged = hmac.new(b"", body, hashlib.sha256).hexdigest()
        with self.assertLogs("movies.views", level="ERROR"):
            response = Client().post(reverse("razorpay_webhook"), data=body,
                                     content_type="application/json",
                                     HTTP_X_RAZORPAY_SIGNATURE=forged)
        self.assertEqual(response.status_code, 503)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.STATUS_CREATED)
        self.assertFalse(Booking.objects.exists())

    def test_rejects_bad_signature(self):
        self._payment(self.alice, "order_badsig", [self.seat_a1])
        response = self._post("payment.captured", "order_badsig", secret="wrong")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Booking.objects.exists())

    def test_valid_captured_event_books_and_is_idempotent(self):
        payment = self._payment(self.alice, "order_wh", [self.seat_a1])
        with patch("movies.views.get_razorpay_client",
                   return_value=fake_razorpay_captured("order_wh", payment.amount)), \
                self.captureOnCommitCallbacks(execute=False):
            self.assertEqual(self._post("payment.captured", "order_wh").status_code, 200)
            self.assertEqual(self._post("payment.captured", "order_wh").status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.STATUS_SUCCESS)
        self.assertEqual(Booking.objects.filter(payment=payment).count(), 1)

    def test_failed_event_releases_seats(self):
        self.seat_a1.held_by = self.alice
        self.seat_a1.held_until = timezone.now() + timedelta(minutes=2)
        self.seat_a1.save()
        payment = self._payment(self.alice, "order_wh_fail", [self.seat_a1])
        self.assertEqual(self._post("payment.failed", "order_wh_fail").status_code, 200)
        payment.refresh_from_db()
        self.seat_a1.refresh_from_db()
        self.assertEqual(payment.status, Payment.STATUS_FAILED)
        self.assertIsNone(self.seat_a1.held_by)

    def test_malformed_json_with_valid_signature_is_400(self):
        body = b"not json"
        sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        response = Client().post(reverse("razorpay_webhook"), data=body,
                                 content_type="application/json",
                                 HTTP_X_RAZORPAY_SIGNATURE=sig)
        self.assertEqual(response.status_code, 400)


class VerifyPaymentTests(BaseBookingTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.alice)
        self.payment = self._payment(self.alice, "order_v", [self.seat_a1])

    def _verify(self):
        return self.client.post(reverse("verify_payment"), {
            "razorpay_order_id": "order_v",
            "razorpay_payment_id": "pay_v",
            "razorpay_signature": "sig",
        })

    @patch("movies.views.get_razorpay_client")
    def test_success_redirects_to_profile_and_queues_email(self, get_client):
        get_client.return_value = fake_razorpay()
        with patch("movies.queueing.transaction.on_commit", side_effect=lambda fn: fn()), \
                patch.object(send_booking_ticket_email, "apply_async") as apply:
            response = self._verify()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redirect_url"], "/profile/")
        apply.assert_called_once_with(args=[self.payment.id], retry=False)

    @patch("movies.views.get_razorpay_client")
    def test_repeated_callback_is_idempotent(self, get_client):
        get_client.return_value = fake_razorpay()
        self._verify()
        response = self._verify()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Booking.objects.filter(payment=self.payment).count(), 1)

    @patch("movies.views.get_razorpay_client")
    def test_broker_outage_does_not_break_a_confirmed_booking(self, get_client):
        # Regression: .delay() raised after ~19s and turned a paid booking into a 500.
        get_client.return_value = fake_razorpay()
        with patch.object(send_booking_ticket_email, "apply_async",
                          side_effect=ConnectionError("redis down")) as apply, \
                self.assertLogs("movies.queueing", level="ERROR"), \
                self.captureOnCommitCallbacks(execute=True):
            response = self._verify()
        self.assertEqual(response.status_code, 200)
        apply.assert_called_once()
        self.assertTrue(Booking.objects.filter(payment=self.payment).exists())

    @patch("movies.views.get_razorpay_client")
    def test_signature_failure_marks_payment_failed(self, get_client):
        client = fake_razorpay()
        client.utility.verify_payment_signature.side_effect = \
            razorpay.errors.SignatureVerificationError("bad signature")
        get_client.return_value = client
        response = self._verify()
        self.assertEqual(response.status_code, 400)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.STATUS_FAILED)

    def test_payment_failed_endpoint_marks_failed_and_releases_seat(self):
        self.seat_a1.held_by = self.alice
        self.seat_a1.held_until = timezone.now() + timedelta(minutes=2)
        self.seat_a1.save()
        response = self.client.post(reverse("record_payment_failure"), {
            "razorpay_order_id": "order_v", "reason": "Bank declined"})
        self.assertEqual(response.status_code, 200)
        self.payment.refresh_from_db()
        self.seat_a1.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.STATUS_FAILED)
        self.assertIsNone(self.seat_a1.held_by)


# ---------------------------------------------------------------------------
# Task 1: reviews, ratings, verified badge, moderation, recommendations
# ---------------------------------------------------------------------------

class ReviewTests(BaseBookingTestCase):
    def setUp(self):
        super().setUp()
        past_theater = self._theater(days=-2)
        past_seat = Seat.objects.create(theater=past_theater, seat_number="P1")
        # Alice actually watched the film: a booking for a show that has already happened.
        # A qualifying booking is a paid one: the checkout always records a successful payment.
        payment = Payment.objects.create(
            user=self.alice, theater=past_theater, amount=past_theater.price_per_seat,
            status=Payment.STATUS_SUCCESS, razorpay_order_id="order_test_alice_past",
            razorpay_payment_id="pay_test_alice_past",
        )
        payment.seats.set([past_seat])
        Booking.objects.create(user=self.alice, seat=past_seat, movie=self.movie,
                               theater=past_theater, payment=payment)
        self.url = reverse("movie_detail", kwargs={"slug": self.movie.slug})

    def test_verified_viewer_badge_and_average_rating(self):
        Review.objects.create(movie=self.movie, user=self.alice, rating=4, body="good")
        Review.objects.create(movie=self.movie, user=self.bob, rating=2, body="meh")
        self.assertTrue(Review.objects.get(user=self.alice).is_verified_viewer)
        self.assertFalse(Review.objects.get(user=self.bob).is_verified_viewer)
        self.movie.refresh_from_db()
        self.assertEqual(self.movie.average_rating, Decimal("3.0"))
        self.assertEqual(self.movie.total_ratings, 2)

    def test_user_who_never_watched_cannot_review(self):
        self.client.force_login(self.bob)
        self.client.post(self.url, {"submit_review": "1", "rating": 5,
                                    "title": "t", "body": "never watched"})
        self.assertFalse(Review.objects.filter(user=self.bob).exists())

    def test_watched_user_can_submit_then_edit_one_review(self):
        self.client.force_login(self.alice)
        self.client.post(self.url, {"submit_review": "1", "rating": 3,
                                    "title": "ok", "body": "first"})
        self.client.post(self.url, {"submit_review": "1", "rating": 5,
                                    "title": "ok", "body": "edited"})
        review = Review.objects.get(user=self.alice)
        self.assertEqual(review.rating, 5)
        self.assertTrue(review.is_edited)
        self.assertEqual(Review.objects.filter(user=self.alice).count(), 1)

    def test_hidden_review_resubmit_does_not_crash_or_duplicate(self):
        # Regression: a hidden review looked like "no review", so re-posting hit IntegrityError (500).
        self.client.force_login(self.alice)
        Review.objects.create(movie=self.movie, user=self.alice, rating=4, body="x")
        Review.objects.filter(user=self.alice).update(is_hidden=True)
        response = self.client.post(self.url, {"submit_review": "1", "rating": 5,
                                               "title": "t", "body": "again"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Review.objects.filter(user=self.alice).count(), 1)
        self.assertEqual(Review.objects.get(user=self.alice).rating, 4)

    def test_duplicate_report_is_counted_once(self):
        self.client.force_login(self.alice)
        review = Review.objects.create(movie=self.movie, user=self.bob, rating=1, body="spam")
        url = reverse("report_review", kwargs={"review_id": review.id})
        self.client.post(url, {"reason": "spam", "note": ""})
        self.client.post(url, {"reason": "spam", "note": ""})
        review.refresh_from_db()
        self.assertEqual(review.report_count, 1)

    def test_similar_movies_match_genre_or_language_and_skip_unreleased(self):
        genre = Genre.objects.create(name="Thriller")
        language = Language.objects.create(name="Hindi")
        self.movie.genres.add(genre)
        self.movie.languages.add(language)
        same_genre = Movie.objects.create(name="Genre Match", image="x.jpg",
                                          release_date=date.today() - timedelta(days=5))
        same_genre.genres.add(genre)
        same_language = Movie.objects.create(name="Language Match", image="x.jpg",
                                             release_date=date.today() - timedelta(days=5))
        same_language.languages.add(language)
        future = Movie.objects.create(name="Future", image="x.jpg",
                                      release_date=date.today() + timedelta(days=30))
        future.genres.add(genre)
        names = set(self.movie.similar_movies().values_list("name", flat=True))
        self.assertIn("Genre Match", names)
        self.assertIn("Language Match", names)
        self.assertNotIn("Future", names)


# ---------------------------------------------------------------------------
# Task 2/3: cancellation and refunds
# ---------------------------------------------------------------------------

class CancellationTests(BaseBookingTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.alice)

    @patch("movies.views.get_razorpay_client")
    def test_cancel_refunds_and_frees_seat(self, get_client):
        get_client.return_value = fake_razorpay("rfnd_c1")
        booking = self._paid_booking(self.alice, self.seat_a1, "order_c1")
        self.client.post(reverse("cancel_booking", kwargs={"booking_id": booking.id}))
        booking.refresh_from_db()
        self.assertTrue(booking.is_cancelled)
        self.assertEqual(booking.refund_status, Booking.REFUND_REFUNDED)
        self.assertEqual(booking.refund_id, "rfnd_c1")
        self.seat_a1.refresh_from_db()
        self.assertFalse(self.seat_a1.is_booked)

    @patch("movies.views.get_razorpay_client")
    def test_refund_failure_keeps_booking_active(self, get_client):
        client = fake_razorpay()
        get_client.return_value = client
        booking = self._paid_booking(self.alice, self.seat_a1, "order_c2")
        client.payment.refund.side_effect = Exception("razorpay 500")
        self.client.post(reverse("cancel_booking", kwargs={"booking_id": booking.id}))
        booking.refresh_from_db()
        self.assertFalse(booking.is_cancelled)
        self.assertEqual(booking.refund_status, Booking.REFUND_FAILED)
        self.seat_a1.refresh_from_db()
        self.assertTrue(self.seat_a1.is_booked)

    @patch("movies.views.get_razorpay_client")
    def test_cannot_cancel_after_show_started(self, get_client):
        get_client.return_value = fake_razorpay()
        started = self._theater(days=-1)
        seat = Seat.objects.create(theater=started, seat_number="S1")
        booking = Booking.objects.create(user=self.alice, seat=seat,
                                         movie=self.movie, theater=started)
        self.client.post(reverse("cancel_booking", kwargs={"booking_id": booking.id}))
        booking.refresh_from_db()
        self.assertFalse(booking.is_cancelled)

    def test_profile_shows_cancel_control_for_active_booking(self):
        booking = self._paid_booking(self.alice, self.seat_a1, "order_c3")
        response = self.client.get(reverse("profile"))
        self.assertContains(response, reverse("cancel_booking", kwargs={"booking_id": booking.id}))


# ---------------------------------------------------------------------------
# Task 6: ticket PDF, signed verification links, download rules, e-mail
# ---------------------------------------------------------------------------

class TicketTests(BaseBookingTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.alice)
        self.booking = self._paid_booking(self.alice, self.seat_a1, "order_t")

    def test_pdf_is_generated(self):
        pdf = generate_ticket_pdf(self.booking.payment, [self.booking])
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_download_returns_pdf_for_active_booking(self):
        response = self.client.get(reverse("download_ticket", kwargs={"booking_id": self.booking.id}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_download_refused_for_cancelled_booking(self):
        self.booking.cancel_and_refund(fake_razorpay())
        response = self.client.get(reverse("download_ticket", kwargs={"booking_id": self.booking.id}))
        self.assertEqual(response.status_code, 404)

    def test_verify_requires_signed_token(self):
        url = reverse("verify_ticket", kwargs={"booking_id": self.booking.id})
        anonymous = Client()
        self.assertEqual(anonymous.get(url).status_code, 404)
        self.assertEqual(anonymous.get(url, {"t": "forged"}).status_code, 404)
        token = make_verify_token(self.booking.id)
        self.assertEqual(anonymous.get(url, {"t": token}).status_code, 200)

    def test_token_for_one_booking_does_not_verify_another(self):
        token = make_verify_token(self.booking.id + 999)
        self.assertFalse(is_valid_verify_token(token, self.booking.id))

    def test_email_task_sends_once_and_is_idempotent(self):
        mail.outbox = []
        payment = self.booking.payment
        send_booking_ticket_email.apply(args=[payment.id])
        send_booking_ticket_email.apply(args=[payment.id])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].attachments[0][2], "application/pdf")
        payment.refresh_from_db()
        self.assertEqual(payment.ticket_email_status, Payment.TICKET_EMAIL_SENT)

    def test_resend_command_requeues_unsent_tickets(self):
        out = StringIO()
        with patch("movies.queueing.transaction.on_commit", side_effect=lambda fn: fn()), \
                patch.object(send_booking_ticket_email, "apply_async") as apply:
            call_command("send_pending_ticket_emails", stdout=out)
        apply.assert_called_once()
        self.assertIn("Re-queued 1", out.getvalue())

    def test_queue_helper_never_raises(self):
        with patch.object(send_booking_ticket_email, "apply_async",
                          side_effect=RuntimeError("no broker")), \
                self.assertLogs("movies.queueing", level="ERROR"), \
                self.captureOnCommitCallbacks(execute=True):
            queue_ticket_email(self.booking.payment.id)  # must not raise


# ---------------------------------------------------------------------------
# Task 5: discovery and filtering
# ---------------------------------------------------------------------------

class DiscoveryTests(BaseBookingTestCase):
    def test_list_reports_match_count_and_filters_by_genre(self):
        action = Genre.objects.create(name="Action")
        drama = Genre.objects.create(name="Drama")
        Movie.objects.create(name="Explosive", image="x.jpg").genres.add(action)
        Movie.objects.create(name="Tearjerker", image="x.jpg").genres.add(drama)
        response = self.client.get(reverse("movie_list"), {"genre": action.id})
        self.assertEqual(response.context["total_count"], 1)
        self.assertEqual(response.context["page_obj"][0].name, "Explosive")

    def test_every_sort_option_works(self):
        for sort in ["popularity", "newest", "rating", "price_low", "price_high"]:
            response = self.client.get(reverse("movie_list"), {"search": "Test", "sort": sort})
            self.assertEqual(response.status_code, 200)

    def test_recently_released_excludes_future_titles(self):
        Movie.objects.create(name="Coming Soon", image="x.jpg",
                             release_date=date.today() + timedelta(days=60))
        page = self.client.get(reverse("movie_detail", kwargs={"slug": self.movie.slug}))
        names = [m.name for m in page.context["recent_movies"]]
        self.assertNotIn("Coming Soon", names)


# ---------------------------------------------------------------------------
# Task 4: admin dashboard access control and CSV export
# ---------------------------------------------------------------------------

class AdminDashboardTests(BaseBookingTestCase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user("staff", "s@example.com", PASSWORD, is_staff=True)
        from django.contrib.auth.models import Permission
        self.staff.user_permissions.add(Permission.objects.get(codename="view_analytics"))

    def test_anonymous_and_regular_users_are_blocked(self):
        self.assertEqual(Client().get(reverse("admin_dashboard")).status_code, 302)
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(reverse("admin_dashboard")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin_dashboard_export_csv")).status_code, 403)

    def test_staff_sees_dashboard_and_downloads_csv(self):
        self._paid_booking(self.alice, self.seat_a1, "order_d")
        self.client.force_login(self.staff)
        dash = self.client.get(reverse("admin_dashboard"))
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.context["active_count"], 1)
        csv_response = self.client.get(reverse("admin_dashboard_export_csv"))
        self.assertEqual(csv_response["Content-Type"], "text/csv")
        body = b"".join(csv_response.streaming_content).decode()
        self.assertIn("Booking ID", body)
        self.assertIn("alice", body)

    def test_derived_revenue_buckets_match_independent_orm_sums(self):
        """Week/month buckets are derived from day totals; verify them against direct queries."""
        extra = Seat.objects.create(theater=self.future_theater, seat_number="A3")
        for seat, days_ago in [(self.seat_a1, 3), (self.seat_a2, 20), (extra, 40)]:
            payment = self._payment(self.alice, f"order_rev_{seat.id}", [seat])
            payment.mark_success(f"pay_rev_{seat.id}", "sig", razorpay_client=fake_razorpay())
            when = timezone.make_aware(datetime.combine(
                date.today() - timedelta(days=days_ago), time(12, 0)))
            Payment.objects.filter(pk=payment.pk).update(created_at=when)

        self.client.force_login(self.staff)
        start = (date.today() - timedelta(days=60)).isoformat()
        end = date.today().isoformat()
        response = self.client.get(reverse("admin_dashboard"), {"start": start, "end": end})
        total = Payment.objects.filter(status=Payment.STATUS_SUCCESS).aggregate(
            total=Sum("amount"))["total"]
        self.assertEqual(response.context["total_revenue"], total)

        for granularity, span in [("revenue_by_week", 7), ("revenue_by_month", None)]:
            buckets = response.context[granularity]
            self.assertEqual(sum(b["revenue"] for b in buckets), total)
            for bucket in buckets:
                period = bucket["period"]
                if span:
                    upper = period + timedelta(days=span)
                else:
                    upper = (period.replace(day=28) + timedelta(days=4)).replace(day=1)
                expected = Payment.objects.filter(
                    status=Payment.STATUS_SUCCESS,
                    created_at__date__gte=period,
                    created_at__date__lt=upper,
                ).aggregate(s=Sum("amount"))["s"]
                self.assertEqual(bucket["revenue"], expected, (granularity, period))

class RevenueRulesTests(TestCase):
    """Revenue must be exact: successful payments only, refunds subtracted, Decimal throughout."""

    def setUp(self):
        self.admin = User.objects.create_superuser("rev_admin", "ra@example.com", PASSWORD)
        self.admin.user_permissions.add(Permission.objects.get(codename="view_analytics"))
        self.client.force_login(self.admin)
        movie = Movie.objects.create(
            name="Rev Movie", slug="rev-movie", description="d", image="movies/x.jpg",
            duration_minutes=120, age_certificate="UA", release_date=timezone.now().date(),
        )
        self.theater = Theater.objects.create(
            name="Rev PVR", city="Pune", movie=movie, screen="R1",
            time=timezone.now() + timedelta(days=2), price_per_seat=Decimal("200.00"),
        )
        self.buyer = User.objects.create_user("rev_buyer", "rb@example.com", PASSWORD)
        self.seat_count = 0

    def _payment(self, amount, status, order):
        return Payment.objects.create(
            user=self.buyer, theater=self.theater, amount=Decimal(amount),
            status=status, razorpay_order_id=order,
        )

    def _booking(self, payment, refund_status=Booking.REFUND_NONE, refund_amount=None):
        self.seat_count += 1
        seat = Seat.objects.create(theater=self.theater, seat_number=f"R{self.seat_count}")
        return Booking.objects.create(
            user=self.buyer, seat=seat, movie=self.theater.movie, theater=self.theater,
            payment=payment, refund_status=refund_status, refund_amount=refund_amount,
        )

    def _dashboard(self):
        response = self.client.get(reverse("admin_dashboard"))
        self.assertEqual(response.status_code, 200)
        return response.context

    def test_only_successful_payments_count_as_collected(self):
        self._payment("200.00", Payment.STATUS_SUCCESS, "o_ok")
        self._payment("500.00", Payment.STATUS_FAILED, "o_fail")
        self._payment("700.00", Payment.STATUS_CREATED, "o_pending")
        self._payment("900.00", Payment.STATUS_CANCELLED, "o_cancel")
        ctx = self._dashboard()
        self.assertEqual(ctx["total_revenue"], Decimal("200.00"))

    def test_full_refund_reduces_net_revenue(self):
        p = self._payment("200.00", Payment.STATUS_SUCCESS, "o_full")
        self._booking(p, Booking.REFUND_REFUNDED, Decimal("200.00"))
        ctx = self._dashboard()
        self.assertEqual(ctx["total_revenue"], Decimal("200.00"))
        self.assertEqual(ctx["refunded_amount"], Decimal("200.00"))
        self.assertEqual(ctx["net_revenue"], Decimal("0.00"))

    def test_partial_refund_reduces_net_revenue_by_exact_amount(self):
        p = self._payment("400.00", Payment.STATUS_SUCCESS, "o_part")
        self._booking(p, Booking.REFUND_REFUNDED, Decimal("200.00"))
        self._booking(p, Booking.REFUND_NONE)
        ctx = self._dashboard()
        self.assertEqual(ctx["total_revenue"], Decimal("400.00"))
        self.assertEqual(ctx["net_revenue"], Decimal("200.00"))

    def test_failed_or_processing_refund_is_not_subtracted(self):
        p = self._payment("300.00", Payment.STATUS_SUCCESS, "o_fail_ref")
        self._booking(p, Booking.REFUND_FAILED, Decimal("150.00"))
        self._booking(p, Booking.REFUND_PROCESSING, Decimal("50.00"))
        ctx = self._dashboard()
        self.assertEqual(ctx["refunded_amount"], Decimal("0"))
        self.assertEqual(ctx["net_revenue"], Decimal("300.00"))

    def test_multiple_payments_sum_exactly_without_float_error(self):
        for i, amount in enumerate(["0.10", "0.20", "0.30"]):
            self._payment(amount, Payment.STATUS_SUCCESS, f"o_sum_{i}")
        ctx = self._dashboard()
        self.assertEqual(ctx["total_revenue"], Decimal("0.60"))
        self.assertIsInstance(ctx["total_revenue"], Decimal)

    def test_no_payments_gives_zero_not_none(self):
        ctx = self._dashboard()
        self.assertEqual(ctx["total_revenue"], Decimal("0"))
        self.assertEqual(ctx["net_revenue"], Decimal("0"))
        
        
class CsvInjectionTests(TestCase):
    def test_formula_characters_are_neutralised(self):
        from movies.views import _csv_safe
        self.assertEqual(_csv_safe("=HYPERLINK(\"http://x\")"), "'=HYPERLINK(\"http://x\")")
        self.assertEqual(_csv_safe("+cmd"), "'+cmd")
        self.assertEqual(_csv_safe("-2+3"), "'-2+3")
        self.assertEqual(_csv_safe("@SUM(A1)"), "'@SUM(A1)")

    def test_legitimate_text_and_numbers_are_unchanged(self):
        from movies.views import _csv_safe
        self.assertEqual(_csv_safe("Avengers"), "Avengers")
        self.assertEqual(_csv_safe(Decimal("-200.00")), Decimal("-200.00"))
        self.assertEqual(_csv_safe(42), 42)
        self.assertEqual(_csv_safe(None), None)
        self.assertEqual(_csv_safe(True), True)

    def test_export_contains_no_unsafe_cell_from_user_data(self):
        admin = User.objects.create_superuser("csv_admin", "c@example.com", PASSWORD)
        admin.user_permissions.add(Permission.objects.get(codename="view_analytics"))
        victim = User.objects.create_user("=evil_user", "e@example.com", PASSWORD)
        movie = Movie.objects.create(
            name="+Movie", slug="csv-movie", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
        )
        theater = Theater.objects.create(
            name="PVR", city="Pune", movie=movie, screen="S1",
            time=timezone.now() + timedelta(days=2), price_per_seat=Decimal("200.00"),
        )
        seat = Seat.objects.create(theater=theater, seat_number="A1")
        Booking.objects.create(user=victim, seat=seat, movie=movie, theater=theater)
        self.client.force_login(admin)
        response = self.client.get(reverse("admin_dashboard_export_csv"))
        body = b"".join(response.streaming_content).decode("utf-8")
        data_lines = body.strip().splitlines()[1:]
        self.assertTrue(data_lines)
        for line in data_lines:
            cells = next(csv.reader([line]))
            self.assertFalse(cells[1].startswith(("=", "+", "-", "@")), cells[1])
            self.assertFalse(cells[2].startswith(("=", "+", "-", "@")), cells[2])
            
    def test_csv_movie_filter_excludes_other_movies(self):
        admin = User.objects.create_superuser("csv_flt", "cf@example.com", PASSWORD)
        admin.user_permissions.add(Permission.objects.get(codename="view_analytics"))
        buyer = User.objects.create_user("csv_flt_buyer", "cfb@example.com", PASSWORD)
        movie_a = Movie.objects.create(
            name="CSV Alpha", slug="csv-alpha", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
        )
        movie_b = Movie.objects.create(
            name="CSV Beta", slug="csv-beta", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
        )
        show_a = Theater.objects.create(
            name="CSV PVR", city="Pune", movie=movie_a, screen="A",
            time=timezone.now() + timedelta(days=1), price_per_seat=Decimal("200.00"),
        )
        show_b = Theater.objects.create(
            name="CSV PVR", city="Pune", movie=movie_b, screen="B",
            time=timezone.now() + timedelta(days=1), price_per_seat=Decimal("200.00"),
        )
        Booking.objects.create(
            user=buyer, seat=Seat.objects.create(theater=show_a, seat_number="A1"),
            movie=movie_a, theater=show_a,
        )
        Booking.objects.create(
            user=buyer, seat=Seat.objects.create(theater=show_b, seat_number="B1"),
            movie=movie_b, theater=show_b,
        )
        self.client.force_login(admin)
        response = self.client.get(reverse("admin_dashboard_export_csv"), {
            "start": (timezone.now() - timedelta(days=1)).date().isoformat(),
            "end": (timezone.now() + timedelta(days=10)).date().isoformat(),
            "movie": movie_a.id,
        })
        body = b"".join(response.streaming_content).decode("utf-8")
        self.assertIn("CSV Alpha", body)
        self.assertNotIn("CSV Beta", body)
            
            
class OccupancyRulesTests(TestCase):
    """Weighted occupancy = occupied seats / capacity across shows, cancelled seats excluded."""

    def setUp(self):
        self.admin = User.objects.create_superuser("occ_admin", "oa@example.com", PASSWORD)
        self.admin.user_permissions.add(Permission.objects.get(codename="view_analytics"))
        self.client.force_login(self.admin)
        self.movie = Movie.objects.create(
            name="Occ Movie", slug="occ-movie", description="d", image="movies/x.jpg",
            duration_minutes=120, age_certificate="UA", release_date=timezone.now().date(),
        )
        self.buyer = User.objects.create_user("occ_buyer", "ob@example.com", PASSWORD)
        self.now = timezone.now()

    def _dashboard_context(self):
        start = (self.now - timedelta(days=1)).date().isoformat()
        end = (self.now + timedelta(days=10)).date().isoformat()
        response = self.client.get(reverse("admin_dashboard"), {"start": start, "end": end})
        self.assertEqual(response.status_code, 200)
        return response.context

    def _show(self, screen, when, seat_count):
        theater = Theater.objects.create(
            name="Occ PVR", city="Pune", movie=self.movie, screen=screen,
            time=when, price_per_seat=Decimal("200.00"),
        )
        seats = [Seat.objects.create(theater=theater, seat_number=f"S{i}") for i in range(seat_count)]
        return theater, seats

    def _book(self, theater, seat, cancelled=False):
        return Booking.objects.create(
            user=self.buyer, seat=seat, movie=self.movie, theater=theater, is_cancelled=cancelled,
        )

    def test_weighted_occupancy_across_two_shows_of_different_sizes(self):
        # Show A: 10 seats, 5 booked. Show B: 20 seats, 5 booked.
        # Weighted = (5 + 5) / (10 + 20) = 33.3%. An average of percentages would give 37.5%.
        a, a_seats = self._show("A", self.now + timedelta(days=1), 10)
        b, b_seats = self._show("B", self.now + timedelta(days=2), 20)
        for seat in a_seats[:5]:
            self._book(a, seat)
        for seat in b_seats[:5]:
            self._book(b, seat)
        ctx = self._dashboard_context()
        row = next(t for t in ctx["occupancy_by_theater"] if t["name"] == "Occ PVR")
        self.assertEqual(row["capacity"], 30)
        self.assertEqual(row["occupied"], 10)
        self.assertEqual(row["weighted_pct"], 33.3)

    def test_cancelled_booking_does_not_count_as_occupied(self):
        theater, seats = self._show("C", self.now + timedelta(days=1), 10)
        self._book(theater, seats[0])
        self._book(theater, seats[1], cancelled=True)
        ctx = self._dashboard_context()
        row = next(t for t in ctx["occupancy_by_theater"] if t["name"] == "Occ PVR")
        self.assertEqual(row["occupied"], 1)
        self.assertEqual(row["weighted_pct"], 10.0)

    def test_theater_with_no_seats_does_not_divide_by_zero(self):
        Theater.objects.create(
            name="Empty PVR", city="Pune", movie=self.movie, screen="E",
            time=self.now + timedelta(days=1), price_per_seat=Decimal("200.00"),
        )
        ctx = self._dashboard_context()
        self.assertFalse(any(t["name"] == "Empty PVR" for t in ctx["occupancy_by_theater"]))
        
        

class MovieRankingRulesTests(TestCase):
    """Rankings count real confirmed bookings and seats, and revenue is not inflated by joins."""

    def setUp(self):
        self.admin = User.objects.create_superuser("rank_admin", "ra2@example.com", PASSWORD)
        self.admin.user_permissions.add(Permission.objects.get(codename="view_analytics"))
        self.client.force_login(self.admin)
        self.buyer = User.objects.create_user("rank_buyer", "rb2@example.com", PASSWORD)
        self.now = timezone.now()
        self.popular = self._movie("Popular", "popular")
        self.quiet = self._movie("Quiet", "quiet")

    def _movie(self, name, slug):
        return Movie.objects.create(
            name=name, slug=slug, description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
        )

    def _show(self, movie, screen):
        return Theater.objects.create(
            name="Rank PVR", city="Pune", movie=movie, screen=screen,
            time=self.now + timedelta(days=1), price_per_seat=Decimal("200.00"),
        )

    def _book(self, theater, seat_number, payment=None, cancelled=False):
        seat, _ = Seat.objects.get_or_create(theater=theater, seat_number=seat_number)
        return Booking.objects.create(
            user=self.buyer, seat=seat, movie=theater.movie, theater=theater,
            payment=payment, is_cancelled=cancelled,
        )

    def _paid(self, theater, amount, order):
        return Payment.objects.create(
            user=self.buyer, theater=theater, amount=Decimal(amount),
            status=Payment.STATUS_SUCCESS, razorpay_order_id=order,
        )

    def _ranking(self):
        ctx = self.client.get(reverse("admin_dashboard"), {
            "start": (self.now - timedelta(days=1)).date().isoformat(),
            "end": (self.now + timedelta(days=10)).date().isoformat(),
        }).context
        return {m["name"]: m for m in ctx["top_movies"]}

    def test_popular_movie_ranks_above_quiet_one(self):
        show_p = self._show(self.popular, "P1")
        show_q = self._show(self.quiet, "Q1")
        self._book(show_p, "A1")
        self._book(show_p, "A2")
        self._book(show_q, "A1")
        rank = self._ranking()
        self.assertEqual(rank["Popular"]["bookings"], 2)
        self.assertEqual(rank["Quiet"]["bookings"], 1)

    def test_cancelled_bookings_are_excluded_from_rankings(self):
        show = self._show(self.popular, "P2")
        self._book(show, "B1")
        self._book(show, "B2", cancelled=True)
        rank = self._ranking()
        self.assertEqual(rank["Popular"]["bookings"], 1)
        self.assertEqual(rank["Popular"]["seats"], 1)

    def test_revenue_is_not_multiplied_by_booking_count(self):
        show = self._show(self.popular, "P3")
        payment = self._paid(show, "400.00", "order_rank_1")
        self._book(show, "C1", payment=payment)
        self._book(show, "C2", payment=payment)
        rank = self._ranking()
        self.assertEqual(rank["Popular"]["bookings"], 2)
        self.assertEqual(rank["Popular"]["revenue"], Decimal("400.00"))

    def test_failed_payments_add_no_revenue(self):
        show = self._show(self.popular, "P4")
        Payment.objects.create(
            user=self.buyer, theater=show, amount=Decimal("999.00"),
            status=Payment.STATUS_FAILED, razorpay_order_id="order_rank_fail",
        )
        self._book(show, "D1")
        rank = self._ranking()
        self.assertEqual(rank["Popular"]["revenue"], Decimal("0"))

    def test_review_count_and_rating_come_from_task1_fields(self):
        Movie.objects.filter(pk=self.popular.pk).update(average_rating=Decimal("4.5"), total_ratings=7)
        show = self._show(self.popular, "P5")
        self._book(show, "E1")
        rank = self._ranking()
        self.assertEqual(rank["Popular"]["average_rating"], Decimal("4.5"))
        self.assertEqual(rank["Popular"]["review_count"], 7)
        
        
        

class CancellationRefundRulesTests(TestCase):
    """Cancelled and refunded counts come from booking records; pending and failed are reported as 0."""

    def setUp(self):
        self.admin = User.objects.create_superuser("cx_admin", "cx@example.com", PASSWORD)
        self.admin.user_permissions.add(Permission.objects.get(codename="view_analytics"))
        self.client.force_login(self.admin)
        self.buyer = User.objects.create_user("cx_buyer", "cx2@example.com", PASSWORD)
        self.now = timezone.now()
        self.movie = Movie.objects.create(
            name="Cx Movie", slug="cx-movie", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
        )
        self.show = Theater.objects.create(
            name="Cx PVR", city="Pune", movie=self.movie, screen="C1",
            time=self.now + timedelta(days=1), price_per_seat=Decimal("200.00"),
        )
        self._n = 0

    def _payment(self, amount, order):
        return Payment.objects.create(
            user=self.buyer, theater=self.show, amount=Decimal(amount),
            status=Payment.STATUS_SUCCESS, razorpay_order_id=order,
        )

    def _booking(self, payment=None, **fields):
        self._n += 1
        seat = Seat.objects.create(theater=self.show, seat_number=f"X{self._n}")
        return Booking.objects.create(
            user=self.buyer, seat=seat, movie=self.movie, theater=self.show,
            payment=payment, **fields,
        )

    def _stats(self):
        ctx = self.client.get(reverse("admin_dashboard"), {
            "start": (self.now - timedelta(days=1)).date().isoformat(),
            "end": (self.now + timedelta(days=10)).date().isoformat(),
        }).context
        return ctx["cancellation_stats"]

    def test_cancelled_and_refunded_counts_are_separate(self):
        p = self._payment("200.00", "o_cx_1")
        self._booking(payment=p, is_cancelled=True, cancelled_at=self.now,
                      refund_status=Booking.REFUND_REFUNDED, refund_amount=Decimal("200.00"))
        self._booking(is_cancelled=True, cancelled_at=self.now)  # cancelled, not refunded
        stats = self._stats()
        self.assertEqual(stats["cancelled_bookings"], 2)
        self.assertEqual(stats["refunded_bookings"], 1)
        self.assertEqual(stats["refunded_amount"], Decimal("200.00"))

    def test_pending_and_failed_refunds_are_reported_as_zero(self):
        self._booking()
        stats = self._stats()
        self.assertEqual(stats["processing_refunds"], 0)
        self.assertEqual(stats["failed_refunds"], 0)
        self.assertIn("not recorded", stats["refund_status_note"])

    def test_full_and_partial_refunds_are_classified(self):
        p = self._payment("400.00", "o_cx_2")
        self._booking(payment=p, is_cancelled=True, cancelled_at=self.now,
                      refund_status=Booking.REFUND_REFUNDED, refund_amount=Decimal("200.00"))
        self._booking(payment=p, is_cancelled=True, cancelled_at=self.now,
                      refund_status=Booking.REFUND_REFUNDED, refund_amount=Decimal("150.00"))
        stats = self._stats()
        self.assertEqual(stats["full_refunds"], 1)
        self.assertEqual(stats["partial_refunds"], 1)

    def test_no_refunds_gives_zero_amount_not_none(self):
        stats = self._stats()
        self.assertEqual(stats["refunded_amount"], Decimal("0"))
        
        

class FilterRulesTests(TestCase):
    """Movie and theater filters include only matching data and never leak other rows."""

    def setUp(self):
        self.admin = User.objects.create_superuser("flt_admin", "fa@example.com", PASSWORD)
        self.admin.user_permissions.add(Permission.objects.get(codename="view_analytics"))
        self.client.force_login(self.admin)
        self.buyer = User.objects.create_user("flt_buyer", "fb@example.com", PASSWORD)
        self.now = timezone.now()
        self.movie_a = Movie.objects.create(
            name="Filter A", slug="filter-a", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
        )
        self.movie_b = Movie.objects.create(
            name="Filter B", slug="filter-b", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
        )
        self.venue_1 = Theater.objects.create(
            name="Venue One", city="Pune", movie=self.movie_a, screen="V1",
            time=self.now + timedelta(days=1), price_per_seat=Decimal("200.00"),
        )
        self.venue_2 = Theater.objects.create(
            name="Venue Two", city="Pune", movie=self.movie_b, screen="V2",
            time=self.now + timedelta(days=1), price_per_seat=Decimal("200.00"),
        )
        self._n = 0

    def _book(self, theater, movie):
        self._n += 1
        seat = Seat.objects.create(theater=theater, seat_number=f"F{self._n}")
        return Booking.objects.create(
            user=self.buyer, seat=seat, movie=movie, theater=theater,
        )

    def _context(self, **params):
        query = {
            "start": (self.now - timedelta(days=1)).date().isoformat(),
            "end": (self.now + timedelta(days=10)).date().isoformat(),
        }
        query.update(params)
        response = self.client.get(reverse("admin_dashboard"), query)
        self.assertEqual(response.status_code, 200)
        return response.context

    def test_movie_filter_excludes_other_movies(self):
        self._book(self.venue_1, self.movie_a)
        self._book(self.venue_2, self.movie_b)
        ctx = self._context(movie=self.movie_a.id)
        names = [m["name"] for m in ctx["top_movies"]]
        self.assertEqual(names, ["Filter A"])

    def test_theater_filter_excludes_other_venues(self):
        self._book(self.venue_1, self.movie_a)
        self._book(self.venue_2, self.movie_b)
        ctx = self._context(theater="Venue One")
        names = [m["name"] for m in ctx["top_movies"]]
        self.assertEqual(names, ["Filter A"])

    def test_theater_filter_matches_venue_across_all_its_shows(self):
        second_show = Theater.objects.create(
            name="Venue One", city="Pune", movie=self.movie_a, screen="V3",
            time=self.now + timedelta(days=2), price_per_seat=Decimal("200.00"),
        )
        self._book(self.venue_1, self.movie_a)
        self._book(second_show, self.movie_a)
        ctx = self._context(theater="Venue One")
        row = next(m for m in ctx["top_movies"] if m["name"] == "Filter A")
        self.assertEqual(row["bookings"], 2)

    def test_invalid_movie_filter_shows_message_not_error(self):
        ctx = self._context(movie="not-a-number")
        self.assertIsNotNone(ctx["filter_error"])
        self.assertIsNone(ctx["filter_movie_id"])

    def test_unknown_theater_returns_no_rows(self):
        self._book(self.venue_1, self.movie_a)
        ctx = self._context(theater="Nonexistent Venue")
        self.assertEqual(list(ctx["top_movies"]), [])


class UserGrowthRulesTests(TestCase):
    """New registrations count from date_joined in the range; growth compares equal-length periods."""

    def setUp(self):
        self.admin = User.objects.create_superuser("ug_admin", "ua@example.com", PASSWORD)
        self.admin.user_permissions.add(Permission.objects.get(codename="view_analytics"))
        User.objects.filter(pk=self.admin.pk).update(date_joined=timezone.now() - timedelta(days=100))
        self.client.force_login(self.admin)
        self.now = timezone.now()
        self.start = (self.now - timedelta(days=10)).date()
        self.end = self.now.date() + timedelta(days=1)

    def _user(self, name, joined):
        user = User.objects.create_user(name, f"{name}@example.com", PASSWORD)
        User.objects.filter(pk=user.pk).update(date_joined=joined)
        return user

    def _context(self):
        response = self.client.get(reverse("admin_dashboard"), {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        return response.context

    def test_new_users_count_only_inside_the_range(self):
        self._user("ug_in_1", self.now - timedelta(days=1))
        self._user("ug_in_2", self.now - timedelta(days=2))
        self._user("ug_out", self.now - timedelta(days=30))
        ctx = self._context()
        self.assertEqual(ctx["new_users"], 2)

    def test_growth_compares_with_previous_equal_length_period(self):
        # Current period: 4 new users. Previous equal-length period: 2 new users. Growth = +100%.
        for i in range(4):
            self._user(f"ug_cur_{i}", self.now - timedelta(days=1 + i))
        for i in range(2):
            self._user(f"ug_prev_{i}", self.now - timedelta(days=12 + i))
        ctx = self._context()
        self.assertEqual(ctx["new_users"], 4)
        self.assertEqual(ctx["user_growth_pct"], 100.0)

    def test_empty_previous_period_gives_no_percentage(self):
        self._user("ug_only", self.now - timedelta(days=1))
        ctx = self._context()
        self.assertEqual(ctx["new_users"], 1)
        self.assertIsNone(ctx["user_growth_pct"])

    def test_total_users_counts_every_registered_user(self):
        before = User.objects.count()
        self._user("ug_total", self.now - timedelta(days=50))
        ctx = self._context()
        self.assertEqual(ctx["total_users"], before + 1)
        
        
        


class TheaterPerformanceRulesTests(TestCase):
    """Venues rank by successful revenue; occupancy and cancellation come from the venue's own rows."""

    def setUp(self):
        self.admin = User.objects.create_superuser("tp_admin", "tp@example.com", PASSWORD)
        self.admin.user_permissions.add(Permission.objects.get(codename="view_analytics"))
        self.client.force_login(self.admin)
        self.buyer = User.objects.create_user("tp_buyer", "tpb@example.com", PASSWORD)
        self.now = timezone.now()
        self.movie = Movie.objects.create(
            name="TP Movie", slug="tp-movie", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
        )
        self._n = 0

    def _show(self, name, screen="S"):
        return Theater.objects.create(
            name=name, city="Pune", movie=self.movie, screen=screen,
            time=self.now + timedelta(days=1), price_per_seat=Decimal("200.00"),
        )

    def _paid(self, theater, amount, order):
        return Payment.objects.create(
            user=self.buyer, theater=theater, amount=Decimal(amount),
            status=Payment.STATUS_SUCCESS, razorpay_order_id=order,
        )

    def _booking(self, theater, payment=None, cancelled=False):
        self._n += 1
        seat = Seat.objects.create(theater=theater, seat_number=f"T{self._n}")
        return Booking.objects.create(
            user=self.buyer, seat=seat, movie=self.movie, theater=theater,
            payment=payment, is_cancelled=cancelled,
        )

    def _venues(self):
        ctx = self.client.get(reverse("admin_dashboard"), {
            "start": (self.now - timedelta(days=1)).date().isoformat(),
            "end": (self.now + timedelta(days=10)).date().isoformat(),
        }).context
        return {t["name"]: t for t in ctx["top_theaters"]}

    def test_venues_are_ordered_by_revenue_not_booking_count(self):
        big = self._show("Big Venue", "B")
        busy = self._show("Busy Venue", "C")
        self._paid(big, "900.00", "o_tp_big")
        for _ in range(5):
            self._booking(busy)
        ctx = self.client.get(reverse("admin_dashboard"), {
            "start": (self.now - timedelta(days=1)).date().isoformat(),
            "end": (self.now + timedelta(days=10)).date().isoformat(),
        }).context
        names = [t["name"] for t in ctx["top_theaters"]]
        self.assertEqual(names[0], "Big Venue")

    def test_failed_payments_add_no_venue_revenue(self):
        show = self._show("Failing Venue")
        Payment.objects.create(
            user=self.buyer, theater=show, amount=Decimal("500.00"),
            status=Payment.STATUS_FAILED, razorpay_order_id="o_tp_fail",
        )
        self._booking(show)
        venues = self._venues()
        self.assertEqual(venues["Failing Venue"]["revenue"], Decimal("0"))

    def test_venue_cancellation_rate_uses_that_venues_bookings(self):
        show = self._show("Cancel Venue")
        self._booking(show)
        self._booking(show, cancelled=True)
        venues = self._venues()
        self.assertEqual(venues["Cancel Venue"]["cancellation_rate"], 50.0)

    def test_venue_with_payments_and_no_bookings_does_not_divide_by_zero(self):
        show = self._show("Paid No Seats")
        self._paid(show, "300.00", "o_tp_noseat")
        venues = self._venues()
        self.assertEqual(venues["Paid No Seats"]["bookings"], 0)
        self.assertIsNone(venues["Paid No Seats"]["weighted_occupancy"])
        
        
        

class PeakHourRulesTests(TestCase):
    """Booking activity is grouped by LOCAL hour (Asia/Kolkata), using the booking timestamp."""

    def setUp(self):
        self.admin = User.objects.create_superuser("ph_admin", "ph@example.com", PASSWORD)
        self.admin.user_permissions.add(Permission.objects.get(codename="view_analytics"))
        self.client.force_login(self.admin)
        self.buyer = User.objects.create_user("ph_buyer", "phb@example.com", PASSWORD)
        self.movie = Movie.objects.create(
            name="PH Movie", slug="ph-movie", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
        )
        self.show = Theater.objects.create(
            name="PH PVR", city="Pune", movie=self.movie, screen="P",
            time=timezone.now() + timedelta(days=1), price_per_seat=Decimal("200.00"),
        )
        self._n = 0
        self.day = timezone.localdate() - timedelta(days=1)

    def _booking_at(self, local_hour, minute=30):
        self._n += 1
        seat = Seat.objects.create(theater=self.show, seat_number=f"H{self._n}")
        booking = Booking.objects.create(
            user=self.buyer, seat=seat, movie=self.movie, theater=self.show,
        )
        local = timezone.make_aware(
            datetime.combine(self.day, datetime.min.time()).replace(hour=local_hour, minute=minute),
            timezone.get_current_timezone(),
        )
        Booking.objects.filter(pk=booking.pk).update(booked_at=local)

    def _hours(self):
        ctx = self.client.get(reverse("admin_dashboard"), {
            "start": self.day.isoformat(),
            "end": (self.day + timedelta(days=1)).isoformat(),
        }).context
        return {row["hour"]: row["count"] for row in ctx["peak_hours"]}

    def test_booking_just_after_local_midnight_is_hour_zero(self):
        self._booking_at(0, minute=30)
        self.assertEqual(self._hours().get(0), 1)

    def test_local_hour_is_used_not_utc_hour(self):
        # 00:30 IST is 19:00 UTC the previous day. Grouping by UTC would put it in hour 19.
        self._booking_at(0, minute=30)
        hours = self._hours()
        self.assertNotIn(19, hours)
        self.assertEqual(hours.get(0), 1)

    def test_hourly_totals_match_bookings_per_hour(self):
        self._booking_at(9)
        self._booking_at(9, minute=59)
        self._booking_at(21)
        hours = self._hours()
        self.assertEqual(hours.get(9), 2)
        self.assertEqual(hours.get(21), 1)

    def test_empty_hours_are_absent_not_zero_rows(self):
        self._booking_at(12)
        hours = self._hours()
        self.assertEqual(set(hours), {12})


class DateRangeValidationTests(TestCase):
    """Bad dates and reversed ranges are reported, not silently replaced."""

    def setUp(self):
        self.admin = User.objects.create_superuser("dr_admin", "dr@example.com", PASSWORD)
        self.admin.user_permissions.add(Permission.objects.get(codename="view_analytics"))
        self.client.force_login(self.admin)

    def _context(self, **params):
        response = self.client.get(reverse("admin_dashboard"), params)
        self.assertEqual(response.status_code, 200)
        return response.context

    def test_malformed_date_is_reported(self):
        ctx = self._context(start="2026-13-01", end="2026-12-01")
        self.assertIsNotNone(ctx["date_error"])
        self.assertIn("YYYY-MM-DD", ctx["date_error"])

    def test_start_after_end_is_reported(self):
        ctx = self._context(start="2026-12-10", end="2026-12-01")
        self.assertIsNotNone(ctx["date_error"])
        self.assertIn("before the end date", ctx["date_error"])

    def test_valid_range_has_no_error(self):
        ctx = self._context(start="2026-12-01", end="2026-12-10")
        self.assertIsNone(ctx["date_error"])

    def test_no_dates_uses_default_without_error(self):
        ctx = self._context()
        self.assertIsNone(ctx["date_error"])

    def test_error_message_is_shown_on_page(self):
        response = self.client.get(reverse("admin_dashboard"), {
            "start": "not-a-date", "end": "2026-12-01",
        })
        self.assertContains(response, "YYYY-MM-DD")
        
        
        
        
        

class IstDayBoundaryTests(TestCase):
    """A payment just before IST midnight belongs to that IST day, not the next UTC day."""

    def setUp(self):
        self.admin = User.objects.create_superuser("ist_admin", "ist@example.com", PASSWORD)
        self.admin.user_permissions.add(Permission.objects.get(codename="view_analytics"))
        self.client.force_login(self.admin)
        self.buyer = User.objects.create_user("ist_buyer", "istb@example.com", PASSWORD)
        self.movie = Movie.objects.create(
            name="IST Movie", slug="ist-movie", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
        )
        self.show = Theater.objects.create(
            name="IST PVR", city="Pune", movie=self.movie, screen="I1",
            time=timezone.now() + timedelta(days=1), price_per_seat=Decimal("200.00"),
        )

    def _payment_at(self, utc_time, amount, order):
        payment = Payment.objects.create(
            user=self.buyer, theater=self.show, amount=Decimal(amount),
            status=Payment.STATUS_SUCCESS, razorpay_order_id=order,
        )
        Payment.objects.filter(pk=payment.pk).update(created_at=utc_time)
        return payment

    def test_payment_at_2330_ist_is_on_that_ist_day(self):
        # 23:30 IST on 2026-03-10 is 18:00 UTC on the same date.
        self._payment_at(datetime(2026, 3, 10, 18, 0, tzinfo=dt_timezone.utc), "100.00", "o_ist_1")
        ctx = self.client.get(reverse("admin_dashboard"), {
            "start": "2026-03-09", "end": "2026-03-12",
        }).context
        by_day = {row["period"]: row["revenue"] for row in ctx["revenue_by_day"]}
        self.assertEqual(by_day.get(date(2026, 3, 10)), Decimal("100.00"))
        self.assertNotIn(date(2026, 3, 11), by_day)

    def test_payment_at_0030_ist_is_on_the_next_ist_day(self):
        # 00:30 IST on 2026-03-11 is 19:00 UTC on 2026-03-10: a UTC grouping would put it on the 10th.
        self._payment_at(datetime(2026, 3, 10, 19, 0, tzinfo=dt_timezone.utc), "250.00", "o_ist_2")
        ctx = self.client.get(reverse("admin_dashboard"), {
            "start": "2026-03-09", "end": "2026-03-12",
        }).context
        by_day = {row["period"]: row["revenue"] for row in ctx["revenue_by_day"]}
        self.assertEqual(by_day.get(date(2026, 3, 11)), Decimal("250.00"))
        self.assertNotIn(date(2026, 3, 10), by_day)
        
        
class TicketPriceSortRulesTests(TestCase):
    """The displayed and sorted price is the cheapest price among shows matching the active filters."""

    def setUp(self):
        self.movie = Movie.objects.create(
            name="Price Movie", slug="price-movie", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
            is_published=True,
        )
        self.delhi_cheap = Theater.objects.create(
            name="Delhi Budget", city="Delhi", movie=self.movie, screen="D1",
            time=timezone.now() + timedelta(days=1), price_per_seat=Decimal("100.00"),
        )
        self.mumbai_cheap = Theater.objects.create(
            name="Mumbai Budget", city="Mumbai", movie=self.movie, screen="M1",
            time=timezone.now() + timedelta(days=1), price_per_seat=Decimal("50.00"),
        )
        self.delhi_premium = Theater.objects.create(
            name="Delhi Premium", city="Delhi", movie=self.movie, screen="D2",
            time=timezone.now() + timedelta(days=1), price_per_seat=Decimal("300.00"),
        )

    def _price_for(self, **params):
        response = self.client.get(reverse("movie_list"), params)
        self.assertEqual(response.status_code, 200)
        movies = list(response.context["movies"])
        self.assertEqual(len(movies), 1)
        return movies[0].lowest_price

    def test_unfiltered_price_is_cheapest_across_all_cities(self):
        self.assertEqual(self._price_for(), Decimal("50.00"))

    def test_city_filter_ignores_cheaper_prices_in_other_cities(self):
        self.assertEqual(self._price_for(city="Delhi"), Decimal("100.00"))

    def test_theater_filter_restricts_price_to_that_theater(self):
        self.assertEqual(self._price_for(theater="Delhi Premium"), Decimal("300.00"))

class PopularityPaymentRulesTests(TestCase):
    """Popularity counts only bookings whose payment succeeded, matching the Task 4 dashboard."""

    def setUp(self):
        self.buyer = User.objects.create_user("pop_buyer", "pop@example.com", PASSWORD)
        self.real = Movie.objects.create(
            name="Real Hit", slug="real-hit", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
            is_published=True,
        )
        self.fake = Movie.objects.create(
            name="Unpaid Hit", slug="unpaid-hit", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
            is_published=True,
        )
        self._n = 0

    def _show(self, movie, screen):
        return Theater.objects.create(
            name="Pop PVR", city="Pune", movie=movie, screen=screen,
            time=timezone.now() + timedelta(days=1), price_per_seat=Decimal("200.00"),
        )

    def _booking(self, show, payment_status):
        self._n += 1
        payment = Payment.objects.create(
            user=self.buyer, theater=show, amount=Decimal("200.00"), status=payment_status,
            razorpay_order_id=f"pop_order_{self._n}",
        )
        seat = Seat.objects.create(theater=show, seat_number=f"P{self._n}")
        return Booking.objects.create(
            user=self.buyer, seat=seat, movie=show.movie, theater=show, payment=payment,
        )

    def test_failed_payment_bookings_do_not_count_toward_popularity(self):
        paid_show = self._show(self.real, "R1")
        self._booking(paid_show, Payment.STATUS_SUCCESS)
        failed_show = self._show(self.fake, "F1")
        self._booking(failed_show, Payment.STATUS_FAILED)
        self._booking(failed_show, Payment.STATUS_FAILED)
        response = self.client.get(reverse("movie_list"), {"sort": "popularity"})
        self.assertEqual(response.status_code, 200)
        counts = {m.name: m.booking_count for m in response.context["movies"]}
        self.assertEqual(counts["Real Hit"], 1)
        self.assertEqual(counts["Unpaid Hit"], 0)
        names = [m.name for m in response.context["movies"]]
        self.assertEqual(names[0], "Real Hit")


class DiscoveryValidationRulesTests(TestCase):
    """Theater filter takes a venue name; unknown or malformed values match nothing and never crash."""

    def setUp(self):
        self.movie = Movie.objects.create(
            name="Validation Movie", slug="validation-movie", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
            is_published=True,
        )
        Theater.objects.create(
            name="Val PVR", city="Pune", movie=self.movie, screen="V1",
            time=timezone.now() + timedelta(days=1), price_per_seat=Decimal("200.00"),
        )

    def test_unfiltered_page_has_no_theater_condition(self):
        response = self.client.get(reverse("movie_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], 1)

    def test_unknown_theater_name_returns_no_movies(self):
        response = self.client.get(reverse("movie_list"), {"theater": "No Such Venue"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], 0)

    def test_valid_theater_name_still_filters(self):
        response = self.client.get(reverse("movie_list"), {"theater": "Val PVR"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], 1)
        response = self.client.get(reverse("movie_list"), {"theater": "Nonexistent PVR"})
        self.assertEqual(response.context["total_count"], 0)

    def test_malformed_theater_value_does_not_crash(self):
        response = self.client.get(reverse("movie_list"), {"theater": "<script>x</script>"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], 0)


class DiscoveryDateErrorTests(TestCase):
    """An invalid date in the discovery filters is reported on the page, not silently dropped."""

    def setUp(self):
        self.movie = Movie.objects.create(
            name="Date Movie", slug="date-movie", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
            is_published=True,
        )

    def test_invalid_release_date_is_reported(self):
        response = self.client.get(reverse("movie_list"), {"release_from": "2026-99-01"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Release start date must be in the format YYYY-MM-DD.")

    def test_invalid_show_date_is_reported(self):
        response = self.client.get(reverse("movie_list"), {"date_from": "not-a-date"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Show date from must be in the format YYYY-MM-DD.")

    def test_valid_dates_report_no_error(self):
        response = self.client.get(reverse("movie_list"), {"release_from": "2026-01-01"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "must be in the format YYYY-MM-DD")


class ColdStartRecommendationTests(TestCase):
    """Users without valid history get a labelled general fallback, never a blank or fake personal list."""

    def setUp(self):
        self.user = User.objects.create_user("cold_user", "cold@example.com", PASSWORD)
        self.liked = Movie.objects.create(
            name="Liked Movie", slug="liked-movie", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
            is_published=True,
        )
        self.other = Movie.objects.create(
            name="Other Movie", slug="other-movie", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
            is_published=True,
        )
        from movies.models import Genre
        shared = Genre.objects.create(name="Shared Genre")
        self.liked.genres.add(shared)
        self.other.genres.add(shared)
        self.client.force_login(self.user)

    def _context(self):
        response = self.client.get(reverse("movie_list"))
        self.assertEqual(response.status_code, 200)
        return response.context

    def test_new_user_gets_general_fallback_not_personal(self):
        ctx = self._context()
        self.assertFalse(ctx["recommendations_personal"])
        self.assertTrue(len(ctx["recommended_movies"]) > 0)
        self.assertContains(self.client.get(reverse("movie_list")), "Popular Right Now")

    def test_failed_payment_booking_does_not_make_recommendations_personal(self):
        show = Theater.objects.create(
            name="Cold PVR", city="Pune", movie=self.liked, screen="C1",
            time=timezone.now() + timedelta(days=1), price_per_seat=Decimal("200.00"),
        )
        seat = Seat.objects.create(theater=show, seat_number="C1")
        payment = Payment.objects.create(
            user=self.user, theater=show, amount=Decimal("200.00"),
            status=Payment.STATUS_FAILED, razorpay_order_id="cold_failed",
        )
        Booking.objects.create(
            user=self.user, seat=seat, movie=self.liked, theater=show, payment=payment,
        )
        ctx = self._context()
        self.assertFalse(ctx["recommendations_personal"])

    def test_successful_booking_makes_recommendations_personal(self):
        show = Theater.objects.create(
            name="Cold Paid", city="Pune", movie=self.liked, screen="C2",
            time=timezone.now() + timedelta(days=1), price_per_seat=Decimal("200.00"),
        )
        seat = Seat.objects.create(theater=show, seat_number="C2")
        payment = Payment.objects.create(
            user=self.user, theater=show, amount=Decimal("200.00"),
            status=Payment.STATUS_SUCCESS, razorpay_order_id="cold_paid",
        )
        Booking.objects.create(
            user=self.user, seat=seat, movie=self.liked, theater=show, payment=payment,
        )
        ctx = self._context()
        self.assertTrue(ctx["recommendations_personal"])


class DiscoveryCountEndpointTests(TestCase):
    """The count endpoint must match the list the page shows, for the same filters."""

    def setUp(self):
        self.movie = Movie.objects.create(
            name="Count Movie", slug="count-movie", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
            is_published=True,
        )
        Movie.objects.create(
            name="Other Film", slug="other-film", description="d", image="movies/x.jpg",
            duration_minutes=100, age_certificate="UA", release_date=timezone.now().date(),
            is_published=True,
        )

    def test_count_without_filters_matches_all_published_movies(self):
        response = self.client.get(reverse("discovery_count"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 2)

    def test_count_with_search_matches_the_list(self):
        response = self.client.get(reverse("discovery_count"), {"search": "count"})
        self.assertEqual(response.json()["count"], 1)
        listing = self.client.get(reverse("movie_list"), {"search": "count"})
        self.assertEqual(listing.context["total_count"], 1)

    def test_invalid_date_is_returned_as_an_error(self):
        response = self.client.get(reverse("discovery_count"), {"release_from": "bad"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["errors"]), 1)

    def test_endpoint_rejects_post(self):
        response = self.client.post(reverse("discovery_count"))
        self.assertEqual(response.status_code, 405)




