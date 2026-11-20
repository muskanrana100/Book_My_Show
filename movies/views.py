
import csv
import hmac
import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.shortcuts import get_object_or_404, render
from django.http import HttpResponse
from .ticket_service import generate_ticket_pdf
from .models import Booking
import razorpay
from django.conf import settings
from django.db import OperationalError
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import permission_required
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.core.paginator import Paginator
from django.db.models import Func, DateField
from django.db.models import Count, Sum, Avg, F, Q, OuterRef, Subquery, Min, Exists
from django.db.models.functions import TruncDate, TruncWeek, TruncMonth, TruncYear, ExtractHour
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .forms import ReviewForm, ReviewReportForm
from .models import Booking,Genre, Language, Movie, Payment, Review,RecentlyViewed, Seat, Theater, WebhookEvent
from .models import BookingCancellationError
from .eligibility import user_has_completed_viewing, verified_viewer_expression
from . import reservations
from .queueing import queue_ticket_email
from .ticket_service import is_valid_verify_token
import logging
from django.http import Http404

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value):
    """
    Neutralises spreadsheet formula injection. A text value that starts with a formula
    character is prefixed with an apostrophe so Excel and Sheets show it as text.
    Numbers, dates, None and booleans are returned unchanged, so legitimate values survive.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return value
    text = str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text

logger = logging.getLogger(__name__)
CHECKOUT_PREPARING_GRACE = timedelta(seconds=60)


def _discovery_queryset(search_query, genre_id, language_id, city, theater_id,
                        date_from, date_to, release_from, release_to,
                        show_time_slot, min_rating, sort_by):
    """
    The single definition of "matching movies" for the discovery page and its count endpoint.
    Returns (movies_queryset, date_errors). Invalid dates are reported, never silently dropped.
    """
    date_errors = []
    movies = Movie.objects.filter(is_published=True)

    if search_query:
        movies = movies.filter(name__icontains=search_query)

    if genre_id:
        movies = movies.filter(genres__id=genre_id)

    if language_id:
        movies = movies.filter(languages__id=language_id)

    if min_rating:
        try:
            movies = movies.filter(average_rating__gte=float(min_rating))
        except (ValueError, TypeError):
            pass

    if release_from:
        try:
            release_start = datetime.strptime(release_from, '%Y-%m-%d').date()
            movies = movies.filter(release_date__gte=release_start)
        except ValueError:
            date_errors.append("Release start date must be in the format YYYY-MM-DD.")

    if release_to:
        try:
            release_end = datetime.strptime(release_to, '%Y-%m-%d').date()
            movies = movies.filter(release_date__lte=release_end)
        except ValueError:
            date_errors.append("Release end date must be in the format YYYY-MM-DD.")

    theater_filter = Q()
    has_theater_condition = False

    if city:
        theater_filter &= Q(city__icontains=city)
        has_theater_condition = True

    if theater_id:
        theater_filter &= Q(
            name=theater_id
        )
        has_theater_condition = True

    if date_from:
        try:
            show_date_from = datetime.strptime(date_from, '%Y-%m-%d').date()
            theater_filter &= Q(time__date__gte=show_date_from)
            has_theater_condition = True
        except ValueError:
            date_errors.append("Show date from must be in the format YYYY-MM-DD.")

    if date_to:
        try:
            show_date_to = datetime.strptime(date_to, '%Y-%m-%d').date()
            theater_filter &= Q(time__date__lte=show_date_to)
            has_theater_condition = True
        except ValueError:
            date_errors.append("Show date to must be in the format YYYY-MM-DD.")

    SHOW_TIME_SLOTS = {
        'morning': (6, 12),
        'afternoon': (12, 16),
        'evening': (16, 20),
        'night': (20, 24),
    }

    if show_time_slot in SHOW_TIME_SLOTS:
        start_hour, end_hour = SHOW_TIME_SLOTS[show_time_slot]
        theater_filter &= Q(time__hour__gte=start_hour, time__hour__lt=end_hour)
        has_theater_condition = True

    if has_theater_condition:
        matching_theater = Theater.objects.filter(
            movie=OuterRef('pk')
        ).filter(theater_filter)
        movies = movies.filter(Exists(matching_theater))

    movies = movies.distinct()

    movies = movies.annotate(
        booking_count=Count(
            'bookings',
            filter=Q(
                bookings__is_cancelled=False,
                bookings__payment__status=Payment.STATUS_SUCCESS,
            ),
            distinct=True,
        )
    )

    lowest_price_sq = (
        Theater.objects
        .filter(movie=OuterRef('pk'))
        .filter(theater_filter)
        .values('movie')
        .annotate(minimum_price=Min('price_per_seat'))
        .values('minimum_price')
    )

    movies = movies.annotate(lowest_price=Subquery(lowest_price_sq))

    sort_map = {
        'popularity': '-booking_count',
        'newest': '-release_date',
        'rating': '-average_rating',
        'price_low': 'lowest_price',
        'price_high': '-lowest_price',
    }

    movies = movies.order_by(sort_map.get(sort_by, '-booking_count'))

    return movies, date_errors


def movie_list(request):
    search_query = request.GET.get('search', '').strip()
    genre_id = request.GET.get('genre', '').strip()
    language_id = request.GET.get('language', '').strip()
    city = request.GET.get('city', '').strip()
    theater_id = request.GET.get('theater', '').strip()

    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    release_from = request.GET.get('release_from', '').strip()
    release_to = request.GET.get('release_to', '').strip()

    show_time_slot = request.GET.get('show_time', '').strip()
    min_rating = request.GET.get('min_rating', '').strip()

    sort_by = request.GET.get('sort', 'popularity').strip()

    movies, date_errors = _discovery_queryset(
        search_query, genre_id, language_id, city, theater_id,
        date_from, date_to, release_from, release_to,
        show_time_slot, min_rating, sort_by,
    )

    total_count = movies.count()

    paginator = Paginator(movies, 12)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    recommended_movies = []
    recommendations_personal = False

    if request.user.is_authenticated:
        recommended_movies, recommendations_personal = _get_recommendations(
            request.user
        )

    context = {
        'movies': page_obj,
        'page_obj': page_obj,

        'total_count': total_count,
        'date_errors': date_errors,

        'genres': Genre.objects.all(),
        'languages': Language.objects.all(),

        'theaters': (
            Theater.objects
            .order_by('name')
            .values_list('name', flat=True)
            .distinct()
        ),

        'recommended_movies': recommended_movies,
        'recommendations_personal': recommendations_personal,

        'search_query': search_query,
        'selected_genre': genre_id,
        'selected_language': language_id,
        'selected_city': city,
        'selected_theater': theater_id,

        'date_from': date_from,
        'date_to': date_to,

        'release_from': release_from,
        'release_to': release_to,

        'show_time_slot': show_time_slot,
        'min_rating': min_rating,

        'sort_by': sort_by,
    }

    return render(request, 'movies/movie_list.html', context)


@require_GET
def discovery_count(request):
    """
    Returns the number of movies matching the current filters. Uses the same queryset as the
    discovery page, so the count and the list always agree. JSON only; no templates.
    """
    params = {
        'search_query': request.GET.get('search', '').strip(),
        'genre_id': request.GET.get('genre', '').strip(),
        'language_id': request.GET.get('language', '').strip(),
        'city': request.GET.get('city', '').strip(),
        'theater_id': request.GET.get('theater', '').strip(),
        'date_from': request.GET.get('date_from', '').strip(),
        'date_to': request.GET.get('date_to', '').strip(),
        'release_from': request.GET.get('release_from', '').strip(),
        'release_to': request.GET.get('release_to', '').strip(),
        'show_time_slot': request.GET.get('show_time', '').strip(),
        'min_rating': request.GET.get('min_rating', '').strip(),
        'sort_by': request.GET.get('sort', 'popularity').strip(),
    }
    movies, date_errors = _discovery_queryset(
        params['search_query'], params['genre_id'], params['language_id'],
        params['city'], params['theater_id'], params['date_from'], params['date_to'],
        params['release_from'], params['release_to'], params['show_time_slot'],
        params['min_rating'], params['sort_by'],
    )
    return JsonResponse({
        'count': movies.count(),
        'errors': date_errors,
    })


def _get_recommendations(user, limit=8):
    """
    Recommends movies sharing a genre or language with what the user has
    either booked or recently viewed, excluding movies they've already
    booked. Falls back to trending movies if there's no history yet.
    """
    booked_movie_ids = list(
        Booking.objects.filter(
            user=user,
            is_cancelled=False,
            payment__status=Payment.STATUS_SUCCESS,
        ).values_list('movie_id', flat=True)
    )
    viewed_movie_ids = RecentlyViewed.objects.filter(user=user).values_list('movie_id', flat=True)
    interest_movie_ids = list(booked_movie_ids) + list(viewed_movie_ids)

    if not interest_movie_ids:
        fallback = Movie.objects.filter(is_published=True).order_by('-total_ratings', '-average_rating')[:limit]
        return list(fallback), False

    genre_ids = Genre.objects.filter(movies__id__in=interest_movie_ids).values_list('id', flat=True)
    language_ids = Language.objects.filter(movies__id__in=interest_movie_ids).values_list('id', flat=True)

    recommended = list(
        Movie.objects.filter(is_published=True)
        .filter(Q(genres__id__in=genre_ids) | Q(languages__id__in=language_ids))
        .exclude(id__in=booked_movie_ids)
        .distinct()
        .order_by('-average_rating', '-total_ratings')[:limit]
    )
    if not recommended:
        fallback = Movie.objects.filter(is_published=True).order_by('-total_ratings', '-average_rating')[:limit]
        return list(fallback), False
    return recommended, True

def _trending_movies(exclude_id=None, days=30, limit=8):
    """
    Trending = released, published movies with the most non-cancelled bookings in the last
    `days` days. Ties go to the higher average rating, then to more ratings. Movies with no
    bookings in the window are not shown, so nothing is invented when there is little activity.
    """
    since = timezone.now() - timedelta(days=days)
    qs = Movie.objects.filter(is_published=True, release_date__lte=timezone.localdate())
    if exclude_id:
        qs = qs.exclude(id=exclude_id)
    return (
        qs.annotate(
            recent_bookings=Count(
                'bookings',
                filter=Q(bookings__is_cancelled=False, bookings__booked_at__gte=since),
                distinct=True,
            )
        )
        .filter(recent_bookings__gt=0)
        .order_by('-recent_bookings', '-average_rating', '-total_ratings', '-id')[:limit]
    )


def _recently_released(exclude_id=None, limit=8):
    """Published movies whose release date has passed, newest release date first."""
    qs = Movie.objects.filter(is_published=True, release_date__lte=timezone.localdate())
    if exclude_id:
        qs = qs.exclude(id=exclude_id)
    return qs.order_by('-release_date', '-id')[:limit]


def movie_detail(request, slug):
    movie = get_object_or_404(
        Movie.objects.prefetch_related("genres", "languages", "gallery_images", "movie_cast__cast_member"),
        slug=slug,
    )
    if not movie.is_published and not request.user.is_staff:
        raise Http404("Movie not found.")

    now = timezone.now()

    # Visible reviews, newest first, each annotated with the viewer's CURRENT verified status
    # (computed in the database), so a cancelled booking removes the badge immediately.
    reviews_qs = (
        movie.reviews.filter(is_hidden=False)
        .select_related("user")
        .annotate(verified_now=verified_viewer_expression(movie, now=now))
        .order_by("-created_at", "-id")
    )
    paginator = Paginator(reviews_qs, 10)
    reviews_page = paginator.get_page(request.GET.get("page"))

    recommended_movies = []
    recommendations_personal = False

    if request.user.is_authenticated:
        recommended_movies, recommendations_personal = _get_recommendations(
            request.user
        )

    user_review = None   # the viewer's own visible review
    own_review = None    # the viewer's review including hidden ones (for the one-per-movie rule)
    can_review = False
    can_edit = False
    review_form = None

    if request.user.is_authenticated:
        own_review = movie.reviews.filter(user=request.user).first()
        user_review = own_review if (own_review and not own_review.is_hidden) else None
        eligible = user_has_completed_viewing(request.user, movie, now=now)
        can_review = eligible and own_review is None
        can_edit = eligible and user_review is not None

        if request.method == "POST" and "submit_review" in request.POST:
            if own_review and own_review.is_hidden:
                messages.error(request, "Your review has been hidden by moderators and cannot be edited.")
                return redirect("movie_detail", slug=movie.slug)
            if not (can_review or can_edit):
                if own_review is None:
                    messages.error(request, "You can rate and review this movie once you have booked and watched a show.")
                else:
                    messages.error(request, "You can edit your review once you have booked and watched a show.")
                return redirect("movie_detail", slug=movie.slug)

            review_form = ReviewForm(request.POST, instance=user_review) if user_review else ReviewForm(request.POST)
            if review_form.is_valid():
                review = review_form.save(commit=False)
                review.movie = movie
                review.user = request.user
                try:
                    with transaction.atomic():
                        review.save()
                except IntegrityError:
                    # Two submissions raced: the database kept one review and refused the other.
                    messages.error(request, "You have already reviewed this movie. Edit your existing review instead.")
                    return redirect("movie_detail", slug=movie.slug)
                messages.success(request, "Your review has been saved.")
                return redirect("movie_detail", slug=movie.slug)
            # Invalid input: fall through and show the form with its errors.
        else:
            if user_review and can_edit:
                review_form = ReviewForm(instance=user_review)
            elif can_review:
                review_form = ReviewForm()

    # Rating breakdown from visible reviews, computed in one query.
    visible = movie.reviews.filter(is_hidden=False)
    counts = visible.aggregate(**{f"s{n}": Count("id", filter=Q(rating=n)) for n in range(1, 6)})
    total_visible = sum(counts.values())
    distribution = [
        {
            "stars": n,
            "count": counts[f"s{n}"],
            "percent": round(counts[f"s{n}"] * 100 / total_visible) if total_visible else 0,
        }
        for n in (5, 4, 3, 2, 1)
    ]

    context = {
        "movie": movie,
        "reviews_page": reviews_page,
        "reviews_total": paginator.count,
        "review_distribution": distribution,
        "user_review": user_review,
        "can_review": can_review,
        "can_edit": can_edit,
        "review_form": review_form,
        "similar_movies": movie.similar_movies(),
        "trending_movies": _trending_movies(exclude_id=movie.id),
        "recent_movies": _recently_released(exclude_id=movie.id),
    }

    return render(request, "movies/movie_detail.html", context)


def theater_list(request, movie_id):
    movie = get_object_or_404(Movie, id=movie_id)
    if not movie.is_published and not request.user.is_staff:
        raise Http404("Movie not found.")
    theaters = Theater.objects.filter(movie=movie)
    return render(request, "movies/theater_list.html" , {"movie" : movie ,"theaters": theaters})


@login_required(login_url='/login/')
def seat_selection(request, theater_id):
    theater = get_object_or_404(Theater.objects.select_related("movie"), id=theater_id)
    if not theater.movie.is_published and not request.user.is_staff:
        raise Http404("Show not found.")
    seats = Seat.objects.filter(theater=theater)
    for seat in seats:
        seat.display_status = seat.status_for(request.user)
    return render(request, "movies/seat_selection.html", {"theater" : theater, "seats":seats })

@login_required(login_url="/login/")
def download_ticket(request, booking_id):
    booking = get_object_or_404(
        Booking.objects.select_related(
            "movie",
            "theater",
            "seat",
            "payment",
        ),
        id=booking_id,
        user=request.user,
    )

    if booking.is_cancelled or not booking.payment:
        raise Http404("No valid ticket for this booking.")

    bookings = list(
        Booking.objects.select_related(
            "movie",
            "theater",
            "seat",
        )
        .filter(
            payment=booking.payment,
            user=request.user,
            is_cancelled=False,
        )
        .order_by("id")
    )

    pdf_data = generate_ticket_pdf(
        booking.payment,
        bookings,
    )

    response = HttpResponse(
        pdf_data,
        content_type="application/pdf",
    )

    response[
        "Content-Disposition"
    ] = f'attachment; filename="BookMySeat_Ticket_{booking.payment.id}.pdf"'

    return response


def verify_ticket(request, booking_id):
    # Verification links carry a signed token. Plain sequential booking IDs
    # would let anyone enumerate other people's bookings.
    if not is_valid_verify_token(request.GET.get("t", ""), booking_id):
        raise Http404("Invalid ticket link.")

    booking = get_object_or_404(
        Booking.objects.select_related(
            "movie",
            "theater",
            "seat",
            "payment",
        ),
        id=booking_id,
    )

    is_valid = (
        not booking.is_cancelled
        and booking.payment is not None
        and booking.payment.status == Payment.STATUS_SUCCESS
    )

    return render(
        request,
        "movies/verify_ticket.html",
        {
            "booking": booking,
            "is_valid": is_valid,
        },
    )


@login_required(login_url="/login/")
@require_POST
def hold_seat(request, theater_id, seat_id):
    """Adds one seat to the user's selection (kept for compatibility; the page uses reserve_seats)."""
    theater = get_object_or_404(Theater, id=theater_id)
    current = list(
        theater.seats.filter(held_by=request.user, is_booked=False, held_until__gt=timezone.now())
        .values_list("id", flat=True)
    )
    return _reservation_response(
        lambda: reservations.reserve(request.user, theater, sorted(set(current) | {seat_id}))
    )


@login_required(login_url="/login/")
@require_POST
def release_seat(request, theater_id, seat_id):
    theater = get_object_or_404(Theater, id=theater_id)
    try:
        result = reservations.release(request.user, theater, [seat_id])
    except reservations.ReservationError as exc:
        return JsonResponse({"success": False, "reason": exc.reason}, status=exc.status)
    except reservations.ReservationUnavailable as exc:
        return JsonResponse({"success": False, "reason": exc.args[0], "retryable": True}, status=503)
    return JsonResponse({"success": True, "released": result["released"]})


@login_required(login_url="/login/")
@require_POST
def reserve_seats(request, theater_id):
    """
    Sets the user's whole selection for this show to exactly the posted seat IDs. Used for the first
    selection and for every change to it. All-or-nothing: a conflict changes nothing.
    Body: {"seat_ids": [1, 2, 3]}
    """
    theater = get_object_or_404(Theater, id=theater_id)
    if len(request.body or b"") > 2048:
        return JsonResponse({"success": False, "reason": "The request is too large."}, status=413)
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"success": False, "reason": "Invalid request."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"success": False, "reason": "Invalid request."}, status=400)
    return _reservation_response(
        lambda: reservations.reserve(request.user, theater, payload.get("seat_ids"))
    )


def _reservation_response(operation):
    """Turns the reservation service's outcome into a safe JSON response."""
    try:
        result = operation()
    except reservations.ReservationConflict as exc:
        return JsonResponse(
            {"success": False, "reason": exc.reason, "conflicts": exc.conflicts, "retryable": True},
            status=409,
        )
    except reservations.ReservationError as exc:
        return JsonResponse({"success": False, "reason": exc.reason}, status=exc.status)
    except reservations.ReservationUnavailable as exc:
        return JsonResponse({"success": False, "reason": exc.args[0], "retryable": True}, status=503)
    return JsonResponse({
        "success": True,
        "held_seat_ids": result["held_seat_ids"],
        "held_until": result["held_until"].isoformat(),
        "expires_in_seconds": result["expires_in_seconds"],
        "server_time": timezone.now().isoformat(),
    })


def seat_status(request, theater_id):
    """Read-only live map. Polling never extends a hold."""
    theater = get_object_or_404(Theater, id=theater_id)
    return JsonResponse({
        "seats": reservations.seat_states_for(request.user, theater),
        "server_time": timezone.now().isoformat(),
    })


@login_required(login_url="/login/")
@require_POST
def confirm_booking(request, theater_id):
    return JsonResponse(
        {
            "success": False,
            "reason": (
                "Bookings are confirmed only after successful "
                "payment verification. Please complete payment."
            ),
        },
        status=403,
    )



def get_razorpay_client():
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


@login_required(login_url='/login/')
@require_POST

def create_payment(request, theater_id):
    theater = get_object_or_404(Theater, id=theater_id)

    if theater.time <= timezone.now():
        return JsonResponse(
            {"success": False, "reason": "This show has already started."},
            status=400,
        )

    # Step 1 (short transaction, database work only): validate the holds, reuse a live order for
    # the same seats, or create the internal record BEFORE any provider call.
    with transaction.atomic():
        my_seats = list(
            Seat.objects.select_for_update().filter(theater=theater, held_by=request.user).order_by("id")
        )
        valid_seats = [s for s in my_seats if not s.is_booked and s.is_hold_active()]
        if not valid_seats:
            return JsonResponse(
                {"success": False, "reason": "Your seat hold has expired. Please select seats again."},
                status=400,
            )

        seat_ids = sorted(s.id for s in valid_seats)
        amount_rupees = theater.price_per_seat * len(valid_seats)
        amount_paise = int(amount_rupees * 100)

        live = (
            Payment.objects.select_for_update()
            .filter(user=request.user, theater=theater, status=Payment.STATUS_CREATED)
            .order_by("-created_at")
        )
        now = timezone.now()
        for existing in live:
            same_seats = sorted(existing.seats.values_list("id", flat=True)) == seat_ids
            if not same_seats or existing.amount != amount_rupees:
                continue
            if existing.razorpay_order_id:
                # Idempotent: the same checkout is already live. Return it instead of a second order.
                return _checkout_response(request, theater, existing, amount_paise)
            if now - existing.created_at < CHECKOUT_PREPARING_GRACE:
                # Another request is creating this order right now. Do not create a duplicate.
                return JsonResponse(
                    {"success": False, "reason": "Your payment is being prepared. Please wait a moment."},
                    status=409,
                )
            # A half-created record from a crashed request: close it, then create a fresh one.
            existing.status = Payment.STATUS_FAILED
            existing.failure_reason = "Checkout was not completed."
            existing.save(update_fields=["status", "failure_reason", "updated_at"])

        payment = Payment.objects.create(
            user=request.user,
            theater=theater,
            amount=amount_rupees,
            currency="INR",
            status=Payment.STATUS_CREATED,
            razorpay_order_id=None,
        )
        payment.seats.set(valid_seats)

    # Step 2 (NO database lock held): create the provider order.
    try:
        client = get_razorpay_client()
        razorpay_order = client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "receipt": f"bms_{payment.id}",
            "payment_capture": 1,
            "notes": {
                "user_id": str(request.user.id),
                "theater_id": str(theater.id),
                "payment_db_id": str(payment.id),
            },
        })
    except Exception:
        logger.exception("Razorpay order creation failed for theater %s", theater.id)
        Payment.objects.filter(pk=payment.pk, status=Payment.STATUS_CREATED).update(
            status=Payment.STATUS_FAILED,
            failure_reason="Payment service was unavailable at checkout.",
            failed_at=timezone.now(),
        )
        return JsonResponse(
            {"success": False, "reason": "Payment service is unavailable right now. Please try again."},
            status=503,
        )

    # Step 3 (short transaction): attach the order ID and supersede older unpaid orders for this
    # theater. Their holds are not released, because this order reuses the same seats.
    with transaction.atomic():
        Payment.objects.filter(pk=payment.pk).update(razorpay_order_id=razorpay_order["id"])
        Payment.objects.filter(
            user=request.user, theater=theater, status=Payment.STATUS_CREATED
        ).exclude(pk=payment.pk).update(status=Payment.STATUS_CANCELLED)
        payment.refresh_from_db()

    return _checkout_response(request, theater, payment, amount_paise, order_id=razorpay_order["id"])


def _checkout_response(request, theater, payment, amount_paise, order_id=None):
    """Only the public fields the checkout widget needs. No secrets, no provider metadata."""
    return JsonResponse({
        "success": True,
        "order_id": order_id or payment.razorpay_order_id,
        "amount_paise": amount_paise,
        "currency": payment.currency,
        "key_id": settings.RAZORPAY_KEY_ID,
        "payment_db_id": payment.id,
        "movie_name": theater.movie.name,
        "user_email": request.user.email,
    })

@login_required(login_url="/login/")
@require_POST
def verify_payment(request):
    order_id = request.POST.get("razorpay_order_id")
    payment_id = request.POST.get("razorpay_payment_id")
    signature = request.POST.get("razorpay_signature")

    if not (order_id and payment_id and signature):
        return JsonResponse(
            {"success": False, "reason": "Missing payment details."},
            status=400,
        )

    payment = get_object_or_404(
        Payment,
        razorpay_order_id=order_id,
        user=request.user,
    )

    # Idempotent: a repeated browser callback for a settled payment just reports success.
    if payment.status == Payment.STATUS_SUCCESS:
        return JsonResponse({"success": True, "redirect_url": "/profile/"})

    client = get_razorpay_client()

    try:
        client.utility.verify_payment_signature({
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        })
    except razorpay.errors.SignatureVerificationError:
        payment.mark_failed(reason="Signature verification failed.")
        return JsonResponse(
            {"success": False, "reason": "Payment verification failed."},
            status=400,
        )

    # Settles the payment: books the seats, or refunds if they were taken.
    payment = payment.mark_success(payment_id, signature, razorpay_client=client)

    if payment.status == Payment.STATUS_SUCCESS:
        # Non-blocking: the booking is already committed; the email is sent in the background.
        queue_ticket_email(payment.id)
        messages.success(request, "Payment successful! Your booking is confirmed.")
        return JsonResponse({"success": True, "redirect_url": "/profile/"})

    reason = payment.failure_reason or "Seats were no longer available."
    return JsonResponse({"success": False, "reason": reason}, status=409)
@login_required(login_url="/login/")
@require_GET
def payment_status(request, payment_id):
    payment = get_object_or_404(Payment, pk=payment_id, user=request.user)
    return JsonResponse({
        "status": payment.status,
        "amount": str(payment.amount),
        "currency": payment.currency,
        "failure_reason": payment.failure_reason,
    })

@login_required(login_url="/login/")
@require_POST
def record_payment_failure(request):
    """
    Called by the browser when Razorpay reports payment.failed. Marks the
    payment FAILED and releases the seats held for it, so the user can retry.
    """
    order_id = request.POST.get("razorpay_order_id", "").strip()
    reason = request.POST.get("reason", "").strip() or "Payment failed"

    payment = None
    if order_id:
        payment = Payment.objects.filter(
            razorpay_order_id=order_id,
            user=request.user,
        ).first()

    if payment is not None:
        payment.mark_failed(reason=reason)

    return JsonResponse({"success": True})


@login_required(login_url="/login/")
@require_POST
def cancel_payment(request , theater_id):
    theater = get_object_or_404(Theater,id=theater_id)
    payment = Payment.objects.filter(
        user=request.user, theater=theater ,status=Payment.STATUS_CREATED
    ).order_by("-created_at").first()

    if payment:
        payment.mark_cancelled()

    return JsonResponse( {"success":True})


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    secret = settings.RAZORPAY_WEBHOOK_SECRET

    # Fail closed. With an empty secret the HMAC key is public, so anyone could
    # forge a "payment.captured" event and get free bookings.
    if not secret:
        logger.error("Razorpay webhook called but RAZORPAY_WEBHOOK_SECRET is not set.")
        return JsonResponse({"error": "Webhook not configured"}, status=503)

    body = request.body
    received_signature = request.headers.get("X-Razorpay-Signature", "")
    expected_signature = hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(
        expected_signature.encode("utf-8"),
        received_signature.encode("utf-8"),
    ):
        return JsonResponse({"error": "Invalid signature"}, status=400)

    try:
        event = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "Malformed payload"}, status=400)

    event_id = request.headers.get("X-Razorpay-Event-Id", "")
    event_type = event.get("event", "")
    payload = event.get("payload") or {}
    entity = (payload.get("payment") or {}).get("entity") or {}
    order_id = entity.get("order_id")
    payment_id = entity.get("id")

    # Duplicate deliveries: a delivery with an event ID we already processed changes nothing.
    if event_id and WebhookEvent.objects.filter(event_id=event_id).exists():
        return JsonResponse({"status": "duplicate"})

    try:
        with transaction.atomic():
            payment = Payment.objects.filter(razorpay_order_id=order_id).first() if order_id else None
            result = WebhookEvent.RESULT_IGNORED

            if payment is not None and event_type == "payment.captured" and payment_id:
                # Verify the captured payment with Razorpay before settling anything.
                client = get_razorpay_client()
                provider_payment = client.payment.fetch(payment_id)
                if (provider_payment.get("order_id") == order_id
                        and provider_payment.get("status") == "captured"
                        and provider_payment.get("currency") == payment.currency
                        and provider_payment.get("amount") == int(payment.amount * 100)):
                    payment.mark_success(payment_id, "", razorpay_client=client)
                    result = WebhookEvent.RESULT_PROCESSED
                else:
                    result = WebhookEvent.RESULT_FAILED

            elif payment is not None and event_type == "payment.failed":
                payment.mark_failed(reason=entity.get("error_description") or "Payment failed")
                result = WebhookEvent.RESULT_PROCESSED

            if event_id:
                WebhookEvent.objects.create(event_id=event_id, event_type=event_type, result=result)
    except Exception:
        # Not recorded, and a 500 makes Razorpay redeliver the event later.
        logger.exception("Razorpay webhook processing failed for event %s", event_id)
        return JsonResponse({"error": "Temporary failure"}, status=500)

    if payment is not None and result == WebhookEvent.RESULT_PROCESSED and payment.status == Payment.STATUS_SUCCESS:
        queue_ticket_email(payment.id)

    return JsonResponse({"status": "ok"})

@login_required(login_url='/login/')
@require_POST
def report_review(request, review_id):
    """
    Records a report on a visible review. A user cannot report their own review, cannot report
    a hidden review, and can report each review only once.
    """
    review = get_object_or_404(Review, id=review_id, is_hidden=False)
    detail_url = redirect("movie_detail", slug=review.movie.slug)

    if review.user_id == request.user.id:
        messages.error(request, "You cannot report your own review.")
        return detail_url

    form = ReviewReportForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Please choose a valid reason for the report.")
        return detail_url

    report = form.save(commit=False)
    report.review = review
    report.reported_by = request.user
    try:
        with transaction.atomic():  # own savepoint so a duplicate does not break the request
            report.save()
    except IntegrityError:
        messages.info(request, "You have already reported this review.")
        return detail_url

    messages.success(request, "Thanks - this review has been reported to our moderators.")
    return detail_url


@login_required(login_url="/login/")
def delete_review(request, review_id):
    review = get_object_or_404(Review, id=review_id, user=request.user)
    movie_slug = review.movie.slug
    if request.method == "POST" :
        review.delete()
        messages.success(request , "Your review has been deleted.")
    return redirect("movie_detail", slug=movie_slug)


@login_required(login_url="/login/")
@require_POST
def cancel_booking(request, booking_id):
    """
    Lets a user cancel their own booking. If it was paid for, a real refund is
    issued via Razorpay first. The seat is released only after the refund succeeds.
    """
    booking = get_object_or_404(Booking, id=booking_id, user=request.user)

    if booking.is_cancelled:
        messages.info(request, "This booking had already been cancelled.")
        return redirect("profile")

    client = get_razorpay_client()

    try:
        locked = booking.cancel_and_refund(client)
    except BookingCancellationError as exc:
        messages.error(request, str(exc))
        return redirect("profile")

    if locked.refund_status == Booking.REFUND_REFUNDED:
        messages.success(request, f"Booking cancelled and ₹{locked.refund_amount} refunded successfully.")
    else:
        messages.success(request, "Booking cancelled successfully.")

    return redirect("profile")


def _parse_date_range(request):
    """
    Reads ?start=YYYY-MM-DD and ?end=YYYY-MM-DD. Returns (start, end, error).

    end is exclusive internally (the selected end date plus one day), so the selected end
    date is included in full. Invalid input is reported in `error`, not silently replaced:
    a malformed date, or a start later than the end, returns the default last-30-days range
    together with a message the page shows.
    """
    now = timezone.now()
    default_start = now - timedelta(days=30)
    default_end = now

    start_param = request.GET.get("start", "").strip()
    end_param = request.GET.get("end", "").strip()
    error = None

    start = default_start
    end = default_end

    try:
        if start_param:
            start = timezone.make_aware(datetime.strptime(start_param, "%Y-%m-%d"))
        if end_param:
            end = timezone.make_aware(datetime.strptime(end_param, "%Y-%m-%d")) + timedelta(days=1)
    except ValueError:
        error = "Dates must be in the format YYYY-MM-DD. Showing the last 30 days."
        return default_start, default_end, error

    if start >= end:
        error = "The start date must be before the end date. Showing the last 30 days."
        return default_start, default_end, error

    return start, end, error


class _IstDay(Func):
    """
    Local (IST) calendar day for a UTC timestamp.
    On SQLite, shifts by the fixed +05:30 offset and takes the date. This avoids a Python
    callback per row, which is the cost the benchmark measured. Other databases use the
    native timezone conversion, which is already set-based there.
    """
    output_field = DateField()

    def as_sqlite(self, compiler, connection, **extra):
        sql, params = compiler.compile(self.source_expressions[0])
        return f"DATE({sql}, '+5 hours', '+30 minutes')", params

    def as_sql(self, compiler, connection, **extra):
        return TruncDate(self.source_expressions[0]).as_sql(compiler, connection, **extra)


def _parse_filters(request):
    """
    Reads ?movie=<id> and ?theater=<venue name>. Returns (movie_id, theater_name, error).
    Invalid values are reported, never raised, so a bad link shows a message, not a crash.
    """
    error = None
    movie_raw = request.GET.get('movie', '').strip()
    theater_name = request.GET.get('theater', '').strip()

    movie_id = None
    if movie_raw:
        if movie_raw.isdigit():
            movie_id = int(movie_raw)
        else:
            error = 'Movie filter must be a numeric movie ID.'

    if len(theater_name) > 100:
        error = 'Theater filter is too long.'
        theater_name = ''

    return movie_id, theater_name or None, error


def _bucket_revenue(daily_rows, key_fn):
    """Sums day-level revenue rows into coarser buckets (week/month/year)."""
    buckets = {}
    for row in daily_rows:
        key = key_fn(row["day"])
        buckets[key] = buckets.get(key, Decimal("0")) + row["revenue"]
    return [{"period": key, "revenue": value} for key, value in sorted(buckets.items())]


@login_required(login_url="/login/")
@permission_required("movies.view_analytics", raise_exception=True)
def admin_dashboard(request):
    """
    Admin dashboard.

    Only authenticated staff/admin users can access this page.

    Analytics are generated using Django ORM aggregation rather than
    loading all Booking/Payment objects into Python memory.
    """

    start, end, date_error = _parse_date_range(request)
    movie_id, theater_name, filter_error = _parse_filters(request)

    # ============================================================
    # 1. SUCCESSFUL PAYMENTS

    successful_payments = Payment.objects.filter(
        status=Payment.STATUS_SUCCESS,
        created_at__gte=start,
        created_at__lt=end,
    )
    if movie_id is not None:
        successful_payments = successful_payments.filter(theater__movie_id=movie_id)
    if theater_name:
        successful_payments = successful_payments.filter(theater__name=theater_name)

    # ============================================================
    # 1-5. REVENUE (day, week, month, year, total)
    #
    # ONE grouped pass over payments, bucketed by day. Week, month and
    # year totals are sums of those day buckets, so they are computed here
    # in Python from at most ~366 rows. Before, each granularity was its own
    # full-table GROUP BY; on SQLite every TruncWeek/TruncMonth/TruncYear
    # call runs as a per-row Python callback, so four passes cost ~4x one.
    # ============================================================

    # Collected revenue: each SUCCESSFUL payment counted once, by its amount.
    # Refunds: completed refunds recorded on bookings (refund_status = refunded).
    # Net revenue = collected - refunded. Money is exact Decimal arithmetic throughout.
    
    daily_rows = list(
        successful_payments
        .annotate(day=_IstDay("created_at"))
        .values("day")
        .annotate(revenue=Sum("amount"))
        .order_by("day")
    )

    total_revenue = successful_payments.aggregate(
        total=Sum("amount")
    )["total"] or Decimal("0")

    refund_qs = Booking.objects.filter(
        refund_status=Booking.REFUND_REFUNDED,
        booked_at__gte=start,
        booked_at__lt=end,
    )
    if movie_id is not None:
        refund_qs = refund_qs.filter(movie_id=movie_id)
    if theater_name:
        refund_qs = refund_qs.filter(theater__name=theater_name)
    refunded_amount = refund_qs.aggregate(total=Sum("refund_amount"))["total"] or Decimal("0")
    
    net_revenue = total_revenue - refunded_amount
    
    
    revenue_by_day = [
        {"period": row["day"], "revenue": row["revenue"]} for row in daily_rows
    ]
    revenue_by_week = _bucket_revenue(
        daily_rows, lambda d: d - timedelta(days=d.weekday())  # ISO week starts Monday
    )
    revenue_by_month = _bucket_revenue(daily_rows, lambda d: d.replace(day=1))
    revenue_by_year = _bucket_revenue(daily_rows, lambda d: d.replace(month=1, day=1))

    # ==========================
    # 6. ACTIVE BOOKINGS
    # =====================================================

    active_bookings = Booking.objects.filter(
        is_cancelled=False,
        booked_at__gte=start,
        booked_at__lt=end,
    )
    if movie_id is not None:
        active_bookings = active_bookings.filter(movie_id=movie_id)
    if theater_name:
        active_bookings = active_bookings.filter(theater__name=theater_name)

    active_count = active_bookings.count()

    # ===============================
    # 7. BOOKING TREND
    # =============================

    booking_trend = list(
        active_bookings
        .annotate(period=_IstDay('booked_at'))
        .values('period')
        .annotate(count=Count('id'))
        .order_by('period')
    )

    # ============================================================
    # 8. THEATER OCCUPANCY
    # ============================================================

    # Per-show capacity and occupied seats, then combined per theater.
    # Occupied = seats with a NON-cancelled booking. Holds and cancelled seats do not count.
    # Weighted occupancy = total occupied seats / total seat capacity (not an average of percentages).
    show_occupancy = (
        Theater.objects
        .filter(time__gte=start, time__lt=end)
        .annotate(
            capacity=Count('seats', distinct=True),
            occupied=Count(
                'seats',
                filter=Q(seats__bookings__is_cancelled=False),
                distinct=True,
            ),
        )
        .filter(capacity__gt=0)
        .values('id', 'name', 'movie__name', 'time', 'capacity', 'occupied')
    )

    theater_totals = {}
    for show in show_occupancy:
        t = theater_totals.setdefault(show['name'], {
            'name': show['name'], 'shows': 0, 'capacity': 0, 'occupied': 0,
            'best': None, 'worst': None,
        })
        pct = (show['occupied'] * 100.0 / show['capacity']) if show['capacity'] else 0.0
        t['shows'] += 1
        t['capacity'] += show['capacity']
        t['occupied'] += show['occupied']
        detail = {'movie': show['movie__name'], 'time': show['time'], 'pct': round(pct, 1)}
        if t['best'] is None or pct > t['best']['pct']:
            t['best'] = detail
        if t['worst'] is None or pct < t['worst']['pct']:
            t['worst'] = detail

    occupancy_by_theater = []
    for t in theater_totals.values():
        t['weighted_pct'] = round(t['occupied'] * 100.0 / t['capacity'], 1) if t['capacity'] else 0.0
        occupancy_by_theater.append(t)
    occupancy_by_theater.sort(key=lambda t: t['weighted_pct'], reverse=True)

    # ============================================================
    # 9. MOST BOOKED MOVIES
    # ============================================================

    # Movie rankings. Each measure is its own grouped query so that joining bookings to payments
    # does not multiply revenue. Ranking is by confirmed bookings, then seats, then revenue.
    movie_bookings = (
        active_bookings
        .values('movie_id', 'movie__name', 'movie__release_date', 'movie__average_rating', 'movie__total_ratings')
        .annotate(bookings=Count('id'))
    )
    movie_seats = {
        row['movie_id']: row['seats']
        for row in active_bookings.values('movie_id').annotate(seats=Count('seat_id', distinct=True))
    }
    movie_revenue = {
        row['theater__movie_id']: row['revenue'] or Decimal('0')
        for row in (
            successful_payments
            .values('theater__movie_id')
            .annotate(revenue=Sum('amount'))
        )
    }

    top_movies = []
    for row in movie_bookings:
        top_movies.append({
            'movie_id': row['movie_id'],
            'name': row['movie__name'],
            'release_date': row['movie__release_date'],
            'bookings': row['bookings'],
            'seats': movie_seats.get(row['movie_id'], 0),
            'revenue': movie_revenue.get(row['movie_id'], Decimal('0')),
            'average_rating': row['movie__average_rating'],
            'review_count': row['movie__total_ratings'],
        })
    top_movies.sort(key=lambda m: (-m['bookings'], -m['seats'], -m['revenue']))
    top_movies = top_movies[:10]

    # ============================================================
    # 10. TOP-PERFORMING THEATERS
    # ============================================================

    # Theater performance, ranked by collected revenue (successful payments only).
    # Occupancy and cancellation rate are shown beside the ranking, not mixed into the order.
    venue_revenue = (
        successful_payments
        .values('theater__name')
        .annotate(revenue=Sum('amount'), transactions=Count('id'))
    )
    venue_bookings = (
        active_bookings
        .values('theater__name')
        .annotate(bookings=Count('id'), seats=Count('seat_id', distinct=True))
    )
    venue_cancelled = {
        row['theater__name']: row
        for row in (
            Booking.objects
            .filter(booked_at__gte=start, booked_at__lt=end)
            .values('theater__name')
            .annotate(cancelled=Count('id', filter=Q(is_cancelled=True)),
                      total=Count('id'))
        )
    }
    venue_bookings_map = {row['theater__name']: row for row in venue_bookings}
    venue_occupancy = {}
    for show in occupancy_by_theater:
        v = venue_occupancy.setdefault(show['name'], {'capacity': 0, 'occupied': 0})
        v['capacity'] += show['capacity']
        v['occupied'] += show['occupied']

    top_theaters = []
    venue_names = set(row['theater__name'] for row in venue_revenue) | set(venue_bookings_map)
    revenue_map = {row['theater__name']: row for row in venue_revenue}
    for name in venue_names:
        row = revenue_map.get(name, {'revenue': Decimal('0'), 'transactions': 0})
        booked = venue_bookings_map.get(name, {})
        occ = venue_occupancy.get(name, {'capacity': 0, 'occupied': 0})
        cancel_info = venue_cancelled.get(name, {'cancelled': 0, 'total': 0})
        top_theaters.append({
            'name': name,
            'revenue': row['revenue'] or Decimal('0'),
            'transactions': row['transactions'],
            'bookings': booked.get('bookings', 0),
            'seats': booked.get('seats', 0),
            'weighted_occupancy': round(occ['occupied'] * 100.0 / occ['capacity'], 1) if occ['capacity'] else None,
            'cancellation_rate': round(cancel_info['cancelled'] * 100.0 / cancel_info['total'], 1)
                                 if cancel_info['total'] else None,
        })
    top_theaters.sort(key=lambda t: (-t['revenue'], -t['bookings'], t['name']))
    top_theaters = top_theaters[:10]
    # ============================================================
    # 11. PEAK BOOKING HOURS
    # ============================================================

    peak_hours = list(
        active_bookings
        .annotate(
            hour=ExtractHour('booked_at')
        )
        .values('hour')
        .annotate(
            count=Count('id')
        )
        .order_by('hour')
    )

    # ============================================================
    # 12. CANCELLATION + REFUND STATISTICS
    # ============================================================

    # Cancellations and refunds, from the booking records. Only states the existing refund flow
    # actually writes are reported: 'refunded'. 'processing' and 'failed' are not written by the
    # current code, so they are reported as 0 with a note rather than guessed.
    cancelled_in_range = Booking.objects.filter(
        is_cancelled=True,
        cancelled_at__gte=start,
        cancelled_at__lt=end,
    )
    refunded_in_range = Booking.objects.filter(
        refund_status=Booking.REFUND_REFUNDED,
        booked_at__gte=start,
        booked_at__lt=end,
    )

    cancellation_stats = refunded_in_range.aggregate(
        cancelled_bookings=Count('id', filter=Q(is_cancelled=True)),
        refunded_bookings=Count('id'),
        refunded_amount=Sum('refund_amount'),
    )
    cancellation_stats['cancelled_bookings'] = cancelled_in_range.count()
    cancellation_stats['refunded_amount'] = cancellation_stats['refunded_amount'] or Decimal('0')
    cancellation_stats['processing_refunds'] = 0
    cancellation_stats['failed_refunds'] = 0
    cancellation_stats['refund_status_note'] = (
        'Pending and failed refunds are not recorded by the current refund flow, so they show as 0.'
    )

    # Partial vs full: a refund is full when it equals the booking's share of its payment.
    # A booking's share = payment.amount / number of bookings on that payment.
    refund_split = refunded_in_range.annotate(
        seats_on_payment=Count('payment__bookings'),
    ).aggregate(
        full=Count('id', filter=Q(refund_amount=F('payment__amount') / F('seats_on_payment'))),
        partial=Count('id', filter=~Q(refund_amount=F('payment__amount') / F('seats_on_payment'))),
    )
    cancellation_stats['full_refunds'] = refund_split['full'] or 0
    cancellation_stats['partial_refunds'] = refund_split['partial'] or 0

    # ============================================================
    # 13. USER GROWTH
    # ============================================================

    # User growth: registrations in the selected range, the total registered users,
    # and growth against the previous period of equal length (exact, not averaged).
    period_length = end - start
    previous_start = start - period_length
    new_users = User.objects.filter(date_joined__gte=start, date_joined__lt=end).count()
    previous_new_users = User.objects.filter(
        date_joined__gte=previous_start, date_joined__lt=start,
    ).count()
    total_users = User.objects.count()
    if previous_new_users:
        user_growth_pct = round((new_users - previous_new_users) * 100.0 / previous_new_users, 1)
    else:
        user_growth_pct = None  # no previous period to compare against

    user_growth = list(
        User.objects
        .filter(
            date_joined__gte=start,
            date_joined__lt=end,
        )
        .annotate(
            period=TruncDate('date_joined')
        )
        .values('period')
        .annotate(
            count=Count('id')
        )
        .order_by('period')
    )

    # ============================================================
    # 14. CONTEXT
    # ============================================================

    context = {
        'start': start.date(),
        'end': (end - timedelta(days=1)).date(),

        'total_revenue': total_revenue,
        'filter_movie_id': movie_id,
        'filter_theater': theater_name,
        'filter_error': filter_error,
        'date_error': date_error,
        'refunded_amount': refunded_amount,
        'net_revenue': net_revenue,
        'active_count': active_count,

        'revenue_by_day': revenue_by_day,
        'revenue_by_week': revenue_by_week,
        'revenue_by_month': revenue_by_month,
        'revenue_by_year': revenue_by_year,

        'booking_trend': booking_trend,

        'occupancy_by_theater': occupancy_by_theater,

        'top_movies': top_movies,
        'top_theaters': top_theaters,

        'peak_hours': peak_hours,

        'cancellation_stats': cancellation_stats,

        'user_growth': user_growth,
        
        'new_users': new_users,
        'total_users': total_users,
        'user_growth_pct': user_growth_pct,

        # JSON for Chart.js
        'revenue_by_day_json': json.dumps(
            revenue_by_day,
            cls=DjangoJSONEncoder,
        ),

        'revenue_by_week_json': json.dumps(
            revenue_by_week,
            cls=DjangoJSONEncoder,
        ),

        'revenue_by_month_json': json.dumps(
            revenue_by_month,
            cls=DjangoJSONEncoder,
        ),

        'revenue_by_year_json': json.dumps(
            revenue_by_year,
            cls=DjangoJSONEncoder,
        ),

        'booking_trend_json': json.dumps(
            booking_trend,
            cls=DjangoJSONEncoder,
        ),

        'peak_hours_json': json.dumps(
            peak_hours,
            cls=DjangoJSONEncoder,
        ),

        'user_growth_json': json.dumps(
            user_growth,
            cls=DjangoJSONEncoder,
        ),
    }

    return render(
        request,
        'movies/admin_dashboard.html',
        context,
    )


@login_required(login_url="/login/")
@permission_required("movies.view_analytics", raise_exception=True)
def admin_dashboard_export_csv(request):
    """
    Streams booking data as CSV.

    Uses iterator() so 100,000+ bookings are not loaded into
    memory simultaneously.
    """

    start, end, date_error = _parse_date_range(request)
    movie_id, theater_name, _filter_error = _parse_filters(request)
    
    export_qs = Booking.objects.filter(
            booked_at__gte=start,
            booked_at__lt=end,
    )
    if movie_id is not None:
        export_qs = export_qs.filter(movie_id=movie_id)
    if theater_name:
        export_qs = export_qs.filter(theater__name=theater_name)
       

    class Echo:
        def write(self, value):
            return value

    def row_generator():

        writer = csv.writer(Echo())

        # CSV header
        yield writer.writerow([
            'Booking ID',
            'User',
            'Movie',
            'Theater',
            'Seat',
            'Booked At',
            'Cancelled',
            'Refund Status',
            'Refund Amount',
        ])

        # values_list() returns plain tuples: no model instances are built and
        # no related objects are fetched per row. This is the main CSV cost.
       
        rows = (
            export_qs
            .order_by('id')
            .values_list(
                'id',
                'user__username',
                'movie__name',
                'theater__name',
                'seat__seat_number',
                'booked_at',
                'is_cancelled',
                'refund_status',
                'refund_amount',
            )
            .iterator(chunk_size=5000)
        )

        for (
            booking_id, username, movie_name, theater_name, seat_number,
            booked_at, is_cancelled, refund_status, refund_amount,
        ) in rows:
             yield writer.writerow([
                _csv_safe(booking_id),
                _csv_safe(username),
                _csv_safe(movie_name),
                _csv_safe(theater_name),
                _csv_safe(seat_number),
                _csv_safe(timezone.localtime(booked_at).strftime('%Y-%m-%d %H:%M')),
                _csv_safe(is_cancelled),
                _csv_safe(refund_status),
                _csv_safe(refund_amount if refund_amount is not None else ''),
            ])

    response = StreamingHttpResponse(
        row_generator(),
        content_type='text/csv',
    )

    response['Content-Disposition'] = (
        f'attachment; '
        f'filename="bookings_'
        f'{start.date()}_to_'
        f'{(end - timedelta(days=1)).date()}.csv"'
    )

    return response
