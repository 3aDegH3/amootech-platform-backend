import re
from django.core.exceptions import ValidationError


def normalize_mobile(value):
    value = str(value or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    if value.startswith("+98"):
        value = "0" + value[3:]
    if not re.fullmatch(r"09[0-9]{9}", value):
        raise ValidationError("شماره موبایل معتبر ایران را با 09 یا +98 وارد کنید.")
    return value
