from django.shortcuts import get_object_or_404
from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import StudentProfile, User
from apps.accounts.permissions import IsAdmin
from .models import TelegramGroupConnection, TelegramStudentConnection, TelegramAdminActionLog
from .bot_control import PermissionSnapshotMissing, bot_get_sync, bot_post_sync, BotControlError, BotControlUnavailable, telemetry_sync, bot_reachable_sync


def _get_student_or_404(pk: int) -> StudentProfile:
    return get_object_or_404(StudentProfile.objects.select_related("user", "counselor__user"), pk=pk)


def _is_owner_counselor(user: User, student: StudentProfile) -> bool:
    return user.role == User.Role.COUNSELOR and hasattr(user, "counselor_profile") and student.counselor_id == user.counselor_profile.pk


def _connection_state(student: StudentProfile) -> dict:
    group = TelegramGroupConnection.objects.filter(student=student, is_active=True).first()
    try:
        tconn = TelegramStudentConnection.objects.get(student=student, is_active=True)
    except TelegramStudentConnection.DoesNotExist:
        tconn = None
    return {
        "group_connected": bool(group),
        "chat_title": group.chat_title if group else None,
        "chat_id": group.telegram_chat_id if group else None,
        "student_connected": bool(tconn),
        "telegram_username": tconn.telegram_username if tconn else None,
        "telegram_user_id": tconn.telegram_user_id if tconn else None,
        "enabled": bool(tconn.is_enabled) if tconn else True,
        "suspended": bool(tconn.is_suspended) if tconn else False,
        "banned": bool(tconn.is_banned) if tconn else False,
    }


def _log(actor, student, action, reason="", success=True, details=None, group_conn=None):
    TelegramAdminActionLog.objects.create(
        actor=actor, student=student, group_connection=group_conn, action=action, reason=reason[:500], success=success, details=details
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_telegram_status(request, student_id: int):
    student = _get_student_or_404(student_id)
    user: User = request.user  # type: ignore
    if user.role == User.Role.ADMIN:
        pass
    elif user.role == User.Role.COUNSELOR and _is_owner_counselor(user, student):
        pass
    else:
        return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    conn_state = _connection_state(student)
    # Bot telemetry — best effort, never break
    bot_info: dict = {"reachable": bot_reachable_sync()}
    if conn_state["chat_id"]:
        tele = telemetry_sync(conn_state["chat_id"], conn_state["telegram_user_id"])
        bot_info.update({
            "last_activity_at": tele.get("last_activity_at"),
            "last_error_at": tele.get("last_error_at"),
            "last_error_code": tele.get("last_error_code"),
            "last_error_message": tele.get("last_error_message"),
            "locked": tele.get("is_locked", False),
        })
    else:
        bot_info.update({"last_activity_at": None, "last_error_at": None, "last_error_code": None, "last_error_message": None, "locked": False})
    return Response({
        "connection": {"group_connected": conn_state["group_connected"], "student_connected": conn_state["student_connected"], "chat_title": conn_state["chat_title"], "telegram_username": conn_state["telegram_username"]},
        "access": {"enabled": conn_state["enabled"], "suspended": conn_state["suspended"], "banned": conn_state["banned"]},
        "group": {"locked": bool(bot_info.get("locked", False))},
        "bot": bot_info,
    })


def _require_admin(request):
    user: User = request.user  # type: ignore
    if user.role != User.Role.ADMIN:
        return Response({"detail": "Admin required."}, status=status.HTTP_403_FORBIDDEN)
    return None


def _action_endpoint(action: str, need_reason: bool = False):
    def decorator(view_fn):
        async def wrapper(*args, **kwargs):
            return await view_fn(*args, **kwargs)
        wrapper._telegram_action = action  # type: ignore
        wrapper._need_reason = need_reason  # type: ignore
        return wrapper
    return decorator


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_enable(request, student_id: int):
    student = _get_student_or_404(student_id)
    tconn = TelegramStudentConnection.objects.filter(student=student, is_active=True).first()
    if not tconn:
        return Response({"detail": "No Telegram connection."}, status=status.HTTP_404_NOT_FOUND)
    tconn.is_enabled = True
    tconn.save(update_fields=["is_enabled", "updated_at"])
    _log(request.user, student, TelegramAdminActionLog.Action.ENABLE, reason=request.data.get("reason", ""), success=True)
    return Response({"enabled": True})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_disable(request, student_id: int):
    reason = (request.data.get("reason") or "").strip()
    if not reason:
        return Response({"detail": "Reason required for disable."}, status=status.HTTP_400_BAD_REQUEST)
    student = _get_student_or_404(student_id)
    tconn = TelegramStudentConnection.objects.filter(student=student, is_active=True).first()
    if not tconn:
        return Response({"detail": "No Telegram connection."}, status=status.HTTP_404_NOT_FOUND)
    tconn.is_enabled = False
    tconn.save(update_fields=["is_enabled", "updated_at"])
    _log(request.user, student, TelegramAdminActionLog.Action.DISABLE, reason=reason, success=True)
    return Response({"enabled": False})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_suspend(request, student_id: int):
    reason = (request.data.get("reason") or "").strip()
    if not reason:
        return Response({"detail": "Reason required for suspend."}, status=status.HTTP_400_BAD_REQUEST)
    student = _get_student_or_404(student_id)
    tconn = TelegramStudentConnection.objects.filter(student=student, is_active=True).first()
    if not tconn:
        return Response({"detail": "No Telegram connection."}, status=status.HTTP_404_NOT_FOUND)
    tconn.is_suspended = True
    tconn.save(update_fields=["is_suspended", "updated_at"])
    _log(request.user, student, TelegramAdminActionLog.Action.SUSPEND, reason=reason, success=True)
    return Response({"suspended": True})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_resume(request, student_id: int):
    student = _get_student_or_404(student_id)
    tconn = TelegramStudentConnection.objects.filter(student=student, is_active=True).first()
    if not tconn:
        return Response({"detail": "No Telegram connection."}, status=status.HTTP_404_NOT_FOUND)
    tconn.is_suspended = False
    tconn.save(update_fields=["is_suspended", "updated_at"])
    _log(request.user, student, TelegramAdminActionLog.Action.RESUME, reason=request.data.get("reason", ""), success=True)
    return Response({"suspended": False})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_ban(request, student_id: int):
    reason = (request.data.get("reason") or "").strip()
    if not reason:
        return Response({"detail": "Reason required for ban."}, status=status.HTTP_400_BAD_REQUEST)
    student = _get_student_or_404(student_id)
    group = TelegramGroupConnection.objects.filter(student=student, is_active=True).first()
    tconn = TelegramStudentConnection.objects.filter(student=student, is_active=True).first()
    if not group or not tconn:
        return Response({"detail": "No Telegram connection."}, status=status.HTTP_404_NOT_FOUND)
    try:
        bot_post_sync("/internal/v1/groups/ban-member", json={"telegram_chat_id": group.telegram_chat_id, "telegram_user_id": tconn.telegram_user_id})
    except (BotControlError, BotControlUnavailable) as exc:
        _log(request.user, student, TelegramAdminActionLog.Action.BAN, reason=reason, success=False, details={"error": str(exc)[:500]}, group_conn=group)
        return Response({"detail": "Bot ban failed.", "code": "bot_error"}, status=status.HTTP_502_BAD_GATEWAY)
    tconn.is_banned = True
    tconn.save(update_fields=["is_banned", "updated_at"])
    _log(request.user, student, TelegramAdminActionLog.Action.BAN, reason=reason, success=True, group_conn=group)
    return Response({"banned": True})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_unban(request, student_id: int):
    student = _get_student_or_404(student_id)
    group = TelegramGroupConnection.objects.filter(student=student, is_active=True).first()
    tconn = TelegramStudentConnection.objects.filter(student=student, is_active=True).first()
    if not group or not tconn:
        return Response({"detail": "No Telegram connection."}, status=status.HTTP_404_NOT_FOUND)
    try:
        bot_post_sync("/internal/v1/groups/unban-member", json={"telegram_chat_id": group.telegram_chat_id, "telegram_user_id": tconn.telegram_user_id})
    except (BotControlError, BotControlUnavailable) as exc:
        _log(request.user, student, TelegramAdminActionLog.Action.UNBAN, reason=request.data.get("reason", ""), success=False, details={"error": str(exc)[:500]}, group_conn=group)
        return Response({"detail": "Bot unban failed."}, status=status.HTTP_502_BAD_GATEWAY)
    tconn.is_banned = False
    tconn.save(update_fields=["is_banned", "updated_at"])
    _log(request.user, student, TelegramAdminActionLog.Action.UNBAN, reason=request.data.get("reason", ""), success=True, group_conn=group)
    return Response({"banned": False})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_group_lock(request, student_id: int):
    reason = (request.data.get("reason") or "").strip()
    if not reason:
        return Response({"detail": "Reason required for lock."}, status=status.HTTP_400_BAD_REQUEST)
    student = _get_student_or_404(student_id)
    group = TelegramGroupConnection.objects.filter(student=student, is_active=True).first()
    if not group:
        return Response({"detail": "No group."}, status=status.HTTP_404_NOT_FOUND)
    try:
        bot_post_sync("/internal/v1/groups/lock", json={"telegram_chat_id": group.telegram_chat_id})
    except (BotControlError, BotControlUnavailable) as exc:
        _log(request.user, student, TelegramAdminActionLog.Action.GROUP_LOCK, reason=reason, success=False, details={"error": str(exc)[:500]}, group_conn=group)
        return Response({"detail": "Bot lock failed."}, status=status.HTTP_502_BAD_GATEWAY)
    _log(request.user, student, TelegramAdminActionLog.Action.GROUP_LOCK, reason=reason, success=True, group_conn=group)
    return Response({"locked": True})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_group_unlock(request, student_id: int):
    student = _get_student_or_404(student_id)
    group = TelegramGroupConnection.objects.filter(student=student, is_active=True).first()
    if not group:
        return Response({"detail": "No group."}, status=status.HTTP_404_NOT_FOUND)
    try:
        result = bot_post_sync("/internal/v1/groups/unlock", json={"telegram_chat_id": group.telegram_chat_id})
        if result.get("error") == "permission_snapshot_missing":
            raise PermissionSnapshotMissing("permission_snapshot_missing")
        if result.get("status") not in {"unlocked", "already_unlocked"}:
            raise BotControlError("Unexpected unlock response")
    except PermissionSnapshotMissing:
        _log(request.user, student, TelegramAdminActionLog.Action.GROUP_UNLOCK, reason=request.data.get("reason", ""), success=False, details={"error": "permission_snapshot_missing"}, group_conn=group)
        return Response({"detail": "تنظیمات قبلی گروه برای بازیابی در دسترس نیست؛ دسترسی‌های گروه را در تلگرام بررسی کنید.", "code": "permission_snapshot_missing"}, status=status.HTTP_409_CONFLICT)
    except (BotControlError, BotControlUnavailable) as exc:
        _log(request.user, student, TelegramAdminActionLog.Action.GROUP_UNLOCK, reason=request.data.get("reason", ""), success=False, details={"error": str(exc)[:500]}, group_conn=group)
        return Response({"detail": "Bot unlock failed."}, status=status.HTTP_502_BAD_GATEWAY)
    _log(request.user, student, TelegramAdminActionLog.Action.GROUP_UNLOCK, reason=request.data.get("reason", ""), success=True, group_conn=group)
    return Response({"locked": False})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_resend_plan(request, student_id: int):
    student = _get_student_or_404(student_id)
    user: User = request.user  # type: ignore
    is_admin = user.role == User.Role.ADMIN
    is_owner = _is_owner_counselor(user, student)
    if not (is_admin or is_owner):
        return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    group = TelegramGroupConnection.objects.filter(student=student, is_active=True).first()
    tconn = TelegramStudentConnection.objects.filter(student=student, is_active=True).first()
    if not group or not tconn:
        return Response({"detail": "No Telegram connection."}, status=status.HTTP_404_NOT_FOUND)
    try:
        bot_post_sync("/internal/v1/daily-plan/send", json={"telegram_chat_id": group.telegram_chat_id, "telegram_user_id": tconn.telegram_user_id})
    except (BotControlError, BotControlUnavailable) as exc:
        # Only admin logs resend; counselor resend is allowed but not audit-logged as admin action? Log anyway.
        if is_admin:
            _log(user, student, TelegramAdminActionLog.Action.RESEND_PLAN, reason=request.data.get("reason", ""), success=False, details={"error": str(exc)[:500]}, group_conn=group)
        return Response({"detail": "Bot resend failed."}, status=status.HTTP_502_BAD_GATEWAY)
    if is_admin:
        _log(user, student, TelegramAdminActionLog.Action.RESEND_PLAN, reason=request.data.get("reason", ""), success=True, group_conn=group)
    return Response({"sent": True})


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_audit(request, student_id: int):
    student = _get_student_or_404(student_id)
    logs = TelegramAdminActionLog.objects.filter(student=student).order_by("-created_at")[:50]
    data = [
        {"id": l.pk, "actor": l.actor.username if l.actor else None, "action": l.action, "reason": l.reason, "success": l.success, "details": l.details, "created_at": l.created_at.isoformat()}
        for l in logs
    ]
    return Response({"results": data})


# Counselor read-only status is same as admin_telegram_status (owner allowed)
# Already handled there.
