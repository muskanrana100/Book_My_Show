from django.contrib import admin

from .models import(
    Booking, CastMember, Genre, Language, Movie, MovieCast,
    MoviePoster, Review, ReviewReport, Seat, Theater,
)

class MovieCastInline(admin.TabularInline):
    model = MovieCast
    extra = 1
    autocomplete_fields = ['cast_member']

class MoviePosterInline(admin.TabularInline):
    model = MoviePoster
    extra = 1

class  TheaterInline(admin.TabularInline):
    model = Theater
    extra = 1

@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']

@admin.register(Language)
class LanguageAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']

@admin.register(CastMember)
class CastMemberAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']

@admin.register(Movie)
class MovieAdmin(admin.ModelAdmin):
    list_display = ['name', 'release_date' ,'age_certificate','duration_display','average_rating','total_ratings',]
    list_filter = ['age_certificate', 'genres','languages']
    search_fields = ['name','description']
    prepopulated_fields ={'slug':('name',)}
    filter_horizontal = ['genres', 'languages']
    readonly_fields = ['average_rating','total_ratings']
    inlines = [MovieCastInline, MoviePosterInline, TheaterInline]

@admin.register(Theater)
class TheaterAdmin(admin.ModelAdmin):
    list_display = ['name','movie','time']
    list_filter = ['movie']
    ordering = ['time']

@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ['theater','seat_number','is_booked']
    list_filter = ['is_booked', 'theater']

@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ['user','seat','movie','theater','booked_at']
    list_filter = ['movie','theater']

@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ['movie', 'user' ,'rating', 'is_verified_viewer', 'report_count','is_hidden','created_at']
    list_filter = ['rating', 'is_verified_viewer', 'is_hidden']
    search_fields = ['movie__name','user__username','body']
    readonly_fields = ['is_verified_viewer','is_edited', 'report_count']

@admin.register(ReviewReport)
class ReviewReportAdmin(admin.ModelAdmin):
    list_display = ['review', 'reported_by', 'reason', 'created_at']
    list_filter = ['reason']