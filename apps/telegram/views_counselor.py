from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import StudentProfile, User

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
