from datetime import datetime, timezone as datetime_timezone

from django import forms
from django.contrib import admin
from django.test import SimpleTestCase

from apps.core.admin_dates import JalaliDateTimeField, jalali_datetime
from apps.telegram.models import TelegramConnectionToken, TelegramGroupConnection


class JalaliAdminDatesTests(SimpleTestCase):
    def test_tehran_midnight_display_and_jalali_input_roundtrip(self):
        value = datetime(2026, 10, 23, 21, tzinfo=datetime_timezone.utc)
        field = JalaliDateTimeField()
        self.assertEqual(jalali_datetime(value), '۱۴۰۵/۰۸/۰۲ ۰۰:۳۰:۰۰')
        self.assertEqual(field.clean(field.prepare_value(value)), value)
        with self.assertRaises(forms.ValidationError):
            field.clean('۱۴۰۴/۱۲/۳۰ ۰۹:۰۰')

    def test_registered_admin_uses_jalali_fields_and_list_columns(self):
        group_admin = admin.site._registry[TelegramGroupConnection]
        self.assertIn('connected_at_fa', group_admin.list_display)
        token_admin = admin.site._registry[TelegramConnectionToken]
        field = token_admin.formfield_for_dbfield(TelegramConnectionToken._meta.get_field('expires_at'), request=None)
        self.assertIsInstance(field, JalaliDateTimeField)
        self.assertEqual(field.widget.input_type, 'text')
