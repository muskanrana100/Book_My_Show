from django.urls import path

from . import views

urlpatterns = [
    path('', views.movie_list, name = 'movie_list'),
    path('theater/<int:theater_id>/seats/', views.seat_selection , name= 'book_seats'),
    path('theater/<int:theater_id>/seats/<int:seat_id>/hold/',views.hold_seat, name='hold_seat'),
    path('theater/<int:theater_id>/seats/<int:seat_id>/release/',views.release_seat, name='release_seat'),
    path('theater/<int:theater_id>/seats/status/', views.seat_status , name= 'seat_status'),
    path('theater/<int:theater_id>/seats/confirm/', views.confirm_booking , name= 'confirm_booking'),
    path('theater/<int:theater_id>/payment/create/', views.create_payment, name='create_payment'),
    path('theater/<int:theater_id>/payment/cancel/', views.cancel_payment, name='cancel_payment'),
    path('payment/verify/', views.verify_payment, name='verify_payment'),
    path('payment/webhook/', views.razorpay_webhook, name='razorpay_webhook'),
    path('<int:movie_id>/theaters/',views.theater_list, name='theater_list'),
    path('review/<int:review_id>/report/', views.report_review, name='report_review'),
    path('review/<int:review_id>/delete/', views.delete_review, name='delete_review'),
    path('booking/<int:booking_id>/cancel/', views.cancel_booking, name = 'cancel_booking'),
    path('admin-dashboard/', views.admin_dashboard, name = 'admin_dashboard'),
    path('admin-dashboard/export/', views.admin_dashboard_export_csv , name = 'admin_dashboard_export_csv'),
    path('<slug:slug>/', views.movie_detail, name = 'movie_detail'),
]
