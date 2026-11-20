import tempfile
import uuid
from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from movies import views
from movies.models import Booking, Movie, Review, Seat, Theater


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class DemoCatalogueTests(TestCase):
    def setUp(self):
        # Demo titles that the command may turn bookable (the real seed creates these).
        for i in range(1, 5):
            Movie.objects.create(name=f"Demo Movie {i}", slug=f"demo-movie-{i}", description="d",
                                 duration_minutes=100)
        self.user = User.objects.create_user("reviewer", "r@example.com", "Strong-pass-9")

    def run_command(self, *args):
        out = StringIO()
        call_command("setup_demo_catalog", *args, stdout=out)
        return out.getvalue()

    def test_command_creates_the_avengers_with_details(self):
        self.run_command()
        m = Movie.objects.get(name="The Avengers")
        self.assertEqual(m.duration_minutes, 143)
        self.assertEqual(m.age_certificate, "UA")
        self.assertEqual(m.genres.count(), 3)
        self.assertEqual(m.languages.count(), 2)
        self.assertEqual(m.movie_cast.count(), 6)
        self.assertEqual(m.gallery_images.count(), 2)

    def test_command_is_idempotent(self):
        self.run_command("--watched-user", "reviewer")
        counts = (Theater.objects.count(), Seat.objects.count(), Booking.objects.count(),
                  Movie.objects.get(name="The Avengers").movie_cast.count())
        self.run_command("--watched-user", "reviewer")
        after = (Theater.objects.count(), Seat.objects.count(), Booking.objects.count(),
                 Movie.objects.get(name="The Avengers").movie_cast.count())
        self.assertEqual(counts, after)

    def test_every_show_has_the_full_hundred_seat_layout(self):
        self.run_command()
        for theater in Theater.objects.filter(movie__name="The Avengers"):
            self.assertEqual(theater.seats.count(), 100)

    def test_five_titles_have_bookable_shows(self):
        self.run_command("--bookable-titles", "5")
        now = timezone.now()
        titles_with_free_seats = set(
            Theater.objects.filter(time__gt=now, seats__is_booked=False)
            .values_list("movie__name", flat=True)
        )
        self.assertGreaterEqual(len(titles_with_free_seats), 5)

    def test_trailer_id_is_validated(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self.run_command("--trailer-id", "not-a-valid-id!!")

    def test_trailer_is_not_set_unless_given(self):
        self.run_command()
        self.assertEqual(Movie.objects.get(name="The Avengers").trailer_youtube_id, "")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class BookingAcrossTitlesTests(TestCase):
    def setUp(self):
        for i in range(1, 5):
            Movie.objects.create(name=f"Demo Movie {i}", slug=f"demo-movie-{i}", description="d",
                                 duration_minutes=100)
        call_command("setup_demo_catalog", "--bookable-titles", "5", stdout=StringIO())
        self.user = User.objects.create_user("booker", "b@example.com", "Strong-pass-9")
        self.client_user = Client()
        self.client_user.force_login(self.user)

    def book_two_seats(self, theater):
        seats = list(Seat.objects.filter(theater=theater, is_booked=False).order_by("id")[:2])
        for seat in seats:
            r = self.client_user.post(f"/movies/theater/{theater.id}/seats/{seat.id}/hold/")
            self.assertEqual(r.status_code, 200)
        order_id = f"order_{uuid.uuid4().hex[:12]}"
        fake = MagicMock()
        fake.order.create.return_value = {"id": order_id}
        with patch.object(views, "get_razorpay_client", return_value=fake):
            r = self.client_user.post(f"/movies/theater/{theater.id}/payment/create/")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(fake.order.create.call_args[0][0]["amount"], 2 * 200 * 100)
            fake.utility.verify_payment_signature.return_value = None
            r = self.client_user.post("/movies/payment/verify/", {
                "razorpay_order_id": order_id,
                "razorpay_payment_id": f"pay_{uuid.uuid4().hex[:12]}",
                "razorpay_signature": "sig",
            })
            self.assertEqual(r.status_code, 200)
        return seats, order_id

    def test_the_avengers_seats_book_and_produce_a_ticket(self):
        theater = Theater.objects.filter(movie__name="The Avengers", time__gt=timezone.now(),
                                         seats__is_booked=False).distinct().order_by("time").first()
        seats, order_id = self.book_two_seats(theater)
        self.assertEqual(Seat.objects.filter(id__in=[s.id for s in seats], is_booked=True).count(), 2)
        self.assertEqual(Booking.objects.filter(payment__razorpay_order_id=order_id).count(), 2)
        booking = Booking.objects.filter(payment__razorpay_order_id=order_id).first()
        r = self.client_user.get(f"/movies/tickets/download/{booking.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")

    def test_a_booked_seat_cannot_be_held_by_another_user(self):
        theater = Theater.objects.filter(movie__name="The Avengers", time__gt=timezone.now(),
                                         seats__is_booked=False).distinct().order_by("time").first()
        seats, _ = self.book_two_seats(theater)
        other = User.objects.create_user("other", "o@example.com", "Strong-pass-9")
        c = Client()
        c.force_login(other)
        r = c.post(f"/movies/theater/{theater.id}/seats/{seats[0].id}/hold/")
        self.assertEqual(r.status_code, 409)

    def test_each_demo_title_books_two_seats(self):
        now = timezone.now()
        titles = Movie.objects.filter(theaters__time__gt=now, theaters__seats__is_booked=False).distinct()[:5]
        self.assertEqual(len(titles), 5)
        for movie in titles:
            theater = (Theater.objects.filter(movie=movie, time__gt=now, seats__is_booked=False)
                       .distinct().order_by("time").first())
            seats, order_id = self.book_two_seats(theater)
            self.assertEqual(Booking.objects.filter(payment__razorpay_order_id=order_id).count(), 2)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ReviewLifecycleTests(TestCase):
    def setUp(self):
        User.objects.create_user("reviewer", "r@example.com", "Strong-pass-9")
        call_command("setup_demo_catalog", "--watched-user", "reviewer", stdout=StringIO())
        self.user = User.objects.get(username="reviewer")
        self.movie = Movie.objects.get(name="The Avengers")
        self.client_user = Client()
        self.client_user.force_login(self.user)
        self.url = f"/movies/{self.movie.slug}/"

    def test_add_review_after_watching(self):
        r = self.client_user.post(self.url, {"submit_review": "1", "rating": 5,
                                             "title": "Great", "body": "Loved it."})
        self.assertEqual(r.status_code, 302)
        review = Review.objects.get(user=self.user, movie=self.movie)
        self.assertEqual(review.rating, 5)
        self.assertTrue(review.is_verified_viewer)

    def test_update_review(self):
        self.client_user.post(self.url, {"submit_review": "1", "rating": 5, "title": "Great", "body": "Enjoyed the story."})
        self.client_user.post(self.url, {"submit_review": "1", "rating": 3, "title": "Changed", "body": "Enjoyed the story."})
        review = Review.objects.get(user=self.user, movie=self.movie)
        self.assertEqual((review.rating, review.title, review.is_edited), (3, "Changed", True))
        self.assertEqual(Review.objects.filter(user=self.user, movie=self.movie).count(), 1)

    def test_delete_review_updates_average(self):
        self.client_user.post(self.url, {"submit_review": "1", "rating": 4, "title": "t", "body": "Enjoyed the story."})
        review = Review.objects.get(user=self.user, movie=self.movie)
        self.client_user.post(f"/movies/review/{review.id}/delete/")
        self.assertFalse(Review.objects.filter(pk=review.pk).exists())
        self.movie.refresh_from_db()
        self.assertEqual(self.movie.total_ratings, 0)

    def test_anonymous_visitor_cannot_review(self):
        r = Client().post(self.url, {"submit_review": "1", "rating": 5, "title": "t", "body": "Enjoyed the story."})
        self.assertEqual(Review.objects.filter(movie=self.movie).count(), 0)

    def test_user_without_watched_booking_cannot_review(self):
        other = User.objects.create_user("nowatch", "n@example.com", "Strong-pass-9")
        c = Client()
        c.force_login(other)
        c.post(self.url, {"submit_review": "1", "rating": 5, "title": "t", "body": "Enjoyed the story."})
        self.assertFalse(Review.objects.filter(user=other).exists())
