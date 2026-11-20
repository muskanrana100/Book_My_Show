"""
Seat reservation service (Task 2).

Lifecycle of a seat for one show (Theater row):
  AVAILABLE  is_booked=False and no active hold
  HELD       held_by=<user>, held_until > now            (120 seconds from the last change)
  BOOKED     is_booked=True                              (set only after verified payment)

A hold that passes its held_until is simply AVAILABLE again. The periodic cleanup
(`release_expired_holds`) only clears the stored fields of such holds, so it never changes what a
seat means. A booked seat is never released by any function in this module.

Correctness design
------------------
Every change to a seat's hold is one conditional UPDATE, for example:

    UPDATE movies_seat SET held_by=:user, held_until=:expiry
     WHERE id IN (...) AND theater_id=:show AND is_booked=false
       AND (held_by_id IS NULL OR held_until <= :now OR held_by_id = :user)

The database evaluates that condition and writes the row in one step, so two requests that compete
for the same seat cannot both succeed, on SQLite, PostgreSQL or MySQL. The number of rows updated
is compared with the number requested. If they differ, the whole transaction is rolled back, so a
multi-seat request is all-or-nothing. select_for_update() is also taken on the seats in id order to
serialise competing requests and reduce deadlocks on backends that support row locks.
"""
import time
from datetime import timedelta

from django.db import OperationalError, transaction
from django.db.models import Q
from django.utils import timezone

HOLD_DURATION = timedelta(seconds=120)
MAX_SEATS_PER_REQUEST = 10
TRANSIENT_MARKERS = ("locked", "deadlock", "serialization", "busy", "timeout")


class ReservationError(Exception):
    """The request itself is invalid (not a conflict). `reason` is safe to show the user."""

    def __init__(self, reason, status=400):
        super().__init__(reason)
        self.reason = reason
        self.status = status


class ReservationConflict(Exception):
    """One or more requested seats are held by someone else or already booked."""

    def __init__(self, reason, conflicts):
        super().__init__(reason)
        self.reason = reason
        self.conflicts = conflicts  # list of {"id": ..., "seat_number": ..., "status": ...}


class ReservationUnavailable(Exception):
    """A transient database conflict persisted after retries. The client may try again."""


def _validate_seat_ids(seat_ids, allow_empty=False):
    if not isinstance(seat_ids, (list, tuple)):
        raise ReservationError("Seat selection must be a list of seat IDs.")
    if not seat_ids:
        if allow_empty:
            return []
        raise ReservationError("Select at least one seat.")
    if len(seat_ids) > MAX_SEATS_PER_REQUEST:
        raise ReservationError(f"You can select at most {MAX_SEATS_PER_REQUEST} seats at a time.")
    cleaned = []
    for value in seat_ids:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ReservationError("Seat IDs must be positive whole numbers.")
        cleaned.append(value)
    if len(set(cleaned)) != len(cleaned):
        raise ReservationError("The same seat was selected more than once.")
    return cleaned


def _is_transient(exc):
    text = str(exc).lower()
    return any(marker in text for marker in TRANSIENT_MARKERS)


def _with_retry(operation, attempts=3):
    """Retries an operation that failed on a transient lock conflict. Each attempt is a full
    transaction, so a retry never sees a partial result."""
    last = None
    for attempt in range(attempts):
        try:
            return operation()
        except OperationalError as exc:
            if not _is_transient(exc):
                raise
            last = exc
            time.sleep(0.05 * (attempt + 1))
    raise ReservationUnavailable("The seats are busy right now. Please try again.") from last


def reserve(user, theater, seat_ids, now=None):
    """
    Makes the user's hold on exactly `seat_ids` for `theater` (one show).

    - Adds newly requested seats and keeps the ones already held by this user.
    - Releases the user's other seats for this show (that is how a selection is modified).
    - All-or-nothing: if any requested seat is booked or held by someone else, nothing changes.
    - Every retained or new seat gets held_until = now + 120 seconds.

    Returns {"held_seat_ids": [...], "held_until": datetime, "expires_in_seconds": 120}.
    """
    seat_ids = _validate_seat_ids(seat_ids, allow_empty=True)
    now = now or timezone.now()

    def attempt():
        with transaction.atomic():
            show = type(theater).objects.select_for_update().get(pk=theater.pk)
            if not seat_ids:
                # Deselecting every seat: release all of this user's holds for the show.
                from movies.models import Seat as _Seat
                released = _Seat.objects.filter(
                    theater_id=show.pk, held_by_id=user.id, is_booked=False
                ).update(held_by=None, held_until=None)
                return {"held_seat_ids": [], "held_until": None, "expires_in_seconds": 0, "released": released}
            if show.time <= now:
                raise ReservationError("This show has already started.")

            from movies.models import Seat  # local import: models import this module indirectly

            requested = list(
                Seat.objects.select_for_update()
                .filter(theater_id=show.pk, id__in=seat_ids)
                .order_by("id")
            )
            if len(requested) != len(seat_ids):
                raise ReservationError("One or more seats do not belong to this show.")

            conflicts = []
            for seat in requested:
                if seat.is_booked:
                    conflicts.append({"id": seat.id, "seat_number": seat.seat_number, "status": "booked"})
                elif seat.held_until and seat.held_until > now and seat.held_by_id != user.id:
                    conflicts.append({"id": seat.id, "seat_number": seat.seat_number, "status": "held"})
            if conflicts:
                raise ReservationConflict(
                    "Some of these seats were just taken. Please choose again.", conflicts
                )

            expiry = now + HOLD_DURATION
            claimable = Q(held_by_id__isnull=True) | Q(held_until__lte=now) | Q(held_by_id=user.id)
            updated = (
                Seat.objects.filter(pk__in=seat_ids, theater_id=show.pk, is_booked=False)
                .filter(claimable)
                .update(held_by=user, held_until=expiry)
            )
            if updated != len(seat_ids):
                # Another request changed one of these seats after our check. Roll everything back.
                raise ReservationConflict(
                    "Some of these seats were just taken. Please choose again.",
                    [{"id": s.id, "seat_number": s.seat_number, "status": "changed"} for s in requested],
                )

            # Release this user's other seats for this show (the selection was modified).
            Seat.objects.filter(
                theater_id=show.pk, held_by_id=user.id, is_booked=False
            ).exclude(pk__in=seat_ids).update(held_by=None, held_until=None)

            return {
                "held_seat_ids": sorted(seat_ids),
                "held_until": expiry,
                "expires_in_seconds": int(HOLD_DURATION.total_seconds()),
            }

    return _with_retry(attempt)


def release(user, theater, seat_ids, now=None):
    """
    Releases the user's holds on the given seats of this show. Seats held by someone else, booked
    seats, and seats the user does not hold are left untouched.
    """
    seat_ids = _validate_seat_ids(seat_ids)

    def attempt():
        from movies.models import Seat

        with transaction.atomic():
            list(Seat.objects.select_for_update().filter(
                theater_id=theater.pk, id__in=seat_ids
            ).order_by("id"))  # row locks taken in id order where the backend supports them
            released = (
                Seat.objects.filter(theater_id=theater.pk, id__in=seat_ids, held_by_id=user.id, is_booked=False)
                .update(held_by=None, held_until=None)
            )
            return {"released": released}

    return _with_retry(attempt)


def release_expired_holds(now=None):
    """
    Clears the stored hold on seats whose hold has expired and that are not booked. Idempotent.
    Never touches a booked seat, a valid hold, or a hold that is still in progress on a payment
    (a payment in progress is reconciled by the payment flow; see Payment.mark_success).
    """
    from movies.models import Seat

    now = now or timezone.now()
    # A single idempotent UPDATE with no external calls, so it is safe to retry on a transient lock.
    return _with_retry(lambda: Seat.objects.filter(
        held_by_id__isnull=False,
        held_until__lte=now,
        is_booked=False,
    ).update(held_by=None, held_until=None))


def seat_states_for(user, theater, now=None):
    """Read-only view of a show's seats for the live map. Never changes any hold."""
    from movies.models import Seat

    now = now or timezone.now()
    rows = []
    for seat in Seat.objects.filter(theater=theater).order_by("seat_number"):
        if seat.is_booked:
            status = "booked"
        elif seat.held_until and seat.held_until > now:
            status = "held_by_you" if seat.held_by_id == user.id else "held"
        else:
            status = "available"
        rows.append({
            "id": seat.id,
            "seat_number": seat.seat_number,
            "status": status,
            # Only the viewer's own expiry is shared; other users' hold times stay private.
            "held_until": seat.held_until.isoformat() if status == "held_by_you" and seat.held_until else None,
        })
    return rows
