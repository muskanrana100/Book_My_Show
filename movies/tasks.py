from celery import shared_task
from django.core.mail import EmailMessage

from .models import Payment
from .ticket_service import generate_ticket_pdf


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_kwargs={"max_retries": 5},
)
def send_booking_ticket_email(self, payment_id):
    """
    Generate the booking ticket PDF and email it to the user
    asynchronously using Celery.

    If email delivery fails, Celery automatically retries
    the task up to 5 times with increasing delays.
    """

    # Get the successful payment
    payment = (
        Payment.objects
        .select_related(
            "user",
            "theater",
            "theater__movie",
        )
        .get(id=payment_id)
    )

    # Only send tickets for successful payments
    if payment.status != Payment.STATUS_SUCCESS:
        return "Payment is not successful."

    # Get all active bookings belonging to this payment
    bookings = list(
        payment.bookings
        .select_related(
            "movie",
            "theater",
            "seat",
        )
        .filter(is_cancelled=False)
        .order_by("seat__seat_number")
    )

    if not bookings:
        return "No active bookings found."

    # Check whether the user has an email address
    user_email = payment.user.email

    if not user_email:
        raise ValueError(
            f"User {payment.user_id} does not have an email address."
        )

    # Generate the PDF ticket
    pdf_data = generate_ticket_pdf(
        payment,
        bookings,
    )

    # Movie name for the email subject
    movie_name = bookings[0].movie.name

    # Create email
    email = EmailMessage(
        subject=f"BookMySeat Ticket - {movie_name}",
        body=(
            f"Hello {payment.user.get_username()},\n\n"
            f"Your booking for {movie_name} has been confirmed.\n\n"
            "Your ticket is attached to this email.\n\n"
            "Thank you for using BookMySeat."
        ),
        from_email=None,
        to=[user_email],
    )

    # Attach generated PDF
    email.attach(
        f"BookMySeat_Ticket_{payment.id}.pdf",
        pdf_data,
        "application/pdf",
    )

    # Send email
    # If sending fails, Celery automatically retries.
    email.send(fail_silently=False)

    return f"Ticket email sent to {user_email}"