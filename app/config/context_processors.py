from django.conf import settings


def bootstamp(request):
    """Add bootstamp for cache busting static assets (?v=YYYYMMDDHHMMSS)."""
    return {
        "bootstamp": getattr(settings, "BOOTSTAMP", "0"),
        "ping_trail_version": getattr(settings, "PING_TRAIL_VERSION", "1.0"),
    }
