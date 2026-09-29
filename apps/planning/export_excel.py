"""
Template-based Excel renderer.
Loads planning_template.xlsx (Sheet2) and writes canonical view model into it.
"""
from io import BytesIO
from pathlib import Path
from copy import copy

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

from .export_utils import to_persian_digits, normalize_persian_text, safe_excel_text
from .export_exceptions import ExportTemplateNotFound

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "planning_template.xlsx"
SHEET_NAME = "Sheet2"

# Light cream for floating (very subtle)
FLOATING_FILL = PatternFill("solid", fgColor="FFFFF8DC")  # cornsilk very light
# Keep existing fills for scheduled; floating gets this fill on title and value cells

# Font sizes
TITLE_FONT_SIZE = 10
B_VALUE_FONT_SIZE = 9
B_LABEL_FONT_SIZE = 7
C_VALUE_FONT_SIZE = 9
C_LABEL_FONT_SIZE = 7
D_VALUE_FONT_SIZE = 9
D_LABEL_FONT_SIZE = 7
DURATION_FONT_SIZE = 8

# Bottom metadata fonts
META_VALUE_FONT_SIZE = 14

PERSIAN_WEEKDAY_NAMES = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]

def _display_name(user):
    try:
        name = user.get_full_name().strip()
        if name:
            return normalize_persian_text(name)
    except Exception:
        pass
    return normalize_persian_text(getattr(user, "username", "") or "")

def _field_label(field_name: str) -> str:
    if field_name:
        return f"گروه آموزشی آموتک (رشته {field_name})"
    return "گروه آموزشی آموتک"

def _ensure_template():
    if not TEMPLATE_PATH.exists():
        raise ExportTemplateNotFound(f"Template not found at {TEMPLATE_PATH}")
    return TEMPLATE_PATH

def _style_cell(cell, font_name=None, font_size=None, bold=None, wrap=None, horiz="center", vert="center"):
    if font_name:
        cell.font = Font(name=font_name, size=font_size or cell.font.size, bold=bold if bold is not None else cell.font.bold, color=cell.font.color)
    elif font_size:
        cell.font = Font(name=cell.font.name, size=font_size, bold=cell.font.bold, color=cell.font.color)
    # alignment
    cell.alignment = Alignment(horizontal=horiz, vertical=vert, wrap_text=wrap if wrap is not None else cell.alignment.wrap_text)

def _set_value(ws, col_letter, row, value, font_size=None, bold=None, wrap=True):
    cell = ws[f"{col_letter}{row}"]
    cell.value = safe_excel_text(value)
    if font_size:
        cell.font = Font(name=cell.font.name or "Calibri", size=font_size, bold=bold if bold is not None else cell.font.bold, color=cell.font.color)
    if wrap is not None:
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=wrap)
    return cell

def _apply_floating_fill(ws, col_start, col_end, row_title, row_vals, row_labels):
    """Apply light fill to floating activity box."""
    from openpyxl.utils import column_index_from_string
    start_idx = column_index_from_string(col_start)
    end_idx = column_index_from_string(col_end)
    # Title row
    for c in range(start_idx, end_idx+1):
        ws.cell(row_title, c).fill = FLOATING_FILL
    # Values row (row_vals)
    for c in range(start_idx, end_idx):  # E col is duration, not filled same? include all
        ws.cell(row_vals, c).fill = FLOATING_FILL
        ws.cell(row_labels, c).fill = FLOATING_FILL
    # Duration column E etc also fill
    # last col (duration) also
    ws.cell(row_vals, end_idx).fill = FLOATING_FILL
    ws.cell(row_vals+1, end_idx).fill = FLOATING_FILL  # merged E4:E5 case is actually two rows but we handle top only fill already; merged cell fill is top-left only

def render_excel_from_viewmodel(vm) -> bytes:
    _ensure_template()
    wb = openpyxl.load_workbook(str(TEMPLATE_PATH))
    # Sheet may be named Sheet2 or first sheet
    if SHEET_NAME in wb.sheetnames:
        ws = wb[SHEET_NAME]
    else:
        ws = wb.active

    ws.sheet_view.rightToLeft = True

    # --- Header ---
    # D1:AK1 -> field-aware title
    header_text = _field_label(vm.field_name)
    cell = ws["D1"]
    cell.value = header_text
    # Keep original font but ensure center
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=False)

    # --- Metadata bottom ---
    # We need to inspect template merges to know where values go:
    # B32/C32 is date label row, but value cells are? According spec:
    # A32:A44 navy bar, B32="تاریخ" etc. Let's use explicit mapping:
    # Values:
    #  - Date range: we have two date value cells? Looking at template: C33:E33 is likely date display? Check structure:
    #    Row32: "تاریخ" in B32, Row33: also "تاریخ" in B33? Actually both B32 and B33 have "تاریخ" - seems two rows for start/end.
    #    C32:E32 and C33:E33 are value areas for dates.
    #    B35:E36 counselor value, B38:E40 student value, B42:E43 mentor (leave)
    #    F32:R34 plan number value, S32:AK34 goal value
    #    A31:AK31 is top separator?

    # Persist raw template values for plan number and goal cells are merged F32:R34 and S32:AK34
    # The top-left of those merges holds the label "شماره برنامه:" and "هدف برنامه:".
    # We need to write the VALUE below or inside? Inspect: merged area is 3 rows tall.
    # Looking at earlier dump: F32:R34 merged, S32:AK34 merged. So those cells ARE both label and value container?
    # In many templates, label and value share same merged cell separated by line break. We'll write into same cell appending value.
    # Better: write value into G33 (inside merge)?? But merged value is only at top-left.
    # Simpler: overwrite F32 with "شماره برنامه: ۳۹۲۲" and S32 with "هدف برنامه: <goal>"

    # Plan number
    ws["F32"].value = f"شماره برنامه: {vm.number}"
    ws["F32"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    # Goal
    goal_val = vm.goal or ""
    ws["S32"].value = f"هدف برنامه: {goal_val}" if goal_val else "هدف برنامه:"
    ws["S32"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # Dates - C32:E32 for start, C33:E33 for end (2 rows)
    from datetime import date as date_type
    def fmt_date(d: date_type) -> str:
        return f"{to_persian_digits(d.year)}/{to_persian_digits(f'{d.month:02d}')}/{to_persian_digits(f'{d.day:02d}')}"
    # Start date in C32 (merged C32:E32)
    ws["C32"].value = fmt_date(vm.start_date)
    ws["C32"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws["C32"].font = Font(name=ws["C32"].font.name or "Calibri", size=11, bold=True)
    # End date in C33
    ws["C33"].value = fmt_date(vm.end_date)
    ws["C33"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws["C33"].font = Font(name=ws["C33"].font.name or "Calibri", size=11, bold=True)

    # Counselor name in B35:E36 merged
    counselor_name = _display_name(vm.counselor.user) if hasattr(vm.counselor, "user") else ""
    ws["B35"].value = counselor_name
    ws["B35"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws["B35"].font = Font(name="Calibri", size=META_VALUE_FONT_SIZE, bold=True)

    # Student name in B38:E40 merged
    student_name = _display_name(vm.student.user) if hasattr(vm.student, "user") else ""
    ws["B38"].value = student_name
    ws["B38"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws["B38"].font = Font(name="Calibri", size=META_VALUE_FONT_SIZE, bold=True)

    # Clear placeholder "درس:" in title cells -> set blank (we will fill with real titles)
    # We'll first blank all title cells (Row 3,7,11,...) that currently contain "درس:"
    title_rows = [3, 7, 11, 15, 19, 23, 27]
    title_cols = ["B", "F", "J", "N", "R", "V", "Z", "AD", "AH"]
    for r in title_rows:
        for col in title_cols:
            c = ws[f"{col}{r}"]
            if c.value == "درس:":
                c.value = None
            # ensure wrap and center
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # Clear default "زمان"/"تعداد تست"/etc? Keep labels? The labels at row5 are the correct labels per spec.
    # We'll keep row5 labels as template (they match our presenter defaults). No need to change unless activity needs different label.
    # But we must blank value cells B4,D4 etc and row6? Row6 is blank extra row? Keep.

    # --- Grid population ---
    # Build map date -> DayViewModel for quick lookup
    day_by_weekday = {d.weekday_index: d for d in vm.days}
    # SLOT_DEFS order matches columns
    # For each weekday 0..6, its rows: 3+4*idx
    for w_idx in range(7):
        dvm = day_by_weekday.get(w_idx)
        if not dvm:
            continue
        # Update weekday name in A column (A3:A6 etc merged)
        # Top-left of weekday merge is A{row_start}
        ws.cell(dvm.row_start, 1).value = dvm.weekday_name
        ws.cell(dvm.row_start, 1).alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        for slot in dvm.slots:
            if slot.activity is None:
                continue
            box = slot.activity
            # Compute rows for this slot: title row = row_start, values row = row_start+1, labels row = row_start+2
            r_title = dvm.row_start
            r_vals = dvm.row_start + 1
            r_labels = dvm.row_start + 2
            # Column letters: slot.col_start .. slot.col_end (4 cols)
            # Title is merged across slot (e.g., B3:E3)
            ws[f"{slot.col_start}{r_title}"].value = safe_excel_text(box.title)
            ws[f"{slot.col_start}{r_title}"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            # Auto font sizing for title: if long, reduce
            t_len = len(box.title or "")
            if t_len > 55:
                ws[f"{slot.col_start}{r_title}"].font = Font(name=ws[f"{slot.col_start}{r_title}"].font.name or "Calibri", size=8, bold=False)
            elif t_len > 35:
                ws[f"{slot.col_start}{r_title}"].font = Font(name=ws[f"{slot.col_start}{r_title}"].font.name or "Calibri", size=9, bold=False)
            else:
                ws[f"{slot.col_start}{r_title}"].font = Font(name=ws[f"{slot.col_start}{r_title}"].font.name or "Calibri", size=TITLE_FONT_SIZE, bold=False)

            # Determine col indices for B,C,D inside slot
            # Slot has 4 cols: e.g., B,C,D,E where E is duration (merged E4:E5)
            # So B = col_start, C = col_start+1, D = col_start+2, E = col_end (last col)
            from openpyxl.utils import column_index_from_string
            start_idx = column_index_from_string(slot.col_start)
            end_idx = column_index_from_string(slot.col_end)
            b_col = get_column_letter(start_idx)
            c_col = get_column_letter(start_idx + 1)
            d_col = get_column_letter(start_idx + 2)
            e_col = get_column_letter(end_idx)  # duration col

            # Values
            ws[f"{b_col}{r_vals}"].value = safe_excel_text(box.b_value)
            ws[f"{b_col}{r_vals}"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws[f"{b_col}{r_vals}"].font = Font(name="Calibri", size=B_VALUE_FONT_SIZE, bold=True)

            ws[f"{c_col}{r_vals}"].value = safe_excel_text(box.c_value)
            ws[f"{c_col}{r_vals}"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws[f"{c_col}{r_vals}"].font = Font(name="Calibri", size=C_VALUE_FONT_SIZE)

            ws[f"{d_col}{r_vals}"].value = safe_excel_text(box.d_value)
            ws[f"{d_col}{r_vals}"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws[f"{d_col}{r_vals}"].font = Font(name="Calibri", size=D_VALUE_FONT_SIZE)

            # Labels row
            ws[f"{b_col}{r_labels}"].value = safe_excel_text(box.b_label)
            ws[f"{b_col}{r_labels}"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws[f"{b_col}{r_labels}"].font = Font(name="Calibri", size=B_LABEL_FONT_SIZE, color="FF555555")

            ws[f"{c_col}{r_labels}"].value = safe_excel_text(box.c_label)
            ws[f"{c_col}{r_labels}"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws[f"{c_col}{r_labels}"].font = Font(name="Calibri", size=C_LABEL_FONT_SIZE, color="FF555555")

            ws[f"{d_col}{r_labels}"].value = safe_excel_text(box.d_label)
            ws[f"{d_col}{r_labels}"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws[f"{d_col}{r_labels}"].font = Font(name="Calibri", size=D_LABEL_FONT_SIZE, color="FF555555")

            # Duration in E (merged E4:E5) - top cell holds value
            ws[f"{e_col}{r_vals}"].value = safe_excel_text(box.duration_text)
            ws[f"{e_col}{r_vals}"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws[f"{e_col}{r_vals}"].font = Font(name="Calibri", size=DURATION_FONT_SIZE, bold=True)

            # Floating styling
            if box.is_floating:
                # Apply light fill to whole box (title + values area)
                _apply_floating_fill(ws, slot.col_start, slot.col_end, r_title, r_vals, r_labels)

    # Handle overflow items: append rows below grid? Instead append as text in bottom area note?
    # For now, overflow items are placed in a hidden red note row? We'll add after row 30 border.
    # Simpler: if overflows exist, add a new sheet "Overflow" listing them (not silent drop)
    if any(d.overflow_items for d in vm.days):
        overflow_ws = wb.create_sheet("Overflow")
        overflow_ws.sheet_view.rightToLeft = True
        overflow_ws.append(["روز", "عنوان فعالیت", "نوع", "یادداشت"])
        for d in vm.days:
            for box in d.overflow_items:
                overflow_ws.append([d.weekday_name, box.title, box.activity_type, f"شناور - بدون اسلات آزاد | مدت: {box.duration_text}"])
        for cell in overflow_ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="C0392B")
        for row in overflow_ws.iter_rows():
            for c in row:
                c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for col in ["A","B","C","D"]:
            overflow_ws.column_dimensions[col].width = 25 if col=="B" else 15

    # --- Print setup ---
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A3
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.page_setup.horizontalCentered = True
    ws.page_setup.verticalCentered = False
    ws.print_area = "A1:AK44"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    # margins small
    ws.page_margins.left = 0.2
    ws.page_margins.right = 0.2
    ws.page_margins.top = 0.2
    ws.page_margins.bottom = 0.2
    ws.page_margins.header = 0.1
    ws.page_margins.footer = 0.1
    # Hide gridlines in print
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.gridLines = False

    out = BytesIO()
    wb.save(out)
    return out.getvalue()


def render_excel(plan, commitments=None) -> bytes:
    from .export_models import build_export_viewmodel
    vm = build_export_viewmodel(plan, commitments=commitments)
    return render_excel_from_viewmodel(vm)
