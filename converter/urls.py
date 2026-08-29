from django.urls import path
from .views import api_convert, api_validate, download_mir, api_get_file_content, api_download_archive_zip
from edi835.views import api_sftp_connect, api_start_batch_conversion
from project835.drf_compat import authenticated_api

api_convert = authenticated_api(api_convert)
api_validate = authenticated_api(api_validate)
download_mir = authenticated_api(download_mir)
api_get_file_content = authenticated_api(api_get_file_content)
api_download_archive_zip = authenticated_api(api_download_archive_zip)
api_sftp_connect = authenticated_api(api_sftp_connect)
api_start_batch_conversion = authenticated_api(api_start_batch_conversion)

urlpatterns = [
    path("api/convert/", api_convert, name="api_convert"),
    path("api/validate/", api_validate, name="api_validate"),
    path("api/start-batch-conversion/", api_start_batch_conversion, name="api_start_batch_conversion"),
    path("api/download/", download_mir, name="download_mir"),
    path("api/download-zip/", api_download_archive_zip, name="api_download_archive_zip"),
    path("api/file-content/<str:file_id>/", api_get_file_content, name="api_get_file_content"),
    path("api/sftp/connect", api_sftp_connect, name="api_sftp_connect_root_direct"),
    path("api/sftp/connect/", api_sftp_connect, name="api_sftp_connect_direct"),
]
