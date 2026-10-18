from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import viewsets, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import User, StudentProfile, CounselorProfile
from .profile_serializers import StudentSerializer, CounselorSerializer
from .permissions import IsAdmin, IsCounselor


class AdminWrite(IsAuthenticated):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and (
            request.method in ("GET", "HEAD", "OPTIONS") or request.user.role == User.Role.ADMIN
        )


class StudentViewSet(viewsets.ModelViewSet):
    serializer_class = StudentSerializer
    permission_classes = (AdminWrite,)
    queryset = StudentProfile.objects.select_related("user", "counselor__user", "grade", "field").order_by("pk")

    def get_permissions(self):
        if self.action in ("create", "mobile"):
            return [(IsAdmin | IsCounselor)()]
        return super().get_permissions()

    def perform_create(self, serializer):
        if self.request.user.role == User.Role.COUNSELOR:
            if not hasattr(self.request.user, "counselor_profile"):
                raise serializers.ValidationError("پروفایل مشاور موجود نیست.")
            if "counselor" in self.request.data:
                raise serializers.ValidationError("مشاور دانش‌آموز به‌صورت خودکار تعیین می‌شود.")
            serializer.save(counselor=self.request.user.counselor_profile)
        else:
            serializer.save()

    @action(detail=True, methods=("patch",))
    def mobile(self, request, pk=None):
        if set(request.data) != {"mobile"}:
            raise serializers.ValidationError("فقط شماره موبایل قابل ویرایش است.")
        serializer = self.get_serializer(self.get_object(), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def get_queryset(self):
        from apps.planning.models import Plan
        from apps.telegram.models import TelegramGroupConnection, TelegramStudentConnection
        from apps.daily_reports.models import DailyReport
        queryset = super().get_queryset()
        user = self.request.user
        if user.role == User.Role.ADMIN:
            base = queryset
        elif user.role == User.Role.COUNSELOR:
            base = queryset.filter(counselor__user=user)
        else:
            base = queryset.filter(user=user)

        # Admin-only filters/search
        if user.role == User.Role.ADMIN:
            params = self.request.query_params
            q = params.get("q", "").strip()
            if q:
                base = base.filter(
                    Q(user__username__icontains=q)
                    | Q(user__first_name__icontains=q)
                    | Q(user__last_name__icontains=q)
                    | Q(school_name__icontains=q)
                )
            is_active = params.get("is_active")
            if is_active in ("true", "false"):
                base = base.filter(user__is_active=(is_active == "true"))
            counselor = params.get("counselor")
            if counselor == "none":
                base = base.filter(counselor__isnull=True)
            elif counselor and counselor.isdigit():
                base = base.filter(counselor_id=int(counselor))
            grade = params.get("grade")
            if grade and grade.isdigit():
                base = base.filter(grade_id=int(grade))
            field = params.get("field")
            if field and field.isdigit():
                base = base.filter(field_id=int(field))
            # Telegram filter
            telegram = params.get("telegram")
            if telegram == "connected":
                base = base.filter(pk__in=TelegramGroupConnection.objects.filter(is_active=True).values_list("student_id", flat=True))
            elif telegram == "disconnected":
                base = base.exclude(pk__in=TelegramGroupConnection.objects.filter(is_active=True).values_list("student_id", flat=True))
            # Plan status filter
            plan_status = params.get("plan_status")
            today = timezone.localdate()
            if plan_status == "has_current":
                base = base.filter(pk__in=Plan.objects.filter(status=Plan.Status.PUBLISHED, start_date__lte=today, end_date__gte=today).values_list("student_id", flat=True))
            elif plan_status == "no_current":
                base = base.exclude(pk__in=Plan.objects.filter(status=Plan.Status.PUBLISHED, start_date__lte=today, end_date__gte=today).values_list("student_id", flat=True))
            elif plan_status in ("draft", "published"):
                status_val = Plan.Status.DRAFT if plan_status == "draft" else Plan.Status.PUBLISHED
                base = base.filter(pk__in=Plan.objects.filter(status=status_val).values_list("student_id", flat=True))

        # Prefetch current plan cache efficiently for list view
        return base

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        # Enrich with telegram/reports cache if admin
        if request.user.role == User.Role.ADMIN and "results" in response.data:
            from apps.telegram.models import TelegramGroupConnection, TelegramStudentConnection
            from apps.daily_reports.models import DailyReport
            from django.utils import timezone
            ids = [r["id"] for r in response.data["results"]]
            # Telegram cache
            group_map = {g.student_id: g for g in TelegramGroupConnection.objects.filter(student_id__in=ids, is_active=True)}
            stu_map = {s.student_id: s for s in TelegramStudentConnection.objects.filter(student_id__in=ids, is_active=True)}
            today = timezone.localdate()
            report_ids = set(DailyReport.objects.filter(student_id__in=ids, date=today).values_list("student_id", flat=True))
            for r in response.data["results"]:
                gid = group_map.get(r["id"])
                sid = stu_map.get(r["id"])
                r["telegram_status"] = {
                    "group_connected": bool(gid),
                    "student_connected": bool(sid),
                    "chat_title": gid.chat_title if gid else None,
                    "telegram_username": sid.telegram_username if sid else None,
                    "enabled": sid.is_enabled if sid else True,
                    "suspended": sid.is_suspended if sid else False,
                    "banned": sid.is_banned if sid else False,
                } if (gid or sid) else {"group_connected": False, "student_connected": False, "chat_title": None, "telegram_username": None, "enabled": True, "suspended": False, "banned": False}
                r["reports_today"] = r["id"] in report_ids
                # current_plan already computed via serializer method; ensure it has data
        return response

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        # Attach caches for detail
        if request.user.role == User.Role.ADMIN:
            from apps.telegram.models import TelegramGroupConnection, TelegramStudentConnection
            from apps.daily_reports.models import DailyReport
            from django.utils import timezone
            gid = TelegramGroupConnection.objects.filter(student=instance, is_active=True).first()
            sid = TelegramStudentConnection.objects.filter(student=instance, is_active=True).first()
            instance._telegram_cache = {
                "group_connected": bool(gid),
                "student_connected": bool(sid),
                "chat_title": gid.chat_title if gid else None,
                "telegram_username": sid.telegram_username if sid else None,
                "enabled": sid.is_enabled if sid else True,
                "suspended": sid.is_suspended if sid else False,
                "banned": sid.is_banned if sid else False,
            }
            today = timezone.localdate()
            instance._reports_today_cache = DailyReport.objects.filter(student=instance, date=today).exists()
        return super().retrieve(request, *args, **kwargs)

    # Override get_object for detail enrichment: use retrieve override above, but ensure serializer gets cache
    def get_serializer(self, *args, **kwargs):
        # For retrieve with cache, instance already has _telegram_cache
        return super().get_serializer(*args, **kwargs)


class CounselorViewSet(viewsets.ModelViewSet):
    serializer_class = CounselorSerializer
    permission_classes = (AdminWrite,)
    queryset = CounselorProfile.objects.select_related("user").order_by("pk")

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.role == User.Role.ADMIN:
            base = queryset
            # ADMIN search/filter
            params = self.request.query_params
            q = params.get("q", "").strip()
            if q:
                base = base.filter(Q(user__username__icontains=q) | Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q))
            is_active = params.get("is_active")
            if is_active in ("true", "false"):
                base = base.filter(user__is_active=(is_active == "true"))
            # Annotate counts for admin list
            base = base.annotate(
                students_count=Count("students", distinct=True),
                active_students_count=Count("students", filter=Q(students__user__is_active=True), distinct=True),
                plans_count=Count("plans", distinct=True),
            )
            return base
        if user.role == User.Role.COUNSELOR:
            return queryset.filter(user=self.request.user)
        return queryset.none()
