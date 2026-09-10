"""Minimal URL configuration for isolated backend tests."""

from django.urls import path

from edi835 import mpl_views


urlpatterns = [
    path("edi835/api/mpl-notices/", mpl_views.mpl_notices),
    path("edi835/api/mpl-notices/<uuid:notice_id>/", mpl_views.mpl_notice_detail),
    path("edi835/api/mpl-notices/<uuid:notice_id>/analyze/", mpl_views.mpl_notice_analyze),
    path("edi835/api/mpl-notices/<uuid:notice_id>/select-claim/", mpl_views.mpl_notice_select_claim),
    path("edi835/api/mpl-notices/<uuid:notice_id>/process-now/", mpl_views.mpl_notice_process_now),
    path("edi835/api/mpl-notices/<uuid:notice_id>/claims/<int:claim_id>/review/", mpl_views.mpl_analysis_review),
    path("edi835/api/mpl-files/<str:file_type>/<uuid:file_id>/download/", mpl_views.mpl_related_file),
]
