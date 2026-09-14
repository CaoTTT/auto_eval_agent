"""OOXML presentation for the comparison statistics worksheet.

Extends the application's existing XLSX writer, including its WPS images. Numeric
rates stay numeric fractions with an Excel percentage format, not text strings.
"""
from __future__ import annotations

import math
import unicodedata
from typing import Any, Callable

from .compare_statistics import StatisticsTable


def statistics_cell_styles() -> str:
    """Six styles appended after the pre-existing regular/image styles."""
    specs = ((0, 2, 2, "left"), (0, 1, 3, "left"), (0, 0, 0, "left"),
             (3, 0, 0, "right"), (2, 0, 0, "right"), (10, 0, 0, "right"))
    return "".join(
        f'<xf numFmtId="{fmt}" fontId="{font}" fillId="{fill}" borderId="0" xfId="0" '
        'applyNumberFormat="1" applyAlignment="1" applyFill="1">'
        f'<alignment horizontal="{align}" vertical="center" wrapText="1"/></xf>'
        for fmt, font, fill, align in specs
    )


def statistics_sheet_xml(tables: list[StatisticsTable], *, style_start: int,
                         escape_text: Callable[[Any], str], column_name: Callable[[int], str]) -> str:
    width = max((len(table.headers) for table in tables), default=2)
    right = column_name(width)
    rows, merges = [], []
    row_number = 0
    styles = {"text": style_start + 2, "count": style_start + 3,
              "decimal": style_start + 4, "percent": style_start + 5}
    column_widths = [34 if i == 2 else (46 if i == width else (22 if i == 1 else 16))
                     for i in range(1, width + 1)]

    def add(values: list, formats: tuple[str, ...] = (), *, style: int | None = None,
            merge_last: bool = False, height: int = 30) -> None:
        nonlocal row_number
        row_number += 1
        cells = []
        for index, value in enumerate(values, 1):
            ref = f"{column_name(index)}{row_number}"
            fmt = formats[index - 1] if index <= len(formats) else "text"
            selected = style if style is not None else styles.get(fmt, styles["text"])
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                cells.append(f'<c r="{ref}" s="{selected}"><v>{value}</v></c>')
            else:
                text = "—" if value is None else value
                available = (sum(column_widths[index - 1:]) if merge_last and index == len(values)
                             else column_widths[index - 1]) - 2
                display_width = sum(2 if unicodedata.east_asian_width(c) in ("F", "W") else 1 for c in str(text))
                height = max(height, min(180, math.ceil(display_width / max(1, available)) * 15 + 8))
                cells.append(f'<c r="{ref}" s="{selected}" t="inlineStr"><is><t xml:space="preserve">{escape_text(text)}</t></is></c>')
        if merge_last and values and len(values) < width:
            merges.append(f'<mergeCell ref="{column_name(len(values))}{row_number}:{right}{row_number}"/>')
        rows.append(f'<row r="{row_number}" ht="{height}" customHeight="1">{"".join(cells)}</row>')

    for table in tables:
        if row_number:
            add([], height=12)
        add([table.title], style=style_start, merge_last=True, height=28)
        last_is_text = not table.formats or table.formats[-1] == "text"
        add(list(table.headers), style=style_start + 1, merge_last=last_is_text, height=34)
        for values in table.rows:
            # Wrap notes instead of shrinking the font or clipping definitions.
            note = str(values[-1] or "") if values and last_is_text else ""
            note_capacity = max(18, (width - len(values)) * 10 + 18)
            lines = math.ceil(len(note) / note_capacity)
            add(values, table.formats, merge_last=last_is_text, height=max(30, min(150, lines * 16 + 8)))

    cols = "".join(
        f'<col min="{index}" max="{index}" width="{column_widths[index - 1]}" customWidth="1"/>'
        for index in range(1, width + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{right}{row_number}"/>'
        '<sheetViews><sheetView workbookViewId="0" showGridLines="0">'
        '<pane xSplit="2" ySplit="1" topLeftCell="C2" activePane="bottomRight" state="frozen"/>'
        '<selection pane="bottomRight" activeCell="C2" sqref="C2"/>'
        '</sheetView></sheetViews>'
        f'<cols>{cols}</cols><sheetData>{"".join(rows)}</sheetData>'
        f'<mergeCells count="{len(merges)}">{"".join(merges)}</mergeCells>'
        '<pageMargins left="0.25" right="0.25" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>'
        '<pageSetup orientation="landscape" paperSize="9"/>'
        '</worksheet>'
    )
