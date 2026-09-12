from django.urls import path
from .views import (
    api_process_tracked_file,
    api_get_metrics,
    api_archive_files_list,
    api_get_sftp_config,
    api_save_sftp_config,
    api_delete_sftp_config,
    api_sftp_connect,
    api_verify_sftp_paths,
    api_push_to_sftp,
)
from .tracked_files_eastern import tracked_files_list_eastern
from .tracked_file_details import tracked_file_details, conversion_hold_files
from .sftp_browse_admin_routes import api_browse_sftp_admin_routes
from .batch_test_837_v3 import api_start_batch_conversion_with_837
from .checks_catalog import api_checks_catalog
from .held_release_views import api_held_release_history

from converter.views import api_download_archive_zip
from .recon_views import (
    recon_detail, recon_download, recon_files, recon_process, recon_upload,
    reconciliation_claim_detail, reconciliation_export, reconciliation_results, sftp_837_files, sftp_837_ingest,
    reconciliation_file_dashboard, reconciliation_file_export,
    reconciliation_dashboard,
    reconciliation_review_action,
)
from project835.drf_compat import authenticated_api
from .sftp_automation_views import sftp_automation
from .edi837_views import (
    edi837_claim_detail, edi837_claim_export, edi837_search,
    edi837_upload_process,
)
from .edi837_files_v2 import edi837_files
from .edi837_naming_views import edi837_claim_push_sftp_named
from .edi837_search_transfer import edi837_sftp_transfer_for_search
from .mpl_views import (
    mpl_notice_analyze, mpl_notice_detail, mpl_notice_process_now,
    mpl_analysis_review, mpl_notice_select_claim, mpl_notice_source_file, mpl_notices, mpl_related_file,
)

api_process_tracked_file = authenticated_api(api_process_tracked_file)
tracked_files_list = authenticated_api(tracked_files_list_eastern)
tracked_file_details = authenticated_api(tracked_file_details)
conversion_hold_files = authenticated_api(conversion_hold_files)
api_get_metrics = authenticated_api(api_get_metrics)
api_archive_files_list = authenticated_api(api_archive_files_list)
api_get_sftp_config = authenticated_api(api_get_sftp_config)
api_save_sftp_config = authenticated_api(api_save_sftp_config)
api_delete_sftp_config = authenticated_api(api_delete_sftp_config)
api_sftp_connect = authenticated_api(api_sftp_connect)
api_verify_sftp_paths = authenticated_api(api_verify_sftp_paths)
api_push_to_sftp = authenticated_api(api_push_to_sftp)
api_browse_sftp = authenticated_api(api_browse_sftp_admin_routes)
api_start_batch_conversion = authenticated_api(api_start_batch_conversion_with_837)
api_checks_catalog = authenticated_api(api_checks_catalog)
api_held_release_history = authenticated_api(api_held_release_history)
api_download_archive_zip = authenticated_api(api_download_archive_zip)
# RECON views already apply authenticated_api_required and tenant scoping.
# Leave them as native Django views so standard client sessions remain intact.
sftp_837_files = authenticated_api(sftp_837_files)
sftp_837_ingest = authenticated_api(sftp_837_ingest)
sftp_automation = authenticated_api(sftp_automation)
# These views already enforce portal authentication themselves. Keeping them
# as native Django views preserves the authenticated Django session used by
# client accounts; wrapping them a second time in DRF can replace that user
# with AnonymousUser before the view-level authorization runs.
edi837_sftp_transfer = edi837_sftp_transfer_for_search
edi837_claim_push_sftp = edi837_claim_push_sftp_named
mpl_notices = authenticated_api(mpl_notices)
mpl_notice_detail = authenticated_api(mpl_notice_detail)
mpl_notice_analyze = authenticated_api(mpl_notice_analyze)
mpl_notice_select_claim = authenticated_api(mpl_notice_select_claim)
mpl_notice_process_now = authenticated_api(mpl_notice_process_now)
mpl_notice_source_file = authenticated_api(mpl_notice_source_file)
mpl_related_file = authenticated_api(mpl_related_file)
mpl_analysis_review = authenticated_api(mpl_analysis_review)

urlpatterns = [
    path("api/mpl-notices/", mpl_notices, name="mpl_notices"),
    path("api/mpl-notices/<uuid:notice_id>/", mpl_notice_detail, name="mpl_notice_detail"),
    path("api/mpl-notices/<uuid:notice_id>/analyze/", mpl_notice_analyze, name="mpl_notice_analyze"),
    path("api/mpl-notices/<uuid:notice_id>/select-claim/", mpl_notice_select_claim, name="mpl_notice_select_claim"),
    path("api/mpl-notices/<uuid:notice_id>/process-now/", mpl_notice_process_now, name="mpl_notice_process_now"),
    path("api/mpl-notices/<uuid:notice_id>/source-file/", mpl_notice_source_file, name="mpl_notice_source_file"),
    path("api/mpl-files/<str:file_type>/<uuid:file_id>/download/", mpl_related_file, name="mpl_related_file"),
    path("api/mpl-notices/<uuid:notice_id>/claims/<int:claim_id>/review/", mpl_analysis_review, name="mpl_analysis_review"),
    path("api/process/", api_process_tracked_file, name="edi835_api_process"),
    path("api/tracked-files/", tracked_files_list, name="edi835_tracked_files"),
    path("api/tracked-files/<uuid:file_id>/details/", tracked_file_details, name="edi835_tracked_file_details"),
    path("api/checks/conversion-holds/", conversion_hold_files, name="edi835_conversion_hold_files"),
    path("api/metrics/", api_get_metrics, name="edi835_api_metrics"),
    path("api/checks/catalog/", api_checks_catalog, name="edi835_checks_catalog"),
    path("api/checks/held-releases/", api_held_release_history, name="edi835_held_release_history"),
    path("api/archive-files/", api_archive_files_list, name="edi835_archive_files"),
    path("api/download-zip/", api_download_archive_zip, name="edi835_api_download_zip"),
    path("api/sftp/get/", api_get_sftp_config, name="api_get_sftp_config"),
    path("api/sftp/save/", api_save_sftp_config, name="api_save_sftp_config"),
    path("api/sftp/connect", api_sftp_connect, name="api_sftp_connect_root"),
    path("api/sftp/connect/", api_sftp_connect, name="api_sftp_connect"),
    path("api/sftp/verify-paths/", api_verify_sftp_paths, name="api_verify_sftp_paths"),
    path("api/sftp/push/", api_push_to_sftp, name="api_push_to_sftp"),
    path("api/sftp/delete/", api_delete_sftp_config, name="api_delete_sftp_config"),
    path("api/sftp/browse/", api_browse_sftp, name="api_browse_sftp"),
    path("api/start-batch-conversion/", api_start_batch_conversion, name="edi835_api_start_batch_conversion"),
    path("api/admin/sftp-automation/", sftp_automation, name="sftp_automation"),
    path("api/recon/files/", recon_files, name="recon_files"),
    path("api/recon/files/<uuid:file_id>/download/", recon_download, name="recon_download"),
    path("api/recon/upload/", recon_upload, name="recon_upload"),
    path("api/recon/files/<uuid:file_id>/process/", recon_process, name="recon_process"),
    path("api/recon/files/<uuid:file_id>/", recon_detail, name="recon_detail"),
    path("api/reconciliation/", reconciliation_results, name="reconciliation_results"),
    path("api/reconciliation/export/", reconciliation_export, name="reconciliation_export"),
    path("api/reconciliation/dashboard/", reconciliation_dashboard, name="reconciliation_dashboard"),
    path("api/reconciliation/actions/", reconciliation_review_action, name="reconciliation_review_action"),
    path("api/reconciliation/files/<uuid:file_id>/", reconciliation_file_dashboard, name="reconciliation_file_dashboard"),
    path("api/reconciliation/files/<uuid:file_id>/export/", reconciliation_file_export, name="reconciliation_file_export"),
    path("api/sftp/837-files/", sftp_837_files, name="sftp_837_files"),
    path("api/sftp/837-ingest/", sftp_837_ingest, name="sftp_837_ingest"),
    path("api/837/upload-process/", edi837_upload_process, name="edi837_upload_process"),
    path("api/837/sftp-rename/", edi837_sftp_transfer, name="edi837_sftp_transfer"),
    path("api/837/search/", edi837_search, name="edi837_search"),
    path("api/837/files/", edi837_files, name="edi837_files"),
    path("api/837/claims/<int:claim_id>/", edi837_claim_detail, name="edi837_claim_detail"),
    path("api/837/claims/<int:claim_id>/export/", edi837_claim_export, name="edi837_claim_export"),
    path("api/837/claims/<int:claim_id>/push-sftp/", edi837_claim_push_sftp, name="edi837_claim_push_sftp"),
    path("api/reconciliation/claims/<int:claim_id>/", reconciliation_claim_detail, name="reconciliation_claim_detail"),
]
