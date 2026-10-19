from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from .exceptions import Quiz24Error


def valid_http_url(value):
    try:
        URLValidator(schemes=("http", "https"))(value)
        parts = urlsplit(value)
        return not parts.username and not parts.password
    except (ValidationError, ValueError, TypeError):
        return False


class Quiz24Config:
    def __init__(self):
        self.base_url = str(getattr(settings, "QUIZ24_BASE_URL", "") or "").strip().rstrip("/")
        self.base_url = re.sub(r"(/api/v1)(?:/api/v1)+$", r"\1", self.base_url) + "/" if self.base_url else ""
        self.api_key = str(getattr(settings, "QUIZ24_API_KEY", "") or "").strip()
        self.user_id = str(getattr(settings, "QUIZ24_USER_ID", "") or "").strip()

    def validate(self, require_user=False):
        if not valid_http_url(self.base_url) or urlsplit(self.base_url).query or urlsplit(self.base_url).fragment:
            raise Quiz24Error(None, "آدرس سرویس Quiz24 در تنظیمات سامانه معتبر نیست.", api_status=400)
        if not self.api_key:
            raise Quiz24Error(None, "کلید API آزمون‌یار تنظیم نشده است.", api_status=400)
        if require_user and (not self.user_id.isascii() or not self.user_id.isdecimal() or len(self.user_id) > 10 or not 0 < int(self.user_id) <= 2147483647):
            raise Quiz24Error(None, "شناسه کاربری Quiz24 تنظیم نشده یا عدد معتبر نیست.", api_status=400)
        return self

    def callback(self, student=False):
        name = "QUIZ24_STUDENT_CALLBACK_URL" if student else "QUIZ24_CALLBACK_URL"
        explicit = str(getattr(settings, name, "") or "").strip()
        base = str(getattr(settings, "FRONTEND_BASE_URL", "") or "").strip().rstrip("/")
        path = "/student/exams" if student else "/counselor/exams"
        if not explicit and (not valid_http_url(base) or urlsplit(base).query or urlsplit(base).fragment):
            raise Quiz24Error(None, "آدرس بازگشت آزمون در تنظیمات سامانه معتبر نیست.", api_status=400)
        url = explicit or (base + path if base else "")
        if not valid_http_url(url):
            raise Quiz24Error(None, "آدرس بازگشت آزمون در تنظیمات سامانه معتبر نیست.", api_status=400)
        return url

    def manual_callback(self, exam_id, edit_session=None):
        parts = urlsplit(self.callback())
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "manual_exam"]
        query = [(k, v) for k, v in query if k != "edit_session"]
        query.append(("manual_exam", str(exam_id)))
        if edit_session:
            query.append(("edit_session", str(edit_session)))
        return urlunsplit(parts._replace(query=urlencode(query)))

    def public_status(self):
        def valid(operation):
            try:
                operation()
                return True
            except Quiz24Error:
                return False
        flags = {
            "base_url_configured": valid_http_url(self.base_url) and not bool(urlsplit(self.base_url).query or urlsplit(self.base_url).fragment),
            "api_key_configured": bool(self.api_key),
            "user_id_configured": self.user_id.isascii() and self.user_id.isdecimal() and len(self.user_id) <= 10 and 0 < int(self.user_id) <= 2147483647,
            "counselor_callback_configured": valid(lambda: self.callback()),
            "student_callback_configured": valid(lambda: self.callback(student=True)),
        }
        result = {"configured": all(flags.values()), **flags}
        if flags["counselor_callback_configured"]:
            parts = urlsplit(self.callback())
            result["counselor_callback_origin"] = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
            result["counselor_callback_path"] = parts.path
        return result
