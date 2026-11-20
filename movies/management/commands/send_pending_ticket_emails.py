from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from movies.models import Payment
from movies.queueing import queue_ticket_email


class Command(BaseCommand):
    help = (
        "Re-queues ticket e-mails for successful payments whose e-mail was never "
        "sent (e.g. the Celery broker was down), and for e-mails stuck in 'sending' "
        "after a worker crash. Safe to run repeatedly."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=3,
            help="Only look at payments created within this many days (default: 3).",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        since = now - timedelta(days=options["days"])
        stale_sending_before = now - timedelta(minutes=15)

        payment_ids = list(
            Payment.objects.filter(
                Q(ticket_email_status=Payment.TICKET_EMAIL_PENDING)
                | Q(
                    ticket_email_status=Payment.TICKET_EMAIL_SENDING,
                    ticket_email_started_at__lt=stale_sending_before,
                ),
                status=Payment.STATUS_SUCCESS,
                created_at__gte=since,
            )
            .order_by("id")
            .values_list("id", flat=True)
        )

        for payment_id in payment_ids:
            queue_ticket_email(payment_id)

        self.stdout.write(
            self.style.SUCCESS(f"Re-queued {len(payment_ids)} ticket e-mail(s).")
        )
