import os, sys, json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['DJANGO_SETTINGS_MODULE']='apps.exams.tests.postgres_settings'
import django
django.setup()
from django.core.management import call_command
from django.utils import timezone
from datetime import date,timedelta,time
from unittest.mock import patch
from apps.academics.local_catalog import sync_catalog
from apps.academics.models import Grade, Field
from apps.accounts.models import User, CounselorProfile, StudentProfile
from apps.planning.models import Plan, PlanDay, StudentFixedCommitment, StudentFixedEvent
from apps.academics.tests_local_catalog import provider_fixture
from rest_framework_simplejwt.tokens import RefreshToken
call_command('migrate',verbosity=0)
# A recorded real taxonomy is replayed only at the provider transport seam.
# All API serving, validation, persistence and browser requests use actual Django/PostgreSQL.
data=provider_fixture()
if os.getenv('SPRINT_A_CATALOG_CAPTURE'):
 payload=json.loads(Path(os.environ['SPRINT_A_CATALOG_CAPTURE']).read_text())['payload']
 data={**payload,'detail':data['detail']}
 data['courses']=payload.get('source_courses',payload['courses'])
with patch('apps.academics.local_catalog.Quiz24Service') as service:
 for method in ('education_periods','grades','courses'):getattr(service.return_value,method).return_value=data[method]
 service.return_value.course_detail.side_effect=lambda **kwargs:data['details'][f"{kwargs['grade_id']}:{kwargs['course_id']}"] if 'details' in data else data['detail']
 sync_catalog(allow_removals=True)
owner,_=User.objects.get_or_create(username='qa_counselor',defaults={'role':'COUNSELOR'})
counselor,_=CounselorProfile.objects.get_or_create(user=owner)
user,_=User.objects.get_or_create(username='qa_student',defaults={'role':'STUDENT'})
for n,name in ((10,'دهم'),(11,'یازدهم'),(12,'دوازدهم')):
 grade,_=Grade.objects.get_or_create(number=n,defaults={"name":name})
 field,_=Field.objects.get_or_create(grade=grade,name='تجربی')
student,_=StudentProfile.objects.update_or_create(user=user,defaults={"counselor":counselor,"grade":grade,"field":field})
start=date(2026,10,24)
plan=Plan.objects.create(student=student,counselor=counselor,start_date=start,end_date=start+timedelta(days=6),title='برنامه تست یکپارچه',exam_dates=[start.isoformat()])
for i in range(7):PlanDay.objects.create(plan=plan,date=start+timedelta(days=i))
StudentFixedCommitment.objects.get_or_create(student=student,kind='SCHOOL',weekday=5,title='مدرسه شنبه',defaults={'start_time':time(7,30),'end_time':time(13,30)})
print(json.dumps({'student':student.pk,'plan':plan.pk,'counselor_token':str(RefreshToken.for_user(owner).access_token),'student_token':str(RefreshToken.for_user(user).access_token),'taxonomy_grade':data.get('grade'),'taxonomy_course':data.get('course')}))
