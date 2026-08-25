from decimal import Decimal

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify


youtube_id_validator = RegexValidator(
    regex=r'^[A-Za-z0-9_-]{11}$',
    message="Enter just the 11-character YouTube video ID, e.g. 'dQw4w9WgXcQ' "
            "(the part after v= in youtube.com/watch?v=...)."
)


class Genre(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Language(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class CastMember(models.Model):
    ROLE_ACTOR = 'actor'
    ROLE_DIRECTOR = 'director'
    ROLE_PRODUCER = 'producer'
    ROLE_CHOICES = [
        (ROLE_ACTOR, 'Actor'),
        (ROLE_DIRECTOR, 'Director'),
        (ROLE_PRODUCER, 'Producer'),
    ]

    name = models.CharField(max_length=150)
    photo = models.ImageField(upload_to='cast/', blank=True, null=True)
    bio = models.TextField(blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Movie(models.Model):
    CERTIFICATE_U = 'U'
    CERTIFICATE_UA = 'UA'
    CERTIFICATE_A = 'A'
    CERTIFICATE_S = 'S'
    CERTIFICATE_CHOICES = [
        (CERTIFICATE_U, 'U - Universal'),
        (CERTIFICATE_UA, 'UA - Parental Guidance'),
        (CERTIFICATE_A, 'A - Adults Only'),
        (CERTIFICATE_S, 'S - Special'),
    ]

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=280, unique=True, blank=True)
    image = models.ImageField(upload_to='movies/', help_text='Primary poster shown on listing cards.')
    description = models.TextField(blank=True, null=True)

    genres = models.ManyToManyField(Genre, related_name='movies', blank=True)
    languages = models.ManyToManyField(Language, related_name='movies', blank=True)
    cast_members = models.ManyToManyField(
        CastMember, through='MovieCast', related_name='movies', blank=True
    )

    duration_minutes = models.PositiveIntegerField(default=0, help_text='Runtime in minutes.')
    age_certificate = models.CharField(max_length=2, choices=CERTIFICATE_CHOICES, default=CERTIFICATE_UA)
    release_date = models.DateField(default=timezone.now)

    trailer_youtube_id = models.CharField(
        max_length=11, blank=True, validators=[youtube_id_validator],
        help_text="YouTube video ID only, e.g. 'dQw4w9WgXcQ'."
    )

    average_rating = models.DecimalField(max_digits=3, decimal_places=1, default=Decimal('0.0'), editable=False)
    total_ratings = models.PositiveIntegerField(default=0, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-release_date']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            candidate = base_slug
            counter = 1
            while Movie.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                counter += 1
                candidate = f'{base_slug}-{counter}'
            self.slug = candidate
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('movie_detail', kwargs={'slug': self.slug})

    @property
    def trailer_embed_url(self):
        if not self.trailer_youtube_id:
            return ''
        return f'https://www.youtube-nocookie.com/embed/{self.trailer_youtube_id}'

    @property
    def duration_display(self):
        hours, minutes = divmod(self.duration_minutes, 60)
        if hours:
            return f'{hours}h {minutes}m'
        return f'{minutes}m'

    def recalculate_rating(self):
        stats = self.reviews.filter(is_hidden=False).aggregate(
            avg=models.Avg('rating'), count=models.Count('id')
        )
        self.average_rating = Decimal(str(round(stats['avg'] or 0, 1)))
        self.total_ratings = stats['count'] or 0
        self.save(update_fields=['average_rating', 'total_ratings'])

    def similar_movies(self, limit=8):
        genre_ids = self.genres.values_list('id', flat=True)
        if not genre_ids:
            return Movie.objects.none()
        return Movie.objects.filter(genres__in=genre_ids).exclude(id=self.id).distinct()[:limit]


class MovieCast(models.Model):
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='movie_cast')
    cast_member = models.ForeignKey(CastMember, on_delete=models.CASCADE, related_name='movie_roles')
    role = models.CharField(max_length=20, choices=CastMember.ROLE_CHOICES, default=CastMember.ROLE_ACTOR)
    character_name = models.CharField(max_length=150, blank=True)
    order = models.PositiveIntegerField(default=0, help_text='Lower numbers appear first (billing order).')

    class Meta:
        ordering = ['order']
        unique_together = ('movie', 'cast_member', 'role')

    def __str__(self):
        return f'{self.cast_member.name} in {self.movie.name}'


class MoviePoster(models.Model):
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='gallery_images')
    image = models.ImageField(upload_to='movies/gallery/')
    caption = models.CharField(max_length=150, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f'Gallery image for {self.movie.name}'


class Theater(models.Model):
    name = models.CharField(max_length=255)
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='theaters')
    time = models.DateTimeField()

    class Meta:
        ordering = ['time']

    def __str__(self):
        return f'{self.name} - {self.movie.name} at {self.time}'


class Seat(models.Model):
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE, related_name='seats')
    seat_number = models.CharField(max_length=10)
    is_booked = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.seat_number} in {self.theater.name}'


class Booking(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='bookings')
    seat = models.OneToOneField(Seat, on_delete=models.CASCADE)
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='bookings')
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE, related_name='bookings')
    booked_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Booking by {self.user.username} for {self.seat.seat_number} at {self.theater.name}'


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
            self.review.save(update_fields=['report_count'])