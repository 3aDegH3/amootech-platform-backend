"""
Canonical Export View Model - single source of truth for Excel and PDF.
"""
from dataclasses import dataclass, field
from datetime import date, time, datetime
from typing import Optional, List, Dict, Any

from .export_presenters import ActivityBoxViewModel, present_activity
from .export_utils import to_persian_digits, normalize_persian_text

# 9 time slots as in template Row 2
SLOT_DEFS = [
    ("B", "E", time(6, 15),  time(7, 45)),
    ("F", "I", time(8, 0),   time(9, 30)),
    ("J", "M", time(9, 45),  time(11, 0)),
    ("N", "Q", time(11, 30), time(13, 0)),
    ("R", "U", time(14, 0),  time(15, 30)),
    ("V", "Y", time(15, 45), time(17, 15)),
    ("Z", "AC", time(17, 30), time(19, 0)),
    ("AD", "AG", time(19, 15), time(20, 45)),
    ("AH", "AK", time(21, 15), time(22, 45)),
]

# weekday mapping: Plan uses dates, template weekdays are Saturday..Friday
PERSIAN_WEEKDAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]
# Python weekday: Monday=0 -> template Saturday=5, Sunday=6, Monday=0 ...
def persian_weekday_name(d: date) -> str:
    # Convert python weekday to template order (Saturday=0)
    idx = (d.weekday() + 2) % 7  # Monday 0 -> 2, Saturday 5 -> 0, Sunday 6 -> 1
    return PERSIAN_WEEKDAYS[idx]

TEMPLATE_WEEKDAY_ROWS = {
    0: (3, 6),   # شنبه
    1: (7, 10),  # یکشنبه
    2: (11, 14), # دوشنبه
    3: (15, 18), # سه‌شنبه
    4: (19, 22), # چهارشنبه
    5: (23, 26), # پنجشنبه
    6: (27, 30), # جمعه
}

@dataclass
class SlotViewModel:
    index: int
    col_start: str
    col_end: str
    start_time: time
    end_time: time
    activity: Optional[ActivityBoxViewModel] = None
    floating: bool = False
    conflict: Optional[str] = None  # if set, indicates EXPORT_LAYOUT_CONFLICT

@dataclass
class DayViewModel:
    date: date
    weekday_index: int
    weekday_name: str
    row_start: int
    row_end: int
    slots: List[SlotViewModel] = field(default_factory=list)
    overflow_items: List[ActivityBoxViewModel] = field(default_factory=list)  # floating with no free slot

@dataclass
class PlanExportViewModel:
    plan: Any
    student: Any
    counselor: Any
    start_date: date
    end_date: date
    number: str  # persian digits
    raw_number: int
    goal: str
    field_name: str  # for header e.g. "رشته تجربی"
    days: List[DayViewModel] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    overflows: List[Dict[str, Any]] = field(default_factory=list)


def time_in_slot(t: time, slot_start: time, slot_end: time) -> bool:
    return slot_start <= t < slot_end or (t == slot_start)

def overlaps(a_start: Optional[time], a_end: Optional[time], b_start: time, b_end: time) -> bool:
    if a_start is None or a_end is None:
        return False
    return a_start < b_end and a_end > b_start

def find_slot_for_item(item) -> Optional[int]:
    """Find best slot index for a scheduled item based on start_time."""
    st = getattr(item, "start_time", None)
    en = getattr(item, "end_time", None)
    if st is None or en is None:
        return None
    # Find slot where start_time falls inside
    for idx, (_, _, s, e) in enumerate(SLOT_DEFS):
        if s <= st < e:
            return idx
        # Also handle exact boundary: if st == slot start, assign
        if st == s:
            return idx
    # Check overlap heuristic: item overlaps slot significantly
    best = None
    best_overlap = 0
    for idx, (_, _, s, e) in enumerate(SLOT_DEFS):
        if overlaps(st, en, s, e):
            # overlap minutes
            os = max(st, s)
            oe = min(en, e)
            mins = (oe.hour*60+oe.minute) - (os.hour*60+os.minute)
            if mins > best_overlap:
                best_overlap = mins
                best = idx
    return best


def build_export_viewmodel(plan, days_queryset=None, commitments=None) -> PlanExportViewModel:
    """
    Build canonical view model from Plan domain.
    Does NOT mutate DB. Floating placement is presentation-only.
    """
    student = plan.student
    counselor = plan.counselor

    # Determine field name for header
    field_name = ""
    try:
        if getattr(student, "field_id", None) and getattr(student, "field", None):
            field_name = normalize_persian_text(student.field.name)
        elif getattr(student, "field", None):
            field_name = normalize_persian_text(student.field.name)
    except Exception:
        field_name = ""

    # days: if not provided, use plan.days
    if days_queryset is None:
        days_queryset = plan.days.all()
    # Ensure ordered by date
    days_list = sorted(list(days_queryset), key=lambda d: d.date)

    # Build day view models
    day_map: Dict[date, DayViewModel] = {}
    for plan_day in days_list:
        w_idx = (plan_day.date.weekday() + 2) % 7
        rows = TEMPLATE_WEEKDAY_ROWS.get(w_idx, (3, 6))
        dvm = DayViewModel(
            date=plan_day.date,
            weekday_index=w_idx,
            weekday_name=persian_weekday_name(plan_day.date),
            row_start=rows[0],
            row_end=rows[1],
            slots=[SlotViewModel(index=i, col_start=cs, col_end=ce, start_time=s, end_time=e) for i, (cs, ce, s, e) in enumerate(SLOT_DEFS)],
        )
        day_map[plan_day.date] = dvm

    # Also create empty day slots for missing dates within plan range (to keep template structure)
    from datetime import timedelta
    cur = plan.start_date
    while cur <= plan.end_date:
        if cur not in day_map:
            w_idx = (cur.weekday() + 2) % 7
            rows = TEMPLATE_WEEKDAY_ROWS.get(w_idx, (3, 6))
            day_map[cur] = DayViewModel(
                date=cur, weekday_index=w_idx, weekday_name=persian_weekday_name(cur),
                row_start=rows[0], row_end=rows[1],
                slots=[SlotViewModel(index=i, col_start=cs, col_end=ce, start_time=s, end_time=e) for i, (cs, ce, s, e) in enumerate(SLOT_DEFS)],
            )
        cur += timedelta(days=1)

    conflicts: List[Dict[str, Any]] = []
    overflows: List[Dict[str, Any]] = []

    # Commitments for overlap checking (if provided)
    # commitment_map: date -> list of commitments overlapping
    commitment_by_weekday = {}
    if commitments is not None:
        for c in commitments:
            if not getattr(c, "active", True):
                continue
            commitment_by_weekday.setdefault(c.weekday, []).append(c)

    # Placement: first pass scheduled items, second pass floating
    # Collect all items grouped by day
    items_by_day: Dict[date, List[Any]] = {}
    for plan_day in days_list:
        items = list(getattr(plan_day, "items", []).all() if hasattr(getattr(plan_day, "items", []), "all") else getattr(plan_day, "items", []))
        # sort by ordering
        items = sorted(items, key=lambda x: getattr(x, "ordering", 0))
        items_by_day[plan_day.date] = items

    for d, dvm in sorted(day_map.items()):
        items = items_by_day.get(d, [])
        scheduled = [it for it in items if getattr(it, "start_time", None) is not None and getattr(it, "end_time", None) is not None]
        floating = [it for it in items if getattr(it, "start_time", None) is None or getattr(it, "end_time", None) is None]

        occupied = set()  # slot indices occupied by scheduled

        # Place scheduled
        for item in scheduled:
            slot_idx = find_slot_for_item(item)
            if slot_idx is None:
                # No matching slot - treat as overflow conflict
                conflicts.append({
                    "code": "EXPORT_LAYOUT_CONFLICT",
                    "plan_item_id": getattr(item, "pk", None) or getattr(item, "id", None),
                    "day": str(d),
                    "slot": None,
                    "reason": "زمان فعالیت با هیچ بازه قالبی هم‌خوانی ندارد."
                })
                # Place in overflow
                dvm.overflow_items.append(present_activity(item, is_floating=False))
                continue
            # Check commitment overlap
            w_idx_py = d.weekday()  # Monday=0
            comms = commitment_by_weekday.get(w_idx_py, [])
            for comm in comms:
                if overlaps(item.start_time, item.end_time, comm.start_time, comm.end_time):
                    conflicts.append({
                        "code": "EXPORT_LAYOUT_CONFLICT",
                        "plan_item_id": getattr(item, "pk", None) or getattr(item, "id", None),
                        "day": str(d),
                        "slot": slot_idx,
                        "reason": f"تداخل با تعهد ثابت «{comm.title}»"
                    })
            # Check slot already occupied
            if slot_idx in occupied:
                existing = dvm.slots[slot_idx].activity
                conflicts.append({
                    "code": "EXPORT_LAYOUT_CONFLICT",
                    "plan_item_id": getattr(item, "pk", None) or getattr(item, "id", None),
                    "day": str(d),
                    "slot": slot_idx,
                    "reason": f"اسلات {slot_idx+1} قبلاً توسط فعالیت دیگری اشغال شده است."
                })
                # Keep first, overflow the second
                dvm.overflow_items.append(present_activity(item, is_floating=False))
                continue
            dvm.slots[slot_idx].activity = present_activity(item, is_floating=False)
            occupied.add(slot_idx)

        # Place floating into first free slot
        free_slots = [s for s in dvm.slots if s.index not in occupied]
        for item in floating:
            if free_slots:
                slot = free_slots.pop(0)
                slot.activity = present_activity(item, is_floating=True)
                slot.floating = True
                occupied.add(slot.index)
            else:
                # No free slot - overflow
                box = present_activity(item, is_floating=True)
                dvm.overflow_items.append(box)
                overflows.append({
                    "code": "EXPORT_NO_FREE_SLOT",
                    "plan_item_id": getattr(item, "pk", None) or getattr(item, "id", None),
                    "day": str(d),
                    "reason": "هیچ اسلات آزادی برای فعالیت شناور وجود ندارد."
                })

        # Text overflow validation
        for slot in dvm.slots:
            if slot.activity and slot.activity.overflow:
                overflows.append({
                    "code": "EXPORT_TEXT_OVERFLOW",
                    "plan_item_id": slot.activity.plan_item_id,
                    "day": str(d),
                    "slot": slot.index,
                    "field": "title",
                    "text_length": len(slot.activity.title),
                })
        for box in dvm.overflow_items:
            if box.overflow:
                overflows.append({
                    "code": "EXPORT_TEXT_OVERFLOW",
                    "plan_item_id": box.plan_item_id,
                    "day": str(d),
                    "field": "title",
                    "text_length": len(box.title),
                })

    # Sort days by date for rendering (but template expects Saturday->Friday order)
    # We'll keep chronological but renderer will map via weekday rows
    sorted_days = sorted(day_map.values(), key=lambda x: x.date)

    # Goal
    goal = normalize_persian_text(getattr(plan, "title", "") or "")
    # If plan has no title/goal, leave empty

    return PlanExportViewModel(
        plan=plan,
        student=student,
        counselor=counselor,
        start_date=plan.start_date,
        end_date=plan.end_date,
        number=to_persian_digits(plan.pk),
        raw_number=plan.pk,
        goal=goal,
        field_name=field_name,
        days=sorted_days,
        conflicts=conflicts,
        overflows=overflows,
    )
