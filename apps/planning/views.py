from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models.deletion import ProtectedError
from django.db import models, transaction
from django.db.models import Prefetch
from django.http import HttpResponse
import re as _re
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from apps.accounts.models import User
from apps.accounts.permissions import IsStudent
from .execution import transition
from .models import Plan, PlanDay, PlanItem, PlanItemExecution, StudentFixedCommitment, StudentFixedEvent, StudentSchoolSlot
from .export_service import export_plan_excel, export_plan_pdf
from .excel_to_pdf import convert_excel_to_pdf
from .export_exceptions import ExportConversionFailed
from .serializers import (
    BatchCommitmentSerializer, BulkSchoolSlotsSerializer, FinishExecutionSerializer, PlanDayDetailSerializer,
    PlanDaySerializer, PlanDetailSerializer, PlanItemExecutionSerializer, PlanItemSerializer, PlanSerializer,
    SchoolHoursSerializer, StudentFixedCommitmentSerializer, StudentFixedEventSerializer, StudentSchoolSlotSerializer, manageable_plan, manageable_student, item_has_student_data,
)


class PlanningAccess(BasePermission):
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        if request.user.role == User.Role.ADMIN:
            return True
        if request.user.role == User.Role.COUNSELOR:
            if view.basename in ("commitments", "school-slots", "fixed-events"):
                return True
            return True
        if request.user.role == User.Role.STUDENT:
            # Student: can read plans/days/items and manage own commitments
            if view.basename in ("commitments", "school-slots", "fixed-events"):
                return True
            return request.method in ("GET", "HEAD", "OPTIONS")
        return False


class ExecutionReadAccess(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role in (User.Role.STUDENT, User.Role.ADMIN)


class ProtectedReportDeleteMixin:
    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        if isinstance(obj, Plan):
            # Past-range block removed per product requirement: Plan delete no longer checks dates.
            # Only execution/report guards remain.
            if obj.days.filter(items__execution__isnull=False).exists() or obj.days.filter(items__report_items__isnull=False).exists():
                return Response({"detail": "برنامه دارای فعالیت ثبت‌شده دانش‌آموز است و قابل حذف نیست."}, status=status.HTTP_409_CONFLICT)
        elif isinstance(obj, PlanDay):
            # Past-date delete block removed per product rule: all 7 PlanDays are active. Only execution/report guards remain.
            if obj.items.filter(execution__isnull=False).exists() or obj.items.filter(report_items__isnull=False).exists():
                return Response({"detail": "این روز دارای عملکرد ثبت‌شده دانش‌آموز است و قابل حذف نیست."}, status=status.HTTP_409_CONFLICT)
        else:
            # Past-date delete block removed for PlanItem as well.
            if hasattr(obj, "execution") or obj.report_items.exists():
                return Response({"detail": "این باکس توسط دانش‌آموز شروع یا ثبت شده و قابل حذف نیست."}, status=status.HTTP_409_CONFLICT)
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response({"detail": "This planned activity is included in a daily report and cannot be deleted."}, status=status.HTTP_409_CONFLICT)


def visible_plans(user):
    queryset = Plan.objects.select_related("student__user", "counselor__user")
    if user.role == User.Role.ADMIN:
        return queryset
    if user.role == User.Role.COUNSELOR:
        return queryset.filter(counselor__user=user, student__counselor__user=user)
    if user.role == User.Role.STUDENT:
        return queryset.filter(student__user=user, status=Plan.Status.PUBLISHED)
    return queryset.none()


def _fixed_sources(student, date):
    commitments = StudentFixedCommitment.objects.filter(student=student, active=True, weekday=date.weekday())
    slots = list(StudentSchoolSlot.objects.filter(student=student, weekday=date.weekday(), start_time__isnull=False, end_time__isnull=False))
    for source in commitments:
        if source.valid_from and date < source.valid_from or source.valid_until and date > source.valid_until:
            continue
        if source.kind == StudentFixedCommitment.Kind.SCHOOL and slots:
            continue
        yield "commitment", source, source.title, "مدرسه" if source.kind == StudentFixedCommitment.Kind.SCHOOL else "برنامه ثابت"
    for source in slots:
        yield "school_slot", source, source.subject_name, "مدرسه"
    for source in StudentFixedEvent.objects.filter(student=student, active=True, date=date):
        yield "event", source, source.title, "برنامه ثابت"


@transaction.atomic
def _apply_fixed_to_plan_with_report(plan):
    """Reconcile by source identity; legacy editable boxes are replaced once."""
    from datetime import datetime
    # Serialize apply requests for this plan, including empty plans.
    Plan.objects.select_for_update().get(pk=plan.pk)
    desired = {}
    for day in plan.days.all():
        for source_type, source, title, note in _fixed_sources(plan.student, day.date):
            desired[(source_type, source.pk, day.date)] = (day, source, title, note)
    existing = {}
    removed = 0
    for item in PlanItem.objects.filter(plan_day__plan=plan).select_related("plan_day"):
        if not item.fixed_source_type and item.note not in ("مدرسه", "برنامه ثابت"):
            continue
        key = (item.fixed_source_type, item.fixed_source_id, item.plan_day.date)
        if key in desired and key not in existing:
            existing[key] = item
        elif not item_has_student_data(item):
            item.delete()
            removed += 1
    created = 0
    skipped = []
    for key, (day, source, title, note) in desired.items():
        item = existing.get(key)
        if item and item_has_student_data(item):
            continue
        overlaps = day.items.filter(start_time__lt=source.end_time, end_time__gt=source.start_time)
        if item:
            overlaps = overlaps.exclude(pk=item.pk)
        editable_fixed_ids = [entry.pk for entry in existing.values() if not item_has_student_data(entry)]
        overlaps = overlaps.exclude(pk__in=editable_fixed_ids)
        source_overlap = any(other_key != key and other_day.pk == day.pk and other_source.start_time < source.end_time and other_source.end_time > source.start_time for other_key, (other_day, other_source, _, _) in desired.items())
        if overlaps.exists() or source_overlap:
            skipped.append({"date": str(day.date), "title": title, "start": str(source.start_time), "end": str(source.end_time), "reason": "conflict"})
            # Release a stale reservation if the source changed into a conflict.
            if item and (item.start_time != source.start_time or item.end_time != source.end_time):
                item.delete()
                removed += 1
            continue
        if not item:
            item = PlanItem(plan_day=day, kind=PlanItem.Kind.EVENT, ordering=day.items.count())
            created += 1
        item.title, item.note = title, note
        item.start_time, item.end_time = source.start_time, source.end_time
        item.planned_duration_minutes = int((datetime.combine(day.date, source.end_time) - datetime.combine(day.date, source.start_time)).total_seconds() // 60)
        item.fixed_source_type, item.fixed_source_id = key[:2]
        item.save()
    return created, skipped, removed


def _apply_fixed_to_plan(plan):
    """Backward compat wrapper returning only count."""
    created, _, _ = _apply_fixed_to_plan_with_report(plan)
    return created


class PlanViewSet(ProtectedReportDeleteMixin, viewsets.ModelViewSet):
    permission_classes = (PlanningAccess,)
    queryset = Plan.objects.all()

    def get_queryset(self):
        queryset = visible_plans(self.request.user)
        student = self.request.query_params.get("student")
        if student and student.isdecimal():
            queryset = queryset.filter(student_id=int(student))
        # Admin filters
        if self.request.user.role == User.Role.ADMIN:
            counselor = self.request.query_params.get("counselor")
            if counselor and counselor.isdigit():
                queryset = queryset.filter(counselor_id=int(counselor))
            plan_status = self.request.query_params.get("status")
            if plan_status in (Plan.Status.DRAFT, Plan.Status.PUBLISHED):
                queryset = queryset.filter(status=plan_status)
            q = self.request.query_params.get("q", "").strip()
            if q:
                queryset = queryset.filter(
                    models.Q(title__icontains=q)
                    | models.Q(student__user__username__icontains=q)
                    | models.Q(student__user__first_name__icontains=q)
                    | models.Q(student__user__last_name__icontains=q)
                )
            date_from = self.request.query_params.get("date_from")
            date_to = self.request.query_params.get("date_to")
            if date_from:
                try:
                    from datetime import date as _d
                    _d.fromisoformat(date_from)
                    queryset = queryset.filter(start_date__gte=date_from)
                except ValueError:
                    pass
            if date_to:
                try:
                    from datetime import date as _d
                    _d.fromisoformat(date_to)
                    queryset = queryset.filter(end_date__lte=date_to)
                except ValueError:
                    pass
        if self.action in ("retrieve", "export_pdf", "export_excel"):
            items = PlanItem.objects.select_related("subject", "chapter", "topic", "execution").prefetch_related("report_items")
            days = PlanDay.objects.prefetch_related(Prefetch("items", queryset=items))
            queryset = queryset.prefetch_related(Prefetch("days", queryset=days))
        return queryset

    def create(self, request, *args, **kwargs):
        apply_fixed = str(request.data.get("apply_fixed", "")).lower() in ("1", "true", "yes")
        # Remove apply_fixed so serializer doesn't complain
        if "apply_fixed" in request.data:
            data = request.data.copy()
            data.pop("apply_fixed", None)
            request._full_data = data
            # need to re-validate through serializer path: we call super with modified data via serializer
            serializer = self.get_serializer(data=data)
            serializer.is_valid(raise_exception=True)
            self.perform_create(serializer)
            plan = serializer.instance
            # Ensure 7 days exist before applying fixed (like frontend does, but ensure at least start_date day)
            from datetime import timedelta
            for offset in range(7):
                d = plan.start_date + timedelta(days=offset)
                if d > plan.end_date:
                    break
                PlanDay.objects.get_or_create(plan=plan, date=d)
            plan.refresh_from_db()
            if apply_fixed:
                # Prevent duplicate apply
                if not plan.days.filter(items__isnull=False).exists():
                    _apply_fixed_to_plan(plan)
                else:
                    # Plan already has user items (rare branch when frontend pre-created days with items) - still try to add missing fixed boxes
                    _apply_fixed_to_plan_with_report(plan)
                plan.refresh_from_db()
            headers = self.get_success_headers(serializer.data)
            # Return with days
            from .serializers import PlanDetailSerializer as _PDS
            detail = _PDS(plan, context=self.get_serializer_context()).data if hasattr(plan, 'days') else serializer.data
            return Response(detail, status=status.HTTP_201_CREATED, headers=headers)
        return super().create(request, *args, **kwargs)

    @action(detail=True, methods=("post",), url_path="apply-fixed")
    @transaction.atomic
    def apply_fixed(self, request, pk=None):
        """Resync fixed-schedule boxes for an existing DRAFT plan.
        Only modifies fixed-origin items (note مدرسه / برنامه ثابت) that have no execution/report.
        Reports created/skipped so frontend can show 'تداخل دارد' feedback.
        """
        plan = self.get_object()
        if not manageable_plan(plan, request.user):
            return Response({"detail": "This plan is not available to you."}, status=status.HTTP_403_FORBIDDEN)
        if plan.status != Plan.Status.DRAFT:
            return Response({"detail": "فقط برنامه پیش‌نویس قابل همگام‌سازی است."}, status=status.HTTP_409_CONFLICT)
        created, skipped, removed = _apply_fixed_to_plan_with_report(plan)
        plan.refresh_from_db()
        from .serializers import PlanDetailSerializer as _PDS2
        data = _PDS2(plan, context=self.get_serializer_context()).data
        return Response({"plan": data, "created": created, "removed": removed, "skipped": skipped})

    def get_serializer_class(self):
        return PlanDetailSerializer if self.action == "retrieve" else PlanSerializer

    @action(detail=True, methods=("get",), permission_classes=(ExecutionReadAccess,))
    def executions(self, request, pk=None):
        plan = self.get_object()
        records = PlanItemExecution.objects.filter(plan_item__plan_day__plan=plan)
        if request.user.role == User.Role.STUDENT:
            records = records.filter(student__user=request.user)
        return Response(PlanItemExecutionSerializer(records, many=True, context={"now": timezone.now()}).data)

    def _export_filename(self, plan, ext: str) -> str:
        # Deterministic, readable, Telegram-friendly: plan-{id}-{student_username}
        try:
            username = getattr(plan.student.user, "username", "") or ""
            # keep only safe chars, lower
            safe = _re.sub(r"[^a-zA-Z0-9_-]", "-", username).strip("-") or "student"
        except Exception:
            safe = "student"
        base = f"plan-{plan.pk}-{safe}"
        # slugify to ensure ascii-safe while keeping readability
        return f"{base}.{ext}"

    @action(detail=True, methods=("get",), url_path="export/pdf")
    def export_pdf(self, request, pk=None):
        plan = self.get_object()
        filename = self._export_filename(plan, "pdf")
        try:
            pdf_bytes = export_plan_pdf(plan)
        except ExportConversionFailed as exc:
            return Response({"detail": str(exc.detail) if hasattr(exc, "detail") else str(exc), "code": exc.default_code}, status=exc.status_code)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Access-Control-Expose-Headers"] = "Content-Disposition, Content-Length"
        return response

    @action(detail=True, methods=("get",), url_path="export/excel")
    def export_excel(self, request, pk=None):
        plan = self.get_object()
        filename = self._export_filename(plan, "xlsx")
        response = HttpResponse(
            export_plan_excel(plan),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Access-Control-Expose-Headers"] = "Content-Disposition, Content-Length"
        return response

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def publish(self, request, pk=None):
        plan = self.get_object()
        if plan.status != Plan.Status.DRAFT:
            raise serializers.ValidationError({"status": "Plan is already published."})
        days = list(plan.days.prefetch_related("items"))
        if not days or any(not day.items.all() for day in days):
            raise serializers.ValidationError({"days": "A published plan needs at least one day and an item in every day."})
        try:
            plan.full_clean()
            for day in days:
                day.full_clean()
                for item in day.items.all():
                    item.full_clean()
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.message_dict if hasattr(error, "message_dict") else error.messages) from error
        plan.status = Plan.Status.PUBLISHED
        plan.published_at = timezone.now()
        plan.save()
        from apps.telegram.automation import publish_pdf_after_commit
        publish_pdf_after_commit(plan)
        return Response(PlanSerializer(plan, context=self.get_serializer_context()).data)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def duplicate(self, request, pk=None):
        source = self.get_object()
        copied = Plan.objects.create(
            student=source.student, counselor=source.counselor, title=source.title,
            start_date=source.start_date, end_date=source.end_date,
        )
        for source_day in source.days.prefetch_related("items"):
            day = PlanDay.objects.create(plan=copied, date=source_day.date)
            for item in source_day.items.all():
                copy_item(item, day)
        return Response(PlanSerializer(copied, context=self.get_serializer_context()).data, status=status.HTTP_201_CREATED)


class ReorderDaySerializer(serializers.Serializer):
    ordering = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)

    def validate_ordering(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("Duplicate ids in ordering.")
        return value


class PlanDayViewSet(ProtectedReportDeleteMixin, viewsets.ModelViewSet):
    permission_classes = (PlanningAccess,)
    serializer_class = PlanDaySerializer
    queryset = PlanDay.objects.all()

    def get_queryset(self):
        queryset = PlanDay.objects.select_related("plan__student", "plan__counselor__user")
        if self.action in ("retrieve", "duplicate", "reorder"):
            queryset = queryset.prefetch_related(Prefetch("items", queryset=PlanItem.objects.select_related("subject", "chapter", "topic", "execution").prefetch_related("report_items")))
        return queryset.filter(plan__in=visible_plans(self.request.user))

    def get_serializer_class(self):
        return PlanDayDetailSerializer if self.action == "retrieve" else PlanDaySerializer

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def duplicate(self, request, pk=None):
        source = self.get_object()
        action_input = DuplicateDaySerializer(data=request.data)
        action_input.is_valid(raise_exception=True)
        target = action_input.validated_data.get("target_plan", source.plan)
        if not manageable_plan(target, request.user):
            raise serializers.ValidationError({"target_plan": "This plan is not available to you."})
        date = action_input.validated_data["date"]
        candidate = PlanDay(plan=target, date=date)
        try:
            candidate.full_clean()
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.message_dict if hasattr(error, "message_dict") else error.messages) from error
        candidate.save()
        for item in source.items.all():
            copy_item(item, candidate)
        candidate = PlanDay.objects.prefetch_related(Prefetch("items", queryset=PlanItem.objects.select_related("subject", "chapter", "topic"))).get(pk=candidate.pk)
        return Response(PlanDayDetailSerializer(candidate, context=self.get_serializer_context()).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=("post",), url_path="copy-day")
    @transaction.atomic
    def copy_day(self, request, pk=None):
        source = self.get_object()
        payload = CopyDaySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        target_day_id = payload.validated_data["target_day"]
        mode = payload.validated_data.get("mode", "append")
        if source.pk == target_day_id:
            raise serializers.ValidationError({"target_day": "Cannot copy a day onto itself."})
        try:
            target = PlanDay.objects.select_for_update().get(pk=target_day_id)
        except PlanDay.DoesNotExist:
            raise serializers.ValidationError({"target_day": "Target day does not exist."})
        if target.plan_id != source.plan_id:
            raise serializers.ValidationError({"target_day": "Target day must belong to same plan."})
        if not manageable_plan(target.plan, request.user):
            raise serializers.ValidationError({"target_day": "This plan is not available to you."})
        # Do not allow replace if target has protected items
        if mode == "replace":
            if target.items.filter(execution__isnull=False).exists() or target.items.filter(report_items__isnull=False).exists():
                return Response({"detail": "این روز دارای عملکرد ثبت‌شده است و قابل جایگزینی نیست.", "code": "PROTECTED_TARGET"}, status=status.HTTP_409_CONFLICT)
        # Time overlap validation helper
        def _overlaps(a_s, a_e, b_s, b_e):
            if not a_s or not a_e or not b_s or not b_e:
                return False
            return a_s < b_e and a_e > b_s
        existing = list(target.items.select_for_update().all())
        source_items = list(source.items.order_by("ordering", "pk"))
        # Check that source day itself is not protected for copying? copying protected items is allowed but they lose execution
        # Validate timed overlaps
        # 1) source timed items that overlap each other? they already coexist on source, skip
        # 2) source timed vs existing timed
        # 3) source timed vs fixed commitments
        from apps.planning.models import StudentFixedCommitment
        weekday = target.date.weekday()
        commitments = list(StudentFixedCommitment.objects.filter(student_id=target.plan.student_id, active=True, weekday=weekday))
        # Build lists of timed intervals
        def collect_times(items):
            out=[]
            for it in items:
                if it.start_time and it.end_time:
                    out.append((it.start_time, it.end_time, it))
            return out
        existing_times = collect_times(existing)
        source_times = collect_times(source_items)
        # When appending, check source_times vs existing_times
        if mode == "append":
            for s_s, s_e, s_it in source_times:
                for e_s, e_e, _ in existing_times:
                    if _overlaps(s_s, s_e, e_s, e_e):
                        return Response({"detail": "این زمان با فعالیت موجود در روز مقصد تداخل دارد.", "code": "COPY_CONFLICT", "conflict_start": s_s.isoformat(), "conflict_end": s_e.isoformat()}, status=status.HTTP_409_CONFLICT)
                for comm in commitments:
                    if _overlaps(s_s, s_e, comm.start_time, comm.end_time):
                        return Response({"detail": "این بازه با تعهد ثابت دانش‌آموز تداخل دارد.", "code": "COMMITMENT_CONFLICT"}, status=status.HTTP_409_CONFLICT)
        # When replacing, only need to check source vs commitments (existing will be deleted)
        if mode == "replace":
            for s_s, s_e, _ in source_times:
                for comm in commitments:
                    if _overlaps(s_s, s_e, comm.start_time, comm.end_time):
                        return Response({"detail": "این بازه با تعهد ثابت دانش‌آموز تداخل دارد.", "code": "COMMITMENT_CONFLICT"}, status=status.HTTP_409_CONFLICT)
        # Also check internal source timed overlap among themselves is valid (should be, but validate)
        # Perform copy atomically
        if mode == "replace":
            # Delete existing items (they are not protected per above check)
            target.items.all().delete()
            # Reset ordering base
            existing = []
        # Compute ordering base
        max_order = -1
        if existing:
            max_order = max(i.ordering for i in existing)
        # Copy each source item with new id, preserving fields but not execution/report
        # For append, offset copied ordering after existing max
        base = (max(i.ordering for i in existing) + 1) if existing else 0
        for src in source_items:
            created = copy_item(src, target)
            # offset ordering to append
            if existing:
                PlanItem.objects.filter(pk=created.pk).update(ordering=base + src.ordering)
        # Re-normalize ordering for target after copy
        all_items = list(target.items.order_by("ordering", "pk"))
        for new_idx, it in enumerate(sorted(all_items, key=lambda x: (x.ordering, x.pk))):
            if it.ordering != new_idx:
                PlanItem.objects.filter(pk=it.pk).update(ordering=new_idx)
        target = PlanDay.objects.prefetch_related(Prefetch("items", queryset=PlanItem.objects.select_related("subject", "chapter", "topic", "execution").prefetch_related("report_items").order_by("ordering", "pk"))).get(pk=target.pk)
        return Response(PlanDayDetailSerializer(target, context=self.get_serializer_context()).data)

    @action(detail=True, methods=("post",), url_path="reorder")
    @transaction.atomic
    def reorder(self, request, pk=None):
        day = self.get_object()
        if day.plan.status == Plan.Status.PUBLISHED:
            # allow reordering draft? published plan structural change? check current policy - allow if editable
            # mimic existing check: published plan title only can change but days/items editable unless locked; we allow reorder if day editable
            pass
        # check any executed items block reordering? allow reorder but keep editable check per item
        if day.items.filter(execution__isnull=False).exists() or day.items.filter(report_items__isnull=False).exists():
            # if any item locked, we still allow reorder of unlocked? For simplicity reject
            return Response({"detail": "این روز دارای عملکرد ثبت‌شده است و ترتیب آن قابل تغییر نیست."}, status=status.HTTP_409_CONFLICT)
        payload = ReorderDaySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        ids = payload.validated_data["ordering"]
        existing = set(day.items.values_list("id", flat=True))
        if set(ids) != existing:
            raise serializers.ValidationError({"ordering": "Ordering must contain exactly the ids of this day."})
        # enforce manageable_plan
        if not manageable_plan(day.plan, request.user):
            raise serializers.ValidationError({"detail": "This plan is not available to you."})
        from apps.telegram.automation import refresh_after_commit
        refresh_after_commit(day.plan)
        for index, item_id in enumerate(ids):
            PlanItem.objects.filter(pk=item_id, plan_day=day).update(ordering=index)
        day = PlanDay.objects.prefetch_related(Prefetch("items", queryset=PlanItem.objects.select_related("subject", "chapter", "topic", "execution").prefetch_related("report_items").order_by("ordering", "pk"))).get(pk=day.pk)
        return Response(PlanDayDetailSerializer(day, context=self.get_serializer_context()).data)


class DuplicateDaySerializer(serializers.Serializer):
    date = serializers.DateField()
    target_plan = serializers.PrimaryKeyRelatedField(queryset=Plan.objects.all(), required=False)


class CopyDaySerializer(serializers.Serializer):
    target_day = serializers.IntegerField()
    mode = serializers.ChoiceField(choices=["append", "replace"], required=False, default="append")


def copy_item(source, target_day):
    return PlanItem.objects.create(
        plan_day=target_day, kind=source.kind, ordering=source.ordering, title=source.title,
        planned_duration_minutes=source.planned_duration_minutes, start_time=source.start_time,
        end_time=source.end_time, note=source.note, subject=source.subject, chapter=source.chapter,
        topic=source.topic, test_count=source.test_count,
        taxonomy=source.taxonomy, title_is_custom=source.title_is_custom,
    )


class MoveItemSerializer(serializers.Serializer):
    target_day = serializers.IntegerField()
    ordering = serializers.IntegerField(required=False, min_value=0)
    start_time = serializers.TimeField(required=False, allow_null=True)
    end_time = serializers.TimeField(required=False, allow_null=True)
    planned_duration_minutes = serializers.IntegerField(required=False, allow_null=True, min_value=1)

    def validate(self, attrs):
        st = attrs.get("start_time")
        en = attrs.get("end_time")
        if (st is None) != (en is None):
            # allow only one null -> both must be provided together if any time change requested
            if st is not None or en is not None:
                raise serializers.ValidationError({"end_time": "Start and end times must be supplied together."})
        if st and en and en <= st:
            raise serializers.ValidationError({"end_time": "End time must be after start time."})
        return attrs


class PlanItemViewSet(ProtectedReportDeleteMixin, viewsets.ModelViewSet):
    permission_classes = (PlanningAccess,)
    serializer_class = PlanItemSerializer
    queryset = PlanItem.objects.all()

    def get_queryset(self):
        return PlanItem.objects.select_related("plan_day__plan", "subject", "chapter", "topic", "execution").prefetch_related("report_items").filter(
            plan_day__plan__in=visible_plans(self.request.user)
        )

    def _execute(self, request, pk, action_name, *, complete=None):
        item = self.get_object()
        execution = transition(request.user, item.pk, action_name, complete=complete)
        return Response(PlanItemExecutionSerializer(execution, context={"now": timezone.now()}).data)

    @action(detail=True, methods=("post",), permission_classes=(IsStudent,))
    def start(self, request, pk=None):
        return self._execute(request, pk, "start")

    @action(detail=True, methods=("post",), permission_classes=(IsStudent,))
    def pause(self, request, pk=None):
        return self._execute(request, pk, "pause")

    @action(detail=True, methods=("post",), permission_classes=(IsStudent,))
    def resume(self, request, pk=None):
        return self._execute(request, pk, "resume")

    @action(detail=True, methods=("post",), permission_classes=(IsStudent,))
    def finish(self, request, pk=None):
        payload = FinishExecutionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return self._execute(request, pk, "finish", complete=payload.validated_data["complete"])

    @action(detail=True, methods=("post",), permission_classes=(IsStudent,), url_path="quick-complete")
    def quick_complete(self, request, pk=None):
        return self._execute(request, pk, "quick_complete")

    @action(detail=True, methods=("post",), permission_classes=(IsStudent,), url_path="not-done")
    def not_done(self, request, pk=None):
        return self._execute(request, pk, "mark_not_done")

    @action(detail=True, methods=("post",), url_path="duplicate")
    @transaction.atomic
    def duplicate(self, request, pk=None):
        source = self.get_object()
        # check lock
        if hasattr(source, "execution") or source.report_items.exists():
            return Response({"detail": "این باکس توسط دانش‌آموز شروع یا ثبت شده و قابل تکثیر نیست."}, status=status.HTTP_409_CONFLICT)
        if not manageable_plan(source.plan_day.plan, request.user):
            raise serializers.ValidationError({"detail": "This plan is not available to you."})
        # target is same day by default
        target_day = source.plan_day
        # optional target_day param
        payload = MoveItemSerializer(data=request.data) if request.data else None
        if payload and payload.is_valid():
            try:
                candidate = PlanDay.objects.get(pk=payload.validated_data["target_day"])
            except PlanDay.DoesNotExist:
                raise serializers.ValidationError({"target_day": "This day does not exist."})
            if candidate.plan_id != source.plan_day.plan_id:
                raise serializers.ValidationError({"target_day": "Target day must belong to same plan."})
            target_day = candidate
        max_order = target_day.items.aggregate(models.Max("ordering"))["ordering__max"]
        next_order = (max_order + 1) if max_order is not None else 0
        copied = copy_item(source, target_day)
        # ensure ordering at end
        copied.ordering = next_order
        copied.save()
        return Response(PlanItemSerializer(copied, context=self.get_serializer_context()).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=("post",), url_path="move")
    @transaction.atomic
    def move(self, request, pk=None):
        item = self.get_object()
        Plan.objects.select_for_update().get(pk=item.plan_day.plan_id)
        if hasattr(item, "execution") or item.report_items.exists():
            return Response({"detail": "این باکس توسط دانش‌آموز شروع یا ثبت شده و قابل جابه‌جایی نیست."}, status=status.HTTP_409_CONFLICT)
        payload = MoveItemSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            target_day = PlanDay.objects.select_for_update().get(pk=payload.validated_data["target_day"])
        except PlanDay.DoesNotExist:
            raise serializers.ValidationError({"target_day": "This day does not exist."})
        if target_day.plan_id != item.plan_day.plan_id:
            raise serializers.ValidationError({"target_day": "Target day must belong to same plan."})
        if not manageable_plan(target_day.plan, request.user):
            raise serializers.ValidationError({"target_day": "This plan is not available to you."})
        new_start = payload.validated_data.get("start_time", None)
        new_end = payload.validated_data.get("end_time", None)
        new_duration = payload.validated_data.get("planned_duration_minutes", None)
        has_time_change = "start_time" in payload.validated_data or "end_time" in payload.validated_data
        has_duration_change = "planned_duration_minutes" in payload.validated_data
        item = PlanItem.objects.select_for_update().get(pk=item.pk)
        effective_start = new_start if has_time_change else item.start_time
        effective_end = new_end if has_time_change else item.end_time
        effective_duration = new_duration if has_duration_change else item.planned_duration_minutes
        if has_time_change and effective_start and not effective_end and effective_duration:
            from datetime import datetime, timedelta
            dt_start = datetime.combine(target_day.date, effective_start)
            dt_end = dt_start + timedelta(minutes=effective_duration)
            effective_end = dt_end.time()
        if has_duration_change and effective_start and effective_duration and not has_time_change:
            from datetime import datetime, timedelta
            dt_start = datetime.combine(target_day.date, effective_start)
            dt_end = dt_start + timedelta(minutes=effective_duration)
            effective_end = dt_end.time()
        if item.kind != PlanItem.Kind.EVENT and not effective_duration:
            raise serializers.ValidationError({"planned_duration_minutes": "Duration is required for this item type."})
        if (effective_start is None) != (effective_end is None):
            raise serializers.ValidationError({"end_time": "Start and end times must be supplied together."})
        if effective_start and effective_end and effective_end <= effective_start:
            raise serializers.ValidationError({"end_time": "End time must be after start time."})
        if effective_start and effective_end:
            def _overlaps(a_s, a_e, b_s, b_e):
                if not a_s or not a_e or not b_s or not b_e:
                    return False
                return a_s < b_e and a_e > b_s
            other_items = list(target_day.items.select_for_update().exclude(pk=item.pk).filter(start_time__isnull=False, end_time__isnull=False))
            for other in other_items:
                if _overlaps(effective_start, effective_end, other.start_time, other.end_time):
                    return Response({"detail": "این زمان با فعالیت دیگری تداخل دارد.", "code": "MOVE_CONFLICT"}, status=status.HTTP_409_CONFLICT)
            for source_type, source, _, _ in _fixed_sources(target_day.plan.student, target_day.date):
                if (source_type, source.pk) == (item.fixed_source_type, item.fixed_source_id):
                    continue
                if _overlaps(effective_start, effective_end, source.start_time, source.end_time):
                    return Response({"detail": "این بازه با برنامه ثابت دانش‌آموز تداخل دارد.", "code": "COMMITMENT_CONFLICT"}, status=status.HTTP_409_CONFLICT)
        source = None
        if item.fixed_source_type:
            source_model = {"school_slot": StudentSchoolSlot, "commitment": StudentFixedCommitment, "event": StudentFixedEvent}[item.fixed_source_type]
            source = source_model.objects.select_for_update().filter(pk=item.fixed_source_id, student=target_day.plan.student).first()
            if not source:
                raise serializers.ValidationError("منبع این باکس حذف شده است؛ برنامه‌های ثابت را همگام کنید.")
            if not effective_start or not effective_end:
                raise serializers.ValidationError("باکس ثابت باید ساعت شروع و پایان داشته باشد.")
            source.start_time, source.end_time = effective_start, effective_end
            if item.fixed_source_type == "event":
                source.date = target_day.date
            else:
                source.weekday = target_day.date.weekday()
            try:
                source.full_clean()
            except DjangoValidationError as error:
                raise serializers.ValidationError(error.message_dict) from error
        elif item.note in ("مدرسه", "برنامه ثابت"):
            raise serializers.ValidationError("ابتدا برنامه مدرسه و رویدادهای ثابت را به برنامه اضافه کنید تا منبع باکس مشخص شود.")
        desired = payload.validated_data.get("ordering")
        original_day_id = item.plan_day_id
        item.plan_day = target_day
        if has_time_change:
            item.start_time = effective_start
            item.end_time = effective_end
        if has_duration_change:
            item.planned_duration_minutes = effective_duration
        items = list(target_day.items.exclude(pk=item.pk).order_by("ordering", "pk"))
        if desired is not None:
            idx = min(max(0, desired), len(items))
        else:
            idx = len(items)
        items.insert(idx, item)
        try:
            item.full_clean()
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.message_dict if hasattr(error, "message_dict") else error.messages) from error
        if source:
            source.save()
            if item.fixed_source_type == "school_slot" and original_day_id != target_day.pk:
                original_date = PlanDay.objects.get(pk=original_day_id).date
                # A school envelope is only a fallback when no periods exist. Moving
                # the last period must not resurrect that envelope at the old time.
                if not StudentSchoolSlot.objects.filter(student=target_day.plan.student, weekday=original_date.weekday(), start_time__isnull=False, end_time__isnull=False).exists():
                    StudentFixedCommitment.objects.filter(student=target_day.plan.student, kind=StudentFixedCommitment.Kind.SCHOOL, weekday=original_date.weekday()).update(active=False)
        item.save()
        for new_idx, entry in enumerate(items):
            if entry.pk == item.pk:
                PlanItem.objects.filter(pk=entry.pk).update(ordering=new_idx)
            else:
                if entry.ordering != new_idx:
                    PlanItem.objects.filter(pk=entry.pk).update(ordering=new_idx)
        if target_day.pk != original_day_id:
            source_items = list(PlanDay.objects.get(pk=original_day_id).items.order_by("ordering", "pk"))
            for new_idx, entry in enumerate(source_items):
                if entry.ordering != new_idx:
                    PlanItem.objects.filter(pk=entry.pk).update(ordering=new_idx)
        item.refresh_from_db()
        return Response(PlanItemSerializer(item, context=self.get_serializer_context()).data)


class StudentFixedCommitmentViewSet(viewsets.ModelViewSet):
    permission_classes = (PlanningAccess,)
    serializer_class = StudentFixedCommitmentSerializer
    queryset = StudentFixedCommitment.objects.all()

    def get_queryset(self):
        queryset = StudentFixedCommitment.objects.select_related("student__user", "student__counselor__user")
        user = self.request.user
        if user.role == User.Role.ADMIN:
            scoped = queryset
        elif user.role == User.Role.COUNSELOR:
            scoped = queryset.filter(student__counselor__user=user)
        else:
            scoped = queryset.filter(student__user=user)
        student = self.request.query_params.get("student")
        if student and student.isdecimal():
            scoped = scoped.filter(student_id=int(student))
        kind = self.request.query_params.get("kind")
        if kind:
            scoped = scoped.filter(kind=kind)
        active = self.request.query_params.get("active")
        if active in ("true", "false"):
            scoped = scoped.filter(active=(active == "true"))
        group = self.request.query_params.get("group_id")
        if group:
            scoped = scoped.filter(group_id=group)
        return scoped

    def perform_create(self, serializer):
        student = serializer.validated_data.get("student")
        # For student users, manageable_student returns False (only admin/counselor). Skip check for STUDENT.
        if student and self.request.user.role not in ("STUDENT",) and not manageable_student(student, self.request.user) and self.request.user.role != User.Role.ADMIN:
            raise serializers.ValidationError({"student": "This student is not assigned to you."})
        serializer.save()

    def perform_update(self, serializer):
        student = serializer.validated_data.get("student", serializer.instance.student)
        if self.request.user.role not in ("STUDENT",) and not manageable_student(student, self.request.user) and self.request.user.role != User.Role.ADMIN:
            raise serializers.ValidationError({"student": "This student is not assigned to you."})
        serializer.save()

    @action(detail=False, methods=("post",), url_path="batch")
    @transaction.atomic
    def batch(self, request):
        serializer = BatchCommitmentSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        student = data["student"]
        weekdays = data["weekdays"]
        import uuid
        group_id = uuid.uuid4()
        created = []
        for wd in weekdays:
            obj = StudentFixedCommitment(
                student=student, kind=data["kind"], title=data["title"].strip(),
                weekday=wd, start_time=data["start_time"], end_time=data["end_time"],
                active=True, group_id=group_id, valid_from=data.get("valid_from"), valid_until=data.get("valid_until"),
            )
            obj.full_clean()
            obj.save()
            created.append(obj)
        return Response(StudentFixedCommitmentSerializer(created, many=True).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=("post",), url_path="school-hours")
    @transaction.atomic
    def school_hours(self, request):
        serializer = SchoolHoursSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        student = data["student"]
        # Atomic replace of SCHOOL commitments for given weekdays
        import uuid
        group_id = uuid.uuid4()
        # Delete existing SCHOOL commitments for those weekdays (preserve history: we delete and recreate)
        StudentFixedCommitment.objects.filter(student=student, kind=StudentFixedCommitment.Kind.SCHOOL, weekday__in=data["weekdays"]).delete()
        created = []
        for wd in data["weekdays"]:
            obj = StudentFixedCommitment(
                student=student, kind=StudentFixedCommitment.Kind.SCHOOL, title=data.get("title") or "مدرسه",
                weekday=wd, start_time=data["start_time"], end_time=data["end_time"],
                active=True, group_id=group_id, valid_from=data.get("valid_from"), valid_until=data.get("valid_until"),
            )
            obj.full_clean()
            obj.save()
            created.append(obj)
        return Response(StudentFixedCommitmentSerializer(created, many=True).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=("post",), url_path="toggle-active")
    def toggle_active(self, request, pk=None):
        obj = self.get_object()
        if not manageable_student(obj.student, request.user) and request.user.role != User.Role.ADMIN:
            return Response({"detail": "This commitment is not available to you."}, status=status.HTTP_403_FORBIDDEN)
        obj.active = not obj.active
        obj.save(update_fields=["active"])
        return Response(StudentFixedCommitmentSerializer(obj).data)

    @action(detail=False, methods=("post",), url_path="copy-school-day")
    @transaction.atomic
    def copy_school_day(self, request):
        student_id = request.data.get("student")
        source_weekday = request.data.get("source_weekday")
        target_weekdays = request.data.get("target_weekdays")
        if student_id is None or source_weekday is None or not target_weekdays:
            raise serializers.ValidationError({"detail": "student, source_weekday, target_weekdays are required."})
        try:
            from apps.accounts.models import StudentProfile
            student = StudentProfile.objects.get(pk=int(student_id))
        except Exception:
            raise serializers.ValidationError({"student": "Student not found."})
        if not manageable_student(student, request.user) and request.user.role != User.Role.ADMIN:
            return Response({"detail": "This student is not available to you."}, status=status.HTTP_403_FORBIDDEN)
        source_weekday = int(source_weekday)
        target_weekdays = [int(x) for x in target_weekdays]
        # Source data
        source_commit = StudentFixedCommitment.objects.filter(student=student, kind=StudentFixedCommitment.Kind.SCHOOL, weekday=source_weekday).first()
        source_slots = list(StudentSchoolSlot.objects.filter(student=student, weekday=source_weekday).order_by("period"))
        if not source_commit and not source_slots:
            return Response({"detail": "مبدأ برنامه‌ای ندارد."}, status=status.HTTP_404_NOT_FOUND)
        for twd in target_weekdays:
            if twd == source_weekday:
                continue
            # Check overwrite
            has_existing = StudentFixedCommitment.objects.filter(student=student, kind=StudentFixedCommitment.Kind.SCHOOL, weekday=twd).exists() or StudentSchoolSlot.objects.filter(student=student, weekday=twd).exists()
            force = str(request.data.get("force", "")).lower() in ("1", "true", "yes")
            if has_existing and not force:
                return Response({"detail": f"روز مقصد {twd} از قبل برنامه دارد. تأیید لازم است.", "code": "NEEDS_CONFIRM", "weekday": twd}, status=status.HTTP_409_CONFLICT)
            # Replace target
            StudentFixedCommitment.objects.filter(student=student, kind=StudentFixedCommitment.Kind.SCHOOL, weekday=twd).delete()
            StudentSchoolSlot.objects.filter(student=student, weekday=twd).delete()
            if source_commit:
                StudentFixedCommitment.objects.create(student=student, kind=StudentFixedCommitment.Kind.SCHOOL, title=source_commit.title, weekday=twd, start_time=source_commit.start_time, end_time=source_commit.end_time, active=source_commit.active, valid_from=source_commit.valid_from, valid_until=source_commit.valid_until)
            for s in source_slots:
                StudentSchoolSlot.objects.create(student=student, weekday=twd, period=s.period, subject_name=s.subject_name, start_time=s.start_time, end_time=s.end_time)
        return Response({"detail": "کپی انجام شد."})

    @action(detail=False, methods=("post",), url_path="group-update")
    @transaction.atomic
    def group_update(self, request):
        group_id = request.data.get("group_id")
        if not group_id:
            raise serializers.ValidationError({"group_id": "This field is required."})
        items = list(StudentFixedCommitment.objects.filter(group_id=group_id).select_related("student"))
        if not items:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        # Ownership check via first item
        if not manageable_student(items[0].student, request.user) and request.user.role != User.Role.ADMIN:
            return Response({"detail": "This group is not available to you."}, status=status.HTTP_403_FORBIDDEN)
        # Validate via BatchCommitmentSerializer-like rules
        title = request.data.get("title")
        kind = request.data.get("kind")
        start_time = request.data.get("start_time")
        end_time = request.data.get("end_time")
        valid_from = request.data.get("valid_from")
        valid_until = request.data.get("valid_until")
        active = request.data.get("active")
        # Build update dict
        from django.utils.dateparse import parse_time, parse_date
        updates = {}
        if title is not None:
            if not str(title).strip():
                raise serializers.ValidationError({"title": "Title is required."})
            updates["title"] = str(title).strip()
        if kind is not None:
            if kind not in [c[0] for c in StudentFixedCommitment.Kind.choices]:
                raise serializers.ValidationError({"kind": "Invalid kind."})
            updates["kind"] = kind
        if start_time is not None:
            pt = parse_time(str(start_time))
            if not pt:
                raise serializers.ValidationError({"start_time": "Invalid time."})
            updates["start_time"] = pt
        if end_time is not None:
            pt = parse_time(str(end_time))
            if not pt:
                raise serializers.ValidationError({"end_time": "Invalid time."})
            updates["end_time"] = pt
        if "start_time" in updates or "end_time" in updates:
            s = updates.get("start_time", items[0].start_time)
            e = updates.get("end_time", items[0].end_time)
            if e <= s:
                raise serializers.ValidationError({"end_time": "End time must be after start time."})
        if valid_from is not None:
            updates["valid_from"] = parse_date(str(valid_from)) if valid_from else None
        if valid_until is not None:
            updates["valid_until"] = parse_date(str(valid_until)) if valid_until else None
        if "valid_from" in updates or "valid_until" in updates:
            vf = updates.get("valid_from", items[0].valid_from)
            vu = updates.get("valid_until", items[0].valid_until)
            if vf and vu and vu < vf:
                raise serializers.ValidationError({"valid_until": "End date must be on or after start date."})
        if active is not None:
            updates["active"] = bool(active)
        for obj in items:
            for k, v in updates.items():
                setattr(obj, k, v)
            obj.full_clean()
            obj.save()
        return Response(StudentFixedCommitmentSerializer(items, many=True).data)

    @action(detail=False, methods=("post",), url_path="group-delete")
    @transaction.atomic
    def group_delete(self, request):
        group_id = request.data.get("group_id")
        if not group_id:
            raise serializers.ValidationError({"group_id": "This field is required."})
        items = list(StudentFixedCommitment.objects.filter(group_id=group_id).select_related("student"))
        if not items:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        if not manageable_student(items[0].student, request.user) and request.user.role != User.Role.ADMIN:
            return Response({"detail": "This group is not available to you."}, status=status.HTTP_403_FORBIDDEN)
        StudentFixedCommitment.objects.filter(group_id=group_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StudentSchoolSlotViewSet(viewsets.ModelViewSet):
    permission_classes = (PlanningAccess,)
    serializer_class = StudentSchoolSlotSerializer
    queryset = StudentSchoolSlot.objects.all()

    def get_queryset(self):
        queryset = StudentSchoolSlot.objects.select_related("student__user", "student__counselor__user")
        user = self.request.user
        if user.role == User.Role.ADMIN:
            scoped = queryset
        elif user.role == User.Role.COUNSELOR:
            scoped = queryset.filter(student__counselor__user=user)
        else:
            scoped = queryset.filter(student__user=user)
        student = self.request.query_params.get("student")
        if student and student.isdecimal():
            scoped = scoped.filter(student_id=int(student))
        weekday = self.request.query_params.get("weekday")
        if weekday is not None and weekday.isdigit():
            scoped = scoped.filter(weekday=int(weekday))
        return scoped

    @action(detail=False, methods=("post",), url_path="bulk")
    @transaction.atomic
    def bulk(self, request):
        serializer = BulkSchoolSlotsSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        student = serializer.validated_data["student"]
        slots = serializer.validated_data["slots"]
        from django.utils.dateparse import parse_time
        for entry in slots:
            wd = int(entry["weekday"])
            period = int(entry["period"])
            name = str(entry["subject_name"]).strip()
            st = parse_time(str(entry["start_time"])) if entry.get("start_time") else None
            en = parse_time(str(entry["end_time"])) if entry.get("end_time") else None
            StudentSchoolSlot.objects.update_or_create(
                student=student, weekday=wd, period=period,
                defaults={"subject_name": name, "start_time": st, "end_time": en},
            )
        return Response(StudentSchoolSlotSerializer(self.get_queryset().filter(student=student), many=True).data)

    @action(detail=False, methods=("delete",), url_path="clear")
    @transaction.atomic
    def clear(self, request):
        student_id = request.query_params.get("student") or request.data.get("student")
        if not student_id:
            raise serializers.ValidationError({"student": "This field is required."})
        try:
            from apps.accounts.models import StudentProfile
            student = StudentProfile.objects.get(pk=int(student_id))
        except Exception:
            raise serializers.ValidationError({"student": "Student not found."})
        if not manageable_student(student, request.user) and request.user.role != User.Role.ADMIN:
            return Response({"detail": "This student is not available to you."}, status=status.HTTP_403_FORBIDDEN)
        weekday = request.query_params.get("weekday") or request.data.get("weekday")
        qs = StudentSchoolSlot.objects.filter(student=student)
        if weekday is not None and str(weekday).isdigit():
            qs = qs.filter(weekday=int(weekday))
        qs.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StudentFixedEventViewSet(viewsets.ModelViewSet):
    permission_classes = (PlanningAccess,)
    serializer_class = StudentFixedEventSerializer
    queryset = StudentFixedEvent.objects.all()

    def get_queryset(self):
        queryset = StudentFixedEvent.objects.select_related("student__user", "student__counselor__user")
        user = self.request.user
        if user.role == User.Role.ADMIN:
            scoped = queryset
        elif user.role == User.Role.COUNSELOR:
            scoped = queryset.filter(student__counselor__user=user)
        else:
            scoped = queryset.filter(student__user=user)
        student = self.request.query_params.get("student")
        if student and student.isdecimal():
            scoped = scoped.filter(student_id=int(student))
        return scoped
