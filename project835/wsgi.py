import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'project835.settings')
from project835.database_guard import require_postgresql
require_postgresql()
application = get_wsgi_application()
