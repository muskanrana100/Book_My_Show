from decimal import Decimal

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from datetime import timedelta


youtube_id_validator = RegexValidator(
    regex=r"^[A-Za-z0-9_-]{11}$",
    message="Enter just the 11-character YouTube video ID, e.g. 'dQw4w9WgXcQ' "
            "(the part after v= in youtube.com/watch?v=...)."
)


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
    image = models.ImageField(upload_to = "movies/", help_text = "Primary poster shown on listing cards.")
    description = models.TextField(blank =True, null=True)

    genres = models.ManyToManyField(Genre, related_name = "movies", blank = True)
    languages = models.ManyToManyField(Language, related_name ="movies", blank = True)
    cast_members = models.ManyToManyField(
        CastMember, through = "MovieCast", related_name  = "movies", blank = True
    )

    duration_minutes = models.PositiveIntegerField(default = 0, help_text = "Runtime in minutes.")
    age_certificate = models.CharField(max_length = 2, choices = CERTIFICATE_CHOICES, default = CERTIFICATE_UA)
    release_date = models.DateField(default = timezone.now)

    trailer_youtube_id = models.CharField(
        max_length = 11, blank = True, validators =  [youtube_id_validator] ,
        help_text = "YouTube video ID only, e.g. 'dQw4w9WgXcQ'."
    )

    average_rating = models.DecimalField(max_digits = 3 , decimal_places = 1 , default = Decimal('0.0') , editable = False)
    total_ratings = models.PositiveIntegerField(default = 0 , editable = False)

    created_at = models.DateTimeField(auto_now_add =  True)
    updated_at = models.DateTimeField(auto_now =  True)

    class Meta:
        ordering = ["-release_date"]

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
        if not self.trailer_youtube_id:
            return ''
        return f'https://www.youtube-nocookie.com/embed/{self.trailer_youtube_id}'

    @property
    def duration_display (self):
        hours, minutes = divmod(self.duration_minutes, 60)
        if hours:
            return f'{hours}h {minutes}m'
        return f'{minutes}m'

    def recalculate_rating (self):
        stats = self.reviews.filter(is_hidden=False).aggregate(
            avg=models.Avg('rating'), count=models.Count('id')
        )
        self.average_rating = Decimal(str (round (stats["avg"] or 0, 1)))
        self.total_ratings = stats["count"] or 0
        self.save(update_fields =["average_rating", "total_ratings"])

    def similar_movies(self, limit=8):
        genre_ids = self.genres.values_list('id', flat=True)
        if not genre_ids :
            return Movie.objects.none()
        return Movie.objects.filter(genres__in = genre_ids).exclude(id = self.id).distinct()[ :limit]


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
    image = models.ImageField(upload_to =  "movies/gallery/")
    caption = models.CharField(max_length = 150, blank = True)
    order  = models.PositiveIntegerField(default = 0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f'Gallery image for {self.movie.name}'


class Theater(models.Model):
    name = models.CharField (max_length = 255)
    city = models.CharField (max_length = 100, blank = True, db_index = True)
    movie = models.ForeignKey (Movie, on_delete=models.CASCADE, related_name="theaters")
    time = models.DateTimeField()
    price_per_seat = models.DecimalField(max_digits = 7, decimal_places = 2, default = 200)

    class Meta:
        ordering = ["time"]
        indexes = [
            models.Index(fields=["city", "time"]),
        ]

    def __str__(self):
        return f'{self.name} ({self.city}) - {self.movie.name} at {self.time}'


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
        indexes =[ models.Index(fields =["theater","is_booked" ]), ]

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
    seat = models.OneToOneField(Seat, on_delete=models.CASCADE)
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

    def __str__(self):
        return f'Booking by {self.user.username} for {self.seat.seat_number} at {self.theater.name}'

    def cancel_and_refund(self, razorpay_client):
        """
        Cancels this booking and, if it was paid for, issues a real refund
        via Razorpay for this seat's share of the original payment. Frees
        the seat immediately so it becomes bookable again. Safe to call
        only once — checked by the view before this is invoked.
        """

        with transaction.atomic():
            locked = Booking.objects.select_for_update().get(pk=self.pk)
            if locked.is_cancelled:
                return locked
            
            if locked.payment_id and locked.payment.razorpay_payment_id:
                refund_amount_rupees = locked.theater.price_per_seat
                refund_amount_paise = int(refund_amount_rupees * 100)
                try:
                    refund = razorpay_client.payment.refund(
                        locked.payment.razorpay_payment_id,
                        {'amount': refund_amount_paise}
                    )
                    locked.refund_id = refund['id']
                    locked.refund_amount = refund_amount_rupees
                    locked.refund_status = Booking.REFUND_REFUNDED
                except Exception:
                    locked.refund_status = Booking.REFUND_FAILED

            locked.is_cancelled = True
            locked.cancelled_at = timezone.now()
            locked.save(update_fields= [
                'is_cancelled' , 'cancelled_at' , 'refund_id',  'refund_amount' , 'refund_status'])

            seat = locked.seat
            seat.is_booked = False
            seat.save(update_fields=['is_booked'])

            return locked

class Payment(models.Model):
    STATUS_CREATED = 'created'
    STATUS_SUCCESS = 'success'
    STATUS_FAILED = 'failed'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES =[
        (STATUS_CREATED , 'Created'),
        (STATUS_SUCCESS ,'Success'),
        (STATUS_FAILED , 'Failed'),
        (STATUS_CANCELLED , 'Cancelled'),
    ]
    
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete= models.CASCADE, related_name='payments')
    theater = models.ForeignKey(Theater, on_delete= models.CASCADE, related_name='payments')
    seats = models.ManyToManyField(Seat, related_name = 'payments')


    amount = models.DecimalField(max_digits=9, decimal_places=2)
    status = models.CharField(max_length= 20, choices=STATUS_CHOICES, default = STATUS_CREATED)

    razorpay_order_id = models.CharField(max_length=100, unique=True)
    razorpay_payment_id= models.CharField(max_length=100, blank= True,null = True, unique= True)
    razorpay_signature = models.CharField(max_length=225, blank= True)
    failure_reason = models.CharField(max_length = 255, blank = True)

    created_at = models.DateTimeField(auto_now_add = True)
    updated_at = models.DateTimeField(auto_now = True)


    class Meta:
        ordering = ['-created_at']

        indexes = [
            models.Index(
                fields=['created_at'],
                name='payment_created_at_idx'
            ),

            models.Index(
                fields=['created_at', 'status'],
                name='payment_date_status_idx'
            ),
        ]

    def __str__(self):
        return (
            f'Payment {self.razorpay_order_id} '
            f'({self.status}) by {self.user.username}'
        )

    
    def mark_success(self, razorpay_payment_id, razorpay_signature):
        """
        Confirms this payment and creates real Booking records for every seat
        it covers. Safe to call more than once (e.g. once from the browser
        callback and once from the webhook) — if this payment has already
        been marked successful, it does nothing further, so duplicate calls
        can never create duplicate bookings.
        """
        with  transaction.atomic():
            locked = Payment.objects.select_for_update().get(pk=self.pk)
            if locked.status == Payment.STATUS_SUCCESS:
                return locked
            
            locked.razorpay_payment_id = razorpay_payment_id
            locked.razorpay_signature = razorpay_signature
            locked.status = Payment.STATUS_SUCCESS
            locked.save(update_fields=['razorpay_payment_id', 'razorpay_signature','status','updated_at'])

            seats = list(Seat.objects.select_for_update().filter(payments=locked))
            for seat in seats:
                if seat.is_booked:
                    continue
                Booking.objects.create(
                    user = locked.user,
                    seat = seat,
                    movie = locked.theater.movie,
                    theater = locked.theater,
                    payment=locked,
                )

                seat.is_booked = True
                seat.held_by = None
                seat.held_until = None
                seat.save(update_fields=['is_booked', 'held_by', 'held_until'])

            return locked

    def mark_failed(self, reason=''):
        with transaction.atomic():
            locked = Payment.objects.select_for_update().get(pk=self.pk)
            if locked.status == Payment.STATUS_SUCCESS:
                return locked
            
            locked.status = Payment.STATUS_FAILED
            locked.failure_reason = reason[ :255]
            locked.save(update_fields=['status','failure_reason','updated_at'])

            seats =Seat.objects.select_for_update().filter(payments=locked, is_booked= False)

            for seat in seats:
                seat.held_by = None
                seat.held_until = None
                seat.save(update_fields=['held_by','held_until'])
            return locked

    def mark_cancelled(self):
        with transaction.atomic():
            locked = Payment.objects.select_for_update().get(pk=self.pk)
            if locked.status == Payment.STATUS_SUCCESS:
                return locked
            locked.status = Payment.STATUS_CANCELLED
            locked.save(update_fields=['status','updated_at'])

            seats = Seat.objects.select_for_update().filter(payments =locked, is_booked = False)

            for seat in seats:
                seat.held_by = None
                seat.held_until = None
                seat.save(update_fields=['held_by','held_until'])
            return locked



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

    def __str__(self):
        return f'{self.user.username} rated {self.movie.name} {self.rating}/5'

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        if is_new:
            self.is_verified_viewer = Booking.objects.filter(
                user=self.user, movie=self.movie
            ).exists()
        else:
            self.is_edited = True
        super().save(*args, **kwargs)
        self.movie.recalculate_rating()

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

    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name='reports')
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='review_reports')
    reason = models.CharField(max_length=20, choices=REASON_CHOICES, default=REASON_OTHER)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('review', 'reported_by')
        ordering = ['-created_at']

    def __str__(self):
        return f'Report on review #{self.review_id} by {self.reported_by.username}'

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        super().save(*args, **kwargs)
        if is_new:
            self.review.report_count = self.review.reports.count()
            self.review.save(update_fields=["report_count"])


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