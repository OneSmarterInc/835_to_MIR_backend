"""
PyX12 EDI 835 Validation Engine Service
100% PyX12 library validation integration.
Runs entirely locally using open-source PyX12 library.
"""

import io
import json
import logging
import pyx12.params
import pyx12.x12file
import pyx12.x12n_document

logger = logging.getLogger(__name__)

class PyX12Validator:

    def validate(self, edi_text):
        """
        Validate EDI 835 file content using PyX12 library.
        Passes raw EDI text directly to PyX12 without manual splitting or segment counting.
        """
        if edi_text:
            edi_text = edi_text.lstrip("\ufeff").strip()

        if not edi_text:
            return {
                "valid": False,
                "validator_engine": "Validated using PyX12",
                "status_message": "The provided EDI file is empty.",
                "total_segments": 0,
                "claims": 0,
                "errors": [{
                    "line": 0,
                    "segment": "FILE",
                    "element": None,
                    "severity": "error",
                    "code": "EMPTY_FILE",
                    "message": "The provided EDI file is empty."
                }],
                "warnings": [],
                "segment_summary": {}
            }

        # 1. Read and parse X12 structure via pyx12.x12file.X12Reader
        src_reader = io.StringIO(edi_text)
        try:
            reader = pyx12.x12file.X12Reader(src_reader)
            segments = list(reader)
        except Exception as parse_err:
            logger.error("PyX12 parsing exception: %s", parse_err)
            return {
                "valid": False,
                "validator_engine": "Validated using PyX12",
                "status_message": f"PyX12 parsing error: {str(parse_err)}",
                "total_segments": 0,
                "claims": 0,
                "errors": [{
                    "line": 0,
                    "segment": "ISA",
                    "element": None,
                    "severity": "error",
                    "code": "PYX12_PARSE_ERROR",
                    "message": f"Malformed X12 file: {str(parse_err)}"
                }],
                "warnings": [],
                "segment_summary": {}
            }

        total_segments = len(segments)
        segment_summary = {}
        claims_count = 0
        st_types = []

        for seg in segments:
            seg_id = seg.seg_id
            segment_summary[seg_id] = segment_summary.get(seg_id, 0) + 1
            if seg_id == 'CLP':
                claims_count += 1
            elif seg_id == 'ST':
                st_val = None
                try:
                    st_val = seg.get_value('ST01')
                except Exception:
                    pass
                if not st_val and len(seg.elements) > 0:
                    try:
                        st_val = seg.elements[0].get_value()
                    except Exception:
                        pass
                if st_val:
                    st_types.append(str(st_val).strip())

        # 2. Reject non-835 transactions using pyx12's parsed structure
        if st_types and any(t != '835' for t in st_types):
            non_835_type = next(t for t in st_types if t != '835')
            return {
                "valid": False,
                "validator_engine": "Validated using PyX12",
                "status_message": f"Rejected: Expected 835 transaction set, found '{non_835_type}'.",
                "total_segments": total_segments,
                "claims": claims_count,
                "errors": [{
                    "line": 1,
                    "segment": "ST",
                    "element": "ST01",
                    "severity": "error",
                    "code": "NON_835_TRANSACTION",
                    "message": f"Transaction set is '{non_835_type}'. 835 validator accepts only 835 Health Care Payment/Advice files."
                }],
                "warnings": [],
                "segment_summary": segment_summary
            }

        # 3. Validate using pyx12.x12n_document.x12n_document
        src_val = io.StringIO(edi_text)
        param = pyx12.params.params()
        fd_json = io.StringIO()

        try:
            is_valid_doc = pyx12.x12n_document.x12n_document(param, src_val, None, None, fd_json=fd_json)
        except Exception as val_err:
            logger.error("PyX12 validation exception: %s", val_err)
            return {
                "valid": False,
                "validator_engine": "Validated using PyX12",
                "status_message": f"PyX12 validation error: {str(val_err)}",
                "total_segments": total_segments,
                "claims": claims_count,
                "errors": [{
                    "line": 0,
                    "segment": "X12",
                    "element": None,
                    "severity": "error",
                    "code": "PYX12_VALIDATION_ERROR",
                    "message": f"PyX12 validation failed: {str(val_err)}"
                }],
                "warnings": [],
                "segment_summary": segment_summary
            }

        # 4. Extract errors and warnings from PyX12 JSON output
        fd_json.seek(0)
        json_raw = fd_json.read().strip()
        errors = []
        warnings = []

        if json_raw:
            try:
                tree_data = json.loads(json_raw)
                self._extract_pyx12_issues(tree_data, errors, warnings)
            except Exception as json_parse_err:
                logger.error("Failed to parse PyX12 JSON output: %s", json_parse_err)

        overall_valid = is_valid_doc and (len(errors) == 0)

        return {
            "valid": overall_valid,
            "validator_engine": "Validated using PyX12",
            "status_message": "Validated using PyX12: File is valid." if overall_valid else "Validated using PyX12: Errors found.",
            "total_segments": total_segments,
            "claims": claims_count,
            "errors": errors,
            "warnings": warnings,
            "segment_summary": segment_summary
        }

    def _extract_pyx12_issues(self, node, errors, warnings):
        """
        Recursively extract errors and warnings from PyX12 JSON validation report structure.
        """
        if isinstance(node, dict):
            cur_line = node.get("cur_line", 0)
            seg_id = node.get("seg_id") or node.get("name") or "X12"

            for err in node.get("errors", []):
                if isinstance(err, dict):
                    code = str(err.get("err_cde") or "PYX12_ERR")
                    msg = str(err.get("err_str") or "PyX12 validation error")
                    val = err.get("err_val")
                    if val:
                        msg += f" (Value: '{val}')"
                    errors.append({
                        "line": cur_line,
                        "segment": seg_id,
                        "element": None,
                        "severity": "error",
                        "code": code,
                        "message": msg
                    })

            for key, val in node.items():
                if key != "errors":
                    self._extract_pyx12_issues(val, errors, warnings)

        elif isinstance(node, list):
            for item in node:
                self._extract_pyx12_issues(item, errors, warnings)


class EDI835Validator:

    def __init__(self):
        self.engine = PyX12Validator()

    def validate(self, edi_text):
        return self.engine.validate(edi_text)
