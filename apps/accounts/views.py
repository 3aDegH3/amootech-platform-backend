from rest_framework.generics import RetrieveAPIView, CreateAPIView, RetrieveUpdateAPIView
from rest_framework.permissions import AllowAny
from django.shortcuts import get_object_or_404

from .serializers import CurrentUserSerializer
from .profile_serializers import StudentRegistrationSerializer, StudentSelfSerializer
from .models import StudentProfile
from .permissions import IsStudent


class CurrentUserView(RetrieveAPIView):
    serializer_class = CurrentUserSerializer

    def get_object(self):
        return self.request.user


class StudentRegistrationView(CreateAPIView):
    permission_classes = (AllowAny,)
    serializer_class = StudentRegistrationSerializer


class StudentSelfView(RetrieveUpdateAPIView):
    serializer_class = StudentSelfSerializer
    permission_classes = (IsStudent,)
    http_method_names = ("get", "patch", "head", "options")

    def get_object(self):
        return get_object_or_404(StudentProfile.objects.select_related("user", "grade", "field", "counselor__user"), user=self.request.user)
