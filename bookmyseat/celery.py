import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bookmyseat.settings")

app = Celery("bookmyseat")
<<<<<<< HEAD
app.config_from_object("django.conf:settings", namespace="CELERY")
=======

app.config_from_object("django.conf:settings", namespace="CELERY")

>>>>>>> 19f2abe696f078951ad52f1702885481f8b54c4b
app.autodiscover_tasks()