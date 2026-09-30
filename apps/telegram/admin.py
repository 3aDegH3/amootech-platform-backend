from django.contrib import admin

from .models import TelegramConnectionToken, TelegramGroupConnection, TelegramStudentConnection


@admin.register(TelegramGroupConnection)
class TelegramGroupConnectionAdmin(admin.ModelAdmin):
    list_display = ("student", "telegram_chat_id", "chat_title", "chat_type", "is_active", "connected_at")
    search_fields = ("chat_title",)
    list_filter = ("is_active", "chat_type")


@admin.register(TelegramStudentConnection)
class TelegramStudentConnectionAdmin(admin.ModelAdmin):
    list_display = ("student", "telegram_user_id", "telegram_username", "is_active", "connected_at")
    search_fields = ("telegram_username",)
    list_filter = ("is_active",)


@admin.register(TelegramConnectionToken)
class TelegramConnectionTokenAdmin(admin.ModelAdmin):
    list_display = ("student", "expires_at", "used_at", "created_by", "created_at")
    readonly_fields = ("token_hash", "created_at")
    list_filter = ("used_at",)
