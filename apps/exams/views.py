import hashlib
import json
import uuid
from datetime import timedelta

from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import StudentProfile, User
from apps.accounts.permissions import IsAdmin, IsCounselor, IsStudent
from .exceptions import Quiz24Error, quiz24_error_data
from .configuration import Quiz24Config
from .models import ExamProviderOperation, ExamManagementEvent, ExamParticipation, Exam, ExamAnswer, ExamAttempt, ExamCreation, ExamAssignment
from .serializers import (
    ManagementInputSerializer, BulkManagementSerializer, ProviderManagementSerializer, ExamScheduleSerializer, AssignmentInputSerializer, CourseDetailSerializer, CreateExamSerializer, CreatedQuiz24ExamSerializer,
    AutomaticExamSerializer, AutomaticCreatedExamSerializer, QuestionCountSerializer,
    ExamSerializer, ExamAttemptSerializer, StudentExamSerializer, SyncedQuiz24ExamSerializer,
)
from .services.management import event, activity, blocked, require_mutable, local_action, capabilities, provider_action
from .scheduling import availability, results_released
from .services.quiz24 import Quiz24Service, student_quiz24_mobile
from .services.results import parse_answers, parse_result, safe_raw_result
from .services.analytics import analytics_data, sync_analytics, verification_defaults, result_identity
from .services.monitoring import monitoring_data, monitoring_row, sync_monitoring
from .services.automatic import automatic_error_message, normalize_inventory


class Quiz24ProviderView(APIView):
    permission_classes = (IsAdmin | IsCounselor,)

    def provider_response(self, method, **kwargs):
        try:
            result = getattr(Quiz24Service(), method)(**kwargs)
        except Quiz24Error as error:
            return Response(
                quiz24_error_data(error),
                status=error.api_status,
            )
        return Response(result)


class StudentBuilderProviderView(Quiz24ProviderView):
    permission_classes = (IsAdmin | IsCounselor | IsStudent,)


class ProviderStatusView(Quiz24ProviderView):
    def get(self, request):
        return Response(Quiz24Config().public_status())


class EducationPeriodsView(Quiz24ProviderView):
    def get(self, request):
        return self.provider_response("education_periods")


def scoped_provider_response(view, method):
    from apps.academics.provider_catalog import catalog, rows, field, allowed_grades, authorize_provider, valid_provider_id
    try:
        if view.request.user.role == User.Role.STUDENT:
            allowed = allowed_grades(view.request.user)
        response = catalog(method)
        if view.request.user.role == User.Role.STUDENT:
            selected = []
            for row in rows(response):
                if not valid_provider_id(field(row, ('gradeid' if method == 'grades' else 'courseid'), 'id', 'key')):
                    continue
                if method == 'grades':
                    if str(field(row, 'gradeid', 'id', 'key')) in allowed:
                        selected.append(row)
                else:
                    if str(field(row, 'gradeid')) not in allowed:
                        continue
                    from rest_framework.exceptions import PermissionDenied
                    try:
                        authorize_provider(view.request.user, grade_id=field(row, 'gradeid'), course_id=field(row, 'courseid', 'id', 'key'))
                    except PermissionDenied:
                        continue
                    selected.append(row)
            envelope_key = next((key for key in response if key.replace('_', '').lower() in ('result', 'data')), 'result')
            response = {**response, envelope_key: selected}
        return Response(response)
    except Quiz24Error as error:
        return Response(quiz24_error_data(error), status=error.api_status)


class GradesView(StudentBuilderProviderView):
    def get(self, request):
        return scoped_provider_response(self, "grades")


class CoursesView(StudentBuilderProviderView):
    def get(self, request):
        return scoped_provider_response(self, "courses")


class CourseDetailView(StudentBuilderProviderView):
    def post(self, request):
        serializer = CourseDetailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from apps.academics.provider_catalog import authorize_provider, catalog
        try:
            authorize_provider(request.user, serializer.validated_data['grade_id'], serializer.validated_data['course_id'])
            result = catalog('course_detail', **serializer.validated_data)
            if request.user.role == User.Role.STUDENT:
                from .services.catalog import taxonomy_index
                taxonomy_index(result, serializer.validated_data['course_id'], serializer.validated_data['grade_id'])
            return Response(result)
        except Quiz24Error as error:
            return Response(quiz24_error_data(error), status=error.api_status)


class QuestionCountView(StudentBuilderProviderView):
    def post(self, request):
        serializer = QuestionCountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            from .services.catalog import validate_student_selection
            validate_student_selection(request.user, serializer.validated_data)
            counts = normalize_inventory(Quiz24Service().question_count(**serializer.validated_data))
        except Quiz24Error as error:
            return Response(
                quiz24_error_data(Quiz24Error(error.status, automatic_error_message(error))),
                status=error.api_status,
            )
        return Response(counts)


class ExamViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = (IsAdmin | IsCounselor,)
    serializer_class = ExamSerializer
    queryset = Exam.objects.select_related("student__user", "counselor__user")

    def get_permissions(self):
        if self.action in ("schedule", "monitoring", "monitoring_sync", "analytics", "analytics_sync", "management", "management_bulk", "history", "edit_questions", "remote_delete"):
            return [IsCounselor()]
        if self.action in ("list", "retrieve", "result", "automatic"):
            return [(IsAdmin | IsCounselor | IsStudent)()]
        if self.action == "start":
            return [IsStudent()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.request.user.role == User.Role.STUDENT:
            return StudentExamSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        user = self.request.user
        attempts = ExamAttempt.objects.filter(exam_id=OuterRef("pk"), status=ExamAttempt.Status.COMPLETED)
        attempts = attempts.filter(student__user=user) if user.role == User.Role.STUDENT else attempts.filter(student_id=OuterRef("student_id"))
        queryset = super().get_queryset().annotate(management_blocked=Exists(ExamProviderOperation.objects.filter(exam_id=OuterRef("pk"), state__in=("PENDING", "EDITING", "UNKNOWN", "DELETED"))), participation_known=Exists(ExamParticipation.objects.filter(assignment__exam_id=OuterRef("pk"), status__in=("IN_PROGRESS", "COMPLETED"))), schedule_locked=Exists(ExamAttempt.objects.filter(exam_id=OuterRef("pk"))), result_available=Exists(attempts), assignment_count=Count("assignments", filter=Q(assignments__student__counselor__user=user) if user.role == User.Role.COUNSELOR else Q(), distinct=True))
        if user.role == User.Role.ADMIN:
            return queryset
        if user.role == User.Role.STUDENT:
            return queryset.annotate(own_attempt=Exists(ExamAttempt.objects.filter(exam_id=OuterRef("pk"), student__user=user))).filter(
                Q(student__user=user) | Q(assignments__student__user=user, is_personal_practice=False),
                Q(status__in=(Exam.Status.PUBLISHED, Exam.Status.CLOSED)) | Q(own_attempt=True),
            ).distinct().order_by("-created_at", "-pk")
        return queryset.filter(counselor__user=user, is_personal_practice=False)

    @action(detail=True, methods=("get", "post", "delete"))
    @transaction.atomic
    def assignments(self, request, pk=None):
        # No new admin/supervisor management privileges in Sprint 1.
        if request.user.role != User.Role.COUNSELOR:
            return Response({"detail": "فقط مشاور مالک آزمون مجاز است."}, status=403)
        exam = self.get_object()
        exam = Exam.objects.select_for_update().get(pk=exam.pk)
        if request.method == "GET":
            rows = exam.assignments.filter(student__counselor=exam.counselor).select_related("student__user")
            page = self.paginate_queryset(rows)
            data = [{"student": row.student_id, "student_name": row.student.user.get_full_name() or row.student.user.username, "assigned_at": row.assigned_at} for row in (page if page is not None else rows)]
            return self.get_paginated_response(data) if page is not None else Response(data)
        require_mutable(exam)
        serializer = AssignmentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = set(serializer.validated_data["student_ids"])
        students = list(StudentProfile.objects.select_for_update().filter(pk__in=ids, counselor=exam.counselor))
        if len(students) != len(ids):
            return Response({"student_ids": "شناسه نامعتبر یا دانش‌آموز خارج از مسئولیت شماست."}, status=400)
        if request.method == "DELETE":
            if exam.attempts.filter(student_id__in=ids).exists() or exam.assignments.filter(student_id__in=ids, participation__status__in=("IN_PROGRESS", "COMPLETED")).exists():
                return Response({"detail": "حذف تخصیص پس از شروع آزمون مجاز نیست."}, status=409)
            exam.assignments.filter(student_id__in=ids).delete()
            # The legacy field must not keep a removed assignment eligible.
            if exam.student_id in ids:
                exam.student = None
                exam.save(update_fields=("student", "updated_at"))
        else:
            ExamAssignment.objects.bulk_create([ExamAssignment(exam=exam, student=student, assigned_by=request.user) for student in students], ignore_conflicts=True)
        return Response({"assignment_count": exam.assignments.filter(student__counselor=exam.counselor).count()})

    @action(detail=True, methods=("get",))
    def monitoring(self, request, pk=None):
        exam = self.get_object()
        if request.query_params.get("status", "") not in ("", "NOT_STARTED", "IN_PROGRESS", "COMPLETED", "UNKNOWN"):
            return Response({"detail": "فیلتر وضعیت معتبر نیست."}, status=400)
        rows, metadata = monitoring_data(exam, request.query_params)
        page = self.paginate_queryset(rows)
        data = [monitoring_row(row, exam) for row in (page if page is not None else rows)]
        if page is not None:
            response = self.get_paginated_response(data)
            response.data.update(metadata)
            return response
        return Response({"results": data, **metadata})

    @action(detail=True, methods=("post",), url_path="monitoring/sync")
    def monitoring_sync(self, request, pk=None):
        exam = self.get_object()
        error, response_status = sync_monitoring(exam.pk, request.user)
        if error:
            return Response(error, status=response_status)
        exam = self.get_object()
        _, metadata = monitoring_data(exam, {})
        if response_status != 200:
            metadata.update(code="SYNC_FAILED", detail=metadata["error"])
        return Response(metadata, status=response_status)

    @action(detail=True, methods=("get",))
    def analytics(self, request, pk=None):
        exam = self.get_object()
        if request.query_params.get("status", "") not in ("", "SCORED", "IDENTITY_MISSING", "NEEDS_VERIFICATION", "AWAITING_RESULT", "NOT_COMPLETED") or request.query_params.get("ordering", "rank") not in ("rank", "name", "score_asc"):
            return Response({"detail": "فیلتر یا ترتیب نتایج معتبر نیست."}, status=400)
        rows, metadata = analytics_data(exam, request.query_params)
        page = self.paginate_queryset(rows)
        if page is not None:
            response = self.get_paginated_response(page)
            response.data.update(metadata)
            return response
        return Response({"results": rows, **metadata})

    @action(detail=True, methods=("post",), url_path="analytics/sync")
    def analytics_sync(self, request, pk=None):
        exam = self.get_object()
        error, response_status = sync_analytics(exam.pk, request.user)
        if error:
            return Response(error, status=response_status)
        exam = self.get_object()
        _, metadata = analytics_data(exam, {})
        if response_status != 200:
            metadata.update(code="SYNC_FAILED", detail=metadata["error"])
        return Response(metadata, status=response_status)

    @action(detail=True, methods=("get", "post"))
    @transaction.atomic
    def management(self, request, pk=None):
        exam = self.get_object()
        exam = Exam.objects.select_for_update().get(pk=exam.pk)
        if request.method == "POST":
            serializer = ManagementInputSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data
            local_action(exam, request.user, data["action"], data["expected_updated_at"], data.get("title"))
        return Response({"exam": self.get_serializer(self.get_queryset().get(pk=exam.pk)).data, **capabilities(exam, request.user)})

    @action(detail=False, methods=("post",), url_path="management/bulk")
    @transaction.atomic
    def management_bulk(self, request):
        serializer = BulkManagementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        versions = {item["id"]: item["expected_updated_at"] for item in data["exams"]}
        exams = list(Exam.objects.select_for_update().filter(pk__in=versions, counselor__user=request.user, is_personal_practice=False).order_by("pk"))
        if len(exams) != len(versions):
            return Response({"detail": "انتخاب شامل آزمون نامعتبر یا خارج از مسئولیت شماست؛ هیچ تغییری ثبت نشد."}, status=400)
        for exam in exams:
            local_action(exam, request.user, data["action"], versions[exam.pk])
        return Response({"results": self.get_serializer(self.get_queryset().filter(pk__in=versions).order_by("pk"), many=True).data})

    @action(detail=True, methods=("get",))
    def history(self, request, pk=None):
        exam = self.get_object()
        rows = exam.management_events.select_related("actor")
        page = self.paginate_queryset(rows)
        data = [{"id": row.pk, "actor": row.actor.get_full_name() or row.actor.username if row.actor else None, "action": row.action, "previous": row.previous, "current": row.current, "outcome": row.outcome, "created_at": row.created_at} for row in (page if page is not None else rows)]
        return self.get_paginated_response(data) if page is not None else Response(data)

    @action(detail=True, methods=("post",), url_path="edit-questions")
    def edit_questions(self, request, pk=None):
        exam = self.get_object()
        serializer = ProviderManagementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        response, code = provider_action(exam.pk, request.user, "EDIT", data["request_key"], data["expected_updated_at"])
        return Response(response, status=code)

    @action(detail=True, methods=("post",), url_path="remote-delete")
    def remote_delete(self, request, pk=None):
        exam = self.get_object()
        serializer = ProviderManagementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        response, code = provider_action(exam.pk, request.user, "DELETE", data["request_key"], data["expected_updated_at"], data.get("provider_exam_id"), data.get("confirmed_title"), data["confirm_remote_delete"])
        return Response(response, status=code)

    @action(detail=True, methods=("get", "patch"))
    @transaction.atomic
    def schedule(self, request, pk=None):
        exam = self.get_object()
        exam = Exam.objects.select_for_update().get(pk=exam.pk)
        if request.method == "PATCH":
            require_mutable(exam)
            serializer = ExamScheduleSerializer(exam, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            changes = {key: value for key, value in serializer.validated_data.items() if value != getattr(exam, key)}
            if changes and exam.attempts.exists():
                return Response({"code": "SCHEDULE_LOCKED", "detail": "پس از شروع مشارکت دانش‌آموزان، تغییر زمان‌بندی مجاز نیست."}, status=409)
            if changes:
                before = {key: getattr(exam, key).isoformat() if getattr(exam, key) else None for key in changes}
                for key, value in changes.items():
                    setattr(exam, key, value)
                exam.save(update_fields=(*changes, "updated_at"))
                event(exam, request.user, "SCHEDULE", before, {key: value.isoformat() if value else None for key, value in changes.items()})
        return Response(self.get_serializer(exam).data)

    @action(detail=False, methods=("post",))
    def automatic(self, request):
        serializer = AutomaticExamSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        filters = {key: data[key] for key in (
            "course_id", "question_type", "chapter_ids", "subject_ids", "object_ids",
            "from_year", "to_year", "consider_used_question",
        ) if key in data}
        filters["grade_id"] = data["grade_external_id"]
        # Persist intent before contacting Quiz24. A retry can finish local persistence,
        # but must never repeat a successful or uncertain provider creation.
        canonical = {key: (value.pk if hasattr(value, "pk") else value)
                     for key, value in data.items() if key != "creation_key"}
        digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest()
        key = hashlib.sha256(f"{request.user.pk}:{data.get('creation_key', digest)}".encode()).hexdigest()
        receipt = ExamCreation.objects.filter(key=key).first()
        if receipt and receipt.payload_digest != digest:
            return Response({"detail": "شناسه درخواست قبلاً برای تنظیمات دیگری استفاده شده است."}, status=409)
        if receipt and receipt.exam_id:
            from apps.academics.provider_catalog import authorize_provider
            try:
                authorize_provider(request.user, filters['grade_id'], filters['course_id'])
            except Quiz24Error as error:
                return Response(quiz24_error_data(error), status=error.api_status)
            return Response(self.get_serializer(receipt.exam).data, status=200)
        if receipt and receipt.state == "FAILED":
            return Response(receipt.response, status=receipt.error_status)
        service = Quiz24Service()
        report_scope = {}
        def validate_report_context():
            if 'report_context' not in data:
                return {}
            if request.user.role != User.Role.STUDENT:
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied('این مسیر فقط برای دانش‌آموز است.')
            from .services.report_topics import confirmed_report_scope
            scope = confirmed_report_scope(request.user, data['report_context'])
            if any(filters.get(k) != v for k, v in scope['provider_filters'].items()):
                from rest_framework.exceptions import ValidationError
                raise ValidationError({'report_context': 'فیلتر آزمون با مباحث تأییدشده گزارش سازگار نیست.'})
            # Preserve the existing verified Quiz24 identity requirements before mutation.
            from .services.quiz24 import student_quiz24_mobile
            student_quiz24_mobile(data['student'])
            return scope
        if receipt is None or receipt.state == "PENDING":
            try:
                from .services.catalog import validate_student_selection
                report_scope = validate_report_context()
                validate_student_selection(request.user, filters)
                inventory = normalize_inventory(service.question_count(**filters))
            except Quiz24Error as error:
                return Response(quiz24_error_data(Quiz24Error(error.status, automatic_error_message(error))), status=error.api_status)
            errors = {}
            for difficulty in ("easy", "medium", "hard", "very_hard"):
                field = difficulty + "_count"
                if data[field] > inventory[difficulty]:
                    errors[field] = f"تعداد درخواستی بیشتر از موجودی ({inventory[difficulty]}) است."
            if errors:
                return Response(errors, status=400)
            # Recheck the authenticated profile after the availability request,
            # immediately before claiming permission to send createOnlineExam.
            try:
                report_scope = validate_report_context()
                validate_student_selection(request.user, filters)
            except Quiz24Error as error:
                return Response(quiz24_error_data(error), status=error.api_status)
            receipt, _ = ExamCreation.objects.get_or_create(key=key, defaults={"payload_digest": digest, "confirmed_scope": report_scope})
            if receipt.payload_digest != digest:
                return Response({"detail": "شناسه درخواست قبلاً استفاده شده است."}, status=409)
            if ExamCreation.objects.filter(pk=receipt.pk, state="PENDING").update(state="SENDING"):
                try:
                    response = service.create_online_exam(
                        **filters, title=data["title"], duration_minutes=data["duration_minutes"],
                        has_negative_score=data["has_negative_score"],
                        **{field: data[field] for field in ("easy_count", "medium_count", "hard_count", "very_hard_count")},
                    )
                except Quiz24Error as error:
                    failure = quiz24_error_data(Quiz24Error(error.status, automatic_error_message(error)))
                    # Without a provider rejection code, the side effect is uncertain.
                    state = "FAILED" if error.status is not None and str(error.status).strip() != "100" else "UNKNOWN"
                    ExamCreation.objects.filter(pk=receipt.pk).update(state=state, response=failure, error_status=error.api_status)
                    return Response(failure, status=error.api_status)
                ExamCreation.objects.filter(pk=receipt.pk).update(state="RECEIVED", response=response)
            receipt.refresh_from_db()
        if receipt.exam_id:
            return Response(self.get_serializer(receipt.exam).data, status=200)
        if receipt.state == "FAILED":
            return Response(receipt.response, status=receipt.error_status)
        if receipt.state != "RECEIVED":
            return Response({"detail": "درخواست ساخت در حال بررسی است؛ برای جلوگیری از آزمون تکراری دوباره ارسال نشد."}, status=409)
        provider = AutomaticCreatedExamSerializer.from_provider(receipt.response)
        if not provider.is_valid():
            return Response({"detail": "Quiz24 ساخت را تأیید کرد، اما کد آزمون قابل پردازش نیست؛ ساخت مجدد انجام نشد."}, status=502)
        try:
            with transaction.atomic():
                receipt = ExamCreation.objects.select_for_update().get(pk=receipt.pk)
                if receipt.exam_id:
                    return Response(self.get_serializer(receipt.exam).data, status=200)
                exam = Exam.objects.create(
                    creation_mode="AUTOMATIC", ever_published=request.user.role == User.Role.STUDENT, student=data["student"], counselor=data["counselor"], title=data["title"],
                    grade_external_id=data["grade_external_id"], question_type=data["question_type"],
                    duration_minutes=data["duration_minutes"], has_negative_score=data["has_negative_score"],
                    provider_exam_code=provider.validated_data["examCode"],
                    provider_exam_id=provider.validated_data.get("examId"),
                    status=Exam.Status.PUBLISHED if request.user.role == User.Role.STUDENT else Exam.Status.READY,
                    is_personal_practice=request.user.role == User.Role.STUDENT,
                    question_count=sum(data[field] for field in ("easy_count", "medium_count", "hard_count", "very_hard_count")),
                )
                if exam.student_id:
                    ExamAssignment.objects.get_or_create(exam=exam, student=exam.student, defaults={"assigned_by": request.user})
                receipt.exam = exam
                receipt.state = "COMPLETE"
                receipt.save(update_fields=("exam", "state"))
        except DatabaseError:
            return Response({"detail": "آزمون در Quiz24 ساخته شد، اما ثبت محلی کامل نشد؛ تلاش دوباره فقط ثبت محلی را تکمیل می‌کند."}, status=503)
        return Response(self.get_serializer(exam).data, status=201)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def publish(self, request, pk=None):
        exam = self.get_object()
        exam = Exam.objects.select_for_update().get(pk=exam.pk)
        require_mutable(exam)
        if "expected_updated_at" in request.data:
            version = ManagementInputSerializer(data={"action": "unpublish", "expected_updated_at": request.data["expected_updated_at"]})
            version.is_valid(raise_exception=True)
            if version.validated_data["expected_updated_at"] != exam.updated_at:
                return Response({"detail": "نسخه آزمون تغییر کرده؛ اطلاعات را بازخوانی کنید."}, status=409)
        if exam.status == Exam.Status.PUBLISHED:
            return Response(self.get_serializer(exam).data)
        if exam.status != Exam.Status.READY or not exam.provider_exam_code or not exam.question_count or exam.question_count <= 0:
            return Response(
                {"detail": "فقط آزمون آماده دارای کد Quiz24 و حداقل یک سوال قابل انتشار است."},
                status=status.HTTP_409_CONFLICT,
            )
        previous = {"status": exam.status}
        exam.status = Exam.Status.PUBLISHED
        exam.ever_published = True
        exam.save(update_fields=("status", "ever_published", "updated_at"))
        event(exam, request.user, "PUBLISH", previous, {"status": exam.status})
        return Response(self.get_serializer(exam).data)

    def start_restriction(self, exam):
        if exam.archived_at or exam.trashed_at or blocked(exam):
            return Response({"code": "MANAGEMENT_RESTRICTED", "detail": "آزمون بایگانی، حذف محلی یا برای مدیریت قفل شده است."}, status=409)
        if exam.status != Exam.Status.PUBLISHED:
            return Response({"code": "EXAM_NOT_AVAILABLE", "detail": "این آزمون در حال حاضر قابل شروع نیست."}, status=409)
        window = availability(exam)
        if window["code"] in ("UPCOMING", "DEADLINE_PASSED"):
            code = "EXAM_NOT_STARTED" if window["code"] == "UPCOMING" else "PARTICIPATION_DEADLINE_PASSED"
            return Response({"code": code, "detail": window["label"], "availability": window}, status=409)
        if not exam.provider_exam_code:
            return Response({"detail": "کد آزمون Quiz24 ثبت نشده است."}, status=409)
        return None

    @action(detail=True, methods=("post",))
    def start(self, request, pk=None):
        exam = self.get_object()
        token = uuid.uuid4()
        with transaction.atomic():
            exam = Exam.objects.select_for_update().get(pk=exam.pk)
            if not self.get_queryset().filter(pk=exam.pk).exists():
                return Response(status=404)
            restriction = self.start_restriction(exam)
            if restriction is not None:
                return restriction
            student = StudentProfile.objects.select_for_update().get(user=request.user)
            try:
                username = student_quiz24_mobile(student)
            except Quiz24Error as error:
                return Response(quiz24_error_data(error), status=error.api_status)
            attempt, _ = ExamAttempt.objects.get_or_create(exam=exam, student=student, defaults={"provider_username": username})
            if attempt.status == ExamAttempt.Status.COMPLETED:
                return Response({"code": "ALREADY_COMPLETED", "detail": "آزمون تکمیل شده است؛ شرکت مجدد بدون تأیید قابلیت سرویس پشتیبانی نمی‌شود."}, status=409)
            if attempt.participation_lease_until and attempt.participation_lease_until > timezone.now():
                return Response({"code": "START_IN_PROGRESS", "detail": "درخواست ورود در حال انجام است؛ کمی صبر کنید."}, status=409)
            attempt.participation_token = token
            attempt.participation_lease_until = timezone.now() + timedelta(seconds=60)
            attempt.save(update_fields=("participation_token", "participation_lease_until"))
            code, digest = exam.provider_exam_code, result_identity(student)
        # The durable attempt blocks schedule/assignment removal. Only the claim
        # is held across HTTP, never a row lock or database transaction.
        try:
            url = Quiz24Service().get_participation_url(user_name=username, exam_code=code)
        except Quiz24Error as error:
            ExamAttempt.objects.filter(pk=attempt.pk, participation_token=token).update(participation_token=None, participation_lease_until=None)
            return Response(quiz24_error_data(error), status=error.api_status)
        with transaction.atomic():
            current = Exam.objects.select_for_update().get(pk=exam.pk)
            attempt = ExamAttempt.objects.select_for_update().get(pk=attempt.pk)
            if attempt.participation_token != token:
                return Response({"code": "START_SUPERSEDED", "detail": "پاسخ ورود قدیمی کنار گذاشته شد."}, status=409)
            attempt.participation_token = None
            attempt.participation_lease_until = None
            attempt.save(update_fields=("participation_token", "participation_lease_until"))
            if attempt.status == ExamAttempt.Status.COMPLETED:
                return Response({"code": "ALREADY_COMPLETED", "detail": "نتیجه آزمون ثبت شده است؛ لینک شرکت مجدد صادر نشد."}, status=409)
            if not self.get_queryset().filter(pk=current.pk).exists():
                return Response(status=404)
            restriction = self.start_restriction(current)
            if restriction is not None:
                return restriction
            student = StudentProfile.objects.select_for_update().get(pk=student.pk)
            try:
                current_username = student_quiz24_mobile(student)
            except Quiz24Error as error:
                return Response(quiz24_error_data(error), status=error.api_status)
            if current.provider_exam_code != code or current_username != username or result_identity(student) != digest:
                return Response({"code": "START_IDENTITY_CHANGED", "detail": "اطلاعات آزمون یا اتصال حساب تغییر کرده است؛ دوباره بررسی کنید."}, status=409)
            if attempt.started_at is None:
                attempt.started_at = timezone.now()
            attempt.provider_username = username
            attempt.save(update_fields=("started_at", "provider_username", "updated_at"))
        return Response({"participation_url": url})

    @action(detail=True, methods=("get", "post"))
    def result(self, request, pk=None):
        exam = self.get_object()
        if request.user.role == User.Role.STUDENT and not results_released(exam):
            return Response({"code": "RESULTS_NOT_RELEASED", "detail": "زمان انتشار نتیجه آزمون هنوز نرسیده است.", "result_release_at": exam.result_release_at}, status=403)
        student = StudentProfile.objects.get(user=request.user) if request.user.role == User.Role.STUDENT else exam.student
        selected_student = request.query_params.get("student")
        if selected_student is not None:
            if request.user.role == User.Role.COUNSELOR:
                if request.method != "GET":
                    return Response({"detail": "برای دریافت نتایج گروه از به‌روزرسانی نتایج آزمون استفاده کنید."}, status=405)
                if not selected_student.isdecimal() or len(selected_student) > 19 or not 0 < int(selected_student) <= 9223372036854775807:
                    return Response({"detail": "شناسه دانش‌آموز معتبر نیست."}, status=400)
                student = StudentProfile.objects.filter(pk=selected_student, counselor_id=exam.counselor_id, exam_assignments__exam=exam).select_related("user").first()
                if student is None:
                    return Response(status=404)
            elif request.user.role != User.Role.STUDENT or not student or selected_student != str(student.pk):
                return Response(status=404)
        if request.user.role == User.Role.COUNSELOR and student and student.counselor_id != exam.counselor_id:
            return Response({"detail": "دانش‌آموز دیگر تحت مسئولیت شما نیست."}, status=404)
        if student is None:
            return Response({"detail": "این آزمون به دانش‌آموزی اختصاص داده نشده است."}, status=status.HTTP_409_CONFLICT)
        if request.method == "GET":
            attempt = ExamAttempt.objects.filter(exam=exam, student_id=student.pk).prefetch_related("answers").first()
            if not attempt:
                return Response({"detail": "نتیجه این آزمون هنوز دریافت نشده است."}, status=status.HTTP_404_NOT_FOUND)
            return Response(ExamAttemptSerializer(attempt).data)
        if not exam.provider_exam_code:
            return Response({"detail": "کد آزمون Quiz24 ثبت نشده است."}, status=status.HTTP_409_CONFLICT)
        try:
            username = student_quiz24_mobile(student)
        except Quiz24Error as error:
            return Response(quiz24_error_data(error), status=error.api_status)
        initial_attempt = ExamAttempt.objects.filter(exam=exam, student=student).values_list("updated_at", flat=True).first()
        service = Quiz24Service()
        try:
            response = service.get_exam_result(username, exam.provider_exam_code)
        except Quiz24Error as error:
            return self.provider_error(error)
        try:
            metrics, answers = parse_result(response, exam.question_type)
        except Quiz24Error as error:
            return Response({"detail": error.message}, status=status.HTTP_409_CONFLICT)
        original_identity = result_identity(student)
        raw_result = {"examResult": safe_raw_result(response)}
        try:
            sheet = service.get_answersheet(username, exam.provider_exam_code)
        except Quiz24Error:
            sheet = None
        if sheet is not None:
            raw_result["answersheets"] = safe_raw_result(sheet)
            sheet_answers = parse_answers(sheet, exam.question_type)
            if sheet_answers:
                combined = {row["order"]: row.copy() for row in answers or []}
                for row in sheet_answers:
                    old = combined.get(row["order"], {})
                    merged = {key: value if value is not None else old.get(key) for key, value in row.items()}
                    if str(exam.question_type) == "1" and merged["correct_answer"] in ("1", "2", "3", "4"):
                        merged["is_correct"] = merged["answer"] == merged["correct_answer"]
                    combined[row["order"]] = merged
                answers = sorted(combined.values(), key=lambda row: row["order"])
        with transaction.atomic():
            # Serialize repeated syncs without creating duplicate attempts/answers.
            locked = Exam.objects.select_for_update().get(pk=exam.pk)
            if not self.get_queryset().filter(pk=locked.pk).exists() or locked.provider_exam_code != exam.provider_exam_code:
                return Response({"detail": "اطلاعات آزمون تغییر کرده است؛ دوباره نتیجه را دریافت کنید."}, status=status.HTTP_409_CONFLICT)
            current_student = StudentProfile.objects.select_for_update().get(pk=student.pk)
            if request.user.role == User.Role.STUDENT and not results_released(locked):
                return Response({"code": "RESULTS_NOT_RELEASED", "detail": "زمان انتشار نتیجه آزمون هنوز نرسیده است."}, status=403)
            if result_identity(current_student) != original_identity or (request.user.role == User.Role.COUNSELOR and (current_student.counselor_id != locked.counselor_id or selected_student is not None and not locked.assignments.filter(student=current_student).exists())):
                return Response({"detail": "دسترسی یا اتصال حساب تغییر کرده است؛ دوباره بررسی کنید."}, status=409)
            current_attempt = ExamAttempt.objects.filter(exam=exam, student=current_student).values_list("updated_at", flat=True).first()
            if current_attempt != initial_attempt:
                return Response({"detail": "نتیجه جدیدتری ذخیره شده است؛ نتیجه را دوباره مشاهده کنید."}, status=409)
            proof = verification_defaults(locked, current_student, response, metrics)
            previous = ExamAttempt.objects.filter(exam=locked, student=current_student).first()
            if previous and previous.result_verified_at and not proof["result_verified_at"]:
                return Response({"code": "INCOMPLETE_RESULT", "detail": "نتیجه جدید کامل و قابل تأیید نیست؛ نتیجه قبلی حفظ شد."}, status=409)
            attempt, _ = ExamAttempt.objects.update_or_create(
                exam=exam, student_id=student.pk,
                defaults={**metrics, **proof, "provider_username": username, "status": ExamAttempt.Status.COMPLETED, "raw_result": raw_result},
            )
            if answers:
                attempt.answers.all().delete()
                ExamAnswer.objects.bulk_create([ExamAnswer(attempt=attempt, **row) for row in answers])
        return Response(ExamAttemptSerializer(attempt).data)

    def provider_error(self, error=None):
        return Response(
            quiz24_error_data(error or Quiz24Error(None, "ساختار پاسخ آزمون Quiz24 با اطلاعات مورد انتظار مطابقت ندارد.")),
            status=error.api_status if error else status.HTTP_502_BAD_GATEWAY,
        )

    def create(self, request, *args, **kwargs):
        serializer = CreateExamSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            Quiz24Config().validate(require_user=True).callback()
        except Quiz24Error as error:
            return self.provider_error(error)
        canonical = {key: (value.pk if hasattr(value, "pk") else value) for key, value in data.items() if key != "creation_key"}
        digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest()
        key = hashlib.sha256(f"manual:{request.user.pk}:{data.pop('creation_key', digest)}".encode()).hexdigest()
        # Reserve the local exam before the provider side effect, so the callback
        # can identify it and an uncertain response never leads to another create.
        with transaction.atomic():
            receipt, _ = ExamCreation.objects.get_or_create(key=key, defaults={"payload_digest": digest})
            receipt = ExamCreation.objects.select_for_update().get(pk=receipt.pk)
            if receipt.payload_digest != digest:
                return Response({"detail": "شناسه درخواست قبلاً استفاده شده است."}, status=409)
            if receipt.state == "COMPLETE":
                return Response(self.get_serializer(receipt.exam).data, status=200)
            if receipt.state == "FAILED":
                return Response(receipt.response, status=receipt.error_status)
            if receipt.state in ("SENDING", "UNKNOWN"):
                return Response({"detail": "نتیجه ساخت در حال بررسی است؛ آزمون جدیدی ایجاد نشد."}, status=409)
            if not receipt.exam_id:
                receipt.exam = Exam.objects.create(**data, creation_mode="MANUAL", status=Exam.Status.DRAFT)
                if receipt.exam.student_id:
                    ExamAssignment.objects.get_or_create(exam=receipt.exam, student=receipt.exam.student, defaults={"assigned_by": request.user})
            send = receipt.state == "PENDING"
            if send:
                receipt.state = "SENDING"
            receipt.save(update_fields=("exam", "state"))
        exam = receipt.exam
        if send:
            try:
                response = Quiz24Service().create_exam(
                    title=data["title"], grade_external_id=data["grade_external_id"],
                    question_type=data["question_type"], duration_minutes=data["duration_minutes"],
                    has_negative_score=data["has_negative_score"],
                    callback=Quiz24Config().manual_callback(exam.pk),
                )
            except Quiz24Error as error:
                failure = quiz24_error_data(error)
                state = "FAILED" if error.status is not None and str(error.status) != "100" else "UNKNOWN"
                ExamCreation.objects.filter(pk=receipt.pk).update(state=state, response=failure, error_status=error.api_status)
                return self.provider_error(error)
            ExamCreation.objects.filter(pk=receipt.pk).update(state="RECEIVED", response=response)
            receipt.response = response
        provider = CreatedQuiz24ExamSerializer.from_provider(receipt.response)
        if not provider.is_valid():
            return Response({"detail": "پاسخ ساخت قابل پردازش نیست؛ پیش‌نویس حفظ شد و ساخت مجدد انجام نشد."}, status=502)
        try:
            with transaction.atomic():
                receipt = ExamCreation.objects.select_for_update().get(pk=receipt.pk)
                if receipt.state != "COMPLETE":
                    exam.provider_exam_code = provider.validated_data["examCode"]
                    exam.manage_url = provider.validated_data["result"]
                    exam.save(update_fields=("provider_exam_code", "manage_url", "updated_at"))
                    receipt.state = "COMPLETE"
                    receipt.save(update_fields=("state",))
                else:
                    exam = receipt.exam
        except DatabaseError:
            return Response({"detail": "ساخت در Quiz24 انجام شد؛ تلاش دوباره فقط ثبت محلی را تکمیل می‌کند."}, status=503)
        return Response(self.get_serializer(exam).data, status=201)

    @action(detail=True, methods=("post",))
    def sync(self, request, pk=None):
        exam = self.get_object()
        edit_op = ExamProviderOperation.objects.filter(exam=exam).first()
        editing = bool(edit_op and edit_op.kind == "EDIT" and edit_op.state in ("EDITING", "UNKNOWN", "PENDING"))
        if editing:
            if edit_op.actor_id != request.user.pk or edit_op.state == "PENDING" or str(edit_op.token) != str(request.data.get("edit_session", "")):
                return Response({"detail": "شناسه جلسه ویرایش قدیمی یا نامعتبر است؛ جلسه جاری را بررسی کنید."}, status=409)
        else:
            require_mutable(exam)
        if exam.archived_at or exam.trashed_at or activity(exam):
            return Response({"detail": "همگام‌سازی محتوای آزمون در این وضعیت مجاز نیست."}, status=409)
        if exam.status not in (Exam.Status.DRAFT, Exam.Status.READY):
            return Response({"detail": "فقط آزمون پیش‌نویس یا آماده قابل همگام‌سازی است."}, status=status.HTTP_409_CONFLICT)
        if not exam.provider_exam_code:
            return Response({"detail": "کد آزمون Quiz24 ثبت نشده است."}, status=status.HTTP_409_CONFLICT)
        try:
            original_updated_at = exam.updated_at
            response = Quiz24Service().get_exam(exam.provider_exam_code)
        except Quiz24Error as error:
            return self.provider_error(error)
        provider = SyncedQuiz24ExamSerializer.from_provider(response)
        if not provider.is_valid():
            return self.provider_error()
        data = provider.validated_data
        try:
            with transaction.atomic():
                exam = Exam.objects.select_for_update().get(pk=exam.pk)
                if not self.get_queryset().filter(pk=exam.pk).exists():
                    return Response(status=404)
                if exam.status not in (Exam.Status.DRAFT, Exam.Status.READY):
                    return Response({"detail": "آزمون منتشرشده قابل همگام‌سازی نیست."}, status=status.HTTP_409_CONFLICT)
                current_op = ExamProviderOperation.objects.filter(exam=exam).first()
                if editing and (not current_op or current_op.token != edit_op.token or current_op.state not in ("EDITING", "UNKNOWN")):
                    return Response({"detail": "جلسه ویرایش تغییر کرده است؛ پاسخ قدیمی اعمال نشد."}, status=409)
                if not editing and blocked(exam) or exam.archived_at or exam.trashed_at or activity(exam):
                    return Response({"detail": "وضعیت مدیریت آزمون تغییر کرده است."}, status=409)
                if exam.updated_at != original_updated_at:
                    return Response(self.get_serializer(exam).data)
                if (exam.provider_exam_id and exam.provider_exam_id != data["provider_exam_id"]
                        or data.get("provider_exam_code", exam.provider_exam_code) != exam.provider_exam_code):
                    return Response({"detail": "اطلاعات Quiz24 با شناسه آزمون مطابقت ندارد."}, status=409)
                next_status = Exam.Status.READY if data["question_count"] > 0 else Exam.Status.DRAFT
                if not editing and exam.status == next_status and all(getattr(exam, field) == value for field, value in data.items()):
                    return Response(self.get_serializer(exam).data)
                before = {"status": exam.status, "question_count": exam.question_count}
                for field, value in data.items():
                    setattr(exam, field, value)
                exam.status = next_status
                exam.save(update_fields=(*data.keys(), "status", "updated_at"))
                if editing:
                    current_op.state = "IDLE"
                    current_op.response_url = ""
                    current_op.save()
                event(exam, request.user, "EDIT_RECONCILED" if editing else "SYNC_METADATA", before, {"status": exam.status, "question_count": exam.question_count})
        except IntegrityError:
            return Response({"detail": "شناسه آزمون Quiz24 قبلاً برای آزمون دیگری ثبت شده است."}, status=status.HTTP_409_CONFLICT)
        return Response(self.get_serializer(exam).data)
