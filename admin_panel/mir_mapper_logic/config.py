"""Central configuration for the 835 -> MIR converter.

Business constants and MIR defaults belong here.  Keep parsing/generation logic
free of literal business values so format changes can be made in one place.
"""
import os
import ipaddress
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value

APP_TITLE = "835 to MIR Converter"
APP_HOST = os.getenv("MIR_HOST", "127.0.0.1")
APP_PORT = _env_int("MIR_PORT", 8000)

# MIR record structure
MIR_HEADER_LENGTH = 334
MIR_SERVICE_BLOCK_LENGTH = 303
MAX_SERVICE_LINES_PER_RECORD = 50
MAX_RECORD_SEQUENCE = 99
SERVICE_OVERFLOW_MODE = "split"  # "split" per spec; change to "truncate" to mimic capped samples

# Fixed-width padding
BLANK_CHAR = " "

PRIMARY_REASON_LENGTH = 5


# X12 835 segment / qualifier constants
X12_SEGMENT_CLP = "CLP"
X12_SEGMENT_NM1 = "NM1"
X12_SEGMENT_REF = "REF"
X12_SEGMENT_DTM = "DTM"
X12_SEGMENT_SVC = "SVC"
X12_SEGMENT_CAS = "CAS"
X12_ENTITY_PATIENT = "QC"
X12_ENTITY_SUBSCRIBER = "IL"
X12_MEMBER_ID_QUALIFIER = "MI"
X12_GROUP_REF_QUALIFIER = "1L"
X12_DOB_QUALIFIER = "036"
X12_CLAIM_RECEIVED_DATE_QUALIFIER = "050"
X12_SERVICE_DATE_QUALIFIER = "472"
X12_CONTRACTUAL_GROUP = "CO"
X12_PATIENT_RESP_GROUP = "PR"
STANDARD_CONTRACTUAL_PRICING_REASON = "45"
ORDINARY_PATIENT_RESPONSIBILITY_REASONS = {"1", "2", "3"}

# 835 mapping behavior
PAID_CLAIM_STATUS = "1"

# MIR process-date behavior. The 16-character date area is two adjacent
# YYYYMMDD fields (positions 37-44 and 45-52). Both are populated from the
# date the converter processes the file, not from an 835 DTM segment.
USE_PROCESS_DATE_FOR_HEADER_DATES = True
PROCESS_DATE_FORMAT = "%Y%m%d"

# Numeric behavior
AMOUNT_DECIMAL_PLACES = 2
SIGNED_AMOUNT_DIGITS = 10
ALLOW_NEGATIVE_DERIVED_AMOUNTS = False

# Payment reduction slots in the MIR service block.  The reference layout maps
# PR1..PR10 by reason number to slots 1..10.
PAYMENT_REDUCTION_MIN_REASON = 1
PAYMENT_REDUCTION_MAX_REASON = 10
PAYMENT_REDUCTION_CODE_PREFIX = X12_PATIENT_RESP_GROUP

# Web UI
MAX_UPLOAD_BYTES = _env_int("MIR_MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
UPLOAD_CHUNK_BYTES = _env_int("MIR_UPLOAD_CHUNK_BYTES", 1024 * 1024)
MULTIPART_OVERHEAD_BYTES = _env_int("MIR_MULTIPART_OVERHEAD_BYTES", 1024 * 1024)
MAX_MAPPING_BODY_BYTES = _env_int("MIR_MAX_MAPPING_BODY_BYTES", 1024 * 1024)
MAX_CLAIMS = _env_int("MIR_MAX_CLAIMS", 100_000)
MAX_SERVICE_LINES = _env_int("MIR_MAX_SERVICE_LINES", 500_000)
MAX_X12_SEGMENTS = _env_int("MIR_MAX_X12_SEGMENTS", 2_000_000)
MAX_X12_SEGMENT_LENGTH = _env_int("MIR_MAX_X12_SEGMENT_LENGTH", 16_384)
MAX_OUTPUT_BYTES = _env_int("MIR_MAX_OUTPUT_BYTES", 512 * 1024 * 1024)
MAX_CONCURRENT_CONVERSIONS = _env_int("MIR_MAX_CONCURRENT_CONVERSIONS", 2)
CONVERSION_QUEUE_TIMEOUT_SECONDS = _env_int("MIR_CONVERSION_QUEUE_TIMEOUT_SECONDS", 5)
OUTPUT_EXTENSION = ".mir"
DOWNLOAD_TOKEN_LENGTH = 24
DOWNLOAD_TOKEN_TTL_SECONDS = _env_int("MIR_DOWNLOAD_TTL_SECONDS", 3600)
OUTPUT_RETENTION_SECONDS = _env_int("MIR_OUTPUT_RETENTION_SECONDS", 24 * 3600)
CLEANUP_INTERVAL_SECONDS = _env_int("MIR_CLEANUP_INTERVAL_SECONDS", 15 * 60)

# Operational storage can be moved to a persistent/shared volume in deployments.
DATA_DIR = Path(os.getenv("MIR_DATA_DIR", str(BASE_DIR / "data"))).expanduser().resolve()
GENERATED_DIR = Path(os.getenv("MIR_GENERATED_DIR", str(BASE_DIR / "generated"))).expanduser().resolve()
MAPPING_CONFIG_PATH = DATA_DIR / "mapping_config.json"
DOWNLOAD_SIGNING_KEY = os.getenv("MIR_DOWNLOAD_SIGNING_KEY", "")

# "local" permits loopback clients and rejects remote clients. "required"
# requires HTTP Basic credentials for every request, including local requests.
AUTH_MODE = os.getenv("MIR_AUTH_MODE", "local").strip().lower()
AUTH_USERNAME = os.getenv("MIR_AUTH_USERNAME", "")
AUTH_PASSWORD = os.getenv("MIR_AUTH_PASSWORD", "")
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("MIR_ALLOWED_HOSTS", f"127.0.0.1,localhost,testserver,{APP_HOST}").split(",")
    if host.strip()
]

