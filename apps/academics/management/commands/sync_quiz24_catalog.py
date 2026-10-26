import time
import logging
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from apps.academics.local_catalog import sync_catalog
from apps.academics.models import ProviderCatalogState

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Synchronize verified Quiz24 taxonomy; --watch polls monthly due dates and admin requests every 30 seconds.'

    def add_arguments(self, parser):
        parser.add_argument('--allow-removals', action='store_true')
        parser.add_argument('--due', action='store_true')
        parser.add_argument('--watch', action='store_true')

    def handle(self, *args, **options):
        retry_at = 0
        while True:
            try:
                if options['watch'] and time.monotonic() < retry_at and not ProviderCatalogState.objects.filter(pk=1, requested=True).exists():
                    continue
                state = sync_catalog(allow_removals=options['allow_removals'], due_only=options['due'] or options['watch'])
                retry_at = 0
                self.stdout.write(f'Catalog status: {state.status}; revision: {state.revision_id}')
            except Exception as error:
                if not options['watch']:
                    raise CommandError('Catalog sync failed; inspect local catalog status.') from error
                retry_at = time.monotonic() + 3600
                logger.error('Monthly catalog sync failed; last good revision retained; automatic retry in one hour, admin requests still polled.')
            finally:
                close_old_connections()
                if options['watch']:
                    time.sleep(30)
            if not options['watch']:
                break
