import os
from pathlib import Path
from django.shortcuts import render, redirect
from django.http import HttpResponse
from django.conf import settings

def home_view(request):
    react_index = Path(settings.BASE_DIR) / "static" / "react" / "index.html"
    if react_index.exists():
        with open(react_index, "r", encoding="utf-8") as f:
            return HttpResponse(f.read(), content_type="text/html")
    
    # Fallback if react bundle not built yet
    return HttpResponse("React bundle building...", content_type="text/html")

