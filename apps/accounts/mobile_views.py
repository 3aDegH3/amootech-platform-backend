import re

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import StudentProfile
from .permissions import IsStudent
from .profile_serializers import StudentSelfSerializer
from .services.otp import request_code, verify_code
from .services.quiz24_sync import sync_student


class RequestCodeSerializer(serializers.Serializer):
    mobile = serializers.CharField(max_length=20)


class VerifyCodeSerializer(serializers.Serializer):
    code = serializers.CharField(min_length=6, max_length=6, write_only=True)

    def validate_code(self, value):
        value = value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        if not re.fullmatch(r"[0-9]{6}", value):
            raise serializers.ValidationError("کد تأیید باید شش رقم باشد.")
        return value


@method_decorator(transaction.non_atomic_requests, name="dispatch")
class MobileRequestView(APIView):
    permission_classes = (IsStudent,)

    def student(self):
        return get_object_or_404(StudentProfile, user=self.request.user)

    def post(self, request):
        serializer = RequestCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request_code(self.student().pk, serializer.validated_data["mobile"])
        return Response({"detail": "کد تأیید ارسال شد.", "expires_in": 300, "resend_after": 60})


class MobileVerifyView(MobileRequestView):
    def post(self, request):
        serializer = VerifyCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        student = self.student()
        verify_code(student.pk, serializer.validated_data["code"])
        sync_student(student.pk)
        student.refresh_from_db()
        return Response(StudentSelfSerializer(student).data)


class Quiz24SyncView(MobileRequestView):
    def post(self, request):
        student = self.student()
        if not student.mobile_verified_at:
            return Response({"detail": "ابتدا شماره موبایل را تأیید کنید."}, status=409)
        sync_student(student.pk)
        student.refresh_from_db()
        return Response(StudentSelfSerializer(student).data)
