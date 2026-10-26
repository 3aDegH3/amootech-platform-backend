from django.contrib import admin
from django.db import models
from apps.core.admin_dates import JalaliDateTimeField, UsedAtJalaliFilter, jalali_datetime

from .models import TelegramConnectionToken, TelegramGroupConnection, TelegramStudentConnection


class TehranDateAdmin(admin.ModelAdmin):
    formfield_overrides = {models.DateTimeField: {'form_class': JalaliDateTimeField, 'widget': JalaliDateTimeField.widget}}

    @admin.display(description='زمان اتصال', ordering='connected_at')
    def connected_at_fa(self, obj):
        return jalali_datetime(obj.connected_at)

    @admin.display(description='انقضا', ordering='expires_at')
    def expires_at_fa(self, obj):
        return jalali_datetime(obj.expires_at)

    @admin.display(description='زمان مصرف', ordering='used_at')
    def used_at_fa(self, obj):
        return jalali_datetime(obj.used_at)

    @admin.display(description='زمان ایجاد', ordering='created_at')
    def created_at_fa(self, obj):
        return jalali_datetime(obj.created_at)


@admin.register(TelegramGroupConnection)
class TelegramGroupConnectionAdmin(TehranDateAdmin):
    list_display = ("student", "telegram_chat_id", "chat_title", "chat_type", "is_active", "connected_at_fa")
    search_fields = ("chat_title",)
    list_filter = ("is_active", "chat_type")


@admin.register(TelegramStudentConnection)
class TelegramStudentConnectionAdmin(TehranDateAdmin):
    list_display = ("student", "telegram_user_id", "telegram_username", "is_active", "connected_at_fa")
    search_fields = ("telegram_username",)
    list_filter = ("is_active",)


@admin.register(TelegramConnectionToken)
class TelegramConnectionTokenAdmin(TehranDateAdmin):
    list_display = ("student", "expires_at_fa", "used_at_fa", "created_by", "created_at_fa")
    readonly_fields = ("token_hash", "created_at_fa")
    list_filter = (UsedAtJalaliFilter,)
