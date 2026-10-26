"""Isolated real Django/PostgreSQL browser QA with mocked Quiz24 HTTP only.

Run with EXAM_TEST_PG_SOCKET pointing at a newly initialized disposable cluster.
Never loads .env. All remote HTTP is forbidden, including provider mutations.
"""
import argparse
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DJANGO_SETTINGS_MODULE'] = 'apps.exams.tests.postgres_settings'
import django
django.setup()
from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken
from apps.accounts.models import User, StudentProfile, CounselorProfile
from apps.academics.models import Grade, Field
from apps.daily_reports.models import DailyReport, DailyReportItem
from apps.daily_reports.services import report_today
from apps.planning.models import Plan, PlanDay, PlanItem
from apps.exams.tests.sprint_b_fixture import publish_recorded_catalog

parser = argparse.ArgumentParser()
parser.add_argument('--session', required=True)
parser.add_argument('--serve', action='store_true')
args = parser.parse_args()
# Confirm the server belongs to the explicitly disposable directory, before migration.
with connection.cursor() as cursor:
    cursor.execute('SHOW data_directory')
    if Path(cursor.fetchone()[0]).resolve() != Path(os.environ['EXAM_TEST_PG_SOCKET'], 'data').resolve():
        raise RuntimeError('Refusing QA writes: PostgreSQL data directory does not match disposable socket')
call_command('migrate', verbosity=0)
data = publish_recorded_catalog()
owner = User.objects.create_user('sprint-b-counselor', role='COUNSELOR')
counselor = CounselorProfile.objects.create(user=owner)
for number, name in [(10,'دهم'),(11,'یازدهم'),(12,'دوازدهم')]:
    grade, _ = Grade.objects.get_or_create(number=number, defaults={'name':name})
    field, _ = Field.objects.get_or_create(grade=grade, name='تجربی')
students = []
for i in range(2):
    user = User.objects.create_user(f'sprint-b-student-{i}', role='STUDENT', first_name=f'دانش‌آموز {i+1}')
    student = StudentProfile.objects.create(user=user,counselor=counselor,grade=grade,field=field,mobile=f'0912345678{i}')
    student.mobile_verified_at = timezone.now(); student.quiz24_sync_status = 'SYNCED'
    student.quiz24_student_id = 12345+i; student.quiz24_school_id = '174037'; student.save()
    students.append(student)
today = report_today()
plan = Plan.objects.create(student=students[0],counselor=counselor,status='PUBLISHED',published_at=timezone.now(),start_date=today,end_date=today)
day = PlanDay.objects.create(plan=plan,date=today)
report = DailyReport.objects.create(student=students[0],date=today)
for i, (key, tree) in enumerate(data['details'].items()):
    grade_id, course_id = map(int,key.split(':'))
    chapter = tree['result'][0]['children'][0]['key']
    item = PlanItem.objects.create(plan_day=day,kind='STUDY',title='گزارش QA',planned_duration_minutes=30,
        taxonomy={'provider':'quiz24','grade':grade_id,'course':course_id,'chapter':chapter})
    DailyReportItem.objects.create(report=report,plan_item=item,reported_status='COMPLETED' if i==0 else 'PARTIAL')
session = {'student':students[0].pk, 'students':[s.pk for s in students],
    'student_token':str(RefreshToken.for_user(students[0].user).access_token),
    'counselor_token':str(RefreshToken.for_user(owner).access_token)}
path = Path(args.session); path.write_text(json.dumps(session)); path.chmod(0o600)
print('Disposable Sprint B session prepared; provider creation is MOCKED.',flush=True)
if args.serve:
    sequence = 0
    def create(**kwargs):
        global sequence
        sequence += 1
        return {'status':100, 'result':{'examCode':f'mocked-sprint-b-{sequence}', 'examId':f'mocked-sprint-b-id-{sequence}'}}
    with ExitStack() as stack:
        stack.enter_context(override_settings(ALLOWED_HOSTS=['127.0.0.1','localhost'],CORS_ALLOWED_ORIGINS=['http://127.0.0.1:3199'],QUIZ24_USER_ID='174037'))
        stack.enter_context(patch('apps.exams.services.quiz24.Quiz24Service.question_count',return_value=data['inventory']))
        stack.enter_context(patch('apps.exams.services.quiz24.Quiz24Service.create_online_exam',side_effect=create))
        stack.enter_context(patch('apps.exams.services.quiz24.Quiz24Service.get_participation_url',return_value='https://example.test/mocked-sprint-b-participation'))
        stack.enter_context(patch('httpx.Client.post',side_effect=AssertionError('QA forbids live HTTP')))
        call_command('runserver','127.0.0.1:8199',use_reloader=False)
