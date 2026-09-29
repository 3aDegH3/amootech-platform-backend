"""
Export service: Plan -> Excel -> PDF pipeline.

Excel is the Single Source of Truth for layout. PDF is produced only by
converting the generated XLSX; there is no independent PDF renderer.

Public API:
    export_plan_excel(plan) -> bytes  (XLSX)
    export_plan_pdf(plan)   -> bytes  (PDF, via Excel)

Both are read-only (no DB mutation). Temporary files are managed inside the
converter with per-call isolated LibreOffice profiles.
"""
from .export_excel import render_excel
from .excel_to_pdf import convert_excel_to_pdf


def export_plan_excel(plan) -> bytes:
    """Generate canonical XLSX for a Plan (template-based, RTL, A3 fitToPage)."""
    return render_excel(plan)


def export_plan_pdf(plan, *, pdf_timeout: int = 30) -> bytes:
    """
    Generate PDF by first generating the canonical XLSX and converting it.
    No second rendering from the data model - Excel IS the layout.
    """
    xlsx = export_plan_excel(plan)
    return convert_excel_to_pdf(xlsx, timeout=pdf_timeout)
