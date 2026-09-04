import hmac
import hashlib
import json

import razorpay
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import render, redirect ,get_object_or_404
from django.utils import timezone
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .forms import ReviewForm, ReviewReportForm
from .models import Booking , Movie, Payment, Review, Seat,Theater

def movie_list(request):
    search_query=request.GET.get('search')
    movies = Movie.objects.all().prefetch_related('genres','languages')
    if search_query:
        movies=movies.filter(name__icontains=search_query)
    return render(request,'movies/movie_list.html',{'movies':movies})

def movie_detail(request, slug):
    movie = get_object_or_404(
        Movie.objects.prefetch_related('genres','languages','gallery_images','movie_cast__cast_member'),
        slug = slug
    )
    reviews = movie.reviews.filter(is_hidden = False).select_related('user').order_by('-created_at')

    user_review = None
    can_review = False
    review_form = None

    if request.user.is_authenticated:
        user_review = reviews.filter(user=request.user).first()
        has_watched = Booking.objects.filter(user=request.user, movie = movie , theater__time__lt=timezone.now()).exists()
        can_review = has_watched and user_review is None

        if request.method == 'POST' and 'submit_review' in request.POST:
            if user_review:
             review_form = ReviewForm(request.POST, instance = user_review)
            elif can_review:
                review_form = ReviewForm(request.POST)
            else:
                messages.error(request, "You can only review a movie after booking and watching it.")
                review_form= None
            if review_form and review_form.is_valid():
                review = review_form.save(commit = False)
                review.movie = movie
                review.user = request.user
                review.save()
                messages.success(request, "Your review has been saved.")
                return redirect("movie_detail",slug=movie.slug)
    
        else:
            if user_review:
                review_form = ReviewForm(instance = user_review)
            elif can_review:
                review_form = ReviewForm()

    context = {
        'movie': movie,
        'reviews':reviews,
        'user_review':user_review,
        'can_review': can_review,
        'review_form': review_form,
        'similar_movies': movie.similar_movies(),
        'trending_movies': Movie.objects.exclude(id=movie.id).order_by('-total_ratings' , '-average_rating')[:8],
        'recent_movies': Movie.objects.exclude(id =movie.id).order_by('-release_date')[:8],
    }
    return render(request, 'movies/movie_detail.html', context)

def theater_list(request,movie_id):
    movie = get_object_or_404(Movie,id=movie_id)
    theaters=Theater.objects.filter(movie=movie)
    return render(request,'movies/theater_list.html',{'movie':movie,'theaters':theaters})


@login_required(login_url='/login/')
def seat_selection(request, theater_id):
    theater = get_object_or_404(Theater, id=theater_id)
    seats = Seat.objects.filter(theater=theater)
    for seat in seats:
        seat.display_status = seat.status_for(request.user)
    return render(request, 'movies/seat_selection.html',{'theater': theater,'seats':seats})
  
    

@login_required(login_url='/login/')
@require_POST
def hold_seat(request, theater_id, seat_id):
    with transaction.atomic():
        seat = get_object_or_404(
            Seat.objects.select_for_update(), id = seat_id,theater_id = theater_id)
        
        if seat.is_booked:
            return  JsonResponse({'success': False, 'reason':"This seat is already booked."}, status = 409)
        
        if seat.is_hold_active() and seat.held_by_id != request.user.id:
            return JsonResponse({'success': False, 'reason':'This seat is currently held by another user.'}, status=409)
        
        seat.held_by = request.user
        seat.held_until = timezone.now() + Seat.HOLD_DURATION
        seat.save(update_fields =['held_by', 'held_until'])
    
    return JsonResponse({'success': True, 'held_until': seat.held_until.isoformat()})


@login_required(login_url ='/login/')
@require_POST
def release_seat(request, theater_id, seat_id):
    with transaction.atomic():
        seat = get_object_or_404(
            Seat.objects.select_for_update(),id=seat_id, theater_id = theater_id
        )
        if seat.held_by_id == request.user.id:
            seat.held_by = None
            seat.held_until = None
            seat.save(update_fields=['held_by','held_until'])
    
    return JsonResponse({'success': True})

def seat_status(request, theater_id):
    theater = get_object_or_404(Theater, id=theater_id)
    seats = Seat.objects.filter(theater= theater)
    data = [{'id': seat.id,'status': seat.status_for(request.user)} for seat in seats]
    return JsonResponse({'seats':data})

@login_required(login_url='/login/')
@require_POST
def confirm_booking(request, theater_id):
    theater = get_object_or_404(Theater,id = theater_id)
    booked_seat_numbers= []
    failed_seats = []

    with transaction.atomic():
        my_seats = Seat.objects.select_for_update().filter(theater=theater, held_by = request.user)

        if not my_seats.exists():
            return JsonResponse({'success': False,'reason':'You have no held seats. Please select seats again.'}, status = 400)
        
        for seat in my_seats:
            if seat.is_booked or not seat.is_hold_active():
                failed_seats.append(seat.seat_number)
                continue
            try:
                Booking.objects.create(user = request.user, seat=seat,movie= theater.movie, theater= theater,)
            
            except IntegrityError:
                failed_seats.append(seat.seat_number)
                continue

            seat.is_booked = True
            seat.held_by = None
            seat.held_until = None
            seat.save(update_fields=['is_booked','held_by','held_until'])
            booked_seat_numbers.append(seat.seat_number)

    if failed_seats and not booked_seat_numbers:
        return JsonResponse({
            'success':False,
            'reason':f"These seats are no longer available : {', '.join(failed_seats)}. Please reselect.",}, status = 409)

    if failed_seats:
     messages.warning(
            request, f"Some seats couldn't be booked (already taken): {', '.join(failed_seats)}."
    )

    messages.success(request, f"Booking confirmed for seats: {', '.join (booked_seat_numbers)}.")
    return JsonResponse({'success': True, 'redirect_url':'/profile/'})

def get_razorpay_client():
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

@login_required(login_url='/login/')
@require_POST
def create_payment(request, theater_id):
    """
    Called once the user clicks 'Confirm & Pay' with seats already held.
    Creates a Razorpay order for the total amount and a matching local
    Payment record, then hands the frontend what it needs to open the
    Razorpay checkout popup.
    """
    theater = get_object_or_404(Theater, id= theater_id)

    with transaction.atomic():
        my_seats = list(
            Seat.objects.select_for_update().filter(theater=theater, held_by=request.user)
        )

        valid_seats = [s for s  in my_seats if not s.is_booked and s.is_hold_active()]
        if not valid_seats:
            return JsonResponse(
                {'success': False, 'reason':'Your seat hold has expired. Please select seats again.'}, status=400,
            )

        amount_rupees = theater.price_per_seat * len(valid_seats)
        amount_paise =int(amount_rupees * 100)

        client = get_razorpay_client()
        razorpay_order = client.order.create({
            'amount': amount_paise,
            'currency':'INR',
            'payment_capture': 1,
            'notes': {
                'user_id': str(request.user.id),
                'theater_id': str(theater.id),
            },
        })

        payment = Payment.objects.create(
            user= request.user,
            theater = theater,
            amount= amount_rupees,
            razorpay_order_id= razorpay_order['id'],
        )
        payment.seats.set(valid_seats)

    return JsonResponse({
        'success': True,
        'order_id': razorpay_order['id'],
        'amount_paise': amount_paise,
        'currency': 'INR',
        'key_id': settings.RAZORPAY_KEY_ID,
        'payment_db_id': payment.id,
        'movie_name':theater.movie.name,
        'user_email': request.user.email,
    })


@login_required(login_url='/login/')
@require_POST
def verify_payment(request):
    """
    Called by the browser immediately after Razorpay's checkout popup
    reports success. This is a convenience path for a fast UI response —
    the webhook (below) is the authoritative confirmation, so this view
    is safe even if it's skipped (e.g. user closes the tab right after
    paying) because the webhook will confirm the booking independently.
    """
    order_id = request.POST.get('razorpay_order_id')
    payment_id = request.POST.get('razorpay_payment_id')
    signature = request.POST.get('razorpay_signature')

    if not (order_id and payment_id and signature):
        return JsonResponse({'success': False , 'reason':'Missing payment details.'}, status=400)
    
    payment = get_object_or_404(Payment, razorpay_order_id = order_id,user=request.user)

    client = get_razorpay_client()
    try:
        client.utility.verify_payment_signature({
            'razorpay_order_id': order_id,
            'razorpay_payment_id': payment_id,
            'razorpay_signature': signature,
        })

    except razorpay.errors.SignatureVerificationError:
        payment.mark_failed(reason='Signature verfication failed.')
        return JsonResponse({'success': False,'reason':'Payment verification failed.'}, status=400)

    payment.mark_success(payment_id,signature)
    messages.success(request, 'Payment successful! Your booking is confirmed.')
    return JsonResponse({'success':True, 'redirect_url':'/profile/'})

@login_required(login_url='/login/')
@require_POST
def cancel_payment(request, theater_id):
    """
    Called when the user closes the Razorpay checkout popup without paying.
    """
    theater = get_object_or_404(Theater, id= theater_id)
    payment = Payment.objects.filter(
        user=request.user,theater=theater,status = Payment.STATUS_CREATED).order_by('-created_at').first()

    if payment:
        payment.mark_cancelled()

    return JsonResponse({'success':True})

@csrf_exempt
@require_POST
def razorpay_webhook(request):
    """
    Server-to-server notification from Razorpay. This is the authoritative
    source of truth for payment confirmation — it doesn't depend on the
    user's browser staying open or the frontend callback firing correctly.
    Signature verification (using the webhook secret, not the API secret)
    proves this request genuinely came from Razorpay and wasn't forged.
    """

    body = request.body
    received_signature = request.headers.get('X-Razorpay-Signature', '')

    expected_signature  = hmac.new(
        key= settings.RAZORPAY_WEBHOOK_SECRET.encode('utf-8'),
        msg = body,
        digestmod= hashlib.sha256,).hexdigest()
    
    if not hmac.compare_digest(expected_signature, received_signature):
        return JsonResponse({'error':"Invalid signature"}, status=400)
    
    event = json.loads(body)
    event_type = event.get('event')

    if event_type == 'payment.captured':
        payload = event['payload']['payment']['entity']
        order_id = payload['order_id']
        payload_id = payload['id']

        try:
            payment = Payment.objects.get(razorpay_order_id= order_id)
            payment.mark_success(payload_id, razorpay_signature='verified-via-webhook')
        except Payment.DoesNotExist:
            pass
    
    elif event_type == 'payment.failed':
        payload = event['payload']['payment']['entity']
        order_id = payload['order_id']
        reason = payload.get('error_description','Payment failed')

        try:
            payment = Payment.objects.get(razorpay_order_id= order_id)
            payment.mark_failed(reason=reason)
        except Payment.DoesNotExist:
            pass

    return JsonResponse({'status':'ok'})


@login_required(login_url='/login/')
def report_review(request, review_id):
    review = get_object_or_404(Review, id = review_id)
    if request.method == 'POST':
        form = ReviewReportForm(request.POST)
        if form.is_valid():
            report = form.save(commit = False)
            report.review = review
            report.reported_by = request.user
            try:
                report.save()
                messages.success( request, "Thanks - this review has been reported to our moderators.")
            except IntegrityError:
                messages.success(request, "You have already reported this review")

    return redirect('movie_detail', slug= review.movie.slug)

@login_required(login_url='/login/')
def delete_review(request,review_id):
    review = get_object_or_404(Review, id= review_id, user=request.user)
    movie_slug = review.movie.slug
    if request.method == 'POST':
        review.delete()
        messages.success(request, 'Your review has been deleted.')
    return redirect('movie_detail', slug= movie_slug)
