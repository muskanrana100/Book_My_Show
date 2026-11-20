from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('movies/', include('movies.urls')),
    path('', include('users.urls')),
]

if settings.DEBUG:
<<<<<<< HEAD
    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT,
    )
=======
    urlpatterns += static(settings.MEDIA_URL, document_root= settings.MEDIA_ROOT)
>>>>>>> 19f2abe696f078951ad52f1702885481f8b54c4b
