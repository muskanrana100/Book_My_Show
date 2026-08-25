from django.contrib import messages
from django.shortcuts import render, redirect ,get_object_or_404
from .models import Movie,Theater,Seat,Booking
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.utils import timezone

from .forms import ReviewForm, ReviewReportForm
from .models import Booking , Movie, Review, Seat,Theater

def movie_list(request):
    search_query=request.GET.get('search')
    movies = Movie.objects.all().prefetch_related('genres',)
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
        'similar_movies': movie.similar_movies,
        'trending_movies': Movie.objects.exclude(id=movie.id).order_by('-total_ratings' , '-average_rating')[:8],
        'recent_movies': Movie.objects.exclude(id =movie.id).order_by('-release_date')[:8],
    }
    return render(request, 'movies/movie_detail.html', context)

def theater_list(request,movie_id):
    movie = get_object_or_404(Movie,id=movie_id)
    theaters=Theater.objects.filter(movie=movie)
    return render(request,'movies/theater_list.html',{'movie':movie,'theaters':theaters})


@login_required(login_url='/login/')
def book_seats(request, theater_id):
    theater = get_object_or_404(Theater, id=theater_id)
    seats = Seat.objects.filter(theater=theater)

    if request.method == 'POST':
        selected_seats = request.POST.getlist('seats')
        error_seats = []

        if not selected_seats:
            return render(request, 'movies/seat_selection.html',
                          {'theaters': theater, 'seats': seats, 'error': 'No seat selected'})

        for seat_id in selected_seats:
            seat = get_object_or_404(Seat, id=seat_id, theater=theater)
            if seat.is_booked:
                error_seats.append(seat.seat_number)
                continue
            try:
                Booking.objects.create(
                    user=request.user,
                    seat=seat,
                    movie=theater.movie,
                    theater=theater
                )
                seat.is_booked = True
                seat.save()
            except IntegrityError:
                error_seats.append(seat.seat_number)

        if error_seats:
            error_message = f"The following seats are already booked: {', '.join(error_seats)}"
            return render(request, 'movies/seat_selection.html',
                          {'theater': theater, 'seats': seats, 'error': error_message})
        return redirect('profile')

    return render(request, 'movies/seat_selection.html', {'theater': theater, 'seats': seats})  
    

@login_required(login_url='/login/')
def report_review(request, review_id):
    review = get_object_or_404(Review, id=review_id)
    if request.method == 'POST':
        form = ReviewReportForm(request.POST)
        if form.is_valid():
            report = form.save(commit = False)
            report.review = review
            report.reported_by = request.user
            try:
                report.save()
                messages.success(request, "Thanks - this review gas been reported to our moderators.")
            except IntegrityError:
                messages.info(request, "You have already reported this review.")
    return redirect('movie_detail', slug= review.movie.slug)

@login_required(login_url='/login/')
def delete_review(request, review_id):
    review = get_object_or_404(Review, id= review_id, user =request.user)
    movie_slug = review.movie.slug
    if request.method == 'POST':
        review.delete()
        messages.success(request, 'Your review has been deleted.')
    return redirect('movie_detail', slug= movie_slug)
