"""
PDF export - strict Excel -> PDF via LibreOffice.

No ReportLab, no HTML/CSS/WeasyPrint, no independent layout. Changing the Excel
Template automatically changes the PDF because the PDF is the Excel's Print
Preview converted by LibreOffice.

For backward compatibility this module re-exports the same function names but
now delegates exclusively to the Excel pipeline.
"""
from .export_service import export_plan_pdf
from .excel_to_pdf import convert_excel_to_pdf


def render_pdf(plan, commitments=None, excel_bytes: bytes | None = None) -> bytes:  # noqa: ARG001
    """
    Legacy entry point kept for existing callers/tests.
    `commitments` and `excel_bytes` args are accepted but ignored - the canonical
    path is Plan -> Excel -> PDF with no style duplication.
    """
    return export_plan_pdf(plan)


def render_pdf_from_viewmodel(vm, excel_bytes: bytes | None = None) -> bytes:  # noqa: ARG001
    """
    Legacy viewmodel entry. Rebuilds from vm.plan to avoid a second code path.
    """
    return export_plan_pdf(vm.plan)


__all__ = ["render_pdf", "render_pdf_from_viewmodel", "convert_excel_to_pdf"]
