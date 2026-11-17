from datetime import date, datetime
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter


def _safe_cell(value):
    # Prevent spreadsheet formula injection for user-controlled text.
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def build_xlsx(rows, columns, sheet_name="数据"):
    """Generic XLSX writer. Business querying and column selection stay elsewhere."""
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title=(sheet_name or "数据")[:31])
    headers = []
    for col in columns:
        cell = WriteOnlyCell(ws, value=col["title"])
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="365F91")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        headers.append(cell)
    ws.append(headers)
    for idx, col in enumerate(columns, 1):
        width = col.get("width", 16)
        ws.column_dimensions[get_column_letter(idx)].width = width

    row_count = 1
    for row in rows:
        values = []
        for col in columns:
            value = row.get(col["key"]) if isinstance(row, dict) else getattr(row, col["key"], None)
            if isinstance(value, Decimal):
                value = float(value)
            cell = WriteOnlyCell(ws, value=_safe_cell(value))
            if isinstance(value, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm:ss"
            elif isinstance(value, date):
                cell.number_format = "yyyy-mm-dd"
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                if any(token in col["key"] for token in ("cost", "price", "amount", "profit")):
                    cell.number_format = '#,##0.00;[Red]-#,##0.00'
                else:
                    cell.number_format = '#,##0.######;[Red]-#,##0.######'
            values.append(cell)
        ws.append(values)
        row_count += 1

    ws.auto_filter.ref = f"A1:{get_column_letter(max(len(columns), 1))}{row_count}"

    # Write-only worksheets do not support styling existing rows after append.
    # Header styling is intentionally kept minimal for streaming scalability.
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output
