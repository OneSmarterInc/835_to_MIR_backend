"""Plain, bounded-memory Excel export of all Analysis comparison categories."""
from decimal import Decimal
from tempfile import SpooledTemporaryFile
from typing import Any, BinaryIO

from django.db import connection
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from accounts.models import Client
from .analysis_queries import AMOUNTS, comparison_sql, dictionaries, summary_row
from .models import AnalysisUpload


HEADERS = (
    "Claim\n837 / 835", "Internal claim\n835 · CLP07",
    "837 billed\n837 · CLM02", "835 billed\n835 · CLP03 total",
    "Claim payment\n835 · CLP04 total",
    "Patient responsibility\n835 · CLP05 / PR total",
    "Allowed amount\n835 · Payment + responsibility totals",
    "Billed comparison\nCLM02 vs billed total",
    "Allowed comparison\nCLM02 vs allowed total",
)
SHEETS = ("Mismatch amount", "Matched amount", "Missing in 835")
CURRENCY = '"$"#,##0.00;-"$"#,##0.00'


def comparison_workbook(client: Client, options: dict[str, Any]) -> BinaryIO:
    """Export all categories from one query snapshot, ignoring UI pagination.

    Options must come from query_options. The caller owns the returned file and
    must close it after the response. Temporary output spills to disk above 8 MiB.
    """
    workbook = Workbook(write_only=True)
    output = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    header_font = Font(bold=True)
    wrapped = Alignment(vertical="top", wrap_text=True)
    sheets = []
    counts = [1, 1, 1]

    def cell(sheet, value, *, currency=False):
        result = WriteOnlyCell(sheet, value=value)
        if isinstance(value, str):
            # IDs are literal text, including leading zeroes and formula prefixes.
            result.data_type = "s"
        result.alignment = wrapped
        if currency:
            result.number_format = CURRENCY
        return result

    try:
        for title in SHEETS:
            sheet = workbook.create_sheet(title)
            sheets.append(sheet)
            sheet.freeze_panes = "C2"
            sheet.sheet_format.defaultRowHeight = 30
            sheet.row_dimensions[1].height = 46
            for index, width in enumerate((26, 24, 19, 21, 22, 27, 31, 35, 35), 1):
                sheet.column_dimensions[get_column_letter(index)].width = width
            headers = [cell(sheet, title) for title in HEADERS]
            for header in headers:
                header.font = header_font
            sheet.append(headers)

        has835 = AnalysisUpload.objects.filter(client=client, kind="835").exists()
        sql, params = comparison_sql(client, options["search"])
        # Identifiers are validated by query_options; no raw request SQL.
        order = f" ORDER BY {options['sort']} {options['direction'].upper()} NULLS LAST, claim ASC"
        with connection.cursor() as cursor:
            cursor.execute(sql + "SELECT * FROM compared" + order, params)
            while batch := cursor.fetchmany(500):
                for record in dictionaries(cursor, batch):
                    row = summary_row(record, has835)
                    matched = row["billed_status"] == row["allowed_status"] == "Match"
                    index = 2 if record["n835"] == 0 else 1 if matched else 0
                    counts[index] += 1
                    if counts[index] > 1048576:
                        raise ValueError("Too many claims for one Excel sheet. Narrow the search and retry.")
                    sheet = sheets[index]
                    reference = (f"Multiple ({row['reference_count']})" if row["reference_count"] > 1
                                 else row["internal_number"] or "—")
                    values = [cell(sheet, row["claim"]), cell(sheet, reference)]
                    for key in AMOUNTS:
                        amount = row["display_amounts"][key]
                        value = (f"Multiple ({amount['count']})" if amount["count"] > 1
                                 else Decimal(amount["value"]) if amount["value"] is not None else "—")
                        values.append(cell(sheet, value, currency=isinstance(value, Decimal)))
                    for status, difference in (("billed_status", "difference"),
                                               ("allowed_status", "allowed_difference")):
                        text = row[status]
                        if row[difference] is not None:
                            text += f"\nDifference ${Decimal(row[difference]):,.2f}"
                        values.append(cell(sheet, text))
                    sheet.append(values)
        for sheet, count in zip(sheets, counts):
            sheet.auto_filter.ref = f"A1:I{count}"
        workbook.save(output)
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
    finally:
        workbook.close()
