import json
import requests
import re
from datetime import datetime
import os
from dotenv import load_dotenv
load_dotenv()
# ================= CONFIGURATION =================
APP_ID = os.getenv("APP_ID")
APP_SECRET = os.getenv("APP_SECRET")

FIRST_SPREADSHEET_TOKEN = os.getenv("NWR_FIRST_SPREADSHEET_TOKEN")
FIRST_SHEET_ID = os.getenv("NWR_FIRST_SHEET_ID")

SECOND_SPREADSHEET_TOKEN = os.getenv("NWR_SECOND_SPREADSHEET_TOKEN")
SECOND_SHEET_ID = os.getenv("NWR_SECOND_SHEET_ID")

# ================= API FUNCTIONS =================
def get_tenant_access_token():
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json"}
    data = {"app_id": APP_ID, "app_secret": APP_SECRET}
    resp = requests.post(url, headers=headers, json=data)
    result = resp.json()
    if result.get("code") != 0:
        raise Exception(f"获取 token 失败: {result}")
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
    """Convert 1‑based column index to Excel column letters."""
    letters = ''
    while col_index > 0:
        col_index -= 1
        letters = chr(65 + (col_index % 26)) + letters
        col_index //= 26
    return letters

def get_range_values(token, spreadsheet_token, sheet_id, range_str):
    """Get values from a range in a specific spreadsheet."""
    url = f"https://open.larksuite.com/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{sheet_id}!{range_str}?valueRenderOption=FormattedValue"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers)
    result = resp.json()
    if result.get("code") != 0:
        print(f"范围 {range_str} 请求失败: {result}")
        return None
    return result.get("data", {}).get("valueRange", {}).get("values", [])

def get_all_sheet_data(token, spreadsheet_token, sheet_id):
    """Read the entire sheet using metadata to determine max row/col."""
    props = get_sheet_metadata(token, spreadsheet_token, sheet_id)
    if props:
        max_row = props.get("rowCount", 2000)
        max_col = props.get("columnCount", 200)
        end_col = col_index_to_letter(max_col)
        # Important: range_str must be just the cell range, without sheet_id
        range_str = f"A1:{end_col}{max_row}"
    else:
        # Fallback to a generous range (columns up to ZZ, rows up to 2000)
        range_str = "A1:ZZ2000"
    return get_range_values(token, spreadsheet_token, sheet_id, range_str)

def extract_cell_value(cell):
    """Convert a Lark sheet cell into plain text."""
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

def find_data_for_id(data, id_value, sheet_config):
    """
    Search the sheet data for a row where the ID column contains id_value as a number.
    Returns a dict with field values if found, else None.
    """
    required_fields = sheet_config["required_fields"]
    id_column_name = sheet_config["id_column_name"]

    # Locate header row (first row containing any required field)
    header_row = None
    for i, row in enumerate(data):
        if not row:
            continue
        for cell in row:
            if isinstance(cell, str):
                cell_clean = cell.strip()
                if any(field.lower() in cell_clean.lower() for field in required_fields):
                    header_row = i
                    break
        if header_row is not None:
            break

    if header_row is None:
        return None

    headers = data[header_row]

    # Map required fields to column indices
    col_map = {}
    for field in required_fields:
        col_map[field] = None
        for idx, col_name in enumerate(headers):
            if col_name and field.lower() == col_name.strip().lower():
                col_map[field] = idx
                break

    # Find ID column (case‑insensitive partial match)
    id_col = None
    for idx, col_name in enumerate(headers):
        if col_name and isinstance(col_name, str):
            col_lower = col_name.strip().lower()
            if id_column_name.lower() in col_lower:
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
        if match and match.group() == id_value:
            result = {}
            for field, idx in col_map.items():
                if idx is not None and len(row) > idx:
                    result[field] = extract_cell_value(row[idx])
                else:
                    result[field] = ""
            return result
    return None

def get_nwr_info(query_str):
    numbers = re.findall(r'(?i)nwr?(\d+)', query_str)
    if not numbers:
        numbers = re.findall(r'\b(\d{4,5})\b', query_str)
    if not numbers:
        return "❌ No valid nwr numbers found. Use e.g., `/nwr 2005` or `/nwr nwr2005 nwr2006`"

    seen = set()
    numbers_unique = []
    for n in numbers:
        if n not in seen:
            seen.add(n)
            numbers_unique.append(n)

    try:
        token = get_tenant_access_token()
    except Exception as e:
        return f"❌ 获取访问令牌失败: {e}"

    sheets = [
        {
            "name": "Primary Sheet",
            "spreadsheet_token": FIRST_SPREADSHEET_TOKEN,
            "sheet_id": FIRST_SHEET_ID,
            "required_fields": [
                "Top Encoder", "Main Encoder", "Mini PC", "CCTV",
                "TOP Streaming URL", "Main Streaming URL", "CCTV URL"
            ],
            "id_column_name": "Asset id"          # match exactly what's in header
        },
        {
            "name": "Secondary Sheet",
            "spreadsheet_token": SECOND_SPREADSHEET_TOKEN,
            "sheet_id": SECOND_SHEET_ID,
            "required_fields": [
                "Top Encoder", "Main Encoder", "Mini PC", "CCTV",
                "Pool Streaming URL", "Main Streaming URL", "CCTV URL"
            ],
            "id_column_name": "Asset ID"
        }
    ]

    output_lines = []
    for nwr in numbers_unique:
        found = False
        for sheet in sheets:
            data = get_all_sheet_data(token, sheet["spreadsheet_token"], sheet["sheet_id"])
            if not data:
                continue
            row_data = find_data_for_id(data, nwr, sheet)
            if row_data is not None:
                found = True
                output_lines.append(f"Machine : NWR{nwr}")
                output_lines.append("")
                output_lines.append("• IP address")
                output_lines.append(f"Top Encoder : {row_data.get('Top Encoder', '')}")
                output_lines.append(f"Main Encoder : {row_data.get('Main Encoder', '')}")
                output_lines.append(f"Mini PC : {row_data.get('Mini PC', '')}")
                output_lines.append(f"CCTV : {row_data.get('CCTV', '')}")
                output_lines.append("")
                output_lines.append("• URL")
                top_url = row_data.get('TOP Streaming URL') or row_data.get('Pool Streaming URL', '')
                main_url = row_data.get('Main Streaming URL', '')
                cctv_url = row_data.get('CCTV URL', '')
                output_lines.append(f"TOP Streaming URL : {top_url}")
                output_lines.append("")
                output_lines.append(f"Main Streaming URL : {main_url}")
                output_lines.append("")
                output_lines.append(f"CCTV URL : {cctv_url}")
                output_lines.append("")
                break
        if not found:
            output_lines.append(f"nwr{nwr} not found in any sheet.")
            output_lines.append("")

    return "\n".join(output_lines).strip()

if __name__ == "__main__":
    # Uncomment the next line to debug
    # debug_sheet_structure()
    # Then test your nwr lookup:
    import sys
    if len(sys.argv) > 1:
        print(get_nwr_info(sys.argv[1]))
    else:
        print(get_nwr_info("2092"))