from rest_framework.exceptions import APIException

class ExportError(APIException):
    status_code = 400
    default_code = "export_error"

class ExportTemplateNotFound(ExportError):
    status_code = 500
    default_code = "EXPORT_TEMPLATE_NOT_FOUND"
    default_detail = "قالب اکسل یافت نشد."

class ExportLayoutConflict(ExportError):
    status_code = 409
    default_code = "EXPORT_LAYOUT_CONFLICT"
    default_detail = "تداخل در جانمایی فعالیت‌ها."

class ExportTextOverflow(ExportError):
    status_code = 422
    default_code = "EXPORT_TEXT_OVERFLOW"
    default_detail = "متن فعالیت برای نمایش در قالب بیش از حد طولانی است."

class ExportConversionFailed(ExportError):
    status_code = 500
    default_code = "EXPORT_CONVERSION_FAILED"
    default_detail = "تبدیل به PDF با خطا مواجه شد."

class ExportInvalidPlan(ExportError):
    status_code = 422
    default_code = "EXPORT_INVALID_PLAN"
    default_detail = "برنامه برای خروجی گرفتن معتبر نیست."
