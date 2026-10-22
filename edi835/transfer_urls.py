from django.urls import path
from .transfer_api import openapi, transfers

urlpatterns = [
    path("", transfers),
    path("openapi.json", openapi, name="transfer-openapi"),
    path("recover", transfers, {"action": "recover"}, name="transfer-recover"),
    path("<uuid:receipt_id>", transfers, name="transfer-detail"),
    path("<uuid:receipt_id>/retry", transfers, {"action": "retry"}, name="transfer-retry"),
    path("<uuid:receipt_id>/source", transfers, {"action": "source"}, name="transfer-source"),
    path("<uuid:receipt_id>/outputs/<uuid:output_id>", transfers, {"action": "output"}, name="transfer-output"),
]
