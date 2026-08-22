"""Public conversion API used by both web UI and CLI."""
from .edi835_parser import parse_835
from .mir_generator import generate_mir_text


def convert_835_to_mir(text: str, client=None):
    claims = parse_835(text)
    if not claims:
        raise ValueError("No CLP claims were found in the uploaded 835 file.")
    mir_text, summary = generate_mir_text(claims, client)
    return mir_text, summary
