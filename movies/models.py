from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.templatetags.static import static
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from datetime import timedelta
from decimal import ROUND_HALF_UP

from .validators import POSTER_VALIDATORS, YOUTUBE_ID_RE, validate_trailer_reference


PLACEHOLDER_POSTER = "img/poster-placeholder.svg"


class BookingCancellationError(Exception):
    """Raised when a booking cannot be cancelled. The message is safe to show the user."""


class Genre(models.Model):
    name = models.CharField(max_length =100, unique = True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Language(models.Model):
    name = models.CharField(max_length = 100, unique =True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class CastMember(models.Model):
    ROLE_ACTOR = 'actor'
    ROLE_DIRECTOR = "director"
    ROLE_PRODUCER = 'producer'
    ROLE_CHOICES = [ (ROLE_ACTOR, 'Actor'), (ROLE_DIRECTOR, 'Director'), (ROLE_PRODUCER, 'Producer'),]

    name = models.CharField(max_length = 150)
    photo= models.ImageField(upload_to= "cast/", blank =True, null= True)
    bio = models.TextField(blank =True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Movie(models.Model):
    CERTIFICATE_U = 'U'
    CERTIFICATE_UA = 'UA'
    CERTIFICATE_A = 'A'
    CERTIFICATE_S = 'S'
    CERTIFICATE_CHOICES = [(CERTIFICATE_U, 'U - Universal'), (CERTIFICATE_UA, 'UA - Parental Guidance'), (CERTIFICATE_A, 'A - Adults Only'),(CERTIFICATE_S, 'S - Special'),]

    name = models.CharField(max_length =255)
    slug = models.SlugField(max_length = 280, unique = True, blank = True)
    image = models.ImageField(
        upload_to="movies/", validators=POSTER_VALIDATORS,
        help_text="Primary poster shown on listing cards. JPEG, PNG or WebP, up to 5 MB.",
    )
    description = models.TextField(blank =True, null=True)

    genres = models.ManyToManyField(Genre, related_name = "movies", blank = True)
    languages = models.ManyToManyField(Language, related_name ="movies", blank = True)
    cast_members = models.ManyToManyField(
        CastMember, through = "MovieCast", related_name  = "movies", blank = True
    )

    duration_minutes = models.PositiveIntegerField(
        default=120,
        validators=[MinValueValidator(1), MaxValueValidator(600)],
        help_text="Runtime in minutes (1 to 600).",
    )
    age_certificate = models.CharField(max_length = 2, choices = CERTIFICATE_CHOICES, default = CERTIFICATE_UA)
    release_date = models.DateField(default = timezone.now)
    is_published = models.BooleanField(
        default=True, db_index=True,
        help_text="Unpublished movies are hidden from listings, details and recommendations.",
    )

    trailer_youtube_id = models.CharField(
        max_length = 11, blank = True, validators =  [validate_trailer_reference] ,
        help_text = "Paste a YouTube link (youtube.com/watch?v=..., youtu.be/...) or the 11-character video ID."
    )

    average_rating = models.DecimalField(max_digits = 3 , decimal_places = 1 , default = Decimal('0.0') , editable = False)
    total_ratings = models.PositiveIntegerField(default = 0 , editable = False)

    created_at = models.DateTimeField(auto_now_add =  True)
    updated_at = models.DateTimeField(auto_now =  True)

    class Meta:
        ordering = ["-release_date"]
        indexes = [
            models.Index(fields=["is_published", "release_date"], name="movie_published_release_idx"),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            candidate = base_slug
            counter = 1
            while Movie.objects.filter(slug = candidate).exclude(pk = self.pk).exists():
                counter  += 1
                candidate  = f'{base_slug}-{counter}'
            self.slug = candidate
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("movie_detail", kwargs={"slug" :  self.slug})

    @property
    def trailer_embed_url(self):
        """
        youtube-nocookie embed URL built only from a validated 11-character video ID.
        User-supplied text is never placed in the URL or in the page as HTML.
        """
        if not self.trailer_youtube_id or not YOUTUBE_ID_RE.match(self.trailer_youtube_id):
            return ''
        return f'https://www.youtube-nocookie.com/embed/{self.trailer_youtube_id}'

    @property
    def poster_url(self):
        """
        URL of the primary poster. Falls back to a placeholder when the image is missing, so a
        movie without a usable file still renders a card.
        """
        try:
            if self.image and self.image.storage.exists(self.image.name):
                return self.image.url
        except Exception:  # storage unavailable: show the placeholder rather than fail the page
            pass
        return static(PLACEHOLDER_POSTER)

    @property
    def duration_display (self):
        hours, minutes = divmod(self.duration_minutes, 60)
        if hours:
            return f'{hours}h {minutes}m'
        return f'{minutes}m'

    def recalculate_rating(self):
        """
        Recomputes average_rating and total_ratings from visible reviews only (hidden reviews
        are excluded). The movie row is locked so that concurrent review changes are applied one
        after another and the stored values cannot be left stale.
        """
        with transaction.atomic():
            Movie.objects.select_for_update().filter(pk=self.pk).exists()
            stats = self.reviews.filter(is_hidden=False).aggregate(
                avg=models.Avg('rating'), count=models.Count('id')
            )
            average = Decimal(str(stats["avg"] or 0)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            self.average_rating = min(average, Decimal("5.0"))
            self.total_ratings = stats["count"] or 0
            self.save(update_fields=["average_rating", "total_ratings"])

    def similar_movies(self, limit=8):
        """Movies sharing at least one genre or language, best matches first."""
        genre_ids = list(self.genres.values_list('id', flat=True))
        language_ids = list(self.languages.values_list('id', flat=True))
        if not genre_ids and not language_ids:
            return Movie.objects.none()
        return (
            Movie.objects
            .filter(Q(genres__in=genre_ids) | Q(languages__in=language_ids))
            .exclude(id=self.id)
            .filter(is_published=True, release_date__lte=timezone.localdate())
            .annotate(
                genre_matches=models.Count('genres', filter=Q(genres__in=genre_ids), distinct=True),
                language_matches=models.Count('languages', filter=Q(languages__in=language_ids), distinct=True),
            )
            .distinct()
            .order_by('-genre_matches', '-language_matches', '-average_rating')[:limit]
        )


class MovieCast(models.Model):
    movie = models.ForeignKey (Movie , on_delete = models.CASCADE, related_name ="movie_cast")
    cast_member = models.ForeignKey(CastMember, on_delete=models.CASCADE, related_name = "movie_roles")
    role = models.CharField (max_length = 20, choices=CastMember.ROLE_CHOICES, default=CastMember.ROLE_ACTOR)
    character_name = models.CharField (max_length =150, blank= True)
    order = models.PositiveIntegerField (default =0, help_text ='Lower numbers appear first (billing order).')

    class Meta:
        ordering = ["order"]
        unique_together = ("movie", "cast_member", "role")

    def __str__(self):
        return f'{self.cast_member.name} in {self.movie.name}'


class MoviePoster(models.Model):
    movie = models.ForeignKey(Movie, on_delete = models.CASCADE, related_name ="gallery_images")
    image = models.ImageField(upload_to="movies/gallery/", validators=POSTER_VALIDATORS)
    caption = models.CharField(max_length = 150, blank = True)
    order  = models.PositiveIntegerField(default = 0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f'Gallery image for {self.movie.name}'

    @property
    def image_url(self):
        try:
            if self.image and self.image.storage.exists(self.image.name):
                return self.image.url
        except Exception:
            pass
        return static(PLACEHOLDER_POSTER)


class Theater(models.Model):
    name = models.CharField (max_length = 255)
    city = models.CharField (max_length = 100, blank = True, db_index = True)
    movie = models.ForeignKey (Movie, on_delete=models.CASCADE, related_name="theaters")
    screen = models.CharField(max_length=100, default="Screen 1")
    time = models.DateTimeField()
    price_per_seat = models.DecimalField(
        max_digits=7, decimal_places=2, default=200,
        validators=[MinValueValidator(Decimal("1.00")), MaxValueValidator(Decimal("100000.00"))],
    )

    class Meta:
        ordering = ["time"]
        indexes = [
            models.Index(fields=["city", "time"]),
        ]

    def __str__(self):
        return f'{self.name} ({self.city}) - {self.movie.name} at {self.time}'

    @property
    def end_time(self):
        """Show end: start time plus the movie's runtime."""
        return self.time + timedelta(minutes=self.movie.duration_minutes or 0)

    def clean(self):
        super().clean()
        if self.time is None or self.movie_id is None:
            return
        if not self.screen:
            raise ValidationError({"screen": "Enter the screen name."})
        # A conflict is another show on the same screen of the same theater that overlaps in time.
        window_start = self.time - timedelta(days=1)
        candidates = (
            Theater.objects.filter(
                name=self.name, city=self.city, screen=self.screen,
                time__gte=window_start, time__lt=self.end_time,
            )
            .exclude(pk=self.pk)
            .select_related("movie")
        )
        for other in candidates:
            if other.time < self.end_time and self.time < other.end_time:
                raise ValidationError(
                    f"This show overlaps '{other}' on the same screen. Choose a different start time or screen."
                )

    def ensure_seat_layout(self, rows="ABCDEFGHIJ", seats_per_row=10):
        """
        Creates the standard seat layout (rows A to J, seats 1 to 10) for a show that has none.
        Safe to call again: existing seats are kept and nothing is duplicated.
        """
        existing = set(self.seats.values_list("seat_number", flat=True))
        new = [
            Seat(theater=self, seat_number=f"{row}{number}")
            for row in rows
            for number in range(1, seats_per_row + 1)
            if f"{row}{number}" not in existing
        ]
        if new:
            Seat.objects.bulk_create(new)
        return len(new)


class Seat(models.Model):
    HOLD_DURATION = timedelta(minutes = 2)

    theater = models.ForeignKey (Theater, on_delete = models.CASCADE, related_name = "seats")
    seat_number = models.CharField (max_length = 10)
    is_booked = models.BooleanField (default = False)
    held_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete = models.SET_NULL,
        null = True,   blank = True, related_name = "held_seats"
    )
    held_until = models.DateTimeField(null= True, blank = True)

    class Meta:
        ordering = ["seat_number"]
        indexes = [
            models.Index(fields=["theater", "is_booked"], name="seat_theater_booked_idx"),
            models.Index(fields=["theater", "held_until"], name="seat_theater_hold_idx"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["theater", "seat_number"], name="unique_seat_per_show"),
        ]

    def __str__(self):
        return f'{self.seat_number} in {self.theater.name}'

    def is_hold_active(self):
        return self.held_until is not None and self.held_until > timezone.now()
    
    def is_held_by(self,user):
        return self.is_hold_active() and user.is_authenticated and self.held_by_id == user.id
    
    def status_for(self,user):
        if self.is_booked:
            return 'booked'
        if self.is_hold_active():
            return 'held_by_you' if self.is_held_by(user) else 'held'
        return 'available'


class Booking(models.Model):
    REFUND_NONE = 'none'
    REFUND_PROCESSING = 'processing'
    REFUND_REFUNDED = 'refunded'
    REFUND_FAILED = 'failed'
    REFUND_STATUS_CHOICES =[
        (REFUND_NONE, 'None'),
        (REFUND_PROCESSING, 'Processing'),
        (REFUND_REFUNDED, 'Refunded'),
        (REFUND_FAILED, 'Failed'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='bookings')
    seat = models.ForeignKey(Seat, on_delete=models.CASCADE, related_name='bookings')
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='bookings')
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE, related_name='bookings')
    payment = models.ForeignKey(
        'Payment', on_delete= models.SET_NULL, null= True, related_name = 'bookings')
    booked_at = models.DateTimeField(auto_now_add=True)

    is_cancelled = models.BooleanField(default= False)
    cancelled_at = models.DateTimeField(null = True, blank=True)
    refund_id =models.CharField(max_length=100,blank=True)
    refund_amount = models.DecimalField(max_digits=9 , decimal_places=2, null=True, blank= True)
    refund_status = models.CharField(max_length=20,choices=REFUND_STATUS_CHOICES, default= REFUND_NONE)

    class Meta:
        permissions = [
            ("view_analytics", "Can view business analytics dashboard and exports"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['seat'],
                condition=Q(is_cancelled=False),
                name='unique_active_booking_per_seat',
            ),
        ]
        indexes = [
            models.Index(
                fields=["booked_at"],
                name="booking_booked_at_idx"
            ),

            models.Index(
                fields=['booked_at', 'is_cancelled'],
                name='booking_date_cancel_idx'
            ),

            models.Index(
                fields=['movie', 'booked_at'],
                name='booking_movie_date_idx'
            ),

            models.Index(
                fields=['theater', 'booked_at'],
                name='booking_theater_date_idx'
            ),
        ]

    def __str__(self):
        return (
            f'Booking by {self.user.username} '
            f'for {self.seat.seat_number} at {self.theater.name}'
        )

    def cancel_and_refund(self, razorpay_client):
        """
        Cancels this booking and refunds its share of the original payment.

        Rules:
        - A booking for a show that has already started cannot be cancelled.
        - If the Razorpay refund fails, the booking stays ACTIVE (the money is
          still with the customer's booking) and BookingCancellationError is raised.
        - Only after a successful refund (or when there is nothing to refund)
          is the booking cancelled and the seat released.
        """
        refund_error = None

        with transaction.atomic():
            locked = (
                Booking.objects.select_for_update()
                .select_related('theater', 'payment', 'seat')
                .get(pk=self.pk)
            )
            if locked.is_cancelled:
                return locked

            if locked.theater.time <= timezone.now():
                raise BookingCancellationError(
                    'This show has already started, so the booking can no longer be cancelled.'
                )

            if locked.payment_id and locked.payment.razorpay_payment_id:
                refund_amount_rupees = locked.theater.price_per_seat
                try:
                    refund = razorpay_client.payment.refund(
                        locked.payment.razorpay_payment_id,
                        {
                            'amount': int(refund_amount_rupees * 100),
                            'notes': {'booking_id': str(locked.id)},
                        },
                    )
                    locked.refund_id = refund['id']
                    locked.refund_amount = refund_amount_rupees
                    locked.refund_status = Booking.REFUND_REFUNDED
                except Exception:
                    refund_error = (
                        'The refund could not be processed right now, so your booking '
                        'is still active. Please try again shortly.'
                    )
                    locked.refund_status = Booking.REFUND_FAILED
                    locked.save(update_fields=['refund_status'])
                    # Do not raise inside the atomic block: keep the FAILED status.
                    # The error is raised below, after the transaction commits.

            if refund_error is None:
                locked.is_cancelled = True
                locked.cancelled_at = timezone.now()
                locked.save(update_fields=[
                    'is_cancelled', 'cancelled_at', 'refund_id',
                    'refund_amount', 'refund_status',
                ])
                # Seat becomes bookable again through the partial unique constraint.
                Seat.objects.filter(pk=locked.seat_id).update(is_booked=False)

        if refund_error:
            raise BookingCancellationError(refund_error)

        return locked


class _SeatsTaken(Exception):
    """Raised inside Payment settlement when a seat can no longer be claimed."""


class Payment(models.Model):
    STATUS_CREATED = 'created'
    STATUS_SUCCESS = 'success'
    STATUS_FAILED = 'failed'
    STATUS_CANCELLED = 'cancelled'
    STATUS_REFUNDED = 'refunded'
    # Two-phase settlement: the seat decision is committed first, then the refund is issued
    # outside any seat lock. REFUND_PENDING = refund still to issue; REFUND_IN_PROGRESS = claimed by
    # one worker, outcome not yet recorded (needs manual reconciliation if it stays that way).
    STATUS_REFUND_PENDING = 'refund_pending'
    STATUS_REFUND_IN_PROGRESS = 'refund_in_progress'
    # Statuses that a failure or cancellation callback must never overwrite.
    SETTLED_STATUSES = (STATUS_SUCCESS, STATUS_REFUNDED, STATUS_REFUND_PENDING, STATUS_REFUND_IN_PROGRESS)
    STATUS_CHOICES = [
        (STATUS_CREATED, 'Created'),
        (STATUS_SUCCESS, 'Success'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_REFUNDED, 'Refunded'),
        (STATUS_REFUND_PENDING, 'Refund pending'),
        (STATUS_REFUND_IN_PROGRESS, 'Refund in progress'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='payments')
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE, related_name='payments')
    seats = models.ManyToManyField(Seat, related_name='payments')

    amount = models.DecimalField(max_digits=9, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_CREATED)

    # Null until the provider order exists. The internal record is created first, so a crash
    # before the provider call leaves a traceable record rather than nothing.
    razorpay_order_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    provider = models.CharField(max_length=20, default='razorpay')
    currency = models.CharField(max_length=3, default='INR')
    succeeded_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    razorpay_payment_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    razorpay_signature = models.CharField(max_length=225, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    refund_id = models.CharField(max_length=100, blank=True)

    TICKET_EMAIL_PENDING = "pending"
    TICKET_EMAIL_SENDING = "sending"
    TICKET_EMAIL_SENT = "sent"

    TICKET_EMAIL_STATUS_CHOICES = [
        (TICKET_EMAIL_PENDING, "Pending"),
        (TICKET_EMAIL_SENDING, "Sending"),
        (TICKET_EMAIL_SENT, "Sent"),
    ]

    ticket_email_status = models.CharField(
        max_length=20,
        choices=TICKET_EMAIL_STATUS_CHOICES,
        default=TICKET_EMAIL_PENDING,
        db_index=True,
    )
    ticket_email_sent_at = models.DateTimeField(null=True, blank=True)
    ticket_email_started_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['created_at'], name='payment_created_at_idx'),
            models.Index(fields=['created_at', 'status'], name='payment_date_status_idx'),
        ]

    def __str__(self):
        return (
            f'Payment {self.razorpay_order_id} '
            f'({self.status}) by {self.user.username}'
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _release_holds_for_owner(self):
        """
        Releases only the seat holds that belong to this payment's user.
        A seat that has since been taken over by another user's hold (after
        this user's hold expired) is deliberately left alone.
        """
        Seat.objects.filter(
            payments=self,
            is_booked=False,
            held_by_id=self.user_id,
        ).update(held_by=None, held_until=None)

    def _refund_in_full(self, razorpay_client, razorpay_payment_id):
        """
        Attempts a full refund of this payment. Returns the refund id, or None
        when no client is available or Razorpay rejects the refund.
        """
        if razorpay_client is None or not razorpay_payment_id:
            return None
        try:
            refund = razorpay_client.payment.refund(
                razorpay_payment_id,
                {
                    'amount': int(self.amount * 100),
                    'notes': {'payment_id': str(self.id)},
                },
            )
            return refund['id']
        except Exception:
            return None

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def save(self, *args, **kwargs):
     now = timezone.now()
     update_fields = kwargs.get("update_fields")
     if self.status == Payment.STATUS_SUCCESS and self.succeeded_at is None:
         self.succeeded_at = now
         if update_fields is not None:
             kwargs["update_fields"] = list(update_fields) + ["succeeded_at"]
     if self.status == Payment.STATUS_FAILED and self.failed_at is None:
         self.failed_at = now
         if update_fields is not None:
             kwargs["update_fields"] = list(update_fields) + ["failed_at"]
     super().save(*args, **kwargs)
     
    def mark_success(self, razorpay_payment_id, razorpay_signature, razorpay_client=None):
        """
        Settles a captured payment. Called from the browser callback and the webhook; either or both
        may run, in any order, and repeats are harmless.

        Two phases, so no seat lock is held during the refund network call:
        1. A short transaction decides the outcome. If every seat can be claimed, the payment becomes
           SUCCESS and the seats are booked. Otherwise the payment becomes REFUND_PENDING and its
           holds are released. The claim is one conditional UPDATE inside a savepoint.
        2. finish_refund() issues the refund outside that transaction, once, and records the outcome.
        """
        with transaction.atomic():
            locked = Payment.objects.select_for_update().get(pk=self.pk)
            if locked.status in (Payment.STATUS_SUCCESS, Payment.STATUS_REFUNDED,
                                 Payment.STATUS_REFUND_IN_PROGRESS):
                return locked
            if locked.status == Payment.STATUS_REFUND_PENDING:
                pass  # decided earlier; only the refund remains
            else:
                seats = list(
                    Seat.objects.select_for_update()
                    .filter(payments=locked)
                    .order_by('id')
                )
                booked = False
                if seats:
                    try:
                        with transaction.atomic():  # savepoint: a failed claim leaves no partial bookings
                            locked._claim_and_book(seats)
                        booked = True
                    except (_SeatsTaken, IntegrityError):
                        booked = False

                locked.razorpay_payment_id = razorpay_payment_id
                locked.razorpay_signature = razorpay_signature
                if booked:
                    locked.status = Payment.STATUS_SUCCESS
                    locked.failure_reason = ''
                    locked.save(update_fields=[
                        'razorpay_payment_id', 'razorpay_signature', 'status',
                        'failure_reason', 'updated_at',
                    ])
                    return locked
                locked.status = Payment.STATUS_REFUND_PENDING
                locked.failure_reason = (
                    'Seats were no longer available; payment refunded automatically.'
                    if seats else 'No seats were attached to this payment.'
                )[:255]
                locked.save(update_fields=[
                    'razorpay_payment_id', 'razorpay_signature', 'status', 'failure_reason', 'updated_at',
                ])
                locked._release_holds_for_owner()

        return locked.finish_refund(razorpay_client)

    def finish_refund(self, razorpay_client=None):
        """
        Issues the refund for a REFUND_PENDING payment. Runs outside any seat lock. The claim is a
        conditional UPDATE, so only one caller issues the refund even if callbacks race.
        """
        claimed = Payment.objects.filter(
            pk=self.pk, status=Payment.STATUS_REFUND_PENDING
        ).update(status=Payment.STATUS_REFUND_IN_PROGRESS)
        current = Payment.objects.get(pk=self.pk)
        if not claimed:
            return current

        refund_id = current._refund_in_full(razorpay_client, current.razorpay_payment_id)
        with transaction.atomic():
            locked = Payment.objects.select_for_update().get(pk=self.pk)
            if locked.status != Payment.STATUS_REFUND_IN_PROGRESS:
                return locked
            if refund_id:
                locked.refund_id = refund_id
                locked.status = Payment.STATUS_REFUNDED
            else:
                locked.status = Payment.STATUS_FAILED
                locked.failure_reason = (
                    locked.failure_reason + ' Automatic refund failed; manual refund required.'
                )[:255]
            locked.save(update_fields=['refund_id', 'status', 'failure_reason', 'updated_at'])
            return locked

    def _claim_and_book(self, seats):
        """Must run inside a transaction. Raises _SeatsTaken if any seat cannot be claimed."""
        now = timezone.now()
        seat_ids = [seat.pk for seat in seats]
        claimable = (
            Q(held_by_id__isnull=True)
            | Q(held_until__lte=now)
            | Q(held_by_id=self.user_id)
        )
        updated = (
            Seat.objects.filter(pk__in=seat_ids, is_booked=False)
            .filter(claimable)
            .update(is_booked=True, held_by=None, held_until=None)
        )
        if updated != len(seat_ids):
            raise _SeatsTaken()
        movie = self.theater.movie
        for seat in seats:
            Booking.objects.create(
                user=self.user,
                seat=seat,
                movie=movie,
                theater=self.theater,
                payment=self,
            )

    def mark_failed(self, reason=''):
        with transaction.atomic():
            locked = Payment.objects.select_for_update().get(pk=self.pk)
            if locked.status in Payment.SETTLED_STATUSES:
                return locked

            locked.status = Payment.STATUS_FAILED
            locked.failure_reason = (reason or 'Payment failed')[:255]
            locked.save(update_fields=['status', 'failure_reason', 'updated_at'])
            locked._release_holds_for_owner()
            return locked

    def mark_cancelled(self):
        with transaction.atomic():
            locked = Payment.objects.select_for_update().get(pk=self.pk)
            if locked.status in Payment.SETTLED_STATUSES:
                return locked

            locked.status = Payment.STATUS_CANCELLED
            locked.save(update_fields=['status', 'updated_at'])
            locked._release_holds_for_owner()
            return locked


class WebhookEvent(models.Model):
    """Audit record of every webhook delivery. event_id is unique, so a repeat is detected."""
    RESULT_PROCESSED = 'processed'
    RESULT_DUPLICATE = 'duplicate'
    RESULT_IGNORED = 'ignored'
    RESULT_FAILED = 'failed'

    event_id = models.CharField(max_length=100, unique=True)
    event_type = models.CharField(max_length=100)
    result = models.CharField(max_length=20)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_at']

    def __str__(self):
        return f'{self.event_type} {self.event_id} ({self.result})'


class Review(models.Model):
    RATING_CHOICES = [(i, f'{i} star{"s" if i != 1 else ""}') for i in range(1, 6)]

    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='reviews')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='reviews')
    rating = models.PositiveSmallIntegerField(choices=RATING_CHOICES)
    title = models.CharField(max_length=150, blank=True)
    body = models.TextField()

    is_verified_viewer = models.BooleanField(default=False, editable=False)
    is_edited = models.BooleanField(default=False, editable=False)
    is_hidden = models.BooleanField(
        default=False,
        help_text='Hide this review from public view and exclude it from the average rating (moderation).'
    )
    report_count = models.PositiveIntegerField(default=0, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('movie', 'user')
        indexes = [
            models.Index(fields=['movie', 'is_hidden'], name='review_movie_hidden_idx'),
        ]

    def __str__(self):
        return f'{self.user.username} rated {self.movie.name} {self.rating}/5'

    def save(self, *args, **kwargs):
        from .eligibility import user_has_completed_viewing

        is_new = self._state.adding
        if not is_new:
            self.is_edited = True
        # The badge is derived server-side from completed bookings on every save, never from input.
        self.is_verified_viewer = user_has_completed_viewing(self.user, self.movie)
        super().save(*args, **kwargs)
        self.movie.recalculate_rating()

    def refresh_report_count(self):
        """report_count is the number of OPEN reports awaiting moderation."""
        open_count = self.reports.filter(status=ReviewReport.STATUS_OPEN).count()
        if self.report_count != open_count:
            self.report_count = open_count
            Review.objects.filter(pk=self.pk).update(report_count=open_count)

    def delete(self, *args, **kwargs):
        movie = self.movie
        super().delete(*args, **kwargs)
        movie.recalculate_rating()


class ReviewReport(models.Model):
    REASON_SPAM = 'spam'
    REASON_OFFENSIVE = 'offensive'
    REASON_SPOILER = 'spoiler'
    REASON_IRRELEVANT = 'irrelevant'
    REASON_OTHER = 'other'
    REASON_CHOICES = [
        (REASON_SPAM, 'Spam or advertising'),
        (REASON_OFFENSIVE, 'Offensive or abusive language'),
        (REASON_SPOILER, 'Unmarked spoilers'),
        (REASON_IRRELEVANT, 'Not relevant to the movie'),
        (REASON_OTHER, 'Other'),
    ]

    STATUS_OPEN = 'open'
    STATUS_UPHELD = 'upheld'        # moderator agreed: the review was hidden
    STATUS_DISMISSED = 'dismissed'  # moderator found no problem: the review stays visible
    STATUS_CHOICES = [
        (STATUS_OPEN, 'Open'),
        (STATUS_UPHELD, 'Upheld (review hidden)'),
        (STATUS_DISMISSED, 'Dismissed (review kept)'),
    ]

    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name='reports')
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='review_reports')
    reason = models.CharField(max_length=20, choices=REASON_CHOICES, default=REASON_OTHER)
    note = models.TextField(blank=True, max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN, db_index=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='resolved_review_reports',
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    moderator_note = models.TextField(blank=True, max_length=500)

    class Meta:
        unique_together = ('review', 'reported_by')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['review', 'status'], name='reportreview_status_idx'),
        ]

    def clean(self):
        super().clean()
        if self.review_id and self.reported_by_id and self.review.user_id == self.reported_by_id:
            raise ValidationError("You cannot report your own review.")

    def __str__(self):
        return f'Report on review #{self.review_id} by {self.reported_by.username}'

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        super().save(*args, **kwargs)
        if is_new:
            self.review.refresh_report_count()


class RecentlyViewed(models.Model):
    """
    Tracks the last time a user viewed each movie's detail page. Feeds the
    'Recommended for You' section — a movie's genres/languages count as a
    signal of interest even if the user never went on to book it.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='recently_viewed')
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='viewed_by')
    viewed_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "movie")
        ordering = ["-viewed_at"]
        indexes = [
            models.Index(fields=["user", "-viewed_at"]),
        ]

    def __str__(self):
        return f'{self.user.username} viewed {self.movie.name}'