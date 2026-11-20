from django.core.management.base import BaseCommand

from movies import reservations


class Command(BaseCommand):
    help = (
        "Clears stored seat holds that have expired without payment. Safe to run repeatedly. "
        "It never changes a booked seat or a hold that has not expired. Expired holds are also "
        "ignored by the reservation code even if this command never runs."
    )

    def handle(self, *args, **options):
        released = reservations.release_expired_holds()
        self.stdout.write(self.style.SUCCESS(f"Cleared {released} expired hold(s)."))
