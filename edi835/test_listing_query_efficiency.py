"""List responses must not retrieve stored source documents from the database."""
import json
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from accounts.models import Client, User
from edi835.models import EDI835File, EDI837File, MIRFile, RECONFile
from edi835.edi837_files_v2 import edi837_files
from edi835.client_portal_views import history
from edi835.claims_dashboard_views import dashboard_files
from edi835.recon_files_v2 import recon_files
from edi835.views import api_get_metrics


class ListingQueryEfficiencyTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.tenant = Client.objects.create(name='Listing tenant', client_code='LIST', email='list@example.com')
        self.user = User.objects.create_user(email='reader@example.com', name='Reader',
                                            mobile='5550000191', password='test',
                                            client=self.tenant, client_role='CLIENT_USER')
        self.source = EDI835File.objects.create(client=self.tenant, original_filename='a.835',
                                               stored_filename='a.835', input_file_content='X' * 100000)
        self.mir = MIRFile.objects.create(client=self.tenant, source_835=self.source,
                                         mir_filename='a.mir', file_content='Y' * 100000, file_hash='a')
        self.ref = EDI837File.objects.create(client=self.tenant, original_filename='a.837',
                                            stored_filename='a.837', file_content='Z' * 100000, file_hash='b')
        self.recon = RECONFile.objects.create(client=self.tenant, original_filename='a.recon',
                                              stored_filename='a.recon', file_content='R' * 100000, file_hash='c')

    def response_without_content_queries(self, view, params=None):
        request = self.factory.get('/', params or {})
        request.user = self.user
        with CaptureQueriesContext(connection) as queries:
            response = view(request)
        self.assertEqual(response.status_code, 200, response.content)
        for query in queries:
            self.assertNotIn('"file_content"', query['sql'])
            self.assertNotIn('"input_file_content"', query['sql'])
        return json.loads(response.content)

    def test_837_list_reads_metadata_and_excludes_other_tenants(self):
        other = Client.objects.create(name='Other', client_code='OTHER', email='other@example.com')
        EDI837File.objects.create(client=other, original_filename='private.837', stored_filename='private.837',
                                 file_content='secret', file_hash='other')
        data = self.response_without_content_queries(edi837_files)
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['results'][0]['file_name'], 'a.837')

    def test_mir_history_keeps_source_filename_without_source_contents(self):
        data = self.response_without_content_queries(history)
        self.assertEqual(data['files'][0]['source_filename'], 'a.835')
        self.assertEqual(data['files'][0]['filename'], 'a.mir')

    def test_dashboard_lists_all_four_streams_without_contents(self):
        data = self.response_without_content_queries(dashboard_files, {'date': timezone.localdate().isoformat()})
        for kind in ('835', '837', 'MIR', 'RECON'):
            self.assertEqual(data['groups'][kind]['count'], 1)
            self.assertEqual(len(data['groups'][kind]['files']), 1)

    def test_recon_remains_metadata_only_and_preserves_findings(self):
        data = self.response_without_content_queries(recon_files, {'page': 1, 'page_size': 25})
        self.assertEqual(data['files'][0]['original_filename'], 'a.recon')
        self.assertIn('parsing_findings', data['files'][0])

    def test_metrics_use_one_scoped_query_and_keep_counts(self):
        self.source.status = 'ARCHIVED'
        self.source.claims_count = 12
        self.source.present_in_archive_folder = True
        self.source.save()
        EDI835File.objects.create(client=self.tenant, original_filename='error.835', status='ERROR')
        EDI835File.objects.create(client=self.tenant, original_filename='pending.835', status='PROCESSING')
        other = Client.objects.create(name='Other', client_code='OTHER', email='other@example.com')
        EDI835File.objects.create(client=other, original_filename='other.835', status='ARCHIVED', claims_count=999)
        request = self.factory.get('/')
        request.user = self.user
        # Resolve the tenant before counting the metrics queries.
        _ = request.user.client
        with self.assertNumQueries(1):
            response = api_get_metrics(request)
        data = json.loads(response.content)
        self.assertEqual(data['total_claims_converted_today'], 12)
        self.assertEqual(data['converted_today_file_count'], 1)
        self.assertEqual(data['conversion_sets_count'], 3)
        self.assertEqual(data['val_failed_count'], 1)
        self.assertEqual(data['validated_sets_count'], 2)
        self.assertEqual(data['waiting_failed_count'], 2)

    def test_metrics_keep_historical_fallback(self):
        from datetime import timedelta
        EDI835File.objects.filter(pk=self.source.pk).update(
            status='COMPLETED', claims_count=7, uploaded_at=timezone.now()-timedelta(days=3))
        data = self.response_without_content_queries(api_get_metrics)
        self.assertEqual(data['total_claims_converted_today'], 7)
        self.assertEqual(data['converted_today_file_count'], 1)
