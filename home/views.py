import os
from pathlib import Path
from django.shortcuts import render, redirect
from django.http import HttpResponse
from django.conf import settings

def home_view(request):
    react_index = Path(settings.BASE_DIR) / "static" / "react" / "index.html"
    if react_index.exists():
        with open(react_index, "r", encoding="utf-8") as f:
            response = HttpResponse(f.read(), content_type="text/html")
            # The HTML contains hashed JavaScript/CSS asset names. It must be
            # revalidated on every portal load so a deployment cannot leave
            # users pinned to an older compiled frontend bundle.
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"
            return response
    
    # Fallback if react bundle not built yet
    return HttpResponse("React bundle building...", content_type="text/html")
