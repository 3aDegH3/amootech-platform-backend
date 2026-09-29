"""
Central mapping for Activity Box rendering.
Both Excel and PDF must use this presenter so labels stay consistent.
"""
from dataclasses import dataclass
from typing import Optional

from .export_utils import to_persian_digits, normalize_persian_text, persian_duration_text

# kind -> presenter labels (bottom row labels B5/C5/D5 order left-to-right in sheet but visually RTL)
# Geometry per spec: B4/B5 = count/value, C4/C5 = type, D4/D5 = source/study, E4:E5 = duration

KIND_LABELS = {
    "STUDY": "مطالعه",
    "TEST": "تست",
    "REVIEW": "مرور",
    "EXAM": "آزمون",
    "EVENT": "رویداد",
}

# Mapping per activity kind for the three lower labels (and what value goes there)
# Returns (b_label, c_label, d_label)

PRESENTER_LABELS = {
    # STUDY/REVIEW/EXAM stay as generic but meaningful
    "STUDY":        ("حجم", "نوع فعالیت", "مطالعه"),
    "REVIEW":       ("حجم", "نوع فعالیت", "مطالعه"),
    "EXAM":         ("تعداد سؤال", "نوع آزمون", "منبع"),
    # TEST - MCQ style
    "TEST":         ("تعداد تست", "نوع تست", "مطالعه"),
    # EVENT subtypes via title hints - handled in presenter logic but default:
    "EVENT":        ("مقدار", "نوع فعالیت", "جزئیات"),
    "EVENT_SCHOOL": ("مدت", "نوع فعالیت", "جزئیات"),
    "EVENT_CLASS":  ("مدت", "نوع فعالیت", "جزئیات"),
    "EVENT_CLUB":   ("مدت", "نوع فعالیت", "جزئیات"),
    "OTHER":        ("مقدار", "نوع", "توضیح"),
}

# For WRITTEN / descriptive test (if note indicates تشریحی)
WRITTEN_LABELS = ("تعداد سؤال", "نوع تمرین", "منبع")


@dataclass
class ActivityBoxViewModel:
    title: str
    duration_text: str  # e.g. "۹۰ دقیقه"
    b_value: str
    b_label: str
    c_value: str
    c_label: str
    d_value: str
    d_label: str
    is_floating: bool = False
    activity_type: str = ""  # original kind
    plan_item_id: Optional[int] = None
    overflow: bool = False


def _detect_event_subtype(item) -> str:
    title = normalize_persian_text(getattr(item, "title", "") or "")
    lower = title.lower()
    # Persian hints
    if any(k in title for k in ("مدرسه", "school")):
        return "EVENT_SCHOOL"
    if any(k in title for k in ("کلاس", "class")):
        return "EVENT_CLASS"
    if any(k in title for k in ("باشگاه", "club", "ورزش")):
        return "EVENT_CLUB"
    return "EVENT"


def _is_written_test(item) -> bool:
    text = " ".join(filter(None, [
        normalize_persian_text(getattr(item, "title", "") or ""),
        normalize_persian_text(getattr(item, "note", "") or ""),
    ]))
    return any(k in text for k in ("تشریحی", "سوال تشریحی", "written"))


def build_activity_title(item) -> str:
    """
    Priority: title (counselor short instruction) -> subject + chapter/topic -> kind label
    Never return "درس:" placeholder.
    """
    title = normalize_persian_text(getattr(item, "title", "") or "")
    subject_name = ""
    chapter_name = ""
    topic_name = ""
    try:
        if getattr(item, "subject_id", None) and getattr(item, "subject", None):
            subject_name = normalize_persian_text(item.subject.name)
        if getattr(item, "chapter_id", None) and getattr(item, "chapter", None):
            chapter_name = normalize_persian_text(item.chapter.name)
        if getattr(item, "topic_id", None) and getattr(item, "topic", None):
            topic_name = normalize_persian_text(item.topic.name)
    except Exception:
        pass

    # If title already descriptive (longer than 3 chars) use it as is, optionally prepend subject if not present
    if title and len(title) >= 3:
        # Avoid duplicating subject if already in title
        if subject_name and subject_name not in title:
            # For events, title is enough
            if getattr(item, "kind", "") == "EVENT":
                return title
            # For academic, prepend subject context if title doesn't contain it
            # Keep short: "فیزیک — حرکت‌شناسی — <title>"
            parts = []
            if subject_name:
                parts.append(subject_name)
            if chapter_name and chapter_name not in title:
                parts.append(chapter_name)
            elif topic_name and topic_name not in title:
                parts.append(topic_name)
            if parts:
                return " — ".join(parts) + " — " + title
        return title

    # No meaningful title - build from taxonomy
    parts = []
    if subject_name:
        parts.append(subject_name)
    if chapter_name:
        parts.append(chapter_name)
    elif topic_name:
        parts.append(topic_name)
    if parts:
        return " — ".join(parts)
    # Fallback: kind label
    kind = getattr(item, "kind", "")
    return KIND_LABELS.get(kind, "")


def present_activity(item, is_floating: bool = False) -> ActivityBoxViewModel:
    kind = getattr(item, "kind", "STUDY")
    title = build_activity_title(item)

    # Duration: canonical source is planned_duration_minutes; start/end is slot position, not duration text
    duration_text = persian_duration_text(getattr(item, "planned_duration_minutes", None))

    # Determine labels
    if kind == "TEST" and _is_written_test(item):
        b_label, c_label, d_label = WRITTEN_LABELS
    elif kind == "EVENT":
        subtype = _detect_event_subtype(item)
        b_label, c_label, d_label = PRESENTER_LABELS.get(subtype, PRESENTER_LABELS["EVENT"])
    else:
        b_label, c_label, d_label = PRESENTER_LABELS.get(kind, PRESENTER_LABELS["OTHER"])
        # For generic OTHER fallback
        if kind not in PRESENTER_LABELS:
            b_label, c_label, d_label = PRESENTER_LABELS["OTHER"]

    # Values for B/C/D
    b_value = ""
    c_value = ""
    d_value = ""

    # D = source / مطالعه
    # Prefer chapter/topic or note snippet? Use chapter/topic if present else subject
    if kind in ("STUDY", "REVIEW", "TEST"):
        # D = resource / مطالعه source: use chapter name or topic or "-"
        if getattr(item, "chapter_id", None) and getattr(item, "chapter", None):
            d_value = normalize_persian_text(item.chapter.name)
        elif getattr(item, "topic_id", None) and getattr(item, "topic", None):
            d_value = normalize_persian_text(item.topic.name)
        elif getattr(item, "subject_id", None):
            d_value = ""  # subject already in title; keep empty to avoid duplication
        # truncate very long
        if len(d_value) > 18:
            d_value = d_value[:18]
    elif kind == "EVENT":
        # D = جزئیات - keep empty or short note
        note = normalize_persian_text(getattr(item, "note", "") or "")
        if note:
            d_value = note[:18]
    elif kind == "EXAM":
        note = normalize_persian_text(getattr(item, "note", "") or "")
        d_value = note[:18] if note else ""

    # C = type
    if kind == "TEST":
        c_value = "آموزشی"  # default; could be زمان‌دار etc based on note
        note_title = (getattr(item, "title", "") or "") + (getattr(item, "note", "") or "")
        if "زمان‌دار" in note_title or "زماندار" in note_title:
            c_value = "زمان‌دار"
        elif "تشریحی" in note_title:
            c_value = "تشریحی"
        elif "مرور" in note_title:
            c_value = "مروری"
    elif kind in ("STUDY", "REVIEW"):
        c_value = KIND_LABELS.get(kind, "")
    elif kind == "EXAM":
        c_value = "آزمون"
    elif kind == "EVENT":
        c_value = KIND_LABELS.get("EVENT", "رویداد")

    # B = count / حجم
    if kind == "TEST":
        tc = getattr(item, "test_count", None)
        b_value = to_persian_digits(tc) if tc is not None else ""
    elif kind in ("STUDY", "REVIEW"):
        # no generic count; keep empty
        b_value = ""
    elif kind == "EXAM":
        tc = getattr(item, "test_count", None)
        b_value = to_persian_digits(tc) if tc is not None else ""
    else:
        b_value = ""

    # Floating prefix visible in title
    display_title = title
    if is_floating and title:
        # Prepend floating marker - will be styled with light fill
        display_title = f"شناور | {title}"

    # Overflow detection (title too long)
    overflow = len(display_title) > 90  # ~ 2 lines at 9pt in 4-col width ~ 220px

    return ActivityBoxViewModel(
        title=display_title,
        duration_text=duration_text,
        b_value=b_value,
        b_label=b_label,
        c_value=c_value,
        c_label=c_label,
        d_value=d_value,
        d_label=d_label,
        is_floating=is_floating,
        activity_type=kind,
        plan_item_id=getattr(item, "pk", None) or getattr(item, "id", None),
        overflow=overflow,
    )
