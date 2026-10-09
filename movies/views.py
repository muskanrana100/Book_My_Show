import csv
import hmac
import hashlib
import json
from datetime import datetime, timedelta
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
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.core.paginator import Paginator
from django.db.models import Count, Sum, Avg, F, Q, OuterRef, Subquery, Min, Exists
from django.db.models.functions import TruncDate, TruncWeek, TruncMonth, TruncYear, ExtractHour
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .forms import ReviewForm, ReviewReportForm
from .models import Booking,Genre, Language, Movie, Payment, Review,RecentlyViewed, Seat, Theater


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

    # ============================================================
    # 1. BASE MOVIE QUERY
    # ============================================================

    movies = Movie.objects.all()

    # ============================================================
    # 2. SEARCH
    # ============================================================

    if search_query:
        movies = movies.filter(
            name__icontains=search_query
        )

    # ============================================================
    # 3. GENRE
    # ============================================================

    if genre_id:
        movies = movies.filter(
            genres__id=genre_id
        )

    # ============================================================
    # 4. LANGUAGE
    # ============================================================

    if language_id:
        movies = movies.filter(
            languages__id=language_id
        )

    # ============================================================
    # 5. RATING
    # ============================================================

    if min_rating:
        try:
            rating = float(min_rating)

            movies = movies.filter(
                average_rating__gte=rating
            )

        except (ValueError, TypeError):
            pass

    # ============================================================
    # 6. RELEASE DATE
    # ============================================================

    if release_from:
        try:
            release_start = datetime.strptime(
                release_from,
                '%Y-%m-%d'
            ).date()

            movies = movies.filter(
                release_date__gte=release_start
            )

        except ValueError:
            pass

    if release_to:
        try:
            release_end = datetime.strptime(
                release_to,
                '%Y-%m-%d'
            ).date()

            movies = movies.filter(
                release_date__lte=release_end
            )

        except ValueError:
            pass

    # ============================================================
    # 7. THEATER FILTERS
    #
    # IMPORTANT:
    # Use EXISTS instead of joining Theater directly.
    # This prevents duplicate Movie rows and avoids the expensive
    # DISTINCT operation over the large joined query.
    # ============================================================

    theater_filter = Q()

    if city:
        theater_filter &= Q(
            city__icontains=city
        )

    if theater_id:
        theater_filter &= Q(
            id=theater_id
        )

    # Show date FROM
    if date_from:
        try:
            show_date_from = datetime.strptime(
                date_from,
                '%Y-%m-%d'
            ).date()

            theater_filter &= Q(
                time__date__gte=show_date_from
            )

        except ValueError:
            pass

    # Show date TO
    if date_to:
        try:
            show_date_to = datetime.strptime(
                date_to,
                '%Y-%m-%d'
            ).date()

            theater_filter &= Q(
                time__date__lte=show_date_to
            )

        except ValueError:
            pass

    # ============================================================
    # 8. SHOW-TIME FILTER
    # ============================================================

    SHOW_TIME_SLOTS = {
        'morning': (6, 12),
        'afternoon': (12, 16),
        'evening': (16, 20),
        'night': (20, 24),
    }

    if show_time_slot in SHOW_TIME_SLOTS:

        start_hour, end_hour = SHOW_TIME_SLOTS[
            show_time_slot
        ]

        theater_filter &= Q(
            time__hour__gte=start_hour,
            time__hour__lt=end_hour
        )

    # Apply theater existence check only when a theater filter
    # has actually been selected.
    if theater_filter:

        matching_theater = Theater.objects.filter(
            movie=OuterRef('pk')
        ).filter(
            theater_filter
        )

        movies = movies.filter(
            Exists(matching_theater)
        )

    # ============================================================
    # 9. REMOVE DUPLICATES FROM GENRE/LANGUAGE JOINS
    # ============================================================

    movies = movies.distinct()

    # ============================================================
    # 10. POPULARITY
    #
    # Booking has related_name='bookings'.
    #
    # Count only non-cancelled bookings.
    # distinct=True prevents duplicate counts if the movie query
    # has joins through genres/languages.
    # ============================================================

    movies = movies.annotate(
        booking_count=Count(
            'bookings',
            filter=Q(
                bookings__is_cancelled=False
            ),
            distinct=True
        )
    )

    # ============================================================
    # 11. LOWEST TICKET PRICE
    # ============================================================

    lowest_price_sq = (
        Theater.objects
        .filter(
            movie=OuterRef('pk')
        )
        .values('movie')
        .annotate(
            minimum_price=Min(
                'price_per_seat'
            )
        )
        .values('minimum_price')
    )

    movies = movies.annotate(
        lowest_price=Subquery(
            lowest_price_sq
        )
    )

    # ============================================================
    # 12. SORTING
    # ============================================================

    sort_map = {
        'popularity': '-booking_count',
        'newest': '-release_date',
        'rating': '-average_rating',
        'price_low': 'lowest_price',
        'price_high': '-lowest_price',
    }

    movies = movies.order_by(
        sort_map.get(
            sort_by,
            '-booking_count'
        )
    )

    # ============================================================
    # 13. TOTAL COUNT
    # ============================================================

    total_count = movies.count()

    # ============================================================
    # 14. PAGINATION
    # ============================================================

    paginator = Paginator(
        movies,
        12
    )

    page_number = request.GET.get(
        'page',
        1
    )

    page_obj = paginator.get_page(
        page_number
    )

    # ============================================================
    # 15. RECOMMENDATIONS
    # ============================================================

    recommended_movies = []

    if request.user.is_authenticated:
        recommended_movies = _get_recommendations(
            request.user
        )

    # ============================================================
    # 16. CONTEXT
    # ============================================================

    context = {
        'movies': page_obj,
        'page_obj': page_obj,

        'total_count': total_count,

        'genres': Genre.objects.all(),
        'languages': Language.objects.all(),

        'theaters': (
            Theater.objects
            .order_by('name')
            .values(
                'id',
                'name'
            )
            .distinct()
        ),

        'recommended_movies': recommended_movies,

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

    return render(
        request,
        'movies/movie_list.html',
        context
    )


def _get_recommendations(user, limit=8):
    """
    Recommends movies sharing a genre or language with what the user has
    either booked or recently viewed, excluding movies they've already
    booked. Falls back to trending movies if there's no history yet.
    """
    booked_movie_ids = Booking.objects.filter(user=user).values_list('movie_id', flat=True)
    viewed_movie_ids = RecentlyViewed.objects.filter(user=user).values_list('movie_id', flat=True)
    interest_movie_ids = list(booked_movie_ids) + list(viewed_movie_ids)

    if not interest_movie_ids:
        return Movie.objects.order_by('-total_ratings', '-average_rating')[:limit]

    genre_ids = Genre.objects.filter(movies__id__in=interest_movie_ids).values_list('id', flat=True)
    language_ids = Language.objects.filter(movies__id__in=interest_movie_ids).values_list('id', flat=True)

    recommended = (
        Movie.objects.filter(Q(genres__id__in=genre_ids) | Q(languages__id__in=language_ids))
        .exclude(id__in=booked_movie_ids)
        .distinct()
        .order_by('-average_rating', '-total_ratings')[:limit]
    )
    return recommended

def movie_detail(request, slug):
    movie = get_object_or_404(
        Movie.objects.prefetch_related(
            "genres",
            "languages",
            "gallery_images",
            "movie_cast__cast_member",
        ),
        slug=slug
    )

    reviews = (
        movie.reviews
        .filter(is_hidden=False)
        .select_related("user")
    )

    if request.user.is_authenticated:
        try:
            RecentlyViewed.objects.update_or_create(
                user=request.user,
                movie=movie
            )
        except OperationalError:
            pass

    user_review = None
    can_review = False
    review_form = None

    if request.user.is_authenticated:
        user_review = reviews.filter(
            user=request.user
        ).first()

        has_watched = Booking.objects.filter(
            user=request.user,
            movie=movie,
            theater__time__lt=timezone.now()
        ).exists()

        can_review = has_watched and user_review is None

        if request.method == "POST" and "submit_review" in request.POST:

            if user_review:
                review_form = ReviewForm(
                    request.POST,
                    instance=user_review
                )

            elif can_review:
                review_form = ReviewForm(request.POST)

            else:
                messages.error(
                    request,
                    "You can only review a movie after booking and watching it."
                )
                review_form = None

            if review_form and review_form.is_valid():
                review = review_form.save(commit=False)
                review.movie = movie
                review.user = request.user
                review.save()

                messages.success(
                    request,
                    "Your review has been saved."
                )

                return redirect(
                    "movie_detail",
                    slug=movie.slug
                )

        else:
            if user_review:
                review_form = ReviewForm(
                    instance=user_review
                )
            elif can_review:
                review_form = ReviewForm()

    context = {
        "movie": movie,
        "reviews": reviews,
        "user_review": user_review,
        "can_review": can_review,
        "review_form": review_form,
        "similar_movies": movie.similar_movies(),
        "trending_movies": Movie.objects.exclude(
            id=movie.id
        ).order_by(
            "-total_ratings",
            "-average_rating"
        )[:8],
        "recent_movies": Movie.objects.exclude(
            id=movie.id
        ).order_by(
            "-release_date"
        )[:8],
    }

    return render(
        request,
        "movies/movie_detail.html",
        context
    )

    
def theater_list(request, movie_id):
    movie = get_object_or_404(Movie, id=movie_id)
    theaters = Theater.objects.filter(movie=movie)
    return render(request, "movies/theater_list.html" , {"movie" : movie ,"theaters": theaters})


@login_required(login_url='/login/')
def seat_selection(request, theater_id):
    theater = get_object_or_404(Theater, id=theater_id)
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

    if not booking.payment:
        return HttpResponse(
            "No payment information found.",
            status=404,
        )

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
    with transaction.atomic():
        seat = get_object_or_404(
            Seat.objects.select_for_update() , id=seat_id,theater_id=theater_id)

        if seat.is_booked:
            return JsonResponse( {"success":False, 'reason' : "This seat is already booked."}, status=409)

        if seat.is_hold_active() and seat.held_by_id != request.user.id:
            return JsonResponse({ "success" : False, "reason":"This seat is currently held by another user."} ,status=409)

        seat.held_by = request.user
        seat.held_until = timezone.now() + Seat.HOLD_DURATION
        seat.save(update_fields=["held_by","held_until" ])

    return JsonResponse({'success': True, "held_until" : seat.held_until.isoformat()})


@login_required(login_url="/login/")
@require_POST
def release_seat(request, theater_id, seat_id):
    with transaction.atomic():
        seat = get_object_or_404(
            Seat.objects.select_for_update(), id=seat_id, theater_id=theater_id
        )
        if seat.held_by_id == request.user.id:
            seat.held_by = None
            seat.held_until = None
            seat.save(update_fields=["held_by", "held_until"])

    return JsonResponse({"success" : True} )


def seat_status(request, theater_id):
    theater = get_object_or_404(Theater, id=theater_id)
    seats = Seat.objects.filter(theater=theater)
    data = [ {"id": seat.id, "status": seat.status_for(request.user) } for seat in seats]
    return JsonResponse({"seats" : data})


@login_required(login_url="/login/")
@require_POST
def confirm_booking(request, theater_id):
    theater = get_object_or_404(Theater, id=theater_id)
    booked_seat_numbers = []
    failed_seats = []

    with transaction.atomic():
        my_seats = Seat.objects.select_for_update().filter( theater=theater, held_by=request.user)

        if not my_seats.exists():
            return JsonResponse({'success': False ,"reason" :"You have no held seats. Please select seats again."} , status=400)

        for seat in my_seats:
            if seat.is_booked or not seat.is_hold_active():
                failed_seats.append(seat.seat_number)
                continue
            try:
                Booking.objects.create(user=request.user, seat=seat, movie=theater.movie, theater=theater)
            except IntegrityError:
                failed_seats.append(seat.seat_number)
                continue

            seat.is_booked = True
            seat.held_by = None
            seat.held_until = None
            seat.save(update_fields=['is_booked',"held_by", "held_until"])
            booked_seat_numbers.append(seat.seat_number)

    if failed_seats and not booked_seat_numbers:
        return JsonResponse({
            "success" : False,
            "reason" : f"These seats are no longer available: {', '.join(failed_seats)}. Please reselect.",
        }, status=409)

    if failed_seats:
        messages.warning(
            request, f"Some seats couldn't be booked (already taken): {', '.join(failed_seats)}."
        )

    messages.success(request, f"Booking confirmed for seats: {', '.join(booked_seat_numbers)}.")
    return JsonResponse({"success": True, "redirect_url" : '/profile/'})


def get_razorpay_client():
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


@login_required(login_url='/login/')
@require_POST
def create_payment(request, theater_id):
    theater = get_object_or_404(Theater, id=theater_id)

    with transaction.atomic():
        my_seats = list(
            Seat.objects.select_for_update().filter(theater=theater, held_by=request.user)
        )

        valid_seats = [s for s in my_seats if not s.is_booked and s.is_hold_active()]
        if not valid_seats:
            return JsonResponse(
                {"success" : False,"reason": "Your seat hold has expired. Please select seats again."  }, status=400,
            )

        amount_rupees = theater.price_per_seat * len(valid_seats)
        amount_paise = int(amount_rupees * 100)

        client = get_razorpay_client()
        razorpay_order = client.order.create({
            "amount" : amount_paise,
            "currency": 'INR',
            "payment_capture" : 1,
            "notes" : {
                "user_id" : str(request.user.id),
                "theater_id": str(theater.id),
            } ,
        } )

        payment = Payment.objects.create(
            user=request.user,
            theater=theater,
            amount=amount_rupees,
            razorpay_order_id=razorpay_order['id'],
        )
        payment.seats.set(valid_seats)

    return JsonResponse({
        "success" : True ,
        "order_id" : razorpay_order['id'],
        "amount_paise":amount_paise,
        "currency" : "INR",
        "key_id" :settings.RAZORPAY_KEY_ID ,
        "payment_db_id" : payment.id ,
        "movie_name" : theater.movie.name,
        "user_email": request.user.email,
    })


@login_required(login_url="/login/")
@require_POST
def verify_payment(request):
    order_id = request.POST.get("razorpay_order_id")
    payment_id = request.POST.get("razorpay_payment_id")
    signature = request.POST.get("razorpay_signature")

    # Check required Razorpay details
    if not (order_id and payment_id and signature):
        return JsonResponse(
            {
                "success": False,
                "reason": "Missing payment details.",
            },
            status=400,
        )

    # Find the payment belonging to the logged-in user
    payment = get_object_or_404(
        Payment,
        razorpay_order_id=order_id,
        user=request.user,
    )

    client = get_razorpay_client()

    # Verify Razorpay signature
    try:
        client.utility.verify_payment_signature(
            {
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": signature,
            }
        )

    except razorpay.errors.SignatureVerificationError:
        payment.mark_failed(
            reason="Signature verification failed."
        )

        return JsonResponse(
            {
                "success": False,
                "reason": "Payment verification failed.",
            },
            status=400,
        )

    # Mark payment successful
    # This also creates the Booking records.
    payment.mark_success(
        payment_id,
        signature,
    )

    # Queue ticket email asynchronously
    from .tasks import send_booking_ticket_email

    send_booking_ticket_email.delay(payment.id)

    messages.success(
        request,
        "Payment successful! Your booking is confirmed.",
    )

    return JsonResponse(
        {
            "success": True,
            "redirect_url": "/profile/",
        }
    ) 

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
    body = request.body

    received_signature = request.headers.get(
        "X-Razorpay-Signature",
        "",
    )

    # Generate expected webhook signature
    expected_signature = hmac.new(
        key=settings.RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    # Verify webhook signature
    if not hmac.compare_digest(
        expected_signature,
        received_signature,
    ):
        return JsonResponse(
            {
                "error": "Invalid signature",
            },
            status=400,
        )

    # Read webhook data
    event = json.loads(body)
    event_type = event.get("event")

    # ============================================================
    # PAYMENT CAPTURED
    # ============================================================

    if event_type == "payment.captured":

        payload = event["payload"]["payment"]["entity"]

        order_id = payload["order_id"]
        payment_id = payload["id"]

        try:
            payment = Payment.objects.get(
                razorpay_order_id=order_id
            )

            # Mark payment successful
            payment.mark_success(
                payment_id,
                razorpay_signature="verified-via-webhook",
            )

            # Send ticket email asynchronously
            from .tasks import send_booking_ticket_email

            send_booking_ticket_email.delay(payment.id)

        except Payment.DoesNotExist:
            pass

    # ============================================================
    # PAYMENT FAILED
    # ============================================================

    elif event_type == "payment.failed":

        payload = event["payload"]["payment"]["entity"]

        order_id = payload["order_id"]

        reason = payload.get(
            "error_description",
            "Payment failed",
        )

        try:
            payment = Payment.objects.get(
                razorpay_order_id=order_id
            )

            payment.mark_failed(
                reason=reason
            )

        except Payment.DoesNotExist:
            pass

    return JsonResponse(
        {
            "status": "ok",
        }
    )

    
@login_required(login_url='/login/')
def report_review(request, review_id):
    review = get_object_or_404(Review, id=review_id)
    if request.method == "POST" :
        form = ReviewReportForm(request.POST)
        if form.is_valid():
            report = form.save(commit= False)
            report.review = review
            report.reported_by = request.user
            try:
                report.save()
                messages.success(request,"Thanks - this review has been reported to our moderators.")
            except IntegrityError:
                messages.success(request , "You have already reported this review" )

    return redirect("movie_detail" , slug=review.movie.slug)


@login_required(login_url="/login/")
def delete_review(request, review_id):
    review = get_object_or_404(Review, id=review_id, user=request.user)
    movie_slug = review.movie.slug
    if request.method == "POST" :
        review.delete()
        messages.success(request , "Your review has been deleted.")
    return redirect("movie_detail", slug=movie_slug)


@login_required(login_url= "/login/")
@require_POST
def cancel_booking(request, booking_id):
    """
    Lets a user cancel their own booking. If it was paid for, this issues
    a real refund via Razorpay before releasing the seat back to availability.
    """
    booking = get_object_or_404(Booking, id=booking_id, user=request.user)

    if booking.is_cancelled:
        messages.info(request, "This booking had already been cancelled." )
        return redirect("profile")

    client = get_razorpay_client()
    booking.cancel_and_refund(client)

    if booking.refund_status == Booking.REFUND_REFUNDED:
        messages.success(request, f"Booking cancelled and ₹{booking.refund_amount} refunded successfully." )
    elif booking.refund_status == Booking.REFUND_FAILED:
        messages.warning(request , "Booking cancelled, but the refund could not be processed automatically. Our team will follow up.")
    else:
        messages.success(request ,"Booking cancelled successfully.")

    return redirect('profile')


def _parse_date_range(request):
    """
    Reads:
        ?start=YYYY-MM-DD&end=YYYY-MM-DD

    If no dates are supplied, defaults to the last 30 days.
    The end date is converted to the next day so that the
    selected end date is included completely.
    """

    now = timezone.now()

    # Default: last 30 days
    default_end = now
    default_start = now - timedelta(days=30)

    start_param = request.GET.get("start")
    end_param = request.GET.get("end")

    start = default_start
    end = default_end

    if start_param:
        try:
            start = timezone.make_aware(
                datetime.strptime(start_param, "%Y-%m-%d" )
            )
        except ValueError:
            pass

    if end_param :
        try:
            end = timezone.make_aware(
                datetime.strptime(end_param, '%Y-%m-%d')
            )+ timedelta(days=1)
        except ValueError:
            pass

    # Prevent invalid ranges
    if start>= end:
        start = default_start
        end= default_end

    return start, end


@staff_member_required
def admin_dashboard(request):
    """
    Admin dashboard.

    Only authenticated staff/admin users can access this page.

    Analytics are generated using Django ORM aggregation rather than
    loading all Booking/Payment objects into Python memory.
    """

    start, end = _parse_date_range(request)

    # ============================================================
    # 1. SUCCESSFUL PAYMENTS

    successful_payments = Payment.objects.filter(
        status=Payment.STATUS_SUCCESS,
        created_at__gte=start,
        created_at__lt=end,
    )

    # Total revenue
    total_revenue = (
        successful_payments
        .aggregate(total=Sum('amount'))
        ['total']
        or Decimal('0')
    )


    # ============================================================
    # 2. REVENUE BY DAY
    # ============================================================

    revenue_by_day = list(
        successful_payments
        .annotate(period=TruncDate("created_at"))
        .values("period")
        .annotate(revenue=Sum("amount"))
        .order_by("period")
    )

    # ============================================================
    # 3. REVENUE BY WEEK

    revenue_by_week = list(
        successful_payments
        .annotate(period=TruncWeek("created_at"))
        .values("period")
        .annotate(revenue=Sum("amount"))
        .order_by("period")
    )

    # ============================================================
    # 4. REVENUE BY MONTH
    # ============================================================

    revenue_by_month = list(
        successful_payments
        .annotate(period=TruncMonth("created_at"))
        .values('period')
        .annotate(revenue=Sum("amount" ))
        .order_by("period")
    )

    # ============================================================
    # 5. REVENUE BY YEAR

    revenue_by_year = list(
        successful_payments
        .annotate(period=TruncYear("created_at"))
        .values("period" )
        .annotate(revenue=Sum("amount"))
        .order_by("period")
    )

    # ==========================
    # 6. ACTIVE BOOKINGS
    # =====================================================

    active_bookings = Booking.objects.filter(
        is_cancelled=False,
        booked_at__gte=start,
        booked_at__lt=end,
    )

    active_count = active_bookings.count()

    # ===============================
    # 7. BOOKING TREND
    # =============================

    booking_trend = list(
        active_bookings
        .annotate(period=TruncDate('booked_at'))
        .values('period')
        .annotate(count=Count('id'))
        .order_by('period')
    )

    # ============================================================
    # 8. THEATER OCCUPANCY
    # ============================================================

    occupancy_by_theater = list(
        Theater.objects
        .filter(
            time__gte=start,
            time__lt=end,
        )
        .annotate(
            total_seats=Count(
                'seats',
                distinct=True,
            ),
            booked_seats=Count(
                'seats',
                filter=Q(seats__is_booked=True),
                distinct=True,
            ),
        )
        .filter(total_seats__gt=0)
        .annotate(
            occupancy_pct=(
                F('booked_seats') * 100.0 / F('total_seats')
            )
        )
        .values(
            'name',
            'movie__name',
            'total_seats',
            'booked_seats',
            'occupancy_pct',
        )
        .order_by('-occupancy_pct')[:20]
    )

    # ============================================================
    # 9. MOST BOOKED MOVIES
    # ============================================================

    top_movies = list(
        active_bookings
        .values('movie__name')
        .annotate(
            bookings=Count('id')
        )
        .order_by('-bookings')[:10]
    )

    # ============================================================
    # 10. TOP-PERFORMING THEATERS
    # ============================================================

    top_theaters = list(
        active_bookings
        .values('theater__name')
        .annotate(
            bookings=Count('id'),
            revenue=Sum('theater__price_per_seat'),
        )
        .order_by('-revenue')[:10]
    )

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

    cancellation_stats = (
        Booking.objects
        .filter(
            booked_at__gte=start,
            booked_at__lt=end,
        )
        .aggregate(
            total=Count('id'),

            cancelled=Count(
                'id',
                filter=Q(is_cancelled=True),
            ),

            total_refunded=Sum(
                'refund_amount',
                filter=Q(
                    refund_status=Booking.REFUND_REFUNDED
                ),
            ),
        )
    )

    cancellation_stats['total_refunded'] = (
        cancellation_stats['total_refunded']
        or Decimal('0')
    )

    # ============================================================
    # 13. USER GROWTH
    # ============================================================

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


@staff_member_required
def admin_dashboard_export_csv(request):
    """
    Streams booking data as CSV.

    Uses iterator() so 100,000+ bookings are not loaded into
    memory simultaneously.
    """

    start, end = _parse_date_range(request)

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

        bookings = (
            Booking.objects
            .filter(
                booked_at__gte=start,
                booked_at__lt=end,
            )
            .select_related(
                'user',
                'movie',
                'theater',
                'seat',
            )
            .only(
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
            .order_by('id')
            .iterator(chunk_size=2000)
        )

        for booking in bookings:

            yield writer.writerow([
                booking.id,
                booking.user.username,
                booking.movie.name,
                booking.theater.name,
                booking.seat.seat_number,
                booking.booked_at.strftime(
                    '%Y-%m-%d %H:%M'
                ),
                booking.is_cancelled,
                booking.refund_status,
                booking.refund_amount or '',
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
