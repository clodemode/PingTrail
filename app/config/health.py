"""Health probe — used by the compose healthcheck and fleet verifiers."""
from django.conf import settings
from django.db import connection
from django.http import JsonResponse


def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        db_ok = True
    except Exception:
        db_ok = False
    payload = {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "version": getattr(settings, "PING_TRAIL_VERSION", "unknown"),
    }
    return JsonResponse(payload, status=200 if db_ok else 503)
