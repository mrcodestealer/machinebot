#!/usr/bin/env python3
"""
CP (Casino Plus?) Asset Lookup
Usage:
    /cp <asset_number(s)>   e.g. /cp 12
    /cp cp01 cp02
    /cp cp01,cp02
"""

import re
import requests
import os
from dotenv import load_dotenv
load_dotenv()

# ================= Configuration =================
APP_ID = os.getenv("APP_ID")
APP_SECRET = os.getenv("APP_SECRET")

SPREADSHEET_TOKEN = os.getenv("CP_SPREADSHEET_TOKEN")
SHEET_ID = os.getenv("CP_SHEET_ID")

# Column headers as they appear in the sheet (exact match)
COLUMNS = {
    "Assets Number": "Assets Number",
    "Controller IP": "Controller IP",
    "Main Screen IP": "Main Screen IP",
    "Top screen IP": "Top screen IP",
    "CCTV camera": "CCTV camera",
    "Main streaming address": "Main streaming address",
    "Top Streaming address": "Top Streaming address",
    "CCTV address": "CCTV address"
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

def find_row_by_asset_number(data, asset_number):
    """
    Find the row where the Assets Number column matches the given number.
    Returns a dict of column_name -> value.
    """
    if not data:
        return None

    # Locate header row (first row containing "Assets Number")
    header_row = None
    for i, row in enumerate(data):
        if not row:
            continue
        for cell in row:
            if isinstance(cell, str) and "Assets Number" in cell:
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

    # Find Assets Number column index
    asset_col = None
    for idx, cell in enumerate(headers):
        if cell and isinstance(cell, str) and "Assets Number" in cell:
            asset_col = idx
            break
    if asset_col is None:
        return None

    # Scan rows below header
    for row in data[header_row+1:]:
        if len(row) <= asset_col:
            continue
        cell_val = extract_cell_value(row[asset_col])
        # Extract numbers from cell value
        match = re.search(r'\d+', cell_val)
        if match and match.group() == str(asset_number):
            result = {}
            for col_name, idx in col_indexes.items():
                if idx is not None and len(row) > idx:
                    result[col_name] = extract_cell_value(row[idx])
                else:
                    result[col_name] = ""
            return result
    return None

def get_cp_info(query_str):
    # Extract numbers from query (cp prefix optional)
    numbers = re.findall(r'(?i)cp?(\d+)', query_str)
    if not numbers:
        numbers = re.findall(r'\b(\d{2,5})\b', query_str)   # accepts 2‑5 digit numbers
    if not numbers:
        return "❌ No valid asset numbers found. Use e.g., `/cp 12` or `/cp cp01 cp02`"

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
    for asset_number in numbers_unique:
        row = find_row_by_asset_number(data, asset_number)
        if row:
            output_lines.append(f"📌 Machine : OSM{asset_number}")
            output_lines.append("")
            output_lines.append("• IP address")
            output_lines.append(f"Controller IP : {row.get('Controller IP', '')}")
            output_lines.append(f"Main Screen IP : {row.get('Main Screen IP', '')}")
            output_lines.append(f"Top screen IP : {row.get('Top screen IP', '')}")
            output_lines.append(f"CCTV camera : {row.get('CCTV camera', '')}")
            output_lines.append("")
            output_lines.append("• Streaming address")
            output_lines.append(f"Main streaming address : {row.get('Main streaming address', '')}")
            output_lines.append("")
            output_lines.append(f"Top Streaming address : {row.get('Top Streaming address', '')}")
            output_lines.append("")
            output_lines.append(f"CCTV address : {row.get('CCTV address', '')}")
            output_lines.append("")
        else:
            output_lines.append(f"❌ Asset Number {asset_number} not found.")
            output_lines.append("")

    return "\n".join(output_lines).strip()