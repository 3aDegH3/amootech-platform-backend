"""
Excel -> PDF converter via LibreOffice headless.

Single responsibility: convert a generated XLSX bytes (already styled, RTL, A3,
fitToPage) into PDF bytes by invoking soffice with an isolated profile per call.
No ReportLab, no HTML, no style duplication - the PDF is the Excel's Print Preview.

Production-safe: unique temp dir + unique UNO profile, timeout, return-code
checking, cleanup of orphan tmp files.
"""
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from .export_exceptions import ExportConversionFailed, ExportTemplateNotFound


def _find_soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    # Common Debian path
    for cand in ("/usr/bin/soffice", "/usr/lib/libreoffice/program/soffice.bin"):
        if Path(cand).exists():
            return cand
    return None


def _validate_pdf(pdf_bytes: bytes) -> None:
    if not pdf_bytes.startswith(b"%PDF"):
        raise ExportConversionFailed("Converted file is not a PDF.")
    # Light page-count check: not strictly enforcing 1 page here, but warn if obviously wrong
    # Real one-page enforcement is in callers that check Template's PrintArea; converter keeps Template's fitToPage.


def convert_excel_to_pdf(
    excel_bytes: bytes,
    *,
    timeout: int = 30,
    soffice_bin: str | None = None,
) -> bytes:
    """
    Convert XLSX bytes to PDF bytes via LibreOffice.

    Raises ExportConversionFailed on any failure (soffice missing, non-zero exit, missing output, timeout).
    """
    soffice = soffice_bin or _find_soffice()
    if not soffice:
        raise ExportConversionFailed(
            "LibreOffice (soffice) not found in PATH. Install 'libreoffice-calc' in the container."
        )

    tmpdir = Path(tempfile.mkdtemp(prefix=f"plan-export-{uuid.uuid4().hex[:8]}-"))
    profile = tmpdir / f"lo-profile-{uuid.uuid4().hex[:8]}"
    profile.mkdir(parents=True, exist_ok=True)
    xlsx_path = tmpdir / "plan.xlsx"

    try:
        xlsx_path.write_bytes(excel_bytes)

        cmd = [
            soffice,
            f"-env:UserInstallation=file://{profile}",
            "--headless",
            "--nologo",
            "--nofirststartwizard",
            "--nolockcheck",
            "--norestore",
            "--convert-to",
            "pdf:calc_pdf_Export",
            "--outdir",
            str(tmpdir),
            str(xlsx_path),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                text=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExportConversionFailed(f"LibreOffice conversion timed out after {timeout}s.") from exc

        if result.returncode != 0:
            stderr = result.stderr.decode(errors="ignore")[:2000] if result.stderr else ""
            stdout = result.stdout.decode(errors="ignore")[:2000] if result.stdout else ""
            raise ExportConversionFailed(
                f"LibreOffice failed (code {result.returncode}). "
                f"stderr: {stderr} stdout: {stdout}"
            )

        pdf_path = tmpdir / "plan.pdf"
        if not pdf_path.exists():
            # Some versions keep original basename
            pdf_candidates = list(tmpdir.glob("*.pdf"))
            if pdf_candidates:
                pdf_path = pdf_candidates[0]
            else:
                raise ExportConversionFailed(
                    f"LibreOffice did not produce a PDF. stdout: {result.stdout.decode(errors='ignore')[:1000]}"
                )

        pdf_bytes = pdf_path.read_bytes()
        _validate_pdf(pdf_bytes)
        return pdf_bytes

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
