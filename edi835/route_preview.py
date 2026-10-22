"""Read envelope identifiers without creating intake, source or route records."""
import io

from .partner_routing import EnvelopeError, ROUTE_FIELDS, envelope_routes

MAX_PREVIEW_BYTES = 10 * 1024 * 1024


def extract_routes(upload, workflows):
    if not upload.size or upload.size > MAX_PREVIEW_BYTES:
        raise ValueError("Choose one 835 or 837 file up to 10 MiB.")
    raw = upload.read(MAX_PREVIEW_BYTES + 1)
    if len(raw) > MAX_PREVIEW_BYTES:
        raise ValueError("Choose one 835 or 837 file up to 10 MiB.")
    for workflow in ("835", "837"):
        routes = {}
        count = 0
        try:
            with io.BufferedReader(io.BytesIO(raw)) as stream:
                for fields in envelope_routes(stream, workflow):
                    count += 1
                    routes[fields] = {"workflow": workflow, **dict(zip(ROUTE_FIELDS, fields))}
                    if len(routes) > 100:
                        raise ValueError("This file has more than 100 distinct routes. Choose a smaller sample file.")
        except EnvelopeError:
            continue
        if count:
            if workflow not in workflows:
                raise ValueError(f"This partner is not configured for {workflow} files.")
            return {"routes": list(routes.values()), "transaction_set_count": count}
    raise ValueError("Could not read a complete 835 or 837 envelope. Check the ISA, GS, ST and closing segments; use one transaction type per sample file.")
