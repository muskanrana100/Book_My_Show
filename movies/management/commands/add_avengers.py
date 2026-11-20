"""
Adds "The Avengers" (2012) to the catalogue, with two shows and optional watch history.

  python manage.py add_avengers                       # movie + one past show + one future show
  python manage.py add_avengers --watch-user alice    # also gives alice a past booking, so she can review

Safe to run more than once: nothing is duplicated.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from movies.models import Booking, Genre, Language, Movie, Payment, Seat, Theater

MOVIE_NAME = "The Avengers"
POSTER_PATH = "movies/635217f73e372771013edb4c-the-avengers-poster-marvel-movie-canvas1.jpg"
DESCRIPTION = (
    "Earth's mightiest heroes must come together and learn to fight as a team "
    "when Loki leads an army against humanity."
)
SEATS_PER_SHOW = 10


class Command(BaseCommand):
    help = "Adds The Avengers (2012) with two shows. Use --watch-user to allow that user to review it."

    def add_arguments(self, parser):
        parser.add_argument(
            "--watch-user",
            dest="watch_user",
            default=None,
            help="Username that has already 'watched' the film (gets a past booking, so can review).",
        )

    def handle(self, *args, **options):
        movie = self._get_or_create_movie()
        past_show, future_show = self._get_or_create_shows(movie)

        username = options["watch_user"]
        if username:
            self._give_watch_history(username, movie, past_show)

        self.stdout.write(self.style.SUCCESS(
            f"Ready: '{movie.name}' (movie id {movie.id}). "
            f"Past show id {past_show.id}, future show id {future_show.id}."
        ))
        self.stdout.write(f"Detail page: /movies/{movie.slug}/")

    def _get_or_create_movie(self):
        movie = Movie.objects.filter(name=MOVIE_NAME).first()
        if movie:
            self.stdout.write(f"Movie already exists: {movie.name}")
            return movie

        movie = Movie.objects.create(
            name=MOVIE_NAME,
            image=POSTER_PATH,
            description=DESCRIPTION,
            duration_minutes=143,
            age_certificate=Movie.CERTIFICATE_UA,
            release_date=date(2012, 5, 4),
        )
        movie.genres.set([
            Genre.objects.get_or_create(name="Action")[0],
            Genre.objects.get_or_create(name="Science Fiction")[0],
        ])
        movie.languages.set([Language.objects.get_or_create(name="English")[0]])
        self.stdout.write(f"Created movie: {movie.name}")
        return movie

    def _get_or_create_shows(self, movie):
        now = timezone.now()
        past_show = Theater.objects.filter(movie=movie, time__lt=now).first()
        future_show = Theater.objects.filter(movie=movie, time__gt=now).first()

        if past_show is None:
            past_show = self._create_show(movie, now - timedelta(days=7), "Past show")
        if future_show is None:
            future_show = self._create_show(movie, now + timedelta(days=3), "Upcoming show")
        return past_show, future_show

    def _create_show(self, movie, when, label):
        theater = Theater.objects.create(
            name="PVR Demo",
            city="Mumbai",
            movie=movie,
            screen="Screen 1",
            time=when,
            price_per_seat=Decimal("200.00"),
        )
        for number in range(1, SEATS_PER_SHOW + 1):
            Seat.objects.create(theater=theater, seat_number=f"A{number}")
        self.stdout.write(f"Created {label.lower()}: {when:%d %b %Y %H:%M} UTC")
        return theater

    def _give_watch_history(self, username, movie, past_show):
        User = get_user_model()
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"No user named '{username}'. Register the account on the site first.")

        if Booking.objects.filter(user=user, movie=movie, theater=past_show).exists():
            self.stdout.write(f"{username} already has watch history for this movie.")
            return

        seat = past_show.seats.filter(is_booked=False).first()
        if seat is None:
            raise CommandError("No free seat in the past show; cannot create the watch-history booking.")
        # A watch-history booking is a paid purchase, so it gets a successful payment record too.
        payment = Payment.objects.create(
            user=user,
            theater=past_show,
            amount=past_show.price_per_seat,
            status=Payment.STATUS_SUCCESS,
            razorpay_order_id=f"demo_order_avengers_{user.id}_{past_show.id}",
            razorpay_payment_id=f"demo_pay_avengers_{user.id}_{past_show.id}",
        )
        payment.seats.set([seat])
        Booking.objects.create(user=user, seat=seat, movie=movie, theater=past_show, payment=payment)
        seat.is_booked = True
        seat.save(update_fields=["is_booked"])
        self.stdout.write(self.style.SUCCESS(
            f"{username} now has a past booking for {movie.name}, so can write a review."
        ))
