"""Bounded envelope inspection before durable partner acceptance.

This checks every interchange/group/set, not claim identifiers or filenames.
Existing full validators and converter rules still run in the worker.
"""
from .intake_storage import source_path

ROUTE_FIELDS = ("isa_sender_qualifier", "isa_sender", "isa_receiver_qualifier",
                "isa_receiver", "isa_version", "gs_sender", "gs_receiver", "gs_version", "st_version")


class EnvelopeError(ValueError):
    def __init__(self):
        super().__init__("Partner routing requires complete, consistent X12 envelopes and an approved route for every transaction set.")


class Reader:
    def __init__(self, stream):
        self.stream, self.buffer = stream, bytearray()

    def fill(self, count):
        while len(self.buffer) < count:
            block = self.stream.read(8192)
            if not block:
                return
            self.buffer.extend(block)

    def take(self, count):
        self.fill(count)
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        if len(result) != count:
            raise EnvelopeError()
        return result

    def whitespace(self):
        while True:
            self.fill(1)
            self.buffer[:] = self.buffer.lstrip(b" \r\n\t")
            if self.buffer or not self.stream.peek(1):
                return bool(self.buffer)

    def segment(self, delimiter):
        while True:
            position = self.buffer.find(delimiter)
            if position >= 0:
                if position > 65536:
                    raise EnvelopeError()
                return self.take(position + 1)[:-1].lstrip(b"\r\n")
            if len(self.buffer) > 65536:
                raise EnvelopeError()
            previous = len(self.buffer)
            self.fill(previous + 1)
            if len(self.buffer) == previous:
                raise EnvelopeError()


def envelope_routes(stream, workflow):
    reader = Reader(stream)
    reader.fill(3)
    if reader.buffer.startswith(b"\xef\xbb\xbf"):
        reader.take(3)
    total = 0
    while reader.whitespace():
        header = reader.take(106)
        element, terminator = header[3:4], header[105:106]
        isa = header[:-1].split(element)
        if (not header.startswith(b"ISA") or len(isa) != 17 or len(isa[13]) != 9
                or element == terminator or len(isa[6]) != 15 or len(isa[8]) != 15):
            raise EnvelopeError()
        gs = st = None
        groups = sets = segments = 0
        while True:
            fields = reader.segment(terminator).split(element)
            total += 1
            if total > 200000:
                raise EnvelopeError()
            tag = fields[0]
            if st:
                segments += 1
            if tag == b"GS" and gs is None and len(fields) == 9:
                gs, sets = fields, 0
                groups += 1
            elif tag == b"ST" and gs and st is None and len(fields) in {3, 4}:
                st, segments = fields, 1
                sets += 1
                if st[1] != workflow.encode("ascii") or gs[1] != (b"HP" if workflow == "835" else b"HC"):
                    raise EnvelopeError()
                try:
                    yield tuple(value.decode("ascii").strip(" ") for value in
                                (isa[5], isa[6], isa[7], isa[8], isa[12], gs[2], gs[3], gs[8], st[3] if len(st) == 4 else b""))
                except UnicodeDecodeError as exc:
                    raise EnvelopeError() from exc
            elif tag == b"SE" and st and len(fields) == 3:
                if fields[1] != str(segments).encode() or fields[2] != st[2]:
                    raise EnvelopeError()
                st = None
            elif tag == b"GE" and gs and st is None and len(fields) == 3:
                if not sets or fields[1] != str(sets).encode() or fields[2] != gs[6]:
                    raise EnvelopeError()
                gs = None
            elif tag == b"IEA" and gs is None and st is None and len(fields) == 3:
                if not groups or fields[1] != str(groups).encode() or fields[2] != isa[13]:
                    raise EnvelopeError()
                break
            elif tag in {b"ISA", b"GS", b"ST", b"SE", b"GE", b"IEA"} or not st:
                raise EnvelopeError()


def authorize_routes(partner, workflow, source, client=None):
    if partner.auth_mode != "MTLS" and not partner.routing_required:
        return []
    routes = {}
    for route in partner.routes.filter(active=True, workflow=workflow):
        key = tuple(getattr(route, field) for field in ROUTE_FIELDS)
        if key in routes:
            raise EnvelopeError()
        routes[key] = route
    selected = {}
    with source_path(source.key).open("rb") as stream:
        for key in envelope_routes(stream, workflow):
            route = routes.get(key)
            if not route or route.client_id != (client.pk if client else partner.client_id):
                raise EnvelopeError()
            selected[route.pk] = {"route_id": route.pk, "approved_at": route.approved_at.isoformat(),
                                  "client_id": str(route.client_id), "workflow": workflow}
    if not selected:
        raise EnvelopeError()
    return list(selected.values())
