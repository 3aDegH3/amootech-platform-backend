import hashlib

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from .models import TelegramGroupConnection, TelegramStudentConnection, TelegramConnectionToken
from .permissions import ServiceTokenPermission


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class GroupConfirmSerializer(serializers.Serializer):
    token = serializers.CharField()
    telegram_chat_id = serializers.IntegerField()
    chat_title = serializers.CharField(required=False, allow_blank=True, default="")
    chat_type = serializers.CharField(required=False, allow_blank=True, default="")


class StudentConfirmSerializer(serializers.Serializer):
    telegram_chat_id = serializers.IntegerField()
    telegram_user_id = serializers.IntegerField()
    telegram_username = serializers.CharField(required=False, allow_blank=True, allow_null=True, default=None)
    telegram_first_name = serializers.CharField(required=False, allow_blank=True, allow_null=True, default=None)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
@transaction.atomic
def group_confirm(request):
    data = GroupConfirmSerializer(data=request.data)
    data.is_valid(raise_exception=True)
    raw = data.validated_data["token"].strip()
    chat_id = data.validated_data["telegram_chat_id"]
    chat_title = (data.validated_data.get("chat_title") or "").strip()
    chat_type = (data.validated_data.get("chat_type") or "").strip()

    if chat_id == 0:
        return Response({"detail": "Invalid chat_id.", "code": "invalid_chat"}, status=status.HTTP_400_BAD_REQUEST)

    token_hash = _hash_token(raw)
    try:
        token_obj = TelegramConnectionToken.objects.select_for_update().get(token_hash=token_hash)
    except TelegramConnectionToken.DoesNotExist:
        return Response({"detail": "لینک نامعتبر است.", "code": "invalid_token"}, status=status.HTTP_404_NOT_FOUND)

    if token_obj.used_at is not None:
        # Idempotent retry for same group: if this token already created this exact group mapping, return success
        # Check if group connection exists for this student+chat_id
        existing = TelegramGroupConnection.objects.filter(
            student=token_obj.student, telegram_chat_id=chat_id, is_active=True
        ).first()
        if existing:
            return Response(
                {
                    "student_id": existing.student_id,
                    "chat_id": existing.telegram_chat_id,
                    "chat_title": existing.chat_title,
                    "status": "already_connected",
                },
                status=status.HTTP_200_OK,
            )
        return Response({"detail": "این لینک قبلاً استفاده شده است.", "code": "token_used"}, status=status.HTTP_409_CONFLICT)

    if token_obj.expires_at <= timezone.now():
        return Response({"detail": "لینک منقضی شده است.", "code": "token_expired"}, status=status.HTTP_410_GONE)

    student = token_obj.student

    # Rule: Group only for one student
    if TelegramGroupConnection.objects.filter(telegram_chat_id=chat_id, is_active=True).exclude(student=student).exists():
        return Response({"detail": "این گروه قبلاً به دانش‌آموز دیگری متصل شده است.", "code": "group_conflict"}, status=status.HTTP_409_CONFLICT)

    # Rule: Student at most one active group
    existing_student_group = TelegramGroupConnection.objects.filter(student=student, is_active=True).first()
    if existing_student_group:
        if existing_student_group.telegram_chat_id == chat_id:
            # Already connected to same group — mark token used and return idempotent
            token_obj.used_at = timezone.now()
            token_obj.save(update_fields=["used_at"])
            return Response(
                {"student_id": student.pk, "chat_id": chat_id, "chat_title": existing_student_group.chat_title, "status": "already_connected"},
                status=status.HTTP_200_OK,
            )
        return Response({"detail": "این دانش‌آموز قبلاً گروه دیگری دارد.", "code": "student_group_exists"}, status=status.HTTP_409_CONFLICT)

    # Also handle soft-deleted inactive record for same student trying to reconnect same group? reactivate
    inactive = TelegramGroupConnection.objects.filter(student=student, telegram_chat_id=chat_id, is_active=False).first()
    if inactive:
        inactive.chat_title = chat_title
        inactive.chat_type = chat_type
        inactive.is_active = True
        inactive.save()
        token_obj.used_at = timezone.now()
        token_obj.save(update_fields=["used_at"])
        return Response({"student_id": student.pk, "chat_id": chat_id, "chat_title": chat_title, "status": "connected"}, status=status.HTTP_201_CREATED)

    conn = TelegramGroupConnection.objects.create(
        student=student, telegram_chat_id=chat_id, chat_title=chat_title, chat_type=chat_type, is_active=True
    )
    token_obj.used_at = timezone.now()
    token_obj.save(update_fields=["used_at"])
    return Response({"student_id": student.pk, "chat_id": conn.telegram_chat_id, "chat_title": chat_title, "status": "connected"}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def group_resolve(request):
    raw = request.query_params.get("telegram_chat_id")
    if not raw:
        return Response({"detail": "telegram_chat_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        chat_id = int(raw)
    except ValueError:
        return Response({"detail": "Invalid telegram_chat_id."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        conn = TelegramGroupConnection.objects.select_related("student__user").get(telegram_chat_id=chat_id, is_active=True)
    except TelegramGroupConnection.DoesNotExist:
        return Response({"connected": False}, status=status.HTTP_200_OK)
    student = conn.student
    has_student_conn = TelegramStudentConnection.objects.filter(student=student, is_active=True).exists()
    display = (student.user.get_full_name() or student.user.username) if hasattr(student, "user") else str(student.pk)
    return Response(
        {
            "connected": True,
            "student_id": student.pk,
            "student_display": display.strip() if display else str(student.pk),
            "chat_id": conn.telegram_chat_id,
            "chat_title": conn.chat_title,
            "student_connected": has_student_conn,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
@transaction.atomic
def student_confirm(request):
    data = StudentConfirmSerializer(data=request.data)
    data.is_valid(raise_exception=True)
    chat_id = data.validated_data["telegram_chat_id"]
    tg_user_id = data.validated_data["telegram_user_id"]
    tg_username = data.validated_data.get("telegram_username")
    tg_first = data.validated_data.get("telegram_first_name")

    if chat_id == 0 or tg_user_id == 0:
        return Response({"detail": "Invalid ids."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        group_conn = TelegramGroupConnection.objects.select_related("student").get(telegram_chat_id=chat_id, is_active=True)
    except TelegramGroupConnection.DoesNotExist:
        return Response({"detail": "گروه ناشناخته است.", "code": "unknown_group"}, status=status.HTTP_404_NOT_FOUND)

    student = group_conn.student

    # If Telegram User already linked to another student -> conflict
    other = TelegramStudentConnection.objects.filter(telegram_user_id=tg_user_id, is_active=True).exclude(student=student).first()
    if other:
        return Response({"detail": "این حساب تلگرام قبلاً به دانش‌آموز دیگری متصل است.", "code": "user_conflict"}, status=status.HTTP_409_CONFLICT)

    existing = TelegramStudentConnection.objects.filter(student=student, is_active=True).first()
    if existing:
        if existing.telegram_user_id == tg_user_id:
            # idempotent — update username/first_name if provided
            updated = False
            if tg_username is not None and tg_username != existing.telegram_username:
                existing.telegram_username = tg_username
                updated = True
            if tg_first is not None and tg_first != existing.telegram_first_name:
                existing.telegram_first_name = tg_first
                updated = True
            if updated:
                existing.save()
            return Response({"student_id": student.pk, "telegram_user_id": tg_user_id, "status": "already_connected"}, status=status.HTTP_200_OK)
        return Response({"detail": "این دانش‌آموز قبلاً حساب دیگری دارد.", "code": "student_user_exists"}, status=status.HTTP_409_CONFLICT)

    # Also check inactive for same student+user -> reactivate
    inactive = TelegramStudentConnection.objects.filter(student=student, telegram_user_id=tg_user_id, is_active=False).first()
    if inactive:
        inactive.telegram_username = tg_username
        inactive.telegram_first_name = tg_first
        inactive.is_active = True
        inactive.save()
        return Response({"student_id": student.pk, "telegram_user_id": tg_user_id, "status": "connected"}, status=status.HTTP_201_CREATED)

    TelegramStudentConnection.objects.create(
        student=student,
        telegram_user_id=tg_user_id,
        telegram_username=tg_username,
        telegram_first_name=tg_first,
        is_active=True,
    )
    return Response({"student_id": student.pk, "telegram_user_id": tg_user_id, "status": "connected"}, status=status.HTTP_201_CREATED)
