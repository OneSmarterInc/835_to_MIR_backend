"""Plain, bounded-memory Excel export of all Analysis comparison categories."""
from decimal import Decimal
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Any, BinaryIO

from django.db import connection
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.drawing.image import Image
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor
from openpyxl.drawing.xdr import XDRPositiveSize2D
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.utils.units import pixels_to_EMU

from accounts.models import Client
from .analysis_queries import AMOUNTS, comparison_sql, dictionaries, summary_row
from .models import AnalysisUpload


HEADERS = (
    "Claim\n837 / 835", "Internal claim\n835 · CLP07",
    "837 billed\n837 · CLM02", "835 billed\n835 · Latest CLP03",
    "Claim payment\n835 · CLP04 total",
    "Patient responsibility\n835 · CLP05 / PR total",
    "Allowed amount\n835 · Payment + responsibility totals",
    "Billed comparison\nCLM02 vs latest CLP03",
    "Allowed comparison\nCLM02 vs payments / allowed",
)
SHEETS = ("Mismatch amount", "Matched amount", "Missing in 835")
CURRENCY = '"$"#,##0.00;-"$"#,##0.00'


def comparison_workbook(client: Client, options: dict[str, Any]) -> BinaryIO:
    """Export all categories from one query snapshot, ignoring UI pagination.

    Options must come from query_options. The caller owns the returned file and
    must close it after the response. Temporary output spills to disk above 8 MiB.
    """
    workbook = Workbook(write_only=True)
    workbook.properties.creator = "Filex OS"
    workbook.properties.lastModifiedBy = "Filex OS"
    workbook.properties.title = "FileX Claims Reconciliation"
    workbook.properties.subject = "Analysis billed and allowed comparisons"
    workbook.properties.description = "FileX Analysis export: latest 835 billed charges and cumulative payments."
    workbook.properties.keywords = "FileX, Filex OS, Analysis, 835, 837, reconciliation"
    workbook.properties.category = "FileX Analysis"
    workbook.properties.created = workbook.properties.modified = timezone.now().replace(tzinfo=None)
    output = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    header_font = Font(bold=True)
    wrapped = Alignment(vertical="top", wrap_text=True)
    sheets = []
    counts = [3, 3, 3]

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
            sheet.freeze_panes = "C4"
            sheet.sheet_view.showGridLines = False
            sheet.sheet_format.defaultRowHeight = 44
            sheet.row_dimensions[1].height = 34
            sheet.row_dimensions[2].height = 24
            sheet.row_dimensions[3].height = 46
            for index, width in enumerate((26, 24, 19, 21, 22, 27, 31, 35, 35), 1):
                sheet.column_dimensions[get_column_letter(index)].width = width
            # Transparent portal mark and a reserved three-character logo slot.
            # Literal spacing also renders correctly in viewers that ignore indent.
            logo = Image(Path(__file__).parent / "assets" / "filex-mark.png")
            logo.anchor = OneCellAnchor(
                _from=AnchorMarker(col=0, row=0, colOff=pixels_to_EMU(2), rowOff=pixels_to_EMU(2)),
                ext=XDRPositiveSize2D(pixels_to_EMU(40), pixels_to_EMU(40)),
            )
            sheet.add_image(logo)
            wordmark = cell(sheet, "   FileX")
            wordmark.font = Font(name="Consolas", size=18, bold=True, color="172033")
            wordmark.alignment = Alignment(horizontal="left", vertical="center")
            report_title = cell(sheet, "Claims reconciliation")
            report_title.alignment = Alignment(vertical="center")
            sheet.append([wordmark, None, report_title])
            section = cell(sheet, title)
            section.font = Font(size=12, bold=True)
            sheet.append([section])
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
                        if status == "allowed_status" and row[status] == "Match" and row["allowed_basis"] == "payment":
                            text += "\nTotal payments match"
                        if row[difference] is not None:
                            text += f"\nDifference ${Decimal(row[difference]):,.2f}"
                        values.append(cell(sheet, text))
                    sheet.append(values)
        for sheet, count in zip(sheets, counts):
            sheet.auto_filter.ref = f"A3:I{count}"
        workbook.save(output)
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
    finally:
        workbook.close()
