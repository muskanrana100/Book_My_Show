import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from movies.models import Booking, Genre, Language, Movie, Payment, Seat, Theater


class Command(BaseCommand):
    help = 'Seeds the database with a large volume of fake movies, theaters, seats, and bookings for performance testing.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--bookings', type=int, default=100_000,
            help='Number of bookings to create (default: 100,000).'
        )

    def handle(self, *args, **options):
        target_bookings = options['bookings']
        now = timezone.now()

        self.stdout.write(
            'Setting up base data (genre, language, movies, theaters, seats, users)...'
        )

        genre, _ = Genre.objects.get_or_create(name='Action')
        language, _ = Language.objects.get_or_create(name='English')

        movie_names = [f'Demo Movie {i}' for i in range(1, 21)]
        movies = []

        for name in movie_names:
            movie, created = Movie.objects.get_or_create(
                name=name,
                defaults={
                    'image': 'movies/placeholder.jpg',
                    'description': 'Seeded demo movie for performance testing.',
                    'duration_minutes': 120,
                    'release_date': now.date() - timedelta(
                        days=random.randint(0, 365)
                    ),
                },
            )

            if created:
                movie.genres.add(genre)
                movie.languages.add(language)

            movies.append(movie)

        theater_names = ['PVR', 'INOX', 'Cinepolis', 'Miraj', 'Carnival']
        theaters = []

        for movie in movies:
            for theater_name in theater_names:

                for day_offset in range(10):
                    show_time = now - timedelta(
                        days=day_offset,
                        hours=random.randint(0, 23)
                    )

                    theater = Theater.objects.create(
                        name=theater_name,
                        movie=movie,
                        time=show_time,
                        price_per_seat=Decimal(
                            random.choice([150, 200, 250, 300])
                        ),
                    )

                    theaters.append(theater)

        self.stdout.write(
            f'Created {len(theaters)} theater showtimes.'
        )

        self.stdout.write('Creating seats for each theater...')

        for theater in theaters:
            Seat.objects.bulk_create([
                Seat(
                    theater=theater,
                    seat_number=f'{row}{num}'
                )
                for row in 'ABCDEFGHIJ'
                for num in range(1, 11)
            ])

        self.stdout.write('Creating demo users for bookings...')

        demo_users = []

        for i in range(1, 51):
            username = f'demo_user_{i}'
            fake_joined = now - timedelta(
                days=random.randint(0, 59)
            )

            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    'email': f'{username}@example.com',
                    'date_joined': fake_joined
                },
            )

            demo_users.append(user)

        self.stdout.write(
            f'Seeding {target_bookings} bookings - this may take a few minutes...'
        )

        all_seats = list(
            Seat.objects
            .select_related('theater', 'theater__movie')
            .filter(is_booked=False)
        )

        random.shuffle(all_seats)

        batch_size = 2000
        created_count = 0

        payment_batch = []
        pending_meta = []
        seat_ids_to_mark_booked = []

        def flush_batch():

            if not payment_batch:
                return

            with transaction.atomic():

                Payment.objects.bulk_create(payment_batch)

                with connection.cursor() as cursor:
                    cursor.executemany(
                        "UPDATE movies_payment SET created_at = %s WHERE id = %s",
                        [
                            (p.created_at, p.id)
                            for p in payment_batch
                        ],
                    )

                booking_batch = []

                for payment, meta in zip(payment_batch, pending_meta):
                    booking_batch.append(
                        Booking(
                            user=meta['user'],
                            seat=meta['seat'],
                            movie=meta['seat'].theater.movie,
                            theater=meta['seat'].theater,
                            payment=payment,
                            booked_at=meta['fake_time'],
                            is_cancelled=meta['is_cancelled'],
                            cancelled_at=(
                                meta['fake_time']
                                if meta['is_cancelled']
                                else None
                            ),
                            refund_status=(
                                Booking.REFUND_REFUNDED
                                if meta['is_cancelled']
                                else Booking.REFUND_NONE
                            ),
                            refund_amount=(
                                meta['seat'].theater.price_per_seat
                                if meta['is_cancelled']
                                else None
                            ),
                        )
                    )

                Booking.objects.bulk_create(booking_batch)

                with connection.cursor() as cursor:
                    cursor.executemany(
                        "UPDATE movies_booking SET booked_at = %s, cancelled_at = %s WHERE id = %s",
                        [
                            (b.booked_at, b.cancelled_at, b.id)
                            for b in booking_batch
                        ],
                    )

                Seat.objects.filter(
                    id__in=seat_ids_to_mark_booked
                ).update(is_booked=True)

            self.stdout.write(
                f'  ...{created_count} bookings created so far'
            )

            payment_batch.clear()
            pending_meta.clear()
            seat_ids_to_mark_booked.clear()

        for seat in all_seats:

            if created_count >= target_bookings:
                break

            user = random.choice(demo_users)

            fake_time = now - timedelta(
                days=random.randint(0, 59),
                hours=random.randint(0, 23),
                minutes=random.randint(0, 59),
            )

            is_cancelled = random.random() < 0.05

            payment = Payment(
                user=user,
                theater=seat.theater,
                amount=seat.theater.price_per_seat,
                status=Payment.STATUS_SUCCESS,
                razorpay_order_id=(
                    f'seed_order_{seat.id}_{random.randint(100000, 999999)}'
                ),
                razorpay_payment_id=(
                    f'seed_pay_{seat.id}_{random.randint(100000, 999999)}'
                ),
                created_at=fake_time,
            )

            payment_batch.append(payment)

            pending_meta.append({
                'user': user,
                'seat': seat,
                'is_cancelled': is_cancelled,
                'fake_time': fake_time,
            })

            seat_ids_to_mark_booked.append(seat.id)
            created_count += 1

            if len(payment_batch) >= batch_size:
                flush_batch()

        flush_batch()

        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully created {created_count} bookings '
                f'across {len(theaters)} showtimes and {len(movies)} movies.'
            )
        )
