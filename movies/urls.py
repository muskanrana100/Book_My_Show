from django.urls import path

from . import views

urlpatterns = [
    path('', views.movie_list, name = 'movie_list'),
    path('theater/<int:theater_id>/seats/', views.seat_selection , name= 'book_seats'),
    path('theater/<int:theater_id>/seats/<int:seat_id>/hold/',views.hold_seat, name='hold_seat'),
    path('theater/<int:theater_id>/seats/<int:seat_id>/release/',views.release_seat, name='release_seat'),
    path('theater/<int:theater_id>/seats/status/', views.seat_status , name= 'seat_status'),
    path('theater/<int:theater_id>/seats/confirm/', views.confirm_booking , name= 'confirm_booking'),
    path('<int:movie_id>/theaters/',views.theater_list, name='theater_list'),
    path('review/<int:review_id>/report/', views.report_review, name='report_review'),
    path('review/<int:review_id>/delete/', views.delete_review, name='delete_review'),
    path('<slug:slug>/', views.movie_detail, name = 'movie_detail'),

]