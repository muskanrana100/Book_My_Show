from django.core.management.base import BaseCommand

from movies.models import Payment


class Command(BaseCommand):
    help = (
        "Issues refunds that were decided but not yet issued (a crash can leave a payment in "
        "'refund pending'). Safe to run repeatedly. Refunds stuck 'in progress' are only reported: "
        "check those with the payment provider before retrying, so that no money is refunded twice."
    )

    def handle(self, *args, **options):
        from movies.views import get_razorpay_client

        try:
            client = get_razorpay_client()
        except Exception:
            client = None
            self.stdout.write(self.style.WARNING("Payment provider is not configured; refunds cannot be issued."))

        pending = Payment.objects.filter(status=Payment.STATUS_REFUND_PENDING)
        handled = 0
        for payment in pending:
            payment.finish_refund(client)
            handled += 1
        stuck = Payment.objects.filter(status=Payment.STATUS_REFUND_IN_PROGRESS).count()
        self.stdout.write(self.style.SUCCESS(f"Processed {handled} pending refund(s)."))
        if stuck:
            self.stdout.write(self.style.WARNING(
                f"{stuck} refund(s) are stuck 'in progress' and need manual reconciliation."))
