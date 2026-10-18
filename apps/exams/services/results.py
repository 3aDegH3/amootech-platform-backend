import math

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ..exceptions import Quiz24Error


def _fields(value):
    return {str(key).replace("_", "").lower(): entry for key, entry in value.items()}


def safe_raw_result(value):
    """Keep result metadata and answers without persisting provider question content."""
    if isinstance(value, list):
        return [safe_raw_result(entry) for entry in value]
    if isinstance(value, dict):
        answer_row = "order" in _fields(value)
        safe = {}
        for key, entry in value.items():
            normalized = str(key).replace("_", "").lower()
            if answer_row and normalized not in ("order", "answer", "correctanswer", "iscorrect"):
                continue
            if normalized in ("question", "questionid", "questiontext", "questionhtml", "questionbody", "questionimage", "questionimageurl"):
                continue
            safe[key] = safe_raw_result(entry)
        return safe
    return value


def _number(value, integer=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip().rstrip("%"))
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    if integer:
        return int(number) if number.is_integer() and 0 <= number <= 2147483647 else None
    return number


def _nodes(value):
    if isinstance(value, dict):
        yield value
        for entry in value.values():
            yield from _nodes(entry)
    elif isinstance(value, list):
        for entry in value:
            yield from _nodes(entry)


def parse_answers(response, question_type):
    """Order is local to this exam, never a persistent question identifier."""
    answers = {}
    found_collection = False
    for node in _nodes(response):
        fields = _fields(node)
        if any(key in fields and fields[key] == [] for key in ("answers", "answersheets", "questions")):
            found_collection = True
        if "order" not in fields or not any(key in fields for key in ("answer", "correctanswer")):
            continue
        order = _number(fields["order"], integer=True)
        if order is None or order < 1:
            return None
        row = {"order": order}
        for target, source in (("answer", "answer"), ("correct_answer", "correctanswer")):
            value = fields.get(source)
            if value is not None and (not isinstance(value, (str, int, float)) or isinstance(value, bool)):
                return None
            row[target] = str(value).strip() if value is not None else None
        correct = fields.get("iscorrect")
        row["is_correct"] = correct if isinstance(correct, bool) else None
        if isinstance(correct, (str, int)) and str(correct).lower() in ("true", "false", "1", "0"):
            row["is_correct"] = str(correct).lower() in ("true", "1")
        if row["is_correct"] is None and str(question_type) == "1" and row["correct_answer"] in ("1", "2", "3", "4"):
            row["is_correct"] = row["answer"] == row["correct_answer"]
        if order in answers and answers[order] != row:
            # Repeated per-course numbering cannot safely identify exam-wide rows.
            return None
        answers[order] = row
    if answers:
        return sorted(answers.values(), key=lambda row: row["order"])
    result = response.get("result") if isinstance(response, dict) else None
    return [] if found_collection or result == [] else None


def parse_result(response, question_type):
    aliases = {
        "score": ("score", "percent", "percentage"),
        "balance": ("balance",),
        "rank": ("rank",),
        "correct_count": ("correctcount", "correct", "correctanswers"),
        "wrong_count": ("wrongcount", "wrong", "wronganswers"),
        "no_answer_count": ("noanswercount", "noanswer", "unansweredcount"),
    }
    candidates = []

    def summaries(value):
        if isinstance(value, list):
            for entry in value:
                summaries(entry)
        elif isinstance(value, dict):
            fields = _fields(value)
            parsed = {}
            for target, keys in aliases.items():
                for key in keys:
                    if key in fields:
                        number = _number(fields[key], integer=target not in ("score", "balance"))
                        if number is not None:
                            parsed[target] = number
                            break
            if parsed and not any(key in fields for key in ("courseid", "coursename", "order")):
                candidates.append((parsed, fields))
            for key, entry in fields.items():
                if not key.startswith(("course", "question", "answer")):
                    summaries(entry)

    summaries(response.get("result", response))
    answers = parse_answers(response, question_type)
    if not candidates and not answers:
        raise Quiz24Error(None, "نتیجه کامل آزمون هنوز در Quiz24 موجود نیست.")
    # Use one summary; never sum or mix per-course scores/ranks.
    metrics, fields = max(candidates, key=lambda candidate: len(candidate[0])) if candidates else ({}, {})
    for target, keys in (("started_at", ("startedat", "startdate")), ("finished_at", ("finishedat", "finishdate"))):
        value = next((fields[key] for key in keys if key in fields), None)
        if isinstance(value, str):
            try:
                parsed = parse_datetime(value)
            except ValueError:
                parsed = None
            if parsed:
                metrics[target] = timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed
    return metrics, answers
