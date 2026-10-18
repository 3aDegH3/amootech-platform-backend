import hashlib
import json

from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Exists, OuterRef
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.accounts.permissions import IsAdmin, IsCounselor, IsStudent
from .exceptions import Quiz24Error, quiz24_error_data
from .configuration import Quiz24Config
from .models import Exam, ExamAnswer, ExamAttempt, ExamCreation
from .serializers import (
    CourseDetailSerializer, CreateExamSerializer, CreatedQuiz24ExamSerializer,
    AutomaticExamSerializer, AutomaticCreatedExamSerializer, QuestionCountSerializer,
    ExamSerializer, ExamAttemptSerializer, StudentExamSerializer, SyncedQuiz24ExamSerializer,
)
from .services.quiz24 import Quiz24Service, student_quiz24_mobile
from .services.results import parse_answers, parse_result, safe_raw_result
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


class GradesView(StudentBuilderProviderView):
    def get(self, request):
        return self.provider_response("grades")


class CoursesView(StudentBuilderProviderView):
    def get(self, request):
        return self.provider_response("courses")


class CourseDetailView(StudentBuilderProviderView):
    def post(self, request):
        serializer = CourseDetailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.provider_response("course_detail", **serializer.validated_data)


class QuestionCountView(StudentBuilderProviderView):
    def post(self, request):
        serializer = QuestionCountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
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
        queryset = super().get_queryset().annotate(result_available=Exists(ExamAttempt.objects.filter(exam_id=OuterRef("pk"), student_id=OuterRef("student_id"), status=ExamAttempt.Status.COMPLETED)))
        if self.action == "publish":
            queryset = queryset.select_for_update(of=("self",))
        if self.request.user.role == User.Role.ADMIN:
            return queryset
        if self.request.user.role == User.Role.STUDENT:
            return queryset.filter(student__user=self.request.user, status__in=(Exam.Status.PUBLISHED, Exam.Status.CLOSED))
        return queryset.filter(counselor__user=self.request.user)

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
            return Response(self.get_serializer(receipt.exam).data, status=200)
        if receipt and receipt.state == "FAILED":
            return Response(receipt.response, status=receipt.error_status)
        service = Quiz24Service()
        if receipt is None or receipt.state == "PENDING":
            try:
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
            receipt, _ = ExamCreation.objects.get_or_create(key=key, defaults={"payload_digest": digest})
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
                    student=data["student"], counselor=data["counselor"], title=data["title"],
                    grade_external_id=data["grade_external_id"], question_type=data["question_type"],
                    duration_minutes=data["duration_minutes"], has_negative_score=data["has_negative_score"],
                    provider_exam_code=provider.validated_data["examCode"],
                    status=Exam.Status.PUBLISHED if request.user.role == User.Role.STUDENT else Exam.Status.READY,
                    is_personal_practice=request.user.role == User.Role.STUDENT,
                    question_count=sum(data[field] for field in ("easy_count", "medium_count", "hard_count", "very_hard_count")),
                )
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
        if exam.status != Exam.Status.READY or not exam.provider_exam_code or not exam.question_count or exam.question_count <= 0:
            return Response(
                {"detail": "فقط آزمون آماده دارای کد Quiz24 و حداقل یک سوال قابل انتشار است."},
                status=status.HTTP_409_CONFLICT,
            )
        exam.status = Exam.Status.PUBLISHED
        exam.save(update_fields=("status", "updated_at"))
        return Response(self.get_serializer(exam).data)

    @action(detail=True, methods=("post",))
    def start(self, request, pk=None):
        exam = self.get_object()
        if exam.status != Exam.Status.PUBLISHED:
            return Response({"detail": "این آزمون در حال حاضر قابل شروع نیست."}, status=status.HTTP_409_CONFLICT)
        if not exam.provider_exam_code:
            return Response({"detail": "کد آزمون Quiz24 ثبت نشده است."}, status=status.HTTP_409_CONFLICT)
        try:
            username = student_quiz24_mobile(exam.student)
        except Quiz24Error as error:
            return Response(quiz24_error_data(error), status=error.api_status)
        try:
            url = Quiz24Service().get_participation_url(
                user_name=username,
                exam_code=exam.provider_exam_code,
            )
        except Quiz24Error as error:
            return Response(
                quiz24_error_data(error),
                status=error.api_status,
            )
        return Response({"participation_url": url})

    @action(detail=True, methods=("get", "post"))
    def result(self, request, pk=None):
        exam = self.get_object()
        if not exam.student_id:
            return Response({"detail": "این آزمون به دانش‌آموزی اختصاص داده نشده است."}, status=status.HTTP_409_CONFLICT)
        if request.method == "GET":
            attempt = ExamAttempt.objects.filter(exam=exam, student_id=exam.student_id).prefetch_related("answers").first()
            if not attempt:
                return Response({"detail": "نتیجه این آزمون هنوز دریافت نشده است."}, status=status.HTTP_404_NOT_FOUND)
            return Response(ExamAttemptSerializer(attempt).data)
        if not exam.provider_exam_code:
            return Response({"detail": "کد آزمون Quiz24 ثبت نشده است."}, status=status.HTTP_409_CONFLICT)
        try:
            username = student_quiz24_mobile(exam.student)
        except Quiz24Error as error:
            return Response(quiz24_error_data(error), status=error.api_status)
        service = Quiz24Service()
        try:
            response = service.get_exam_result(username, exam.provider_exam_code)
        except Quiz24Error as error:
            return self.provider_error(error)
        try:
            metrics, answers = parse_result(response, exam.question_type)
        except Quiz24Error as error:
            return Response({"detail": error.message}, status=status.HTTP_409_CONFLICT)
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
            if locked.student_id != exam.student_id or locked.provider_exam_code != exam.provider_exam_code:
                return Response({"detail": "اطلاعات آزمون تغییر کرده است؛ دوباره نتیجه را دریافت کنید."}, status=status.HTTP_409_CONFLICT)
            attempt, _ = ExamAttempt.objects.update_or_create(
                exam=exam, student_id=exam.student_id,
                defaults={**metrics, "provider_username": username, "status": ExamAttempt.Status.COMPLETED, "raw_result": raw_result},
            )
            if answers is not None:
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
            response = Quiz24Service().create_exam(
                title=data["title"], grade_external_id=data["grade_external_id"],
                question_type=data["question_type"], duration_minutes=data["duration_minutes"],
                has_negative_score=data["has_negative_score"],
            )
        except Quiz24Error as error:
            return self.provider_error(error)
        provider = CreatedQuiz24ExamSerializer.from_provider(response)
        if not provider.is_valid():
            return self.provider_error()
        exam = Exam.objects.create(
            **data, provider_exam_code=provider.validated_data["examCode"],
            manage_url=provider.validated_data["result"], status=Exam.Status.DRAFT,
        )
        return Response(self.get_serializer(exam).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=("post",))
    def sync(self, request, pk=None):
        exam = self.get_object()
        if exam.status not in (Exam.Status.DRAFT, Exam.Status.READY):
            return Response({"detail": "فقط آزمون پیش‌نویس یا آماده قابل همگام‌سازی است."}, status=status.HTTP_409_CONFLICT)
        if not exam.provider_exam_code:
            return Response({"detail": "کد آزمون Quiz24 ثبت نشده است."}, status=status.HTTP_409_CONFLICT)
        try:
            response = Quiz24Service().get_exam(exam.provider_exam_code)
        except Quiz24Error as error:
            return self.provider_error(error)
        provider = SyncedQuiz24ExamSerializer.from_provider(response)
        if not provider.is_valid():
            return self.provider_error()
        data = provider.validated_data
        try:
            with transaction.atomic():
                exam = self.get_queryset().select_for_update(of=("self",)).get(pk=exam.pk)
                if exam.status not in (Exam.Status.DRAFT, Exam.Status.READY):
                    return Response({"detail": "آزمون منتشرشده قابل همگام‌سازی نیست."}, status=status.HTTP_409_CONFLICT)
                for field, value in data.items():
                    setattr(exam, field, value)
                exam.status = Exam.Status.READY if data["question_count"] > 0 else Exam.Status.DRAFT
                exam.save(update_fields=(*data.keys(), "status", "updated_at"))
        except IntegrityError:
            return Response({"detail": "شناسه آزمون Quiz24 قبلاً برای آزمون دیگری ثبت شده است."}, status=status.HTTP_409_CONFLICT)
        return Response(self.get_serializer(exam).data)
