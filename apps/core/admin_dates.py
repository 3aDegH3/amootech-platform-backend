"""Jalali boundaries for Django's operational admin, retaining UTC datetimes."""
from datetime import datetime, timedelta

import jdatetime
from django import forms
from django.contrib import admin
from django.utils import timezone

PERSIAN_DIGITS = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')
LATIN_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')


def jalali_datetime(value):
    if value is None:
        return '—'
    local = timezone.localtime(value) if timezone.is_aware(value) else value
    return jdatetime.datetime.fromgregorian(datetime=local).strftime('%Y/%m/%d %H:%M:%S').translate(PERSIAN_DIGITS)


class JalaliDateTimeField(forms.DateTimeField):
    widget = forms.TextInput(attrs={'dir': 'ltr', 'placeholder': '۱۴۰۵/۰۸/۰۲ ۰۹:۳۰:۰۰'})

    def prepare_value(self, value):
        return jalali_datetime(value) if isinstance(value, datetime) else value

    def to_python(self, value):
        if not value or isinstance(value, datetime):
            return super().to_python(value)
        text = str(value).strip().translate(LATIN_DIGITS)
        for pattern in ('%Y/%m/%d %H:%M:%S', '%Y/%m/%d %H:%M'):
            try:
                civil = jdatetime.datetime.strptime(text, pattern).togregorian()
            except ValueError:
                continue
            return super().to_python(civil)
        raise forms.ValidationError('تاریخ و ساعت شمسی معتبر وارد کنید؛ مانند ۱۴۰۵/۰۸/۰۲ ۰۹:۳۰.')


class UsedAtJalaliFilter(admin.SimpleListFilter):
    title = 'زمان مصرف'
    parameter_name = 'used_period'

    def lookups(self, request, model_admin):
        return [('today', 'امروز'), ('week', 'این هفته'), ('month', 'این ماه شمسی'), ('unused', 'مصرف‌نشده')]

    def queryset(self, request, queryset):
        value = self.value()
        if value == 'unused':
            return queryset.filter(used_at__isnull=True)
        today = timezone.localdate()
        if value == 'today':
            start = today
        elif value == 'week':
            start = today - timedelta(days=(today.weekday() + 2) % 7)
        elif value == 'month':
            jalali = jdatetime.date.fromgregorian(date=today)
            start = jdatetime.date(jalali.year, jalali.month, 1).togregorian()
        else:
            return queryset
        return queryset.filter(used_at__date__gte=start, used_at__date__lte=today)
