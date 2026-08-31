import os
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'project835.settings')
from project835.database_guard import require_postgresql
require_postgresql()
application = get_asgi_application()
