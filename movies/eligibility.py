"""
Review eligibility and the verified-viewer badge (Task 1).

Evidence model
--------------
The project does not record physical attendance (no check-in or scanning exists). The strongest
evidence available is a booking that completed its whole life cycle:

  * the booking is for this movie, and its show belongs to this movie;
  * the booking is not cancelled;
  * the payment for the booking succeeded (only then is a booking created, but the rule is
    checked explicitly, so a refunded or failed payment never qualifies);
  * the show has finished: start time + the movie's runtime is in the past.

A completed booking is therefore a documented approximation of having watched the movie. It is not
proof of attendance. Future shows, shows still running, pending, failed, cancelled and refunded
bookings never qualify, and neither does a booking for another movie or another user.

Every rule about who may review, and who gets the badge, uses the functions below.
"""
from datetime import timedelta

from django.db.models import Exists, OuterRef
from django.utils import timezone

from .models import Booking, Payment


def completed_bookings(user, movie, now=None):
    """Bookings that count as a completed viewing of `movie` by `user`."""
    now = now or timezone.now()
    show_must_have_ended_by = now - timedelta(minutes=movie.duration_minutes or 0)
    return Booking.objects.filter(
        user=user,
        movie=movie,
        theater__movie=movie,
        is_cancelled=False,
        payment__status=Payment.STATUS_SUCCESS,
        theater__time__lte=show_must_have_ended_by,
    )


def user_has_completed_viewing(user, movie, now=None):
    """True when `user` has a completed booking for `movie`. Anonymous users never qualify."""
    if not user or not user.is_authenticated:
        return False
    return completed_bookings(user, movie, now=now).exists()


def verified_viewer_expression(movie, now=None):
    """
    An Exists() expression for annotating reviews of `movie` with the viewer's current
    verified status, computed in the database at read time. This never goes stale when a
    booking is later cancelled, unlike a flag stored on the review.
    """
    now = now or timezone.now()
    show_must_have_ended_by = now - timedelta(minutes=movie.duration_minutes or 0)
    return Exists(
        Booking.objects.filter(
            user_id=OuterRef("user_id"),
            movie_id=movie.pk,
            theater__movie_id=movie.pk,
            is_cancelled=False,
            payment__status=Payment.STATUS_SUCCESS,
            theater__time__lte=show_must_have_ended_by,
        )
    )
