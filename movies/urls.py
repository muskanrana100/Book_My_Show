from django.urls import path

from . import views

urlpatterns = [
    path('', views.movie_list, name = 'movie_list'),
    path('theater/<int:theater_id>/seats/', views.seat_selection , name= 'book_seats'),
    path('theater/<int:theater_id>/seats/<int:seat_id>/hold/',views.hold_seat, name='hold_seat'),
    path('theater/<int:theater_id>/seats/<int:seat_id>/release/',views.release_seat, name='release_seat'),
    path('theater/<int:theater_id>/seats/status/', views.seat_status , name= 'seat_status'),
<<<<<<< HEAD
    path('theater/<int:theater_id>/seats/reserve/', views.reserve_seats, name='reserve_seats'),
=======
>>>>>>> 19f2abe696f078951ad52f1702885481f8b54c4b
    path('theater/<int:theater_id>/seats/confirm/', views.confirm_booking , name= 'confirm_booking'),
    path('theater/<int:theater_id>/payment/create/', views.create_payment, name='create_payment'),
    path('theater/<int:theater_id>/payment/cancel/', views.cancel_payment, name='cancel_payment'),
    path('payment/verify/', views.verify_payment, name='verify_payment'),
<<<<<<< HEAD
    path('payment/failed/', views.record_payment_failure, name='record_payment_failure'),
    path('payment/webhook/', views.razorpay_webhook, name='razorpay_webhook'),
    path('payment/<int:payment_id>/status/', views.payment_status, name='payment_status'),
=======
    path('payment/webhook/', views.razorpay_webhook, name='razorpay_webhook'),
>>>>>>> 19f2abe696f078951ad52f1702885481f8b54c4b
    path('<int:movie_id>/theaters/',views.theater_list, name='theater_list'),
    path('review/<int:review_id>/report/', views.report_review, name='report_review'),
    path('review/<int:review_id>/delete/', views.delete_review, name='delete_review'),
    path('booking/<int:booking_id>/cancel/', views.cancel_booking, name = 'cancel_booking'),
    path('admin-dashboard/', views.admin_dashboard, name = 'admin_dashboard'),
    path('admin-dashboard/export/', views.admin_dashboard_export_csv , name = 'admin_dashboard_export_csv'),
<<<<<<< HEAD
    path('discover/count/', views.discovery_count, name='discovery_count'),
=======
>>>>>>> 19f2abe696f078951ad52f1702885481f8b54c4b
    path('<slug:slug>/', views.movie_detail, name = 'movie_detail'),
    path(
    "tickets/verify/<int:booking_id>/", views.verify_ticket,name="verify_ticket",),
    path(
    "tickets/download/<int:booking_id>/",
    views.download_ticket,
    name="download_ticket",
),
]
