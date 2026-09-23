from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from datetime import timedelta
from django.utils import timezone
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import User
from apps.accounts.models import StudentProfile
from apps.planning.models import PlanItem, PlanItemExecution
from apps.planning.execution import transition
from .models import DailyReport, DailyReportItem
from .serializers import CloseDaySerializer, DailyFieldsSerializer, OpenReportSerializer, ReportItemInputSerializer, UnplannedItemInputSerializer
from .services import check_editable, open_report, report_payload
from .progress import student_progress
from .reporting import reporting_data
from .services import report_today


class StudentProgressView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        if request.user.role != User.Role.STUDENT:
            raise PermissionDenied("Only students can view their progress.")
        return Response(student_progress(request.user.student_profile))


class CounselorStudentProgressView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request, student_id):
        if request.user.role != User.Role.COUNSELOR:
            raise PermissionDenied("Only counselors can view assigned student progress.")
        student = get_object_or_404(StudentProfile, pk=student_id, counselor__user=request.user)
        start_raw, end_raw = request.query_params.get("start_date"), request.query_params.get("end_date")
        if bool(start_raw) != bool(end_raw):
            raise serializers.ValidationError({"date": "تاریخ شروع و پایان را با هم وارد کنید."})
        if start_raw:
            start = serializers.DateField().run_validation(start_raw)
            end = serializers.DateField().run_validation(end_raw)
            if start > end or end > report_today() or end - start > timedelta(days=89):
                raise serializers.ValidationError({"date": "بازه باید تا امروز، با ترتیب درست و حداکثر ۹۰ روز باشد."})
            return Response(student_progress(
                student, start_date=start, end_date=end, include_items=True, include_analytics=True,
            ))
        return Response(student_progress(student, include_items=True, include_analytics=True))


class CounselorStudentReportsView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request, student_id):
        if request.user.role != User.Role.COUNSELOR:
            raise PermissionDenied("Only counselors can view assigned student reports.")
        student = get_object_or_404(StudentProfile, pk=student_id, counselor__user=request.user)
        params = request.query_params
        start_raw, end_raw = params.get("start_date"), params.get("end_date")
        if not start_raw or not end_raw:
            raise serializers.ValidationError({"date": "تاریخ شروع و پایان را وارد کنید."})
        start = serializers.DateField().run_validation(start_raw)
        end = serializers.DateField().run_validation(end_raw)
        metric = params.get("metric", "all")
        if metric not in ("all", "study", "tests", "plan"):
            raise serializers.ValidationError({"metric": "نوع گزارش معتبر نیست."})
        if start > end or end > report_today() or end - start > timedelta(days=89):
            raise serializers.ValidationError({"date": "بازه باید تا امروز، با ترتیب درست و حداکثر ۹۰ روز باشد."})
        return Response(reporting_data(student, start, end, metric))


class StudentReportsView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        if request.user.role != User.Role.STUDENT:
            raise PermissionDenied("Only students can view their reports.")
        params = request.query_params
        start_raw, end_raw = params.get("start_date"), params.get("end_date")
        if not start_raw or not end_raw:
            raise serializers.ValidationError({"date": "تاریخ شروع و پایان را وارد کنید."})
        start = serializers.DateField().run_validation(start_raw)
        end = serializers.DateField().run_validation(end_raw)
        metric = params.get("metric", "all")
        if metric not in ("all", "study", "tests", "plan"):
            raise serializers.ValidationError({"metric": "نوع گزارش معتبر نیست."})
        if start > end or end > report_today() or end - start > timedelta(days=89):
            raise serializers.ValidationError({"date": "بازه باید تا امروز، با ترتیب درست و حداکثر ۹۰ روز باشد."})
        return Response(reporting_data(request.user.student_profile, start, end, metric))


class DailyReportViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    permission_classes = (IsAuthenticated,)
    serializer_class = DailyFieldsSerializer
    queryset = DailyReport.objects.all()
    http_method_names = ("get", "post", "patch", "put", "delete", "head", "options")

    def get_queryset(self):
        queryset = DailyReport.objects.select_related("student__user", "student__counselor__user")
        user = self.request.user
        if user.role == User.Role.ADMIN:
            pass
        elif user.role == User.Role.COUNSELOR:
            queryset = queryset.filter(student__counselor__user=user)
        elif user.role == User.Role.STUDENT:
            queryset = queryset.filter(student__user=user)
        else:
            return queryset.none()
        if self.action == "list":
            if "student" in self.request.query_params:
                raw = self.request.query_params["student"]
                if not raw.isdecimal() or int(raw) < 1:
                    raise serializers.ValidationError({"student": "Must be a positive integer."})
                queryset = queryset.filter(student_id=int(raw))
            if "date" in self.request.query_params:
                date = serializers.DateField().run_validation(self.request.query_params["date"])
                queryset = queryset.filter(date=date)
        return queryset

    def list(self, request, *args, **kwargs):
        reports = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(reports)
        rows = page if page is not None else reports
        payload = [{"id": report.pk, "student": report.student_id, "date": report.date.isoformat(), "self_rating": report.self_rating} for report in rows]
        return self.get_paginated_response(payload) if page is not None else Response(payload)

    def retrieve(self, request, *args, **kwargs):
        return Response(report_payload(self.get_object()))

    def update(self, request, *args, **kwargs):
        return self.partial_update(request, *args, **kwargs)

    def student_write(self, report):
        if self.request.user.role != User.Role.STUDENT:
            raise PermissionDenied("Only students can edit their daily reports.")
        if report.student.user_id != self.request.user.pk:
            raise PermissionDenied("This report is not yours.")
        check_editable(report.date)

    def _close_review(self, report):
        items = list(PlanItem.objects.filter(
            plan_day__plan__student=report.student,
            plan_day__plan__status="PUBLISHED",
            plan_day__date=report.date,
        ).select_related("subject", "chapter", "topic").order_by("ordering", "pk"))
        executions = {row.plan_item_id: row for row in PlanItemExecution.objects.filter(
            student=report.student, plan_item_id__in=[item.pk for item in items],
        )}
        unresolved = []
        active_timer = PlanItemExecution.objects.filter(
            student=report.student, plan_item_id__in=[item.pk for item in items],
            status=PlanItemExecution.Status.IN_PROGRESS,
        ).values_list("plan_item_id", flat=True).first()
        for item in items:
            # Events use their existing event semantics and never require an
            # academic performance answer during end-of-day review.
            if item.kind == PlanItem.Kind.EVENT:
                continue
            execution = executions.get(item.pk)
            state = execution.status if execution else "NOT_STARTED"
            if state not in (PlanItemExecution.Status.COMPLETED, PlanItemExecution.Status.PARTIAL, PlanItemExecution.Status.NOT_DONE):
                unresolved.append({
                    "id": item.pk, "kind": item.kind,
                    "title": item.title or (item.topic.name if item.topic_id else item.subject.name if item.subject_id else item.get_kind_display()),
                    "subject_name": item.subject.name if item.subject_id else "",
                    "chapter_name": item.chapter.name if item.chapter_id else "",
                    "topic_name": item.topic.name if item.topic_id else "",
                    "planned_duration_minutes": item.planned_duration_minutes,
                    "planned_test_count": item.test_count,
                    "status": state,
                })
        return {"date": report.date.isoformat(), "unresolved": unresolved, "active_timer": active_timer}

    def _enforce_target_date(self, report, raw_date):
        if raw_date in (None, ""):
            raise serializers.ValidationError({"date": "تاریخ روز را مشخص کنید."})
        target_date = serializers.DateField().run_validation(raw_date)
        if target_date != report.date:
            raise serializers.ValidationError({"date": "تاریخ درخواست با گزارش روز یکسان نیست."})
        return target_date

    @action(detail=True, methods=("get",), url_path="close-review")
    def close_review(self, request, pk=None):
        report = self.get_object()
        self.student_write(report)
        self._enforce_target_date(report, request.query_params.get("date"))
        return Response(self._close_review(report))

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def close(self, request, pk=None):
        report = self.get_object()
        self.student_write(report)
        payload = CloseDaySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        target_date = self._enforce_target_date(report, payload.validated_data["date"])
        if target_date != report_today():
            raise serializers.ValidationError({"date": "فقط برنامه امروز را می‌توان پایان داد؛ روز آینده قابل پایان نیست."})
        StudentProfile.objects.select_for_update().get(pk=report.student_id)
        report = DailyReport.objects.select_for_update().get(pk=report.pk)
        if report.closed_at is not None:
            return Response(report_payload(report))
        review = self._close_review(report)
        if review["active_timer"]:
            raise serializers.ValidationError({"detail": "یک فعالیت هنوز در حال اجراست. ابتدا آن را پایان دهید."})
        if review["unresolved"]:
            raise serializers.ValidationError({"unresolved": review["unresolved"]})
        report.self_rating = payload.validated_data["self_rating"]
        report.note = payload.validated_data["note"]
        if report.closed_at is None:
            report.closed_at = timezone.now()
        report.full_clean()
        report.save(update_fields=("self_rating", "note", "closed_at", "updated_at"))
        return Response(report_payload(report))

    @action(detail=False, methods=("post",))
    def open(self, request):
        if request.user.role != User.Role.STUDENT:
            raise PermissionDenied("Only students can open their daily report.")
        payload = OpenReportSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        report = open_report(request.user, payload.validated_data["date"])
        return Response(report_payload(report))

    def partial_update(self, request, *args, **kwargs):
        report = self.get_object()
        self.student_write(report)
        payload = DailyFieldsSerializer(report, data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        try:
            payload.save()
            report.full_clean()
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.message_dict if hasattr(error, "message_dict") else error.messages) from error
        return Response(report_payload(report))

    def _save_item(self, item, data):
        previous_kind = item.kind
        for field, value in data.items():
            setattr(item, field, value)
        if item.pk and "kind" in data and previous_kind == PlanItem.Kind.TEST and item.kind != PlanItem.Kind.TEST:
            item.actual_test_count = None
            item.correct_count = None
            item.wrong_count = None
            item.unanswered_count = None
        if "actual_duration_minutes" in data and "duration_source" not in data:
            item.duration_source = DailyReportItem.DurationSource.MANUAL if data["actual_duration_minutes"] is not None else ""
        if data.get("duration_source") == DailyReportItem.DurationSource.PLAN and "actual_duration_minutes" not in data:
            item.actual_duration_minutes = item.plan_item.planned_duration_minutes
        if not item.plan_item_id and item.start_time and item.end_time and data.get("actual_duration_minutes") is None:
            start = item.start_time.hour * 60 + item.start_time.minute
            end = item.end_time.hour * 60 + item.end_time.minute
            if end > start:
                item.actual_duration_minutes = end - start
                item.duration_source = DailyReportItem.DurationSource.MANUAL
        if item.activity_kind == PlanItem.Kind.TEST and (
            "actual_test_count" in data or "wrong_count" in data
        ):
            actual, wrong = item.actual_test_count, item.wrong_count
            if actual is not None and wrong is not None:
                if wrong > actual:
                    raise serializers.ValidationError({"wrong_count": "تعداد غلط نمی‌تواند بیشتر از تعداد تست‌ها باشد."})
                # Daily tests only need actual + wrong. Preserve a historical
                # explicit unanswered breakdown, otherwise derive correct.
                if item.unanswered_count is None and "unanswered_count" not in data and "correct_count" not in data:
                    item.correct_count = actual - wrong
        try:
            item.save()
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.message_dict if hasattr(error, "message_dict") else error.messages) from error
        return item

    @action(detail=True, methods=("put",), url_path=r"planned/(?P<item_id>[^/.]+)")
    @transaction.atomic
    def planned(self, request, pk=None, item_id=None):
        report = self.get_object()
        self.student_write(report)
        plan_item = PlanItem.objects.filter(
            pk=item_id, plan_day__date=report.date, plan_day__plan__student=report.student,
            plan_day__plan__status="PUBLISHED",
        ).first()
        if not plan_item:
            raise serializers.ValidationError({"plan_item": "This item is not in this published daily plan."})
        payload = ReportItemInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        row, _ = DailyReportItem.objects.select_for_update().get_or_create(report=report, plan_item=plan_item)
        completion_status = payload.validated_data.pop("completion_status", None)
        self._save_item(row, payload.validated_data)
        # Positive manual performance completes a never-started block once.
        # A running/paused timer keeps its own execution state and duration.
        if completion_status:
            transition(request.user, plan_item.pk, "quick_complete" if completion_status == "COMPLETED" else "set_partial")
        elif plan_item.kind != PlanItem.Kind.EVENT and (
            not hasattr(plan_item, "execution") or plan_item.execution.status == PlanItemExecution.Status.NOT_DONE
        ) and any(
            (payload.validated_data.get(field) or 0) > 0 for field in ("actual_duration_minutes", "actual_test_count")
        ):
            transition(request.user, plan_item.pk, "quick_complete")
        return Response(report_payload(report))

    @action(detail=True, methods=("post",), url_path="unplanned")
    @transaction.atomic
    def unplanned(self, request, pk=None):
        report = self.get_object()
        self.student_write(report)
        DailyReport.objects.select_for_update().get(pk=report.pk)
        payload = UnplannedItemInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        row = DailyReportItem(report=report, ordering=report.items.count())
        self._save_item(row, payload.validated_data)
        return Response(report_payload(report), status=status.HTTP_201_CREATED)

    @action(detail=True, methods=("patch", "delete"), url_path=r"unplanned/(?P<item_id>[^/.]+)")
    @transaction.atomic
    def unplanned_detail(self, request, pk=None, item_id=None):
        report = self.get_object()
        self.student_write(report)
        DailyReport.objects.select_for_update().get(pk=report.pk)
        row = report.items.select_for_update().filter(pk=item_id, plan_item__isnull=True).first()
        if not row:
            raise serializers.ValidationError({"item": "This outside-plan activity is not in the report."})
        if request.method == "DELETE":
            row.delete()
            return Response(report_payload(report))
        payload = UnplannedItemInputSerializer(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        self._save_item(row, payload.validated_data)
        return Response(report_payload(report))
