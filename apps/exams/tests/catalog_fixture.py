"""Policy unit tests inject catalog fixtures at the local-read seam.

Database-backed publication/offline integration is tested in academics.tests_local_catalog.
"""
from unittest.mock import patch
from apps.exams.services.quiz24 import Quiz24Service


def inject_catalog_fixture(test):
    def read(method, **kwargs):
        return getattr(Quiz24Service(), method)(**kwargs)
    test.enterContext(patch('apps.academics.local_catalog.read_catalog', side_effect=read))


def link_student_fixture(test, student):
    """Explicitly model a verified test identity; never contact Quiz24."""
    from django.conf import settings
    from django.test import override_settings
    from django.utils import timezone
    if not settings.QUIZ24_USER_ID:
        test.enterContext(override_settings(QUIZ24_USER_ID="174037"))
    student.mobile = f"091{student.pk:08d}"
    student.save()
    student.mobile_verified_at = timezone.now()
    student.quiz24_sync_status = "SYNCED"
    student.quiz24_student_id = 12345
    student.quiz24_school_id = settings.QUIZ24_USER_ID
    student.save()
