"""Review lifecycle for The Avengers: add, update and delete, plus the access rules."""
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from movies.models import Booking, Movie, Review


class AddAvengersCommandTests(TestCase):
    def test_command_creates_movie_and_shows_once(self):
        call_command("add_avengers", stdout=StringIO())
        call_command("add_avengers", stdout=StringIO())
        self.assertEqual(Movie.objects.filter(name="The Avengers").count(), 1)
        movie = Movie.objects.get(name="The Avengers")
        self.assertEqual(movie.theaters.count(), 2)  # one past show, one upcoming show

    def test_watch_user_gets_one_past_booking(self):
        User.objects.create_user("alice", "a@x.com", "Strong-pass-9")
        call_command("add_avengers", "--watch-user", "alice", stdout=StringIO())
        call_command("add_avengers", "--watch-user", "alice", stdout=StringIO())
        self.assertEqual(Booking.objects.filter(user__username="alice").count(), 1)

    def test_unknown_watch_user_is_rejected(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("add_avengers", "--watch-user", "nobody", stdout=StringIO())


class AvengersReviewTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "a@x.com", "Strong-pass-9")
        self.bob = User.objects.create_user("bob", "b@x.com", "Strong-pass-9")
        call_command("add_avengers", "--watch-user", "alice", stdout=StringIO())
        call_command("add_avengers", "--watch-user", "bob", stdout=StringIO())
        self.movie = Movie.objects.get(name="The Avengers")
        self.detail_url = reverse("movie_detail", kwargs={"slug": self.movie.slug})

    def _login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_user_without_watch_history_cannot_review(self):
        carol = User.objects.create_user("carol", "c@x.com", "Strong-pass-9")
        self._login(carol).post(self.detail_url, {"submit_review": "1", "rating": 5,
                                                  "title": "Great", "body": "Not watched"})
        self.assertFalse(Review.objects.filter(user=carol).exists())

    def test_add_review(self):
        client = self._login(self.alice)
        client.post(self.detail_url, {"submit_review": "1", "rating": 4,
                                      "title": "Fun ride", "body": "Loved the finale."})
        review = Review.objects.get(user=self.alice, movie=self.movie)
        self.assertEqual(review.rating, 4)
        self.assertTrue(review.is_verified_viewer)
        page = client.get(self.detail_url).content.decode()
        self.assertIn("Update Review", page)
        self.assertIn("Delete Review", page)

    def test_update_review(self):
        client = self._login(self.alice)
        client.post(self.detail_url, {"submit_review": "1", "rating": 4,
                                      "title": "Fun ride", "body": "Loved the finale."})
        client.post(self.detail_url, {"submit_review": "1", "rating": 5,
                                      "title": "Even better", "body": "Watched it again."})
        self.assertEqual(Review.objects.filter(user=self.alice).count(), 1)
        review = Review.objects.get(user=self.alice)
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.title, "Even better")
        self.assertTrue(review.is_edited)

    def test_delete_review(self):
        client = self._login(self.alice)
        client.post(self.detail_url, {"submit_review": "1", "rating": 3,
                                      "title": "Okay", "body": "Mixed feelings."})
        review = Review.objects.get(user=self.alice)
        response = client.post(reverse("delete_review", args=[review.id]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Review.objects.filter(pk=review.id).exists())

    def test_get_request_does_not_delete(self):
        client = self._login(self.alice)
        client.post(self.detail_url, {"submit_review": "1", "rating": 3,
                                      "title": "Okay", "body": "Mixed feelings."})
        review = Review.objects.get(user=self.alice)
        client.get(reverse("delete_review", args=[review.id]))
        self.assertTrue(Review.objects.filter(pk=review.id).exists())

    def test_user_cannot_delete_someone_elses_review(self):
        self._login(bob := self.bob).post(self.detail_url, {"submit_review": "1", "rating": 2,
                                                             "title": "Meh", "body": "Not for me."})
        bobs_review = Review.objects.get(user=bob)
        response = self._login(self.alice).post(reverse("delete_review", args=[bobs_review.id]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Review.objects.filter(pk=bobs_review.id).exists())
