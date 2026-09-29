import re

PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
ARABIC_NORMALIZE = str.maketrans({"ي": "ی", "ك": "ک", "ى": "ی", "ة": "ه"})

def to_persian_digits(value) -> str:
    if value is None:
        return ""
    return str(value).translate(PERSIAN_DIGITS)

def normalize_persian_text(text: str) -> str:
    if not text:
        return ""
    # remove HTML tags
    text = re.sub(r"<[^>]+>", "", str(text))
    # arabic -> persian chars
    text = text.translate(ARABIC_NORMALIZE)
    # normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # remove zero-width chars
    text = text.replace("\u200c", " ").replace("\u200f", "").replace("\u200e", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text

def safe_excel_text(value) -> str:
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith(("=", "+", "-", "@")):
            return "'" + value
        # Also handle embedded formula marker like " — =SUM" -> the cell still starts with subject prefix, not "=", but contains "=SUM"
        # For security, if any dangerous substring exists after cleaning, prefix with apostrophe
        if any(tok in value for tok in ("=SUM", "=CMD", "@SUM", "+SUM")):
            return "'" + value
    return value

def persian_duration_text(minutes: int | None) -> str:
    if minutes is None:
        return ""
    return f"{to_persian_digits(minutes)} دقیقه"
