from django.db import connection
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from apps.accounts.models import User
from .models import ProviderCatalogState


class CatalogSyncView(APIView):
    permission_classes = (IsAuthenticated,)

    def check_permissions(self, request):
        super().check_permissions(request)
        if request.user.role != User.Role.ADMIN:
            raise PermissionDenied('فقط مدیر می‌تواند فهرست را به‌روزرسانی کند.')

    def get(self, request):
        state = ProviderCatalogState.objects.select_related('revision').filter(pk=1).first()
        running = False
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND classid=0 AND objid=240024 AND objsubid=1 AND database=(SELECT oid FROM pg_database WHERE datname=current_database()) AND granted)")
                running = cursor.fetchone()[0]
        return Response({
            'status': 'RUNNING' if running else 'PENDING' if state and state.requested else state.status if state else 'NEVER',
            'last_success': state.last_success if state else None,
            'last_started': state.last_started if state else None,
            'counts': state.revision.counts if state and state.revision_id else {},
            'quarantine': state.revision.payload.get('quarantine', []) if state and state.revision_id else [],
            'error': state.error if state else '',
            'revision': state.revision_id if state else None,
            'duration_seconds': (state.last_success - state.last_started).total_seconds() if state and state.status == 'SUCCESS' and state.last_started and state.last_success else None,
        })

    def post(self, request):
        if self.get(request).data['status'] == 'RUNNING':
            return Response({'detail': 'همگام‌سازی در حال اجراست.'}, status=409)
        state, _ = ProviderCatalogState.objects.get_or_create(pk=1)
        ProviderCatalogState.objects.filter(pk=state.pk).update(requested=True)
        result = self.get(request)
        result.status_code = 202
        return result
