import logging

from django.db import transaction

logger = logging.getLogger(__name__)


def queue_ticket_email(payment_id):
    """
    Schedules the ticket e-mail for a payment.

    - Runs only after the surrounding database transaction commits, so the
      worker never sees a payment that is not yet confirmed.
    - Never raises into the booking request. If the broker is unreachable the
      failure is logged and the payment stays `ticket_email_status=pending`;
      the `send_pending_ticket_emails` management command picks it up later.
    - retry=False makes a dead broker fail fast instead of blocking the request.
    """

    def _enqueue():
        try:
            from .tasks import send_booking_ticket_email

            send_booking_ticket_email.apply_async(args=[payment_id], retry=False)
        except Exception:
            logger.exception(
                "Could not queue ticket email for payment %s. "
                "Run 'manage.py send_pending_ticket_emails' once the broker is back.",
                payment_id,
            )

    transaction.on_commit(_enqueue)
