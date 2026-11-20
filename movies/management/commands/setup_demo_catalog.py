"""
Completes "The Avengers" as a demo title and makes sure it has bookable shows.

Safe to run more than once: nothing is duplicated.

What it does
- Fills in the movie details (description, duration, release date, certificate, genres,
  languages, cast, and two gallery posters) if they are missing.
- Makes sure there are future shows (bookable) and one past show, and that every show has the
  full 100-seat layout (rows A to J, seats 1 to 10).
- Optionally creates a confirmed booking for one user on the past show. A booking on a show that
  has already happened is what allows that user to write a review (the site's rule).

Usage (from the project root)
    python manage.py setup_demo_catalog
    python manage.py setup_demo_catalog --watched-user alice
    python manage.py setup_demo_catalog --trailer-id dQw4w9WgXcQ
"""
import io
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from movies.models import (
    CastMember, Booking, Genre, Language, Movie, MovieCast, MoviePoster, Payment, Seat, Theater,
)

MOVIE_NAME = "The Avengers"
DESCRIPTION = (
    "Earth's mightiest heroes must come together and learn to fight as a team when an "
    "unexpected enemy threatens the world. Nick Fury assembles Iron Man, Captain America, "
    "Thor, the Hulk, Black Widow and Hawkeye to stop Loki and his alien army."
)
GENRES = ["Action", "Sci-Fi", "Adventure"]
LANGUAGES = ["English", "Hindi"]
CAST = [
    ("Robert Downey Jr.", "Tony Stark", "Iron Man"),
    ("Chris Evans", "Steve Rogers", "Captain America"),
    ("Scarlett Johansson", "Natasha Romanoff", "Black Widow"),
    ("Chris Hemsworth", "Thor", "Thor"),
    ("Mark Ruffalo", "Bruce Banner", "Hulk"),
    ("Jeremy Renner", "Clint Barton", "Hawkeye"),
]
ROWS = "ABCDEFGHIJ"
SEATS_PER_ROW = 10
PRICE = Decimal("200.00")
SHOW_HOUR = 19  # 7 PM local time


def _poster_bytes(title_line, subtitle, top_color, bottom_color):
    """Draws a simple poster with PIL so the gallery has real images (no external download)."""
    from PIL import Image, ImageDraw, ImageFont

    width, height = 600, 900
    img = Image.new("RGB", (width, height), top_color)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / height
        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * t)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * t)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    try:
        big = ImageFont.truetype("arialbd.ttf", 72)
        small = ImageFont.truetype("arial.ttf", 34)
    except OSError:
        big = ImageFont.load_default()
        small = ImageFont.load_default()
    draw.text((width // 2, 380), title_line, fill="white", font=big, anchor="mm")
    draw.text((width // 2, 480), subtitle, fill="#f2f2f2", font=small, anchor="mm")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


class Command(BaseCommand):
    help = (
        "Completes The Avengers and makes sure it has bookable shows with full seat maps. "
        "Optionally creates a watched booking so one user can review the film."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--watched-user",
            default=None,
            help="Username that gets a confirmed booking on the past show (so it can write a review).",
        )
        parser.add_argument(
            "--trailer-id",
            default=None,
            help="YouTube video ID (11 characters) for the trailer. Left unchanged if omitted.",
        )
        parser.add_argument(
            "--bookable-titles",
            type=int,
            default=5,
            help="Number of titles that must have a future show with free seats (default: 5). "
                 "The Avengers is always one of them.",
        )
        parser.add_argument(
            "--future-shows",
            type=int,
            default=2,
            help="Number of bookable shows in the coming days (default: 2).",
        )

    def handle(self, *args, **options):
        if options["trailer_id"] and not re.fullmatch(r"[A-Za-z0-9_-]{11}", options["trailer_id"]):
            raise CommandError("--trailer-id must be exactly 11 letters, digits, '-' or '_'.")
        if options["bookable_titles"] < 1:
            raise CommandError("--bookable-titles must be at least 1.")
        if options["future_shows"] < 1:
            raise CommandError("--future-shows must be at least 1.")

        with transaction.atomic():
            movie = self._ensure_movie(options["trailer_id"])
            self._ensure_gallery(movie)
            shows = self._ensure_shows(movie, options["future_shows"])
            bookable = self._ensure_bookable_titles(movie, options["bookable_titles"])
            watched = None
            if options["watched_user"]:
                watched = self._ensure_watched_booking(movie, shows["past"], options["watched_user"])

        self._report(movie, shows, watched, bookable)

    # ------------------------------------------------------------------ movie
    def _ensure_movie(self, trailer_id):
        movie, created = Movie.objects.get_or_create(
            name=MOVIE_NAME,
            defaults={
                "slug": slugify(MOVIE_NAME),
                "image": "movies/635217f73e372771013edb4c-the-avengers-poster-marvel-movie-canvas1.jpg",
            },
        )
        movie.description = DESCRIPTION
        movie.duration_minutes = 143
        movie.release_date = date(2012, 5, 4)
        movie.age_certificate = "UA"
        if trailer_id:
            movie.trailer_youtube_id = trailer_id
        movie.save()

        movie.genres.set([Genre.objects.get_or_create(name=g)[0] for g in GENRES])
        movie.languages.set([Language.objects.get_or_create(name=l)[0] for l in LANGUAGES])

        for order, (actor, character, _) in enumerate(CAST, start=1):
            member, _ = CastMember.objects.get_or_create(name=actor)
            MovieCast.objects.update_or_create(
                movie=movie,
                cast_member=member,
                role="Actor",
                defaults={"character_name": character, "order": order},
            )
        return movie

    def _ensure_gallery(self, movie):
        if movie.gallery_images.count() >= 2:
            return
        movie.gallery_images.all().delete()
        posters = [
            ("THE AVENGERS", "Assemble", (20, 36, 80), (5, 8, 20), "Main poster"),
            ("THE AVENGERS", "Earth's Mightiest Heroes", (120, 20, 20), (20, 4, 4), "Teaser poster"),
        ]
        for order, (line1, line2, top, bottom, caption) in enumerate(posters, start=1):
            path = default_storage.save(
                f"movies/gallery/avengers-{order}.png",
                ContentFile(_poster_bytes(line1, line2, top, bottom)),
            )
            MoviePoster.objects.create(movie=movie, image=path, caption=caption, order=order)

    # ------------------------------------------------------------------ shows
    def _ensure_shows(self, movie, future_count):
        now = timezone.localtime(timezone.now())
        future_times = []
        for i in range(future_count):
            day = (now + timedelta(days=2 + 3 * i)).date()
            future_times.append(self._show_datetime(day))
        past_day = (now - timedelta(days=6)).date()
        past_time = self._show_datetime(past_day)

        future = [self._ensure_show(movie, t) for t in future_times]
        past = self._ensure_show(movie, past_time)
        # Bring older shows of this title (created by earlier setups) to the full seat layout too.
        for theater in movie.theaters.all():
            self._ensure_seats(theater)
        return {"future": future, "past": past}

    def _show_datetime(self, day):
        local = timezone.get_current_timezone()
        naive = datetime(day.year, day.month, day.day, SHOW_HOUR, 0)
        return timezone.make_aware(naive, local)

    def _ensure_show(self, movie, start_time):
        theater = Theater.objects.filter(movie=movie, time=start_time).first()
        if theater is None:
            theater = Theater.objects.create(
                name="PVR Demo",
                city="Mumbai",
                movie=movie,
                screen="Screen 1",
                time=start_time,
                price_per_seat=PRICE,
            )
        self._ensure_seats(theater)
        return theater

    def _ensure_seats(self, theater):
        existing = set(theater.seats.values_list("seat_number", flat=True))
        missing = [
            Seat(theater=theater, seat_number=f"{row}{num}")
            for row in ROWS
            for num in range(1, SEATS_PER_ROW + 1)
            if f"{row}{num}" not in existing
        ]
        if missing:
            Seat.objects.bulk_create(missing)

    def _ensure_bookable_titles(self, movie, count):
        """
        The seeded shows are fully booked, so other demo titles would have no seats to sell.
        For each chosen title without a future show that has free seats, add one 100-seat show.
        """
        now = timezone.localtime(timezone.now())
        others = Movie.objects.exclude(pk=movie.pk).order_by("id")[: max(count - 1, 0)]
        titles = [movie, *others]
        result = []
        for title in titles:
            free_show = (
                Theater.objects.filter(movie=title, time__gt=timezone.now(), seats__is_booked=False)
                .order_by("time")
                .first()
            )
            if free_show is None:
                day_offset = 2
                start = self._show_datetime((now + timedelta(days=day_offset)).date())
                while Theater.objects.filter(movie=title, time=start).exists():
                    day_offset += 1
                    start = self._show_datetime((now + timedelta(days=day_offset)).date())
                free_show = Theater.objects.create(
                    name="PVR Demo",
                    city="Mumbai",
                    movie=title,
                    screen="Screen 1",
                    time=start,
                    price_per_seat=PRICE,
                )
                self._ensure_seats(free_show)
            result.append((title, free_show))
        return result

    # ------------------------------------------------------------ watched booking
    def _ensure_watched_booking(self, movie, past_theater, username):
        User = get_user_model()
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"No user named '{username}'. Register that account first.")

        # Any earlier watched booking for this title counts, even if the "past show" has moved on
        # since the last run. Otherwise a second run would try to add a duplicate payment.
        already = Booking.objects.filter(
            user=user,
            movie=movie,
            is_cancelled=False,
            theater__time__lt=timezone.now(),
        ).first()
        if already:
            return already

        seat = (
            past_theater.seats.filter(bookings__isnull=True)
            .order_by("seat_number")
            .first()
        )
        if seat is None:
            raise CommandError("No free seat on the past show to attach the watched booking to.")

        payment = Payment.objects.create(
            user=user,
            theater=past_theater,
            amount=PRICE,
            status=Payment.STATUS_SUCCESS,
            razorpay_order_id=f"demo_order_{movie.id}_{user.id}_{past_theater.id}",
            razorpay_payment_id=f"demo_pay_{movie.id}_{user.id}_{past_theater.id}",
        )
        payment.seats.set([seat])
        booking = Booking.objects.create(
            user=user,
            seat=seat,
            movie=movie,
            theater=past_theater,
            payment=payment,
        )
        seat.is_booked = True
        seat.save(update_fields=["is_booked"])
        return booking

    # ------------------------------------------------------------------ report
    def _report(self, movie, shows, watched, bookable):
        self.stdout.write(self.style.SUCCESS(f"'{movie.name}' is ready (slug: {movie.slug})."))
        self.stdout.write(
            f"  Details: {movie.duration_minutes} min, {movie.age_certificate}, "
            f"released {movie.release_date}, {movie.genres.count()} genres, "
            f"{movie.languages.count()} languages, {movie.movie_cast.count()} cast, "
            f"{movie.gallery_images.count()} posters."
        )
        trailer = movie.trailer_youtube_id or "(not set: add with --trailer-id)"
        self.stdout.write(f"  Trailer: {trailer}")
        for t in shows["future"]:
            free = t.seats.filter(is_booked=False).count()
            self.stdout.write(f"  Bookable show {t.id}: {timezone.localtime(t.time):%a %d %b %H:%M}, "
                              f"{free} seats free.")
        past = shows["past"]
        self.stdout.write(f"  Past show {past.id}: {timezone.localtime(past.time):%a %d %b %H:%M} "
                          f"(for reviews).")
        self.stdout.write(self.style.SUCCESS(f"  Bookable titles ({len(bookable)}):"))
        for title, show in bookable:
            free = show.seats.filter(is_booked=False).count()
            self.stdout.write(f"    {title.name}: show {show.id} on {timezone.localtime(show.time):%a %d %b %H:%M}, "
                              f"{free} seats free")
        if watched:
            self.stdout.write(self.style.SUCCESS(
                f"  Watched booking for '{watched.user.username}' on the past show (booking {watched.id}). "
                f"That user can now write a review."))
