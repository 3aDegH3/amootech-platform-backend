from ..exceptions import Quiz24Error


def normalize_inventory(response):
    result = response
    for _ in range(5):
        if isinstance(result, list) and len(result) == 1:
            result = result[0]
        elif isinstance(result, dict):
            fields = {str(key).replace("_", "").lower(): value for key, value in result.items()}
            if "easy" in fields or "easycount" in fields:
                break
            nested = result.get("result", result.get("data"))
            if nested is None:
                break
            result = nested
        else:
            break
    if not isinstance(result, dict):
        raise Quiz24Error(response.get("status"), "ساختار موجودی سوال‌های Quiz24 معتبر نیست.")
    fields = {str(key).replace("_", "").lower(): value for key, value in result.items()}
    counts = {}
    for name in ("easy", "medium", "hard", "very_hard"):
        key = name.replace("_", "")
        value = fields.get(key, fields.get(key + "count"))
        if isinstance(value, bool):
            raise Quiz24Error(response.get("status"), "موجودی سوال‌های Quiz24 معتبر نیست.")
        try:
            count = int(value)
        except (ValueError, TypeError, OverflowError) as error:
            raise Quiz24Error(response.get("status"), "موجودی سوال‌های Quiz24 معتبر نیست.") from error
        if isinstance(value, float) and value != count or isinstance(value, str) and not value.strip().isdecimal() or not 0 <= count <= 2147483647:
            raise Quiz24Error(response.get("status"), "موجودی سوال‌های Quiz24 معتبر نیست.")
        counts[name] = count
    return counts


def automatic_error_message(error):
    text = error.message.lower()
    if str(error.status) == "150" or any(word in text for word in ("subscription", "package", "credit", "اشتراک", "بسته", "اعتبار")):
        return f"ساخت خودکار آزمون نیازمند فعال بودن بسته بانک سوال Quiz24 است. {error.message}"
    return error.message
