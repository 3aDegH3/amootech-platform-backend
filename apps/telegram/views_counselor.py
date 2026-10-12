from django.conf import settings
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import CounselorProfile, StudentProfile, User

from .models import TelegramConnectionToken, TelegramGroupConnection, TelegramStudentConnection


def _get_student_or_403(request, student_id: int) -> StudentProfile:
    student = get_object_or_404(StudentProfile.objects.select_related("user", "counselor__user"), pk=student_id)
    user: User = request.user
    if user.role == User.Role.ADMIN:
        return student
    if user.role == User.Role.COUNSELOR:
        if student.counselor and student.counselor.user_id == user.pk:
            return student
        from rest_framework.exceptions import PermissionDenied

        raise PermissionDenied("This student is not assigned to you.")
    from rest_framework.exceptions import PermissionDenied

    raise PermissionDenied("Students cannot manage Telegram connections.")


def _status_payload(student: StudentProfile) -> dict:
    grp = TelegramGroupConnection.objects.filter(student=student, is_active=True).first()
    if grp:
        group_connected = True
        chat_title = grp.chat_title
        chat_id = grp.telegram_chat_id
    else:
        group_connected = False
        chat_title = None
        chat_id = None
    stu = TelegramStudentConnection.objects.filter(student=student, is_active=True).first()
    if stu:
        student_connected = True
        telegram_username = stu.telegram_username
    else:
        student_connected = False
        telegram_username = None
    return {
        "group": {"connected": group_connected, "chat_title": chat_title, "chat_id": chat_id},
        "student": {"connected": student_connected, "telegram_username": telegram_username},
    }


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def group_link(request, student_id: int):
    student = _get_student_or_403(request, student_id)
    username = (getattr(settings, "TELEGRAM_BOT_USERNAME", "") or "").strip().lstrip("@")
    if not username:
        return Response(
            {"detail": "TELEGRAM_BOT_USERNAME is not configured.", "code": "telegram_not_configured"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    # Optional: if already has active group, allow re-generate after disconnect only — but per spec change group is disconnect+new link
    # Do not block: allow generating new token even if group exists; token will conflict at confirm time per rules
    token_obj, raw = TelegramConnectionToken.create_for_student(student=student, created_by=request.user, ttl_seconds=900)
    # Keep only latest unused token for this student to avoid proliferation
    TelegramConnectionToken.objects.filter(student=student, used_at__isnull=True).exclude(pk=token_obj.pk).delete()
    url = f"https://t.me/{username}?startgroup={raw}&admin=restrict_members"
    return Response(
        {"url": url, "expires_at": token_obj.expires_at.isoformat(), "status": "pending"},
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def group_status(request, student_id: int):
    student = _get_student_or_403(request, student_id)
    return Response(_status_payload(student))


@api_view(["POST", "DELETE"])
@permission_classes([IsAuthenticated])
def group_disconnect(request, student_id: int):
    student = _get_student_or_403(request, student_id)
    conn = TelegramGroupConnection.objects.filter(student=student, is_active=True).first()
    if not conn:
        return Response({"detail": "No active group connection."}, status=status.HTTP_404_NOT_FOUND)
    conn.is_active = False
    conn.save(update_fields=["is_active", "updated_at"])
    return Response({"detail": "Group disconnected.", "group": {"connected": False}})


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def counselor_broadcast(request):
    user = request.user
    if user.role not in (User.Role.COUNSELOR, User.Role.ADMIN):
        return Response({"detail": "دسترسی ارسال پیام ندارید."}, status=403)
    groups = TelegramGroupConnection.objects.filter(is_active=True)
    if user.role == User.Role.COUNSELOR:
        counselor = get_object_or_404(CounselorProfile, user=user)
        groups = groups.filter(student__counselor=counselor)
    chat_ids = list(groups.order_by("telegram_chat_id").values_list("telegram_chat_id", flat=True).distinct())
    if request.method == "GET":
        return Response({"total": len(chat_ids)})
    message = request.data.get("message")
    if not isinstance(message, str) or not message.strip():
        return Response({"detail": "پیام را وارد کنید."}, status=400)
    message = message.strip()
    if len(message) > 4000:
        return Response({"detail": "پیام بیش از حد طولانی است."}, status=400)
    if not chat_ids:
        return Response({"detail": "گروه متصلی یافت نشد."}, status=404)
    from .bot_control import BotControlError, BotControlUnavailable, bot_broadcast_sync
    results, sent, failed = [], 0, 0
    # The bot accepts bounded batches; never silently discard students after 200.
    for offset in range(0, len(chat_ids), 200):
        batch = chat_ids[offset:offset + 200]
        try:
            result = bot_broadcast_sync(batch, message)
            sent += result.get("sent", 0)
            failed += result.get("failed", 0)
            results.extend(result.get("results", []))
        except (BotControlError, BotControlUnavailable):
            failed += len(batch)
            results.extend({"telegram_chat_id": chat, "status": "failed", "error": "bot_unavailable"} for chat in batch)
    return Response({"sent": sent, "failed": failed, "total": len(chat_ids), "results": results})
