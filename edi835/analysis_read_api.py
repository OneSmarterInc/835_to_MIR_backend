"""Read endpoints for paged Analysis comparisons and on-demand reports."""
import csv

from django.http import JsonResponse, StreamingHttpResponse

from .analysis_queries import (CsvBuffer, claim_detail, comparison_csv, comparison_page,
                               file_counts, query_options, safe_csv)
from .models import AnalysisUpload


def analysis_read(request, client):
    view = request.GET.get("view")
    if view in {"comparison", "export"}:
        options = query_options(request.GET)
        if view == "comparison":
            return JsonResponse({"success": True, **comparison_page(client, options)})
        if options["category"] == "auto":
            options["category"] = "both" if file_counts(client)["835"] else "837-only"
        response = StreamingHttpResponse(comparison_csv(client, options), content_type="text/csv; charset=utf-8")
        suffix = {"both": "mismatch-amount", "matched": "matched-amount", "837-only": "missing-in-835"}[options["category"]]
        response["Content-Disposition"] = f'attachment; filename="filex-analysis_{suffix}.csv"'
        return response
    if view == "claim":
        number = request.GET.get("claim", "").strip()
        if not number or len(number) > 255:
            raise ValueError("Invalid claim number.")
        row = claim_detail(client, number)
        return JsonResponse({"success": True, "row": row} if row else
                            {"success": False, "error": "This claim is not available in your Analysis files."}, status=200 if row else 404)
    if view in {"rejections", "rejection-export"}:
        identifier = request.GET.get("upload", "")
        if not identifier.isascii() or not identifier.isdecimal() or len(identifier) > 18:
            raise ValueError("Invalid upload.")
        upload = AnalysisUpload.objects.filter(client=client, pk=int(identifier)).only("id", "rejected_count").first()
        if upload is None:
            return JsonResponse({"success": False, "error": "Upload not found."}, status=404)
        rejected = upload.rejections.order_by("sequence").values("sequence", "reason")
        if view == "rejections":
            return JsonResponse({"success": True, "rejections": list(rejected[:20]), "total": upload.rejected_count})
        def lines():
            writer = csv.writer(CsvBuffer(), quoting=csv.QUOTE_ALL)
            yield "\ufeff" + writer.writerow(["Claim occurrence", "Reason"])
            for issue in rejected.iterator(chunk_size=1000):
                yield writer.writerow([issue["sequence"], safe_csv(issue["reason"])])
        response = StreamingHttpResponse(lines(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="analysis-{upload.pk}-rejections.csv"'
        return response
    raise ValueError("Unknown Analysis view.")
