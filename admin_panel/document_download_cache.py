"""Shared caching for generated onboarding and Go Live PDF downloads."""

import hashlib
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.http import HttpResponse
from django.utils.cache import patch_cache_control


def cached_client_pdf(client, template_name, builder):
    """Build a personalized PDF once and share it across server workers."""
    cache_material = "|".join([
        template_name,
        str(client.id),
        str(client.name or ""),
        str(client.address or ""),
        str(client.state or ""),
        str(client.zip_code or ""),
        datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
    ])
    digest = hashlib.sha256(cache_material.encode("utf-8")).hexdigest()
    key = f"admin-template-pdf:{digest}"
    pdf_bytes = cache.get(key)
    if pdf_bytes is not None:
        return pdf_bytes, digest

    cache_dir = Path(tempfile.gettempdir()) / "onesmarter-template-cache"
    cache_path = cache_dir / f"{digest}.pdf"
    try:
        if cache_path.is_file():
            pdf_bytes = cache_path.read_bytes()
        else:
            pdf_bytes = builder(client)
            cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = cache_dir / f".{digest}.{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(pdf_bytes)
            os.replace(temporary, cache_path)
    except OSError:
        if pdf_bytes is None:
            pdf_bytes = builder(client)

    cache.set(key, pdf_bytes, timeout=24 * 60 * 60)
    return pdf_bytes, digest


def pdf_download_response(pdf_bytes, download_name, digest):
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{download_name}"'
    response["X-OneSmarter-Filename"] = download_name
    response["ETag"] = f'"{digest}"'
    patch_cache_control(response, private=True, max_age=86400)
    return response
