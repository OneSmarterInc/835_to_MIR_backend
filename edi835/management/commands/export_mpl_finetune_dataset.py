import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from edi835.models import MPLClaimAnalysis


SYSTEM_PROMPT = "/no_think\nExplain only supplied healthcare-claim evidence. Return valid JSON. Never invent facts, files, issue codes, or actions and never guarantee payer approval."


def scrub(value):
    text = str(value or "")
    text = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[EMAIL]", text, flags=re.I)
    text = re.sub(r"\b\d{3}-?\d{2}-?\d{4}\b", "[IDENTIFIER]", text)
    text = re.sub(r"\b(?:member|patient)\s*(?:id|number|#)?\s*[:#=-]?\s*[A-Z0-9_-]{4,}\b", "member_id=[MASKED]", text, flags=re.I)
    return text


class Command(BaseCommand):
    help = "Export human-approved, de-identified MPL examples as chat JSONL for fine-tuning."

    def add_arguments(self, parser):
        parser.add_argument("output")
        parser.add_argument("--minimum", type=int, default=50)
        parser.add_argument("--allow-small", action="store_true")

    def handle(self, *args, **options):
        output = Path(options["output"]).expanduser().resolve()
        if output.exists():
            raise CommandError(f"Refusing to overwrite existing file: {output}")
        analyses = MPLClaimAnalysis.objects.filter(review_status="APPROVED").select_related("notice_claim__notice", "notice_claim__claim")
        if analyses.count() < options["minimum"] and not options["allow_small"]:
            raise CommandError(f"Only {analyses.count()} approved examples exist; at least {options['minimum']} are required.")
        output.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with output.open("x", encoding="utf-8") as handle:
            for analysis in analyses.iterator():
                notice, claim = analysis.notice_claim.notice, analysis.notice_claim.claim
                evidence = {
                    "email": {"program": notice.program, "period_start": str(notice.reporting_period_start), "period_end": str(notice.reporting_period_end), "latest_message": scrub(notice.latest_message_body)},
                    "claim": {"claim_number": "[CLAIM]", "status": "under_review"},
                    "timeline": analysis.timeline, "verified_findings": analysis.findings,
                    "approved_actions": [{"number": index + 1, "text": action} for index, action in enumerate(analysis.recommended_actions)],
                }
                target = analysis.raw_model_output or {"summary": analysis.summary, "primary_issue_code": analysis.primary_issue_code, "needs_response": analysis.needs_response, "recommended_actions": [{"catalogue_action_number": index + 1, "explanation": action} for index, action in enumerate(analysis.recommended_actions)], "confidence": float(analysis.confidence), "requires_human_review": True}
                record = {"messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": json.dumps(evidence, separators=(",", ":"))}, {"role": "assistant", "content": json.dumps(target, separators=(",", ":"))}], "metadata": {"schema_version": analysis.schema_version, "prompt_version": analysis.prompt_version}}
                handle.write(json.dumps(record) + "\n")
                count += 1
        self.stdout.write(self.style.SUCCESS(f"Exported {count} approved, de-identified examples to {output}"))
