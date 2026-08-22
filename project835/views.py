import logging
from django.db import connections
from django.db.utils import OperationalError
from django.http import JsonResponse

logger = logging.getLogger(__name__)

def health_check(request):
    status = {
        "status": "healthy",
    }
    
    # Check default database connection
    try:
        connection = connections['default']
        # Simple cursor check to verify connectivity
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1;")
        status["database"] = "connected"
    except OperationalError as e:
        logger.error(f"Database health check failed: {e}")
        status["status"] = "unhealthy"
        status["database"] = "disconnected"
    except Exception as e:
        logger.error(f"Unexpected database health check error: {e}")
        status["status"] = "unhealthy"
        status["database"] = "error"

    http_status = 200 if status["status"] == "healthy" else 503
    return JsonResponse(status, status=http_status)
