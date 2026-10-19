import logging
import re

import httpx
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from ..exceptions import Quiz24Error
from ..configuration import Quiz24Config, valid_http_url

logger = logging.getLogger(__name__)


def quiz24_mobile(value):
    number = str(value or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    if not re.fullmatch(r"09[0-9]{9}", number):
        raise Quiz24Error(None, "شماره موبایل معتبر دانش‌آموز برای اتصال به Quiz24 ثبت نشده است.", api_status=400)
    return number


def student_quiz24_mobile(student):
    if not student or not getattr(student, "mobile_verified_at", None):
        raise Quiz24Error(None, "ابتدا شماره موبایل خود را در پروفایل تأیید کنید.", api_status=409)
    school_id = Quiz24Config().user_id
    if (getattr(student, "quiz24_sync_status", None) != "SYNCED"
            or not getattr(student, "quiz24_student_id", None)
            or getattr(student, "quiz24_school_id", None) != school_id):
        raise Quiz24Error(None, "اتصال حساب آزمون کامل نشده است؛ وضعیت اتصال را در پروفایل بررسی و دوباره تلاش کنید.", api_status=409)
    return quiz24_mobile(student.mobile)


class Quiz24Service:
    def __init__(self):
        self.config = Quiz24Config()
        self.base_url = self.config.base_url
        self.api_key = self.config.api_key
        self.user_id = self.config.user_id

    def _safe_message(self, message):
        message = str(message)
        if self.api_key:
            message = message.replace(self.api_key, "[حذف شد]")
            if self.api_key.startswith("apikey-"):
                message = message.replace(self.api_key[7:], "[حذف شد]")
        message = re.sub(r"(?i)(?:bearer\s+\S+|apikey-[\w-]+)", "[حذف شد]", message)
        message = re.sub(r"09[0-9]{9}", "[شماره حذف شد]", message)
        return " ".join(message.split())[:1000]

    def _failure(self, endpoint, message, *, http_status=None, provider_status=None, response=None, api_status=None):
        if endpoint == "registerStudent":
            if not str(provider_status).isdecimal():
                provider_status = None
            message = "درخواست ثبت دانش‌آموز در Quiz24 ناموفق بود."
        if endpoint == "getExamParticipants":
            message = "دریافت اطلاعات پایش از Quiz24 ناموفق بود."
        message = self._safe_message(message)
        provider_status = self._safe_message(provider_status) if isinstance(provider_status, str) else provider_status
        if not isinstance(provider_status, (str, int)):
            provider_status = None
        # Allowlist provider metadata; never log response data, URLs, or request payloads.
        safe_response = {"status": provider_status, "message": message} if response is not None else None
        logger.warning("Quiz24 failure endpoint=%s http_status=%s provider_status=%s message=%s response=%s", endpoint, http_status, provider_status, message, safe_response)
        api_status = api_status or (409 if provider_status is not None and str(provider_status).strip() != "100" else 502)
        return Quiz24Error(provider_status, message, http_status=http_status, safe_response=safe_response, api_status=api_status)

    def _redact_response(self, value):
        if isinstance(value, dict):
            return {
                key: "[حذف شد]" if str(key).replace("_", "").replace("-", "").lower() in ("apikey", "xapikey", "authorization", "password", "accesstoken", "refreshtoken") else self._redact_response(entry)
                for key, entry in value.items()
            }
        if isinstance(value, list):
            return [self._redact_response(entry) for entry in value]
        if isinstance(value, str) and self.api_key:
            value = value.replace(self.api_key, "[حذف شد]")
            if self.api_key.startswith("apikey-"):
                value = value.replace(self.api_key[7:], "[حذف شد]")
        return value

    def _request(self, endpoint, payload=None, *, timeout=15.0):
        self.config.validate(require_user=endpoint not in ("educationPeriods", "grades", "courses"))
        if endpoint in ("educationPeriods", "grades", "courses"):
            body = {}
        else:
            body = {**(payload or {}), "userId": int(self.user_id)}
            # Local model identifiers are strings; documented JSON examCode is Integer.
            code = body.get("examCode")
            if isinstance(code, str) and code.isascii() and code.isdecimal():
                body["examCode"] = int(code)
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    self.base_url + endpoint,
                    json=body,
                    headers={"X-API-KEY": self.api_key},
                )
        except httpx.TimeoutException as error:
            raise self._failure(endpoint, "پاسخی از Quiz24 دریافت نشد.") from error
        except (httpx.RequestError, httpx.InvalidURL) as error:
            raise self._failure(endpoint, "اتصال به Quiz24 برقرار نشد.") from error
        except (ValueError, ImportError) as error:
            raise self._failure(endpoint, "تنظیمات اتصال یا پراکسی Quiz24 معتبر نیست.", api_status=400) from error
        try:
            result = response.json()
        except ValueError as error:
            raise self._failure(endpoint, "پاسخ نامعتبر از Quiz24 دریافت شد.", http_status=response.status_code) from error
        if not isinstance(result, dict):
            raise self._failure(endpoint, "ساختار پاسخ Quiz24 قابل پردازش نیست.", http_status=response.status_code)
        provider_status = result.get("status")
        if provider_status is None:
            raise self._failure(endpoint, "ساختار پاسخ Quiz24 قابل پردازش نیست.", http_status=response.status_code, response=result)
        if str(provider_status).strip() != "100" or not response.is_success:
            message = result.get("message") or result.get("Message") or result.get("error")
            if not isinstance(message, str) and isinstance(result.get("result"), str):
                message = result["result"]
            raise self._failure(
                endpoint,
                message if isinstance(message, str) and message.strip() else f"درخواست Quiz24 رد شد (HTTP {response.status_code}).",
                provider_status=provider_status, http_status=response.status_code, response=result,
            )
        return self._redact_response(result)

    def register_student(self, *, mobile, first_name, last_name, password):
        return self._request("registerStudent", {
            "userName": quiz24_mobile(mobile), "name": first_name,
            "family": last_name, "password": password, "isActive": True,
        })

    def education_periods(self):
        return self._request("educationPeriods")

    def grades(self):
        return self._request("grades")

    def courses(self):
        return self._request("courses")

    def course_detail(self, grade_id, course_id, consider_used_question=False):
        return self._request("courseDetail", {"gradeId": grade_id, "courseId": course_id, "considerUsedQuestion": consider_used_question})

    def create_exam(self, *, title, grade_external_id, question_type, duration_minutes, has_negative_score, callback=None):
        callback = callback or self.config.callback()
        if not valid_http_url(callback):
            raise Quiz24Error(None, "آدرس بازگشت آزمون معتبر نیست.", api_status=400)
        return self._request("createExam", {
            "title": title,
            "callback": callback,
            "type": 1,
            "gradeId": grade_external_id,
            "examTime": duration_minutes,
            "hasNegativeScore": has_negative_score,
            "questionType": question_type,
        })

    def edit_existing_exam(self, exam_id, callback):
        if not str(exam_id).isascii() or not str(exam_id).isdecimal() or int(exam_id) <= 0 or not valid_http_url(callback):
            raise Quiz24Error(None, "شناسه آزمون یا آدرس بازگشت معتبر نیست.", api_status=400)
        return self._request("editExam", {"examId": int(exam_id), "callback": callback})

    def delete_existing_exam(self, exam_id):
        if not str(exam_id).isascii() or not str(exam_id).isdecimal() or int(exam_id) <= 0:
            raise Quiz24Error(None, "شناسه آزمون معتبر نیست.", api_status=400)
        return self._request("deleteExam", {"examId": int(exam_id)})

    def get_exam(self, exam_code):
        return self._request("exam", {"examCode": int(exam_code) if str(exam_code).isdecimal() else exam_code})

    def get_exam_participants(self, exam_code, page=1, *, timeout=15.0):
        # Public contract: /WebService/v1#getExamParticipantsBox.
        return self._request("getExamParticipants", {"examCode": int(exam_code) if str(exam_code).isdecimal() else exam_code, "page": page}, timeout=timeout)

    def get_exam_result(self, user_name, exam_code, *, timeout=15.0):
        return self._request("examResult", {"userName": quiz24_mobile(user_name), "examCode": exam_code}, timeout=timeout)

    def get_answersheet(self, user_name, exam_code):
        return self._request("answersheets", {"userName": quiz24_mobile(user_name), "examCode": exam_code})

    def question_count(self, *, grade_id, course_id, question_type, chapter_ids=None, subject_ids=None, object_ids=None, from_year=None, to_year=None, consider_used_question=False):
        payload = {
            "gradeId": grade_id, "courseId": course_id, "questionType": question_type,
            "considerUsedQuestion": consider_used_question,
        }
        payload.update(self._taxonomy_ids(chapter_ids, subject_ids, object_ids))
        if from_year is not None:
            payload["fromYear"] = from_year
        if to_year is not None:
            payload["toYear"] = to_year
        return self._request("hardLevelQuestionCount", payload)

    def create_online_exam(self, *, title, grade_id, course_id, question_type, easy_count, medium_count, hard_count, very_hard_count, duration_minutes, has_negative_score=False, chapter_ids=None, subject_ids=None, object_ids=None, from_year=None, to_year=None, consider_used_question=False):
        payload = {
            "callback": self.config.callback(),
            "title": title, "gradeId": grade_id, "courseId": course_id,
            "easyCount": easy_count, "mediumCount": medium_count,
            "hardCount": hard_count, "veryHardCount": very_hard_count,
            "questionType": question_type, "examTime": duration_minutes,
            "hasNegativeScore": has_negative_score, "considerUsedQuestion": consider_used_question,
        }
        payload.update(self._taxonomy_ids(chapter_ids, subject_ids, object_ids))
        if from_year is not None:
            payload["fromYear"] = from_year
        if to_year is not None:
            payload["toYear"] = to_year
        return self._request("createOnlineExam", payload)

    def get_participation_url(self, user_name, exam_code, callback=None):
        user_name = quiz24_mobile(user_name)
        callback = callback or self.config.callback(student=True)
        if not valid_http_url(callback):
            raise Quiz24Error(None, "آدرس بازگشت آزمون در تنظیمات سامانه معتبر نیست.", api_status=400)
        messages = {
            "119": "آزمون معتبر نیست.",
            "120": "آزمون غیرفعال است.",
            "123": "آزمون سوالی ندارد.",
            "124": "آزمون به‌صورت آنلاین برگزار نمی‌شود.",
            "125": "تاریخ‌های برگزاری آزمون مشخص نشده است.",
            "126": "اعتبار اجرای آزمون کافی نیست.",
            "149": "اعتبار اجرای آزمون کافی نیست.",
            "127": "برای شرکت در آزمون پرداخت لازم است.",
            "129": "شما قبلاً در این آزمون شرکت کرده‌اید.",
            "131": "آزمون هنوز شروع نشده است.",
            "132": "زمان آزمون به پایان رسیده است.",
        }
        try:
            response = self._request("examParticipation", {
                "userName": user_name,
                "examCode": exam_code,
                "callback": callback,
                "isEditAnswersheet": False,
            })
        except Quiz24Error as error:
            raise Quiz24Error(
                error.status,
                f"{messages[str(error.status)]} ({error.message})" if str(error.status) in messages else error.message,
                http_status=error.http_status, safe_response=error.safe_response, api_status=error.api_status,
            ) from error
        url = response.get("result")
        if not isinstance(url, str):
            raise Quiz24Error(None, "لینک شرکت در آزمون از Quiz24 دریافت نشد.")
        try:
            URLValidator(schemes=("http", "https"))(url)
        except ValidationError as error:
            raise Quiz24Error(None, "لینک شرکت در آزمون معتبر نیست.") from error
        return url

    @staticmethod
    def _taxonomy_ids(chapter_ids, subject_ids, object_ids):
        if subject_ids and not chapter_ids or object_ids and (not chapter_ids or not subject_ids):
            raise Quiz24Error(None, "انتخاب مبحث و زیرمبحث به انتخاب فصل و مبحث والد نیاز دارد.")
        return {
            key: ",".join(str(value) for value in values)
            for key, values in (("chapterIds", chapter_ids), ("subjectIds", subject_ids), ("objectIds", object_ids))
            if values
        }
