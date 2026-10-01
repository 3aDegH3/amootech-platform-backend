from rest_framework.response import Response
from rest_framework import status

from apps.accounts.models import StudentProfile


def check_telegram_access(student: StudentProfile) -> Response | None:
    """Central access guard for telegram business APIs. Returns error Response or None if allowed."""
    try:
        from .models import TelegramStudentConnection
        conn = TelegramStudentConnection.objects.get(student=student, is_active=True)
    except Exception:
        # No connection -> let _resolve_student handle (403 unknown_group/not_connected) — but for guard we allow? No, block?
        # If no TelegramStudentConnection, then not connected — but for business APIs, _resolve_student already 403.
        # So here just pass — caller will have already resolved.
        return None
    if not conn.is_enabled:
        return Response({"detail": "دسترسی تلگرام شما در حال حاضر غیرفعال است.", "code": "telegram_disabled"}, status=status.HTTP_403_FORBIDDEN)
    if conn.is_suspended:
        return Response({"detail": "دسترسی تلگرام شما موقتاً تعلیق شده است.", "code": "telegram_suspended"}, status=status.HTTP_403_FORBIDDEN)
    if conn.is_banned:
        return Response({"detail": "دسترسی تلگرام شما محدود شده است.", "code": "telegram_banned"}, status=status.HTTP_403_FORBIDDEN)
    return None
