import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from edi835.models import MPLClaimAnalysis
from edi835.mpl_notices import claim_email_context, notice_email_body


SYSTEM_PROMPT = (
    "/no_think\nAnalyze one MPL healthcare claim using only the supplied normalized "
    "Microsoft Graph message and verified 837, 835, MIR, and reconciliation evidence. "
    "Never invent claims, files, facts, or corrective actions. Return JSON only."
)


class Command(BaseCommand):
    help = "Export human-approved MPL claim analyses as supervised fine-tuning JSONL."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True)
        parser.add_argument("--client-id")
        parser.add_argument(
            "--include-identifiers",
            action="store_true",
            help="Retain claim numbers and email addresses. Default output is de-identified.",
        )

    def handle(self, *args, **options):
        queryset = MPLClaimAnalysis.objects.filter(
            review_status="APPROVED"
        ).select_related(
            "notice_claim__notice__client",
            "notice_claim__claim",
        ).order_by("created_at")
        if options.get("client_id"):
            queryset = queryset.filter(
                notice_claim__notice__client_id=options["client_id"]
            )

        output_path = Path(options["output"]).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        exported = 0
        with output_path.open("w", encoding="utf-8") as stream:
            for analysis in queryset.iterator():
                link = analysis.notice_claim
                notice = link.notice
                claim = link.claim
                claim_number = claim.claim_control_number
                graph_message = dict(notice.normalized_email or {})
                graph_message["body"] = {
                    "contentType": "text",
                    "content": claim_email_context(notice_email_body(notice), claim_number),
                }
                evidence = {
                    "normalized_message": graph_message,
                    "claim_number": claim_number,
                    "timeline": analysis.timeline,
                    "verified_findings": analysis.findings,
                    "related_files": analysis.related_files,
                }
                expected = {
                    "claim_number": claim_number,
                    "summary": analysis.summary,
                    "primary_issue_code": analysis.primary_issue_code,
                    "needs_response": analysis.needs_response,
                    "recommended_actions": analysis.recommended_actions,
                    "confidence": float(analysis.confidence),
                    "requires_human_review": True,
                }
                record = {
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)},
                        {"role": "assistant", "content": json.dumps(expected, ensure_ascii=False)},
                    ],
                    "metadata": {
                        "schema_version": "mpl-sft-v1",
                        "source_format": "microsoft-graph-message",
                        "review_status": "APPROVED",
                    },
                }
                if not options["include_identifiers"]:
                    record = self._deidentify(record, claim_number, exported + 1)
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                exported += 1

        if not exported:
            raise CommandError(
                "No APPROVED MPL claim analyses were found; nothing was exported."
            )
        self.stdout.write(self.style.SUCCESS(
            f"Exported {exported} approved training examples to {output_path}"
        ))

    @staticmethod
    def _deidentify(record, claim_number, sequence):
        text = json.dumps(record, ensure_ascii=False)
        replacement = f"900000000000{sequence:05d}"
        if claim_number:
            text = text.replace(claim_number, replacement)
        text = re.sub(
            r"(?i)(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-z]{2,}(?![\w.-])",
            "<EMAIL_ADDRESS>",
            text,
        )
        return json.loads(text)
