"""Python 3.12+ streaming partner client; no third-party dependencies.

Set FILEX_BASE_URL and either FILEX_PARTNER_TOKEN or FILEX_CLIENT_CERT +
FILEX_CLIENT_KEY. Optional FILEX_CA_BUNDLE verifies the server; never disable TLS checks.
Supply and retain --key before the first upload; reuse it after uncertainty.
"""
import argparse
import http.client
import json
import os
import ssl
import uuid
from pathlib import Path
from urllib.parse import urlsplit


class PartnerClient:
    def __init__(self, base_url: str, token: str = "", timeout: int = 60, *,
                 certificate: str | None = None, private_key: str | None = None, ca_bundle: str | None = None):
        url = urlsplit(base_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in {"", "/"}):
            raise ValueError("FILEX_BASE_URL must be an HTTPS origin without credentials or a path.")
        if bool(certificate) != bool(private_key) or bool(token) == bool(certificate):
            raise ValueError("Configure either a token OR a client certificate and private key.")
        if any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("Invalid token format.")
        self.context = ssl.create_default_context(cafile=ca_bundle)
        if certificate:
            self.context.load_cert_chain(certificate, private_key)
        self.host, self.port, self.token, self.timeout = url.hostname, url.port, token, timeout

    def request(self, path: str, *, key: str | None = None, file: Path | None = None,
                workflow: str | None = None) -> tuple[int, dict]:
        if key is not None and (not 1 <= len(key) <= 200 or any(ord(c) < 33 or ord(c) > 126 for c in key)):
            raise ValueError("Use a printable non-space submission key, at most 200 characters.")
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout,
                                                 context=self.context)
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if key:
            headers["Idempotency-Key"] = key
        try:
            if file is None:
                connection.request("GET", path, headers=headers)
            else:
                if workflow not in {"835", "837"} or not key:
                    raise ValueError("Upload requires workflow 835/837 and a retained submission key.")
                filename = file.name
                if not filename.isascii() or any(c in filename for c in '\r\n"\\'):
                    raise ValueError("Sample client requires an ASCII filename without quotes or backslashes.")
                boundary = "FileX" + uuid.uuid4().hex
                prefix = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"workflow\"\r\n\r\n{workflow}\r\n"
                          f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                          "Content-Type: application/octet-stream\r\n\r\n").encode()
                suffix = f"\r\n--{boundary}--\r\n".encode()
                with file.open("rb") as stream:
                    size = os.fstat(stream.fileno()).st_size
                    headers.update({"Content-Type": f"multipart/form-data; boundary={boundary}",
                                    "Content-Length": str(len(prefix) + size + len(suffix))})
                    connection.putrequest("POST", path)
                    for name, value in headers.items():
                        connection.putheader(name, value)
                    connection.endheaders(prefix)
                    remaining = size
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise OSError("Source file changed during upload.")
                        connection.send(chunk)
                        remaining -= len(chunk)
                    if stream.read(1):
                        raise OSError("Source file changed during upload.")
                    connection.send(suffix)
            response = connection.getresponse()
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("Unexpectedly large API response.")
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise ValueError(f"API returned HTTP {response.status} without JSON.") from None
            if not isinstance(result, dict):
                raise ValueError("Expected a JSON object.")
            return response.status, result
        finally:
            connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--file", type=Path)
    operation.add_argument("--recover", action="store_true")
    operation.add_argument("--status", type=uuid.UUID)
    parser.add_argument("--workflow", choices=["835", "837"])
    parser.add_argument("--key", help="Retain this key; never generate a new one to retry a lost response.")
    args = parser.parse_args()
    if (args.file or args.recover) and not args.key:
        parser.error("--key is required for upload/recovery")
    if args.file and not args.workflow:
        parser.error("--workflow is required for upload")
    try:
        client = PartnerClient(os.environ.get("FILEX_BASE_URL", ""), os.environ.get("FILEX_PARTNER_TOKEN", ""),
            certificate=os.environ.get("FILEX_CLIENT_CERT"), private_key=os.environ.get("FILEX_CLIENT_KEY"),
            ca_bundle=os.environ.get("FILEX_CA_BUNDLE"))
        path = "/api/v1/transfers"
        if args.recover:
            path += "/recover"
        elif args.status:
            path += f"/{args.status}"
        status, result = client.request(path, key=args.key, file=args.file, workflow=args.workflow)
        print(json.dumps({"http_status": status, **result}, indent=2))
        return 0 if 200 <= status < 300 else 1
    except (OSError, ValueError, http.client.HTTPException):
        print("Request not confirmed. Check configuration/connectivity, then use --recover with the original --key. No automatic replay was made.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
