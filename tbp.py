#!/usr/bin/env python3
"""
TBP Asset Lookup
Usage:
    /tbp <machine_id(s)>   e.g. /tbp 1234
    /tbp tbp1234 tbp5678
    /tbp 1234,5678
"""

import re
import requests
import os
from dotenv import load_dotenv
load_dotenv()
# ================= Configuration =================
APP_ID = os.getenv("APP_ID")
APP_SECRET = os.getenv("APP_SECRET")
SPREADSHEET_TOKEN = os.getenv("TBP_SPREADSHEET_TOKEN")
SHEET_ID = os.getenv("TBP_SHEET_ID")

# Column headers as they appear in the sheet (exact match)
COLUMNS = {
    "Machine ID": "Machine ID",
    "Mini PC": "Mini PC",
    "Main Encoder": "Main Encoder",
    "Top Encoder": "Top Encoder",
    "CCTV": "CCTV",
    "Main Streaming URL": "Main Streaming URL",
    "TOP Streaming URL": "TOP Streaming URL",
    "CCTV URL": "CCTV URL"
}

# ================= API FUNCTIONS =================
def get_tenant_access_token():
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json"}
    data = {"app_id": APP_ID, "app_secret": APP_SECRET}
    resp = requests.post(url, headers=headers, json=data)
    result = resp.json()
    if result.get("code") != 0:
        raise Exception(f"Failed to get token: {result}")
    return result["tenant_access_token"]

def get_sheet_metadata(token, spreadsheet_token, sheet_id):
    url = f"https://open.larksuite.com/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/metainfo"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers)
    result = resp.json()
    if result.get("code") != 0:
        return None
    sheets = result.get("data", {}).get("sheets", [])
    for sheet in sheets:
        if sheet.get("sheetId") == sheet_id:
            return {
                "rowCount": sheet.get("rowCount"),
                "columnCount": sheet.get("columnCount")
            }
    return None

def col_index_to_letter(col_index):
    letters = ''
    while col_index > 0:
        col_index -= 1
        letters = chr(65 + (col_index % 26)) + letters
        col_index //= 26
    return letters

def get_range_values(token, spreadsheet_token, sheet_id, range_str):
    url = f"https://open.larksuite.com/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{sheet_id}!{range_str}?valueRenderOption=FormattedValue"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers)
    result = resp.json()
    if result.get("code") != 0:
        return None
    return result.get("data", {}).get("valueRange", {}).get("values", [])

def get_all_sheet_data(token, spreadsheet_token, sheet_id):
    props = get_sheet_metadata(token, spreadsheet_token, sheet_id)
    if not props:
        return None
    max_row = props.get("rowCount", 2000)
    max_col = props.get("columnCount", 200)
    end_col = col_index_to_letter(max_col)
    range_str = f"A1:{end_col}{max_row}"
    return get_range_values(token, spreadsheet_token, sheet_id, range_str)

def extract_cell_value(cell):
    if cell is None:
        return ""
    if isinstance(cell, (str, int, float)):
        return str(cell).strip()
    if isinstance(cell, list):
        parts = []
        for part in cell:
            if isinstance(part, dict):
                if "link" in part and part["link"]:
                    parts.append(part["link"])
                elif "text" in part:
                    parts.append(part["text"])
                else:
                    parts.append(str(part))
            else:
                parts.append(str(part))
        return "".join(parts).strip()
    return str(cell).strip()

def find_row_by_machine_id(data, machine_id):
    """
    Find the row where the Machine ID column matches the given ID (as a number).
    Returns a dict of column_name -> value.
    """
    if not data:
        return None

    # Locate header row (first row containing "Machine ID")
    header_row = None
    for i, row in enumerate(data):
        if not row:
            continue
        for cell in row:
            if isinstance(cell, str) and "Machine ID" in cell:
                header_row = i
                break
        if header_row is not None:
            break

    if header_row is None:
        return None

    headers = data[header_row]

    # Map columns to indices
    col_indexes = {}
    for col_name in COLUMNS.values():
        col_indexes[col_name] = None
        for idx, cell in enumerate(headers):
            if cell and isinstance(cell, str) and col_name.lower() in cell.lower():
                col_indexes[col_name] = idx
                break

    # Find Machine ID column index
    id_col = None
    for idx, cell in enumerate(headers):
        if cell and isinstance(cell, str) and "Machine ID" in cell:
            id_col = idx
            break
    if id_col is None:
        return None

    # Scan rows below header
    for row in data[header_row+1:]:
        if len(row) <= id_col:
            continue
        cell_val = extract_cell_value(row[id_col])
        # Extract numbers from cell value
        match = re.search(r'\d+', cell_val)
        if match and match.group() == str(machine_id):
            result = {}
            for col_name, idx in col_indexes.items():
                if idx is not None and len(row) > idx:
                    result[col_name] = extract_cell_value(row[idx])
                else:
                    result[col_name] = ""
            return result
    return None

def get_tbp_info(query_str):
    # Extract numbers from query (tbp prefix optional)
    numbers = re.findall(r'(?i)tbp?(\d+)', query_str)
    if not numbers:
        numbers = re.findall(r'\b(\d{4})\b', query_str)   # exactly 4 digits
    if not numbers:
        return "❌ No valid 4‑digit Machine IDs found. Use e.g., `/tbp 1234` or `/tbp tbp1234 tbp5678`"

    # Remove duplicates preserving order
    seen = set()
    numbers_unique = []
    for n in numbers:
        if n not in seen:
            seen.add(n)
            numbers_unique.append(n)

    try:
        token = get_tenant_access_token()
    except Exception as e:
        return f"❌ Failed to get access token: {e}"

    data = get_all_sheet_data(token, SPREADSHEET_TOKEN, SHEET_ID)
    if not data:
        return "❌ Failed to read sheet data"

    output_lines = []
    for machine_id in numbers_unique:
        row = find_row_by_machine_id(data, machine_id)
        if row:
            output_lines.append(f"📌 Machine : TBP{machine_id}")
            output_lines.append("")
            output_lines.append("• IP address")
            output_lines.append(f"Mini PC : {row.get('Mini PC', '')}")
            output_lines.append(f"Main Encoder : {row.get('Main Encoder', '')}")
            output_lines.append(f"Top Encoder : {row.get('Top Encoder', '')}")
            output_lines.append(f"CCTV : {row.get('CCTV', '')}")
            output_lines.append("")
            output_lines.append("• URL")
            output_lines.append(f"Main Streaming URL : {row.get('Main Streaming URL', '')}")
            output_lines.append("")
            output_lines.append(f"TOP Streaming URL : {row.get('TOP Streaming URL', '')}")
            output_lines.append("")
            output_lines.append(f"CCTV URL : {row.get('CCTV URL', '')}")
            output_lines.append("")
        else:
            output_lines.append(f"❌ Machine ID {machine_id} not found.")
            output_lines.append("")

    return "\n".join(output_lines).strip()