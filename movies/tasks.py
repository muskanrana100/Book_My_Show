
from datetime import timedelta

from celery import shared_task
from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone

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
    Send a PDF ticket asynchronously.

    Prevents duplicate sends from ordinary repeated or concurrent tasks.
    Failed attempts can be retried by Celery.
    """

    # Claim the email task safely.
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(
            id=payment_id
        )

        if payment.status != Payment.STATUS_SUCCESS:
            return "Payment is not successful."

        if payment.ticket_email_status == Payment.TICKET_EMAIL_SENT:
            return "Ticket email has already been sent."

        now = timezone.now()

        # Allow recovery if a worker stopped while sending.
        if (
            payment.ticket_email_status == Payment.TICKET_EMAIL_SENDING
            and payment.ticket_email_started_at
            and payment.ticket_email_started_at > now - timedelta(minutes=15)
        ):
            return "Ticket email is already being processed."

        payment.ticket_email_status = Payment.TICKET_EMAIL_SENDING
        payment.ticket_email_started_at = now
        payment.save(
            update_fields=[
                "ticket_email_status",
                "ticket_email_started_at",
            ]
        )

    try:
        payment = (
            Payment.objects.select_related(
                "user",
                "theater",
                "theater__movie",
            ).get(id=payment_id)
        )

        bookings = list(
            payment.bookings.select_related(
                "movie",
                "theater",
                "seat",
            )
            .filter(is_cancelled=False)
            .order_by("seat__seat_number")
        )

        if not bookings:
            with transaction.atomic():
                locked = Payment.objects.select_for_update().get(
                    id=payment_id
                )
                if locked.ticket_email_status == Payment.TICKET_EMAIL_SENDING:
                    locked.ticket_email_status = Payment.TICKET_EMAIL_PENDING
                    locked.ticket_email_started_at = None
                    locked.save(
                        update_fields=[
                            "ticket_email_status",
                            "ticket_email_started_at",
                        ]
                    )
            return "No active bookings found."

        user_email = payment.user.email
        if not user_email:
            raise ValueError(
                f"User {payment.user_id} does not have an email address."
            )

        pdf_data = generate_ticket_pdf(payment, bookings)
        movie_name = bookings[0].movie.name

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

        email.attach(
            f"BookMySeat_Ticket_{payment.id}.pdf",
            pdf_data,
            "application/pdf",
        )

        email.send(fail_silently=False)

        # Mark as sent only after the email backend reports success.
        with transaction.atomic():
            locked = Payment.objects.select_for_update().get(
                id=payment_id
            )
            locked.ticket_email_status = Payment.TICKET_EMAIL_SENT
            locked.ticket_email_sent_at = timezone.now()
            locked.ticket_email_started_at = None
            locked.save(
                update_fields=[
                    "ticket_email_status",
                    "ticket_email_sent_at",
                    "ticket_email_started_at",
                ]
            )

        return f"Ticket email sent to {user_email}"

    except Exception:
        # Release the claim so Celery can retry the task.
        with transaction.atomic():
            locked = Payment.objects.select_for_update().get(
                id=payment_id
            )
            if locked.ticket_email_status == Payment.TICKET_EMAIL_SENDING:
                locked.ticket_email_status = Payment.TICKET_EMAIL_PENDING
                locked.ticket_email_started_at = None
                locked.save(
                    update_fields=[
                        "ticket_email_status",
                        "ticket_email_started_at",
                    ]
                )
        raise
