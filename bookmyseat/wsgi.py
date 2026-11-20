"""
WSGI config for bookMySeat project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

<<<<<<< HEAD
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bookmyseat.settings')
=======
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bookMySeat.settings')
>>>>>>> 19f2abe696f078951ad52f1702885481f8b54c4b

application = get_wsgi_application()
