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
]
