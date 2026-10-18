import json
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from apps.accounts.models import MobileChallenge, StudentProfile


class Command(BaseCommand):
    help = "Read a development OTP once in an explicit interactive console; never sends SMS or registers Quiz24 accounts."

    def add_arguments(self, parser):
        parser.add_argument("--student", required=True)

    def handle(self, *args, **options):
        if not settings.DEBUG or not settings.STUDENT_OTP_DEV_ENABLED:
            raise CommandError("Development OTP is disabled.")
        if not self.stdout.isatty():
            raise CommandError("Use an interactive terminal; OTP output must not be redirected to logs.")
        student = StudentProfile.objects.filter(user__username=options["student"]).first()
        if not student:
            raise CommandError("Student not found.")
        path = Path(settings.STUDENT_OTP_DEV_OUTBOX) / f"{student.pk}.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            raise CommandError("No development OTP available. Request a code in the student profile first.") from None
        challenge = MobileChallenge.objects.filter(student=student, nonce=data.get("nonce"), consumed=False, expires_at__gt=timezone.now(), attempts__lt=5).first()
        path.unlink(missing_ok=True)
        if not challenge:
            raise CommandError("Code is no longer valid. Request a new code.")
        self.stdout.write(data["code"])
