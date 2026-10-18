from django.db import connection, DatabaseError
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    permission_classes = (AllowAny,)

    @extend_schema(
        responses={200: inline_serializer(
            name="HealthResponse",
            fields={"status": serializers.CharField()},
        )}
    )
    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except DatabaseError:
            return Response({"status": "unavailable"}, status=503)
        return Response({"status": "ok"})
