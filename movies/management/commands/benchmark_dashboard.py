import time

from django.core.management.base import BaseCommand
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.contrib.auth.models import User, Permission
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta


class Command(BaseCommand):
    help = "Times the admin dashboard and CSV export against the current database. Reports query count and wall time."

    def add_arguments(self, parser):
        parser.add_argument("--runs", type=int, default=3, help="Runs per request. The median is reported.")

    def handle(self, *args, **options):
        runs = options["runs"]
        admin, _ = User.objects.get_or_create(
            username="bench_admin", defaults={"is_staff": True, "email": "bench@example.com"}
        )
        admin.is_staff = True
        admin.set_unusable_password()  # the benchmark logs in with force_login, no password is set
        admin.save()
        admin.user_permissions.add(Permission.objects.get(codename="view_analytics"))

        client = Client(HTTP_HOST="localhost")
        client.force_login(admin)

        now = timezone.now()
        ranges = {
            "last 30 days (default)": {},
            "last 365 days": {
                "start": (now - timedelta(days=365)).date().isoformat(),
                "end": now.date().isoformat(),
            },
            "last 7 days": {
                "start": (now - timedelta(days=7)).date().isoformat(),
                "end": now.date().isoformat(),
            },
        }

        self.stdout.write(f"Database: {connection.settings_dict['NAME']}")
        self.stdout.write(f"Runs per request: {runs} (median reported)")
        self.stdout.write("")

        for label, params in ranges.items():
            self._time(client, "dashboard", reverse("admin_dashboard"), params, label, runs)
            self._time(client, "CSV export", reverse("admin_dashboard_export_csv"), params, label, runs)
            self.stdout.write("")

    def _time(self, client, kind, url, params, label, runs):
        timings = []
        self._last_sql = []
        queries = None
        for _ in range(runs):
            with CaptureQueriesContext(connection) as ctx:
                start = time.perf_counter()
                response = client.get(url, params)
                if hasattr(response, "streaming_content"):
                    b"".join(response.streaming_content)
                elapsed = time.perf_counter() - start
            self._last_sql = [f"{float(q['time']) * 1000:8.1f} ms  {q['sql'][:160]}" for q in ctx.captured_queries]
            if response.status_code != 200:
                self.stdout.write(self.style.ERROR(f"{kind} [{label}]: HTTP {response.status_code}"))
                return
            timings.append(elapsed)
            queries = len(ctx.captured_queries)
        timings.sort()
        median = timings[len(timings) // 2]
        self.stdout.write(
            f"{kind:<12} [{label:<24}] median {median * 1000:8.1f} ms   SQL queries: {queries}"
        )
        if kind == "dashboard" and label.startswith("last 30"):
            for sql in self._last_sql:
                self.stdout.write("  " + sql)