from datetime import date
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class FlexibleMigrationTests(TransactionTestCase):
    def test_existing_item_and_execution_survive_additive_migration(self):
        executor = MigrationExecutor(connection)
        target = [('planning', '0009_planitem_fixed_source')]
        latest = executor.loader.graph.leaf_nodes()
        executor.migrate(target)
        try:
            apps = executor.loader.project_state(target).apps
            account_apps = executor.loader.project_state(latest).apps
            User = account_apps.get_model('accounts', 'User')
            counselor_user = User.objects.create(username='old-c', role='COUNSELOR')
            student_user = User.objects.create(username='old-s', role='STUDENT')
            counselor = account_apps.get_model('accounts', 'CounselorProfile').objects.create(user=counselor_user)
            student = account_apps.get_model('accounts', 'StudentProfile').objects.create(user=student_user, counselor=counselor)
            plan = apps.get_model('planning', 'Plan').objects.create(student_id=student.pk, counselor_id=counselor.pk, start_date=date(2026, 1, 1), end_date=date(2026, 1, 1))
            day = apps.get_model('planning', 'PlanDay').objects.create(plan=plan, date=plan.start_date)
            item = apps.get_model('planning', 'PlanItem').objects.create(plan_day=day, kind='STUDY', title='عنوان تاریخی', note='یادداشت', planned_duration_minutes=30)
            execution = apps.get_model('planning', 'PlanItemExecution').objects.create(student_id=student.pk, plan_item=item, status='COMPLETED')
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            from apps.planning.models import PlanItem, PlanItemExecution
            preserved = PlanItem.objects.get(pk=item.pk)
            self.assertEqual(preserved.title, 'عنوان تاریخی')
            self.assertEqual(preserved.note, 'یادداشت')
            self.assertEqual(preserved.taxonomy, {})
            self.assertTrue(preserved.title_is_custom)
            self.assertEqual(PlanItemExecution.objects.get(pk=execution.pk).plan_item_id, item.pk)
        finally:
            MigrationExecutor(connection).migrate(latest)
