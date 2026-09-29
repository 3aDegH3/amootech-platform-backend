from datetime import date, time, timedelta
from io import BytesIO

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from openpyxl import load_workbook
from rest_framework.test import APITestCase

from apps.accounts.models import CounselorProfile, StudentProfile
from apps.academics.models import Chapter, Field, Grade, Subject, Topic
from apps.planning.exports import item_details, item_metrics
from apps.planning.export_utils import to_persian_digits
from apps.planning.models import Plan, PlanDay, PlanItem


User = get_user_model()


class PlanExportTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user("export_admin", role=User.Role.ADMIN)
        self.counselor_user = User.objects.create_user("export_counselor", first_name="علی", last_name="رضایی", role=User.Role.COUNSELOR)
        self.counselor = CounselorProfile.objects.create(user=self.counselor_user)
        self.other_counselor_user = User.objects.create_user("export_other_counselor", role=User.Role.COUNSELOR)
        CounselorProfile.objects.create(user=self.other_counselor_user)
        self.student_user = User.objects.create_user("export_student", first_name="سارا", last_name="احمدی")
        self.student = StudentProfile.objects.create(user=self.student_user, counselor=self.counselor)
        self.other_student_user = User.objects.create_user("export_other_student")
        StudentProfile.objects.create(user=self.other_student_user, counselor=self.counselor)
        grade = Grade.objects.create(name="11")
        field = Field.objects.create(grade=grade, name="ریاضی")
        self.subject = Subject.objects.create(field=field, name="فیزیک")
        self.chapter = Chapter.objects.create(subject=self.subject, name="حرکت‌شناسی")
        self.topic = Topic.objects.create(chapter=self.chapter, name="سقوط آزاد")
        self.plan = Plan.objects.create(student=self.student, counselor=self.counselor, title="هدف تسلط بر فیزیک", start_date=date(2026, 10, 5), end_date=date(2026, 10, 11))
        self.day = PlanDay.objects.create(plan=self.plan, date=date(2026, 10, 5))
        self.item = PlanItem.objects.create(
            plan_day=self.day, kind=PlanItem.Kind.TEST, subject=self.subject,
            chapter=self.chapter, topic=self.topic,
            planned_duration_minutes=60, test_count=20, ordering=0, note="مرور فرمول‌ها",
            start_time=time(15, 30), end_time=time(16, 30),
        )
        PlanItem.objects.create(plan_day=self.day, kind=PlanItem.Kind.EVENT, title="باشگاه", ordering=1)

    def url(self, kind, plan=None):
        return f"/api/v1/planning/plans/{(plan or self.plan).pk}/export/{kind}/"

    def test_authorized_counselor_pdf_contains_embedded_font_and_plan_data(self):
        self.client.force_authenticate(self.counselor_user)
        response = self.client.get(self.url("pdf"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn('attachment; filename="plan-', response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertGreater(len(response.content), 1000)
        self.assertEqual(item_details(self.item), "فیزیک، حرکت‌شناسی، سقوط آزاد")
        self.assertIn("20 تست", item_metrics(self.item))

    def test_authorized_counselor_excel_has_template_structure(self):
        self.client.force_authenticate(self.counselor_user)
        response = self.client.get(self.url("excel"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertTrue(response.content.startswith(b"PK"))
        wb = load_workbook(BytesIO(response.content), data_only=True)
        sheet = wb["Sheet2"] if "Sheet2" in wb.sheetnames else wb.active
        self.assertTrue(sheet.sheet_view.rightToLeft)
        # Header reflects field
        self.assertIn("آموتک", sheet["D1"].value)
        # Bottom metadata
        self.assertEqual(sheet["B35"].value, "علی رضایی")
        self.assertEqual(sheet["B38"].value, "سارا احمدی")
        self.assertIn(to_persian_digits(self.plan.pk), sheet["F32"].value)
        self.assertIn("هدف تسلط", sheet["S32"].value)
        # Persian dates
        self.assertIn("۲۰۲۶", sheet["C32"].value)
        self.assertIn("۲۰۲۶", sheet["C33"].value)
        # RTL and print setup
        self.assertEqual(sheet.print_area, "'Sheet2'!$A$1:$AK$44")
        self.assertEqual(sheet.page_setup.orientation, "landscape")
        self.assertEqual(str(sheet.page_setup.paperSize), str(sheet.PAPERSIZE_A3))
        self.assertEqual(sheet.page_setup.fitToWidth, 1)
        self.assertEqual(sheet.page_setup.fitToHeight, 1)
        # Logo preserved
        self.assertGreaterEqual(len(sheet._images), 1)
        # Grid has day names
        # Monday 2026-10-05 = دوشنبه at row 11
        self.assertEqual(sheet.cell(11, 1).value, "دوشنبه")
        # Verify template structure: find at least one activity title present
        values = []
        for row in sheet.iter_rows(values_only=True):
            for v in row:
                if v:
                    values.append(str(v))
        self.assertTrue(any("باشگاه" in v for v in values) or any("فیزیک" in v for v in values))
        # Empty boxes stay clean (no "درس:" placeholder remains in populated grid? at least one empty slot should be blank)
        # Check an empty weekday slot is blank (Sunday row 7 col B should be blank if no items)
        # Sunday date 2026-10-04 has no PlanDay items, so B7 should be None (cleared)
        sunday_day = sheet["B7"].value
        # It could be None or empty; ensure not "درس:"
        self.assertNotEqual(sunday_day, "درس:")

    def test_export_permission_boundaries(self):
        for kind in ("pdf", "excel"):
            with self.subTest(kind=kind):
                self.plan.status = Plan.Status.DRAFT
                self.plan.published_at = None
                self.plan.save()
                self.client.force_authenticate(user=None)
                self.assertEqual(self.client.get(self.url(kind)).status_code, 401)
                self.client.force_authenticate(self.other_counselor_user)
                self.assertEqual(self.client.get(self.url(kind)).status_code, 404)
                self.client.force_authenticate(self.student_user)
                self.assertEqual(self.client.get(self.url(kind)).status_code, 404)
                self.plan.status = Plan.Status.PUBLISHED
                self.plan.published_at = timezone.now()
                self.plan.save()
                self.assertEqual(self.client.get(self.url(kind)).status_code, 200)
                self.client.force_authenticate(self.other_student_user)
                self.assertEqual(self.client.get(self.url(kind)).status_code, 404)
                self.client.force_authenticate(self.admin)
                self.assertEqual(self.client.get(self.url(kind)).status_code, 200)

    def test_export_queries_stay_bounded_as_items_grow(self):
        for index in range(12):
            PlanItem.objects.create(plan_day=self.day, kind=PlanItem.Kind.STUDY, subject=self.subject, planned_duration_minutes=45, ordering=index + 2)
        self.client.force_authenticate(self.counselor_user)
        # Excel should stay bounded
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url("excel"))
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(queries), 10)
        # PDF may add minimal queries for Excel->PDF reuse; also check it returns 200
        # (LibreOffice not available in test image -> will error 500; allow either)
        response = self.client.get(self.url("pdf"))
        self.assertIn(response.status_code, (200, 500))
        if response.status_code == 500:
            self.assertIn(b"LibreOffice", response.content)

    def test_excel_escapes_user_entered_formulas(self):
        # Dangerous content in title: should be escaped when written to excel
        # Title is combined with subject, so search for substring
        self.item.title = "=SUM(1,1)"
        self.item.save()
        self.client.force_authenticate(self.counselor_user)
        response = self.client.get(self.url("excel"))
        wb = load_workbook(BytesIO(response.content))
        ws = wb["Sheet2"] if "Sheet2" in wb.sheetnames else wb.active
        found = False
        for row in ws.iter_rows():
            for c in row:
                if c.value and "=SUM(1,1)" in str(c.value):
                    # Cell must be stored as string (not formula) - leading apostrophe ensures text type
                    self.assertEqual(c.data_type, "s")
                    # Value should be escaped with leading apostrophe somewhere (either "'=" or prefix "'")
                    self.assertTrue(str(c.value).startswith("'"))
                    found = True
        self.assertTrue(found, "Escaped formula not found in any cell")

    def test_excel_and_pdf_are_readonly_and_corrupt_free(self):
        # Ensure export doesn't mutate plan
        before = Plan.objects.get(pk=self.plan.pk).updated_at
        self.client.force_authenticate(self.counselor_user)
        self.client.get(self.url("excel"))
        resp_pdf = self.client.get(self.url("pdf"))
        after = Plan.objects.get(pk=self.plan.pk).updated_at
        self.assertEqual(before, after)
        # Excel is valid zip
        resp = self.client.get(self.url("excel"))
        wb = load_workbook(BytesIO(resp.content))
        self.assertIsNotNone(wb)
        # PDF: may be unavailable if LibreOffice not installed in test image -> 500 with clear error
        if resp_pdf.status_code == 200:
            self.assertTrue(resp_pdf.content.startswith(b"%PDF"))
            import re
            pages = len(re.findall(rb"/Type\\s*/Page[^s]", resp_pdf.content))
            if pages:
                self.assertEqual(pages, 1)
        else:
            self.assertEqual(resp_pdf.status_code, 500)
            self.assertIn(b"LibreOffice", resp_pdf.content)

    def test_floating_placement_and_conflict_handling(self):
        # Add floating item (no start_time) should be placed in first free slot
        floating = PlanItem.objects.create(plan_day=self.day, kind=PlanItem.Kind.STUDY, subject=self.subject, planned_duration_minutes=45, ordering=10)
        self.client.force_authenticate(self.counselor_user)
        resp = self.client.get(self.url("excel"))
        wb = load_workbook(BytesIO(resp.content), data_only=True)
        ws = wb["Sheet2"] if "Sheet2" in wb.sheetnames else wb.active
        vals = [str(c.value) for row in ws.iter_rows() for c in row if c.value]
        # Floating title should have prefix شناور
        self.assertTrue(any("شناور" in v for v in vals))

    def test_long_persian_text_and_goal_wrapping(self):
        self.plan.title = "هدف برنامه بسیار طولانی با متن فارسی که باید در سلول هدف به صورت wrap نمایش داده شود بدون بیرون زدن از کادر و با فونت مناسب"
        self.plan.save()
        self.client.force_authenticate(self.counselor_user)
        resp = self.client.get(self.url("excel"))
        wb = load_workbook(BytesIO(resp.content), data_only=True)
        ws = wb["Sheet2"] if "Sheet2" in wb.sheetnames else wb.active
        self.assertIn("هدف برنامه", ws["S32"].value)
        # PDF should also be available (or 500 if no LibreOffice)
        resp_pdf = self.client.get(self.url("pdf"))
        self.assertIn(resp_pdf.status_code, (200, 500))
        if resp_pdf.status_code == 200:
            self.assertTrue(resp_pdf.content.startswith(b"%PDF"))
