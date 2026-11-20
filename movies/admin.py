from django.contrib import admin
from django.db.models import Count
from django.utils import timezone

from .forms import MovieAdminForm
from .models import (
    Booking, CastMember, Genre, Language, Movie, MovieCast,
    MoviePoster, Payment, RecentlyViewed, Review, ReviewReport, Seat, Theater,
)


class MovieCastInline(admin.TabularInline):
    model = MovieCast
    extra = 1
    autocomplete_fields = ['cast_member']


class MoviePosterInline(admin.TabularInline):
    model = MoviePoster
    extra = 1
    fields = ['image', 'caption', 'order']


class TheaterInline(admin.TabularInline):
    model = Theater
    extra = 1
    fields = ['name', 'city', 'screen', 'time', 'price_per_seat']


class ReviewReportInline(admin.TabularInline):
    """Read-only view of the reports on a review, shown on the review's admin page."""
    model = ReviewReport
    extra = 0
    can_delete = False
    fields = ['reported_by', 'reason', 'note', 'status', 'created_at']
    readonly_fields = ['reported_by', 'reason', 'note', 'status', 'created_at']

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(MoviePoster)
class MoviePosterAdmin(admin.ModelAdmin):
    list_display = ['movie', 'caption', 'order']
    list_filter = ['movie']
    search_fields = ['movie__name', 'caption']


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    list_display = ['name', 'movie_count']
    search_fields = ['name']

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_movies=Count('movies'))

    @admin.display(description='Movies', ordering='_movies')
    def movie_count(self, obj):
        return obj._movies


@admin.register(Language)
class LanguageAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']


@admin.register(CastMember)
class CastMemberAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['razorpay_order_id', 'user', 'theater', 'amount', 'status', 'created_at']
    list_filter = ['status', 'theater']
    search_fields = ['razorpay_order_id', 'razorpay_payment_id', 'user__username']
    readonly_fields = ['razorpay_order_id', 'razorpay_payment_id', 'razorpay_signature']


@admin.action(description='Publish selected movies')
def publish_movies(modeladmin, request, queryset):
    updated = queryset.update(is_published=True)
    modeladmin.message_user(request, f'{updated} movie(s) published.')


@admin.action(description='Unpublish selected movies (hide from the site)')
def unpublish_movies(modeladmin, request, queryset):
    updated = queryset.update(is_published=False)
    modeladmin.message_user(request, f'{updated} movie(s) unpublished.')


@admin.register(Movie)
class MovieAdmin(admin.ModelAdmin):
    form = MovieAdminForm
    list_display = ['name', 'is_published', 'release_date', 'age_certificate',
                    'duration_display', 'average_rating', 'total_ratings']
    list_filter = ['is_published', 'age_certificate', 'genres', 'languages']
    search_fields = ['name', 'description']
    prepopulated_fields = {'slug': ('name',)}
    filter_horizontal = ['genres', 'languages']
    readonly_fields = ['average_rating', 'total_ratings', 'created_at', 'updated_at']
    inlines = [MovieCastInline, MoviePosterInline, TheaterInline]
    actions = [publish_movies, unpublish_movies]
    fieldsets = [
        ('Basics', {'fields': ['name', 'slug', 'is_published', 'release_date']}),
        ('Details', {'fields': ['description', 'duration_minutes', 'age_certificate', 'genres', 'languages']}),
        ('Media', {'fields': ['image', 'trailer_youtube_id']}),
        ('Ratings (calculated)', {'fields': ['average_rating', 'total_ratings']}),
        ('Record', {'fields': ['created_at', 'updated_at']}),
    ]


@admin.register(Theater)
class TheaterAdmin(admin.ModelAdmin):
    list_display = ['name', 'movie', 'city', 'screen', 'time', 'end_time', 'price_per_seat', 'seat_count']
    list_filter = ['city', 'movie']
    search_fields = ['name', 'city', 'movie__name', 'screen']
    ordering = ['time']
    date_hierarchy = 'time'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('movie').annotate(_seats=Count('seats'))

    @admin.display(description='Seats', ordering='_seats')
    def seat_count(self, obj):
        return obj._seats

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # A show created in the admin gets the standard seat layout, so it can be booked.
        obj.ensure_seat_layout()


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ['theater', 'seat_number', 'is_booked']
    list_filter = ['is_booked', 'theater']


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ['user', 'seat', 'movie', 'theater', 'booked_at']
    list_filter = ['movie', 'theater']


@admin.action(description='Hide selected reviews from the site and rating')
def hide_reviews(modeladmin, request, queryset):
    count = 0
    for review in queryset.select_related('movie', 'user'):
        if not review.is_hidden:
            review.is_hidden = True
            review.save()  # save() recalculates the movie rating
            count += 1
    modeladmin.message_user(request, f'{count} review(s) hidden.')


@admin.action(description='Restore selected reviews to the site and rating')
def restore_reviews(modeladmin, request, queryset):
    count = 0
    for review in queryset.select_related('movie', 'user'):
        if review.is_hidden:
            review.is_hidden = False
            review.save()
            count += 1
    modeladmin.message_user(request, f'{count} review(s) restored.')


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ['movie', 'user', 'rating', 'is_verified_viewer', 'report_count', 'is_hidden', 'created_at']
    list_filter = ['rating', 'is_verified_viewer', 'is_hidden']
    search_fields = ['movie__name', 'user__username', 'body']
    readonly_fields = ['is_verified_viewer', 'is_edited', 'report_count', 'created_at', 'updated_at']
    inlines = [ReviewReportInline]
    actions = [hide_reviews, restore_reviews]


@admin.action(description='Uphold selected reports (hide the review)')
def uphold_reports(modeladmin, request, queryset):
    now = timezone.now()
    reviews = {}
    for report in queryset.filter(status=ReviewReport.STATUS_OPEN).select_related('review'):
        report.status = ReviewReport.STATUS_UPHELD
        report.resolved_by = request.user
        report.resolved_at = now
        report.save(update_fields=['status', 'resolved_by', 'resolved_at'])
        reviews[report.review_id] = report.review
    for review in reviews.values():
        review.refresh_report_count()
        if not review.is_hidden:
            review.is_hidden = True
            review.save()  # recalculates the movie rating
    modeladmin.message_user(request, f'{len(reviews)} review(s) hidden after upheld reports.')


@admin.action(description='Dismiss selected reports (keep the review visible)')
def dismiss_reports(modeladmin, request, queryset):
    now = timezone.now()
    reviews = {}
    for report in queryset.filter(status=ReviewReport.STATUS_OPEN).select_related('review'):
        report.status = ReviewReport.STATUS_DISMISSED
        report.resolved_by = request.user
        report.resolved_at = now
        report.save(update_fields=['status', 'resolved_by', 'resolved_at'])
        reviews[report.review_id] = report.review
    for review in reviews.values():
        review.refresh_report_count()
    modeladmin.message_user(request, f'{len(reviews)} review(s) kept visible after dismissed reports.')


@admin.register(ReviewReport)
class ReviewReportAdmin(admin.ModelAdmin):
    list_display = ['review', 'reported_by', 'reason', 'status', 'created_at', 'resolved_by', 'resolved_at']
    list_filter = ['status', 'reason']
    search_fields = ['review__movie__name', 'reported_by__username', 'note']
    readonly_fields = ['review', 'reported_by', 'reason', 'note', 'created_at', 'resolved_by', 'resolved_at']
    fields = ['review', 'reported_by', 'reason', 'note', 'created_at', 'status', 'moderator_note',
              'resolved_by', 'resolved_at']
    actions = [uphold_reports, dismiss_reports]

    def has_add_permission(self, request):
        return False  # reports are created by users on the movie page only


@admin.register(RecentlyViewed)
class RecentlyViewedAdmin(admin.ModelAdmin):
    list_display = ['user', 'movie', 'viewed_at']
    list_filter = ['movie']
