import json
import signal
import time
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from edi835.batch_jobs import queued_jobs, recover_interrupted_jobs, write_job


class Command(BaseCommand):
    help = "Run the isolated 835 batch conversion worker."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one queued job and exit.")
        parser.add_argument("--poll-seconds", type=float, default=2.0)

    def handle(self, *args, **options):
        recovered = recover_interrupted_jobs()
        if recovered:
            self.stderr.write(f"Marked {recovered} interrupted batch job(s) as failed.")
        stopping = False

        def stop(*_args):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while not stopping:
            pending = queued_jobs()
            if pending:
                self._process(pending[0])
                if options["once"]:
                    return
                continue
            if options["once"]:
                return
            time.sleep(max(0.25, options["poll_seconds"]))

    def _process(self, job):
        job["state"] = "RUNNING"
        job["worker_started_at"] = timezone.now().isoformat()
        write_job(job)
        try:
            user = get_user_model().objects.get(id=job["owner_user_id"])
            body = json.dumps({"client_id": job.get("client_id") or ""}).encode("utf-8")
            request_context = SimpleNamespace(method="POST", body=body, user=user)
            # Import after Django has initialized and after the job is claimed.
            from edi835.views import _execute_batch_conversion
            response = _execute_batch_conversion(request_context)
            payload = json.loads(response.content.decode("utf-8"))
            job["state"] = "COMPLETED" if payload.get("success") else "FAILED"
            job["status_code"] = response.status_code
            job["result"] = payload
        except Exception as exc:
            job["state"] = "FAILED"
            job["status_code"] = 500
            job["result"] = {"success": False, "error": f"Batch worker failed: {exc}"}
        job["finished_at"] = timezone.now().isoformat()
        write_job(job)
