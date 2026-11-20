"""
Task 1 tests: movie management, trailers and posters, reviews and ratings, reporting and
moderation, and recommendations. Each test uses real records (movies, shows, seats, payments,
bookings) and real requests through Django's test client.
"""
import tempfile
import uuid
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

from django.contrib.admin.sites import site as admin_site
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .admin import uphold_reports, dismiss_reports, hide_reviews, restore_reviews
from .eligibility import completed_bookings, user_has_completed_viewing
from .forms import MovieAdminForm, ReviewForm
from .models import (
    Booking, CastMember, Genre, Language, Movie, MovieCast, MoviePoster,
    Payment, Review, ReviewReport, Seat, Theater,
)
from .validators import parse_youtube_id, validate_poster_content

MEDIA = tempfile.mkdtemp()
PASSWORD = "Strong-pass-9"
VALID_TRAILER = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
VALID_ID = "dQw4w9WgXcQ"


def unique(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def make_movie(name, genres=(), languages=(), duration=120, release=None, published=True, slug=None):
    movie = Movie.objects.create(
        name=name,
        slug=slug or unique("movie"),
        description="A film.",
        image="movies/poster.jpg",
        duration_minutes=duration,
        age_certificate="UA",
        release_date=release or date(2020, 1, 1),
        is_published=published,
    )
    movie.genres.set(list(genres))
    movie.languages.set(list(languages))
    return movie


def make_show(movie, start, screen=None, name="PVR"):
    theater = Theater.objects.create(
        name=name, city="Mumbai", movie=movie, screen=screen or unique("screen"),
        time=start, price_per_seat=Decimal("200.00"),
    )
    theater.ensure_seat_layout()
    return theater


def make_booking(user, theater, seat_number="A1", payment_status=Payment.STATUS_SUCCESS,
                 cancelled=False, movie=None):
    seat = theater.seats.get(seat_number=seat_number)
    payment = Payment.objects.create(
        user=user, theater=theater, amount=Decimal("200.00"), status=payment_status,
        razorpay_order_id=unique("order"),
        razorpay_payment_id=unique("pay") if payment_status == Payment.STATUS_SUCCESS else None,
    )
    payment.seats.set([seat])
    booking = Booking.objects.create(
        user=user, seat=seat, movie=movie or theater.movie, theater=theater,
        payment=payment, is_cancelled=cancelled,
    )
    seat.is_booked = not cancelled
    seat.save(update_fields=["is_booked"])
    return booking


def logged_in(user):
    client = Client()
    client.force_login(user)
    return client


def post_review(client, movie, rating=5, body="A thoroughly enjoyable film.", title="Great"):
    return client.post(
        reverse("movie_detail", kwargs={"slug": movie.slug}),
        {"submit_review": "1", "rating": rating, "title": title, "body": body},
    )


class _StubModelAdmin:
    """Stands in for the ModelAdmin that Django passes to admin actions (they call message_user)."""
    def message_user(self, request, message, *args, **kwargs):
        self.last_message = message


class Base(TestCase):
    def setUp(self):
        self.genre = Genre.objects.create(name=unique("genre"))
        self.genre2 = Genre.objects.create(name=unique("genre"))
        self.lang = Language.objects.create(name=unique("lang"))
        self.lang2 = Language.objects.create(name=unique("lang"))
        self.movie = make_movie("Alpha", genres=[self.genre], languages=[self.lang])
        self.alice = User.objects.create_user(unique("alice"), "a@example.com", PASSWORD)
        self.bob = User.objects.create_user(unique("bob"), "b@example.com", PASSWORD)
        self.staff = User.objects.create_user(unique("staff"), "s@example.com", PASSWORD, is_staff=True)
        self.admin = User.objects.create_superuser(unique("admin"), "adm@example.com", PASSWORD)
        self.past = timezone.now() - timedelta(days=3)

    def watched(self, user, movie=None, seat_number="A1"):
        """A confirmed, paid booking for a show that finished in the past."""
        movie = movie or self.movie
        theater = make_show(movie, self.past)
        return make_booking(user, theater, seat_number=seat_number, movie=movie)


# ---------------------------------------------------------------------------
# A. Movie management and administration
# ---------------------------------------------------------------------------

@override_settings(MEDIA_ROOT=MEDIA)
class MovieManagementTests(Base):
    def test_admin_registers_all_task_entities(self):
        for model in [Movie, Genre, Language, CastMember, Theater, Review, ReviewReport, MoviePoster]:
            self.assertTrue(admin_site.is_registered(model), model.__name__)

    def test_duration_must_be_positive_and_realistic(self):
        for bad in (0, 601):
            movie = Movie(name="x", slug=unique("s"), description="d", image="a.jpg",
                          duration_minutes=bad, age_certificate="UA", release_date=date(2020, 1, 1))
            with self.assertRaises(ValidationError, msg=f"duration {bad} accepted"):
                movie.full_clean()

    def test_age_certificate_must_be_an_allowed_value(self):
        movie = Movie(name="x", slug=unique("s"), description="d", image="a.jpg",
                      duration_minutes=90, age_certificate="XXX", release_date=date(2020, 1, 1))
        with self.assertRaises(ValidationError):
            movie.full_clean()

    def test_unpublished_movie_is_hidden_from_visitors(self):
        hidden = make_movie("Hidden title", published=False)
        self.assertEqual(Client().get(reverse("movie_detail", kwargs={"slug": hidden.slug})).status_code, 404)
        self.assertNotContains(Client().get(reverse("movie_list")), "Hidden title")

    def test_unpublished_movie_is_visible_to_staff(self):
        hidden = make_movie("Staff preview", published=False)
        response = logged_in(self.staff).get(reverse("movie_detail", kwargs={"slug": hidden.slug}))
        self.assertEqual(response.status_code, 200)

    def test_admin_add_theatre_gets_bookable_seats(self):
        client = logged_in(self.admin)
        response = client.post(reverse("admin:movies_theater_add"), {
            "name": "Admin PVR", "city": "Mumbai", "movie": self.movie.pk, "screen": "Screen 9",
            "time_0": "2031-06-01", "time_1": "19:00:00", "price_per_seat": "250.00",
        })
        self.assertEqual(response.status_code, 302, response.content[:300])
        theater = Theater.objects.get(name="Admin PVR")
        self.assertEqual(theater.seats.count(), 100)

    def test_overlapping_show_on_same_screen_is_rejected(self):
        start = timezone.now() + timedelta(days=10)
        make_show(self.movie, start, screen="Screen Q")
        clash = Theater(name="PVR", city="Mumbai", movie=self.movie, screen="Screen Q",
                        time=start + timedelta(minutes=30), price_per_seat=Decimal("200.00"))
        with self.assertRaises(ValidationError):
            clash.full_clean()

    def test_back_to_back_shows_on_same_screen_are_allowed(self):
        start = timezone.now() + timedelta(days=12)
        make_show(self.movie, start, screen="Screen R")  # runs 120 minutes
        after = Theater(name="PVR", city="Mumbai", movie=self.movie, screen="Screen R",
                        time=start + timedelta(minutes=120), price_per_seat=Decimal("200.00"))
        after.full_clean()  # does not raise

    def test_admin_form_turns_a_youtube_link_into_its_id(self):
        form = MovieAdminForm(data={
            "name": "Trailer test", "slug": unique("t"), "description": "d", "image": "",
            "duration_minutes": 100, "age_certificate": "UA", "release_date": "2020-01-01",
            "is_published": True, "trailer_youtube_id": VALID_TRAILER,
        })
        form.is_valid()
        self.assertEqual(form.cleaned_data.get("trailer_youtube_id"), VALID_ID)

    def test_non_staff_cannot_reach_the_admin(self):
        response = logged_in(self.alice).get(reverse("admin:movies_movie_changelist"))
        self.assertNotEqual(response.status_code, 200)

    def test_staff_without_permissions_cannot_moderate(self):
        response = logged_in(self.staff).get(reverse("admin:movies_reviewreport_changelist"))
        self.assertEqual(response.status_code, 403)


# ---------------------------------------------------------------------------
# B. Trailer security and safe rendering
# ---------------------------------------------------------------------------

class TrailerSecurityTests(Base):
    def test_supported_youtube_formats_are_accepted(self):
        cases = [
            VALID_TRAILER,
            "https://youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&t=10",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            VALID_ID,
        ]
        for value in cases:
            self.assertEqual(parse_youtube_id(value), VALID_ID, value)

    def test_javascript_data_and_foreign_domains_are_rejected(self):
        bad = [
            "javascript:alert(1)",
            "JAVASCRIPT:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "https://evil.example.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=<script>alert(1)</script>",
            "https://www.youtube.com/watch?v=short",
            "ftp://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "dQw4w9WgXcQ\"><script>alert(1)</script>",
            "not a url at all",
        ]
        for value in bad:
            self.assertIsNone(parse_youtube_id(value), value)

    def test_empty_trailer_is_allowed(self):
        self.assertEqual(parse_youtube_id(""), "")

    def test_embed_url_is_built_only_from_a_valid_id(self):
        self.movie.trailer_youtube_id = VALID_ID
        self.assertEqual(self.movie.trailer_embed_url,
                         f"https://www.youtube-nocookie.com/embed/{VALID_ID}")
        self.movie.trailer_youtube_id = '"><script>alert(1)</script>'
        self.assertEqual(self.movie.trailer_embed_url, "")

    def test_trailer_iframe_has_restricted_attributes(self):
        self.movie.trailer_youtube_id = VALID_ID
        self.movie.save()
        body = Client().get(reverse("movie_detail", kwargs={"slug": self.movie.slug})).content.decode()
        self.assertIn('loading="lazy"', body)
        self.assertIn('referrerpolicy="strict-origin-when-cross-origin"', body)
        self.assertIn('allow="encrypted-media; picture-in-picture; fullscreen"', body)
        self.assertNotIn("autoplay", body.split("<iframe")[1].split("</iframe>")[0])

    def test_script_in_title_and_description_is_escaped(self):
        bad = make_movie('<script>alert("title")</script>', slug=unique("xss"))
        bad.description = '<img src=x onerror=alert(1)> plot'
        bad.save()
        body = Client().get(reverse("movie_detail", kwargs={"slug": bad.slug})).content.decode()
        self.assertNotIn("<script>alert", body)
        self.assertNotIn("<img src=x onerror", body)
        self.assertIn("&lt;script&gt;alert", body)

    def test_non_youtube_trailer_is_not_embedded(self):
        self.movie.trailer_youtube_id = ""
        self.movie.save()
        body = Client().get(reverse("movie_detail", kwargs={"slug": self.movie.slug})).content.decode()
        self.assertNotIn("youtube-nocookie.com/embed/", body)


class PosterValidationTests(TestCase):
    def test_renamed_non_image_is_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        fake = SimpleUploadedFile("poster.jpg", b"<html>not an image</html>", content_type="image/jpeg")
        with self.assertRaises(ValidationError):
            validate_poster_content(fake)

    def test_real_png_is_accepted(self):
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        buf = BytesIO()
        Image.new("RGB", (10, 15), (200, 30, 30)).save(buf, format="PNG")
        validate_poster_content(SimpleUploadedFile("p.png", buf.getvalue(), content_type="image/png"))

    def test_gif_is_rejected_even_when_it_is_an_image(self):
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        buf = BytesIO()
        Image.new("RGB", (10, 15)).save(buf, format="GIF")
        with self.assertRaises(ValidationError):
            validate_poster_content(SimpleUploadedFile("p.gif", buf.getvalue(), content_type="image/gif"))

    @override_settings(MEDIA_ROOT=MEDIA)
    def test_missing_poster_file_falls_back_to_placeholder(self):
        movie = make_movie("No file", slug=unique("nofile"))
        self.assertIn("poster-placeholder.svg", movie.poster_url)

    @override_settings(MEDIA_ROOT=MEDIA)
    def test_missing_gallery_file_falls_back_to_placeholder(self):
        movie = make_movie("Gallery", slug=unique("gallery"))
        poster = MoviePoster.objects.create(movie=movie, image="movies/gallery/missing.png", order=1)
        self.assertIn("poster-placeholder.svg", poster.image_url)


# ---------------------------------------------------------------------------
# C. Review eligibility
# ---------------------------------------------------------------------------

class ReviewEligibilityTests(Base):
    def test_anonymous_visitor_cannot_review(self):
        self.watched(self.alice)
        post_review(Client(), self.movie)
        self.assertFalse(Review.objects.filter(movie=self.movie).exists())

    def test_user_without_a_booking_cannot_review(self):
        post_review(logged_in(self.bob), self.movie)
        self.assertFalse(Review.objects.filter(user=self.bob).exists())

    def test_user_cannot_use_another_users_booking(self):
        self.watched(self.alice)
        post_review(logged_in(self.bob), self.movie)
        self.assertFalse(Review.objects.filter(user=self.bob).exists())

    def test_future_show_does_not_qualify(self):
        theater = make_show(self.movie, timezone.now() + timedelta(days=2))
        make_booking(self.alice, theater)
        post_review(logged_in(self.alice), self.movie)
        self.assertFalse(Review.objects.filter(user=self.alice).exists())

    def test_show_still_running_does_not_qualify(self):
        # Started 30 minutes ago, runs 120 minutes: not finished yet.
        theater = make_show(self.movie, timezone.now() - timedelta(minutes=30))
        make_booking(self.alice, theater)
        post_review(logged_in(self.alice), self.movie)
        self.assertFalse(Review.objects.filter(user=self.alice).exists())

    def test_cancelled_booking_does_not_qualify(self):
        self.watched_cancelled = None
        theater = make_show(self.movie, self.past)
        make_booking(self.alice, theater, cancelled=True)
        post_review(logged_in(self.alice), self.movie)
        self.assertFalse(Review.objects.filter(user=self.alice).exists())

    def test_booking_without_successful_payment_does_not_qualify(self):
        for status in (Payment.STATUS_FAILED, Payment.STATUS_CREATED, Payment.STATUS_REFUNDED):
            theater = make_show(self.movie, self.past - timedelta(days=len(status)))
            make_booking(self.alice, theater, payment_status=status)
        self.assertFalse(user_has_completed_viewing(self.alice, self.movie))

    def test_booking_for_another_movie_does_not_qualify(self):
        other = make_movie("Beta", genres=[self.genre], slug=unique("beta"))
        theater = make_show(other, self.past)
        make_booking(self.alice, theater, movie=other)
        post_review(logged_in(self.alice), self.movie)
        self.assertFalse(Review.objects.filter(user=self.alice, movie=self.movie).exists())

    def test_completed_booking_qualifies(self):
        self.watched(self.alice)
        self.assertTrue(user_has_completed_viewing(self.alice, self.movie))
        post_review(logged_in(self.alice), self.movie)
        self.assertTrue(Review.objects.filter(user=self.alice, movie=self.movie).exists())

    def test_qualifying_is_per_user_and_per_movie_queryset(self):
        self.watched(self.alice)
        self.assertEqual(completed_bookings(self.alice, self.movie).count(), 1)
        self.assertEqual(completed_bookings(self.bob, self.movie).count(), 0)


# ---------------------------------------------------------------------------
# D. Review behaviour: validation, editing, badge, rating average
# ---------------------------------------------------------------------------

class ReviewBehaviourTests(Base):
    def setUp(self):
        super().setUp()
        self.watched(self.alice, seat_number="A1")
        self.watched(self.bob, seat_number="A2")

    def test_rating_must_be_one_to_five(self):
        for bad in (0, 6, "x"):
            form = ReviewForm(data={"rating": bad, "title": "t", "body": "Perfectly fine film."})
            self.assertFalse(form.is_valid(), bad)

    def test_body_below_minimum_is_rejected(self):
        form = ReviewForm(data={"rating": 4, "title": "t", "body": "meh"})
        self.assertFalse(form.is_valid())
        self.assertIn("body", form.errors)

    def test_body_above_maximum_is_rejected(self):
        form = ReviewForm(data={"rating": 4, "title": "t", "body": "x" * 2001})
        self.assertFalse(form.is_valid())

    def test_review_is_saved_with_rating_and_text(self):
        post_review(logged_in(self.alice), self.movie, rating=4, body="Solid performances throughout.")
        review = Review.objects.get(user=self.alice, movie=self.movie)
        self.assertEqual((review.rating, review.body), (4, "Solid performances throughout."))

    def test_only_one_review_per_user_per_movie(self):
        client = logged_in(self.alice)
        post_review(client, self.movie, rating=3, body="The first impression.")
        post_review(client, self.movie, rating=5, body="A much better second thought.")
        self.assertEqual(Review.objects.filter(user=self.alice, movie=self.movie).count(), 1)

    def test_owner_can_edit_their_review(self):
        client = logged_in(self.alice)
        post_review(client, self.movie, rating=3, body="The first impression.")
        post_review(client, self.movie, rating=5, body="Changed my mind completely.")
        review = Review.objects.get(user=self.alice, movie=self.movie)
        self.assertEqual((review.rating, review.is_edited), (5, True))

    def test_user_cannot_edit_another_users_review(self):
        post_review(logged_in(self.alice), self.movie, rating=2, body="Original alice text.")
        post_review(logged_in(self.bob), self.movie, rating=5, body="Bob's own review text.")
        alice_review = Review.objects.get(user=self.alice, movie=self.movie)
        self.assertEqual((alice_review.rating, alice_review.body), (2, "Original alice text."))

    def test_database_refuses_a_second_review_row(self):
        Review.objects.create(movie=self.movie, user=self.alice, rating=4, body="first row here")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Review.objects.create(movie=self.movie, user=self.alice, rating=5, body="second row here")

    def test_forged_verified_flag_is_ignored(self):
        self.watched(self.bob, seat_number="A3", movie=make_movie("Fresh", slug=unique("fresh")))
        client = logged_in(self.bob)
        client.post(reverse("movie_detail", kwargs={"slug": self.movie.slug}), {
            "submit_review": "1", "rating": 4, "title": "t", "body": "Good value overall.",
            "is_verified_viewer": "true",
        })
        review = Review.objects.get(user=self.bob, movie=self.movie)
        self.assertTrue(review.is_verified_viewer)  # derived from bookings, not from the form
        other = make_movie("Other", slug=unique("other"))
        forged = Review.objects.create(movie=other, user=self.alice, rating=3, body="no booking here", is_verified_viewer=True)
        forged.save()
        self.assertFalse(Review.objects.get(pk=forged.pk).is_verified_viewer)

    def test_verified_badge_disappears_when_booking_is_cancelled(self):
        post_review(logged_in(self.alice), self.movie)
        page = Client().get(reverse("movie_detail", kwargs={"slug": self.movie.slug})).content.decode()
        self.assertIn("Verified Viewer", page)
        Booking.objects.filter(user=self.alice, movie=self.movie).update(is_cancelled=True)
        page = Client().get(reverse("movie_detail", kwargs={"slug": self.movie.slug})).content.decode()
        self.assertNotIn("Verified Viewer", page)

    def test_average_and_count_track_create_edit_delete(self):
        post_review(logged_in(self.alice), self.movie, rating=4, body="Enjoyed the film a lot.")
        post_review(logged_in(self.bob), self.movie, rating=2, body="Not my cup of tea.")
        self.movie.refresh_from_db()
        self.assertEqual((self.movie.average_rating, self.movie.total_ratings), (Decimal("3.0"), 2))

        post_review(logged_in(self.alice), self.movie, rating=5, body="Revised: actually excellent.")
        self.movie.refresh_from_db()
        self.assertEqual((self.movie.average_rating, self.movie.total_ratings), (Decimal("3.5"), 2))

        Review.objects.get(user=self.bob, movie=self.movie).delete()
        self.movie.refresh_from_db()
        self.assertEqual((self.movie.average_rating, self.movie.total_ratings), (Decimal("5.0"), 1))

    def test_hidden_review_is_excluded_from_average_and_restored(self):
        review = Review.objects.create(movie=self.movie, user=self.alice, rating=1, body="Strongly disliked it.")
        self.movie.refresh_from_db()
        self.assertEqual(self.movie.total_ratings, 1)
        hide_reviews(_StubModelAdmin(), None, Review.objects.filter(pk=review.pk))
        self.movie.refresh_from_db()
        self.assertEqual(self.movie.total_ratings, 0)
        restore_reviews(_StubModelAdmin(), None, Review.objects.filter(pk=review.pk))
        self.movie.refresh_from_db()
        self.assertEqual(self.movie.total_ratings, 1)

    def test_rating_is_rounded_half_up_to_one_decimal(self):
        Review.objects.create(movie=self.movie, user=self.alice, rating=4, body="Good enough watch.")
        Review.objects.create(movie=self.movie, user=self.bob, rating=5, body="Really rather good.")
        self.movie.refresh_from_db()
        self.assertEqual(self.movie.average_rating, Decimal("4.5"))

    def test_review_page_paginates_and_shows_empty_state(self):
        page = Client().get(reverse("movie_detail", kwargs={"slug": self.movie.slug})).content.decode()
        self.assertIn("No reviews yet", page)
        for i in range(12):
            user = User.objects.create_user(unique("reader"), f"r{i}@example.com", PASSWORD)
            Review.objects.create(movie=self.movie, user=user, rating=4, body=f"Review number {i} here.")
        response = Client().get(reverse("movie_detail", kwargs={"slug": self.movie.slug}))
        self.assertEqual(len(response.context["reviews_page"].object_list), 10)
        second = Client().get(reverse("movie_detail", kwargs={"slug": self.movie.slug}), {"page": 2})
        self.assertEqual(len(second.context["reviews_page"].object_list), 2)

    def test_rating_breakdown_counts_visible_reviews(self):
        Review.objects.create(movie=self.movie, user=self.alice, rating=5, body="Five stars all round.")
        Review.objects.create(movie=self.movie, user=self.bob, rating=3, body="Three stars, fair enough.")
        response = Client().get(reverse("movie_detail", kwargs={"slug": self.movie.slug}))
        counts = {row["stars"]: row["count"] for row in response.context["review_distribution"]}
        self.assertEqual((counts[5], counts[3]), (1, 1))

    def test_csrf_protection_applies_to_review_submission(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.alice)
        response = client.post(reverse("movie_detail", kwargs={"slug": self.movie.slug}),
                               {"submit_review": "1", "rating": 5, "title": "t", "body": "Some review text."})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Review.objects.filter(user=self.alice).exists())


# ---------------------------------------------------------------------------
# E. Reporting and moderation
# ---------------------------------------------------------------------------

class ReportingTests(Base):
    def setUp(self):
        super().setUp()
        self.watched(self.alice)
        self.review = Review.objects.create(movie=self.movie, user=self.alice, rating=1, body="Spam link inside.")
        self.report_url = reverse("report_review", kwargs={"review_id": self.review.id})

    def test_valid_report_is_recorded(self):
        logged_in(self.bob).post(self.report_url, {"reason": ReviewReport.REASON_SPAM, "note": "ads"})
        report = ReviewReport.objects.get(review=self.review, reported_by=self.bob)
        self.assertEqual((report.reason, report.status), (ReviewReport.REASON_SPAM, ReviewReport.STATUS_OPEN))

    def test_duplicate_report_by_same_user_is_stored_once(self):
        client = logged_in(self.bob)
        client.post(self.report_url, {"reason": ReviewReport.REASON_SPAM, "note": ""})
        client.post(self.report_url, {"reason": ReviewReport.REASON_OFFENSIVE, "note": ""})
        self.assertEqual(ReviewReport.objects.filter(review=self.review, reported_by=self.bob).count(), 1)

    def test_invalid_reason_is_rejected(self):
        logged_in(self.bob).post(self.report_url, {"reason": "not-a-reason", "note": ""})
        self.assertFalse(ReviewReport.objects.filter(reported_by=self.bob).exists())

    def test_user_cannot_report_their_own_review(self):
        logged_in(self.alice).post(self.report_url, {"reason": ReviewReport.REASON_SPAM, "note": ""})
        self.assertFalse(ReviewReport.objects.filter(reported_by=self.alice).exists())

    def test_anonymous_visitor_cannot_report(self):
        Client().post(self.report_url, {"reason": ReviewReport.REASON_SPAM, "note": ""})
        self.assertFalse(ReviewReport.objects.exists())

    def test_hidden_review_cannot_be_reported(self):
        self.review.is_hidden = True
        self.review.save()
        logged_in(self.bob).post(self.report_url, {"reason": ReviewReport.REASON_SPAM, "note": ""})
        self.assertFalse(ReviewReport.objects.filter(reported_by=self.bob).exists())

    def test_reporting_does_not_change_the_rating(self):
        self.movie.refresh_from_db()
        before = (self.movie.average_rating, self.movie.total_ratings)
        logged_in(self.bob).post(self.report_url, {"reason": ReviewReport.REASON_SPAM, "note": ""})
        self.movie.refresh_from_db()
        self.assertEqual((self.movie.average_rating, self.movie.total_ratings), before)

    def test_report_count_counts_open_reports(self):
        logged_in(self.bob).post(self.report_url, {"reason": ReviewReport.REASON_SPAM, "note": ""})
        self.review.refresh_from_db()
        self.assertEqual(self.review.report_count, 1)

    def test_reporting_is_get_refused(self):
        response = logged_in(self.bob).get(self.report_url)
        self.assertEqual(response.status_code, 405)

    def test_uphold_hides_review_and_records_the_outcome(self):
        logged_in(self.bob).post(self.report_url, {"reason": ReviewReport.REASON_SPAM, "note": ""})
        report = ReviewReport.objects.get(reported_by=self.bob)
        uphold_reports(_StubModelAdmin(), type("R", (), {"user": self.admin})(), ReviewReport.objects.filter(pk=report.pk))
        report.refresh_from_db()
        self.review.refresh_from_db()
        self.assertEqual(report.status, ReviewReport.STATUS_UPHELD)
        self.assertEqual(report.resolved_by, self.admin)
        self.assertIsNotNone(report.resolved_at)
        self.assertTrue(self.review.is_hidden)
        self.assertEqual(self.review.report_count, 0)

    def test_dismiss_keeps_review_visible_and_records_the_outcome(self):
        logged_in(self.bob).post(self.report_url, {"reason": ReviewReport.REASON_OTHER, "note": ""})
        report = ReviewReport.objects.get(reported_by=self.bob)
        dismiss_reports(_StubModelAdmin(), type("R", (), {"user": self.admin})(), ReviewReport.objects.filter(pk=report.pk))
        report.refresh_from_db()
        self.review.refresh_from_db()
        self.assertEqual(report.status, ReviewReport.STATUS_DISMISSED)
        self.assertFalse(self.review.is_hidden)

    def test_non_staff_cannot_open_the_report_admin(self):
        response = logged_in(self.bob).get(reverse("admin:movies_reviewreport_changelist"))
        self.assertNotEqual(response.status_code, 200)

    def test_moderator_can_open_the_report_admin(self):
        response = logged_in(self.admin).get(reverse("admin:movies_reviewreport_changelist"))
        self.assertEqual(response.status_code, 200)

    def test_report_is_not_shown_to_visitors(self):
        logged_in(self.bob).post(self.report_url, {"reason": ReviewReport.REASON_SPAM, "note": "secret note"})
        body = Client().get(reverse("movie_detail", kwargs={"slug": self.movie.slug})).content.decode()
        self.assertNotIn("secret note", body)


# ---------------------------------------------------------------------------
# F. Recommendations
# ---------------------------------------------------------------------------

class RecommendationTests(Base):
    def test_similar_movies_share_genre_or_language_and_exclude_self(self):
        same_genre = make_movie("Same genre", genres=[self.genre], slug=unique("sg"))
        same_lang = make_movie("Same lang", languages=[self.lang], slug=unique("sl"))
        unrelated = make_movie("Unrelated", genres=[self.genre2], languages=[self.lang2], slug=unique("un"))
        similar = list(self.movie.similar_movies())
        self.assertIn(same_genre, similar)
        self.assertIn(same_lang, similar)
        self.assertNotIn(unrelated, similar)
        self.assertNotIn(self.movie, similar)

    def test_similar_movies_are_not_duplicated_and_skip_unpublished(self):
        both = make_movie("Both", genres=[self.genre], languages=[self.lang], slug=unique("both"))
        make_movie("Hidden similar", genres=[self.genre], published=False, slug=unique("hs"))
        ids = [m.id for m in self.movie.similar_movies()]
        self.assertEqual(ids.count(both.id), 1)
        self.assertNotIn("Hidden similar", [m.name for m in self.movie.similar_movies()])

    def test_movie_without_metadata_has_no_similar_and_page_renders(self):
        bare = make_movie("Bare", slug=unique("bare"))
        self.assertEqual(list(bare.similar_movies()), [])
        self.assertEqual(Client().get(reverse("movie_detail", kwargs={"slug": bare.slug})).status_code, 200)

    def test_recently_released_orders_by_release_date_and_excludes_future(self):
        old = make_movie("Old", release=date(2010, 1, 1), slug=unique("old"))
        new = make_movie("New", release=date(2024, 6, 1), slug=unique("new"))
        future = make_movie("Future", release=timezone.localdate() + timedelta(days=30), slug=unique("fut"))
        names = [m.name for m in
                 Client().get(reverse("movie_detail", kwargs={"slug": self.movie.slug})).context["recent_movies"]]
        self.assertIn("New", names)
        self.assertNotIn("Future", names)
        self.assertLess(names.index("New"), names.index("Old"))

    def test_trending_uses_recent_non_cancelled_bookings_only(self):
        trending = make_movie("Trending", slug=unique("tr"))
        cancelled_only = make_movie("Cancelled only", slug=unique("co"))
        for seat in ["A1", "A2", "A3"]:
            self.watched(self.bob, movie=trending, seat_number=seat)
        theater = make_show(cancelled_only, self.past)
        make_booking(self.bob, theater, cancelled=True, movie=cancelled_only)
        names = [m.name for m in
                 Client().get(reverse("movie_detail", kwargs={"slug": self.movie.slug})).context["trending_movies"]]
        self.assertIn("Trending", names)
        self.assertNotIn("Cancelled only", names)

    def test_trending_ignores_bookings_outside_the_window(self):
        old_movie = make_movie("Ancient", slug=unique("anc"))
        booking = self.watched(self.bob, movie=old_movie, seat_number="B1")
        Booking.objects.filter(pk=booking.pk).update(booked_at=timezone.now() - timedelta(days=90))
        names = [m.name for m in
                 Client().get(reverse("movie_detail", kwargs={"slug": self.movie.slug})).context["trending_movies"]]
        self.assertNotIn("Ancient", names)

    def test_trending_is_empty_without_recent_activity(self):
        response = Client().get(reverse("movie_detail", kwargs={"slug": self.movie.slug}))
        self.assertEqual(list(response.context["trending_movies"]), [])
        self.assertContains(response, "Nothing is trending right now")


# ---------------------------------------------------------------------------
# G. Regression and security
# ---------------------------------------------------------------------------

class RegressionTests(Base):
    def test_movie_list_and_detail_render_for_visitors_and_users(self):
        self.watched(self.alice)
        for client in (Client(), logged_in(self.alice)):
            self.assertEqual(client.get(reverse("movie_list")).status_code, 200)
            self.assertEqual(client.get(reverse("movie_detail", kwargs={"slug": self.movie.slug})).status_code, 200)

    def test_missing_movie_returns_404(self):
        self.assertEqual(Client().get(reverse("movie_detail", kwargs={"slug": "no-such-movie"})).status_code, 404)

    def test_delete_review_is_post_only_and_owner_only(self):
        self.watched(self.alice)
        review = Review.objects.create(movie=self.movie, user=self.alice, rating=3, body="Owner review text.")
        url = reverse("delete_review", kwargs={"review_id": review.id})
        logged_in(self.bob).post(url)
        self.assertTrue(Review.objects.filter(pk=review.pk).exists())
        logged_in(self.alice).get(url)
        self.assertTrue(Review.objects.filter(pk=review.pk).exists())
        logged_in(self.alice).post(url)
        self.assertFalse(Review.objects.filter(pk=review.pk).exists())

    def test_booking_flow_still_confirms_a_seat(self):
        theater = make_show(self.movie, timezone.now() + timedelta(days=4))
        client = logged_in(self.bob)
        client.post(reverse("hold_seat", args=[theater.id, theater.seats.get(seat_number="C5").id]))
        self.assertTrue(theater.seats.get(seat_number="C5").held_by_id == self.bob.id)
