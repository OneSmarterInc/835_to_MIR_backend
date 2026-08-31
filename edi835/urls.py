from django.urls import path
from .views import (
    api_process_tracked_file,
    tracked_files_list,
    api_get_metrics,
    api_archive_files_list,
    api_get_sftp_config,
    api_save_sftp_config,
    api_delete_sftp_config,
    api_sftp_connect,
    api_verify_sftp_paths,
    api_push_to_sftp,
    api_browse_sftp,
    api_start_batch_conversion,
)

from converter.views import api_download_archive_zip
from .recon_views import (
    recon_detail, recon_download, recon_files, recon_process, recon_upload,
    reconciliation_claim_detail, reconciliation_results, sftp_837_files, sftp_837_ingest,
)
from project835.drf_compat import authenticated_api

api_process_tracked_file = authenticated_api(api_process_tracked_file)
tracked_files_list = authenticated_api(tracked_files_list)
api_get_metrics = authenticated_api(api_get_metrics)
api_archive_files_list = authenticated_api(api_archive_files_list)
api_get_sftp_config = authenticated_api(api_get_sftp_config)
api_save_sftp_config = authenticated_api(api_save_sftp_config)
api_delete_sftp_config = authenticated_api(api_delete_sftp_config)
api_sftp_connect = authenticated_api(api_sftp_connect)
api_verify_sftp_paths = authenticated_api(api_verify_sftp_paths)
api_push_to_sftp = authenticated_api(api_push_to_sftp)
api_browse_sftp = authenticated_api(api_browse_sftp)
api_start_batch_conversion = authenticated_api(api_start_batch_conversion)
api_download_archive_zip = authenticated_api(api_download_archive_zip)
recon_files = authenticated_api(recon_files)
recon_download = authenticated_api(recon_download)
recon_upload = authenticated_api(recon_upload)
recon_process = authenticated_api(recon_process)
recon_detail = authenticated_api(recon_detail)
reconciliation_results = authenticated_api(reconciliation_results)
reconciliation_claim_detail = authenticated_api(reconciliation_claim_detail)
sftp_837_files = authenticated_api(sftp_837_files)
sftp_837_ingest = authenticated_api(sftp_837_ingest)

urlpatterns = [
    path("api/process/", api_process_tracked_file, name="edi835_api_process"),
    path("api/tracked-files/", tracked_files_list, name="edi835_tracked_files"),
    path("api/metrics/", api_get_metrics, name="edi835_api_metrics"),
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
    path("api/recon/files/", recon_files, name="recon_files"),
    path("api/recon/files/<uuid:file_id>/download/", recon_download, name="recon_download"),
    path("api/recon/upload/", recon_upload, name="recon_upload"),
    path("api/recon/files/<uuid:file_id>/process/", recon_process, name="recon_process"),
    path("api/recon/files/<uuid:file_id>/", recon_detail, name="recon_detail"),
    path("api/reconciliation/", reconciliation_results, name="reconciliation_results"),
    path("api/sftp/837-files/", sftp_837_files, name="sftp_837_files"),
    path("api/sftp/837-ingest/", sftp_837_ingest, name="sftp_837_ingest"),
    path("api/reconciliation/claims/<int:claim_id>/", reconciliation_claim_detail, name="reconciliation_claim_detail"),
]
