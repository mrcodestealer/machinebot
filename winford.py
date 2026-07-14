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
SPREADSHEET_TOKEN = os.getenv("WIN_SPREADSHEET_TOKEN")
SHEET_ID = os.getenv("WIN_SHEET_ID")

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

def get_range_values(token, range_str):
    url = f"https://open.larksuite.com/open-apis/sheets/v2/spreadsheets/{SPREADSHEET_TOKEN}/values/{range_str}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers)
    result = resp.json()
    if result.get("code") != 0:
        print(f"范围 {range_str} 请求失败: {result}")
        return None
    return result.get("data", {}).get("valueRange", {}).get("values", [])

def get_all_sheet_data(token):
    # Adjust range if needed (more rows/columns)
    range_str = f"{SHEET_ID}!A1:Z2000"
    return get_range_values(token, range_str)

def extract_cell_value(cell):
    """
    Convert a Lark sheet cell (could be string, number, or rich text list) into plain text.
    """
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

def get_winford_info(query_str):
    # Extract numbers (allow prefix "win", "winford", "wf", or just numbers)
    numbers = re.findall(r'(?i)(?:win(?:ford)?|wf)(\d+)', query_str)
    if not numbers:
        numbers = re.findall(r'\b(\d{3,5})\b', query_str)
    if not numbers:
        return "❌ No valid Winford Asset IDs found. Use e.g., `/winford 8092` or `/winford win8092 win8093`"

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
        return f"❌ 获取访问令牌失败: {e}"

    data = get_all_sheet_data(token)
    if not data:
        return "❌ 无法读取表格数据"

    # Find header row (first row containing any of the expected column names)
    required_fields = [
        "Top Encoder", "Main Encoder", "Mini PC", "CCTV",
        "TOP video URL", "Main Video URL", "CCTV Link"
    ]
    header_row = None
    for i, row in enumerate(data):
        if not row:
            continue
        # Look for a row that contains at least one of the required field names
        for cell in row:
            if isinstance(cell, str):
                cell_clean = cell.strip()
                if any(field.lower() in cell_clean.lower() for field in required_fields):
                    header_row = i
                    break
        if header_row is not None:
            break

    if header_row is None:
        return "❌ 未找到包含列名的表头行"

    headers = data[header_row]
    # Build column mapping
    col_map = {}
    for field in required_fields:
        col_map[field] = None
        for idx, col_name in enumerate(headers):
            if col_name and field.lower() == col_name.strip().lower():
                col_map[field] = idx
                break

    # Find Asset ID column (case-insensitive)
    asset_id_col = None
    for idx, col_name in enumerate(headers):
        if col_name and isinstance(col_name, str):
            col_lower = col_name.strip().lower()
            if "asset" in col_lower and "id" in col_lower:
                asset_id_col = idx
                break
    if asset_id_col is None:
        return "❌ 未找到 Asset ID 列（期望表头包含 'Asset ID'）"

    # Check that all required columns exist
    missing = [f for f, idx in col_map.items() if idx is None]
    if missing:
        return f"❌ 表格缺少以下列: {', '.join(missing)}"

    output_lines = []
    for asset_id in numbers_unique:
        found = False
        # Scan rows below the header
        for row in data[header_row+1:]:
            if len(row) <= asset_id_col:
                continue
            cell_val = str(row[asset_id_col]).strip()
            if cell_val == asset_id:
                found = True
                # Extract values using column indices
                top_encoder = extract_cell_value(row[col_map["Top Encoder"]]) if len(row) > col_map["Top Encoder"] else ""
                main_encoder = extract_cell_value(row[col_map["Main Encoder"]]) if len(row) > col_map["Main Encoder"] else ""
                mini_pc = extract_cell_value(row[col_map["Mini PC"]]) if len(row) > col_map["Mini PC"] else ""
                cctv = extract_cell_value(row[col_map["CCTV"]]) if len(row) > col_map["CCTV"] else ""
                top_video_url = extract_cell_value(row[col_map["TOP video URL"]]) if len(row) > col_map["TOP video URL"] else ""
                main_video_url = extract_cell_value(row[col_map["Main Video URL"]]) if len(row) > col_map["Main Video URL"] else ""
                cctv_link = extract_cell_value(row[col_map["CCTV Link"]]) if len(row) > col_map["CCTV Link"] else ""

                output_lines.append(f"WINFORD{asset_id}")
                output_lines.append("")
                output_lines.append("• IP address")
                output_lines.append(f"Top Encoder : {top_encoder}")
                output_lines.append(f"Main Encoder : {main_encoder}")
                output_lines.append(f"Mini PC : {mini_pc}")
                output_lines.append(f"CCTV : {cctv}")
                output_lines.append("")
                output_lines.append("• URL")
                output_lines.append(f"TOP video URL : {top_video_url}")
                output_lines.append("")
                output_lines.append(f"Main Video URL : {main_video_url}")
                output_lines.append("")
                output_lines.append(f"CCTV Link : {cctv_link}")
                output_lines.append("")
                break
        if not found:
            output_lines.append(f"WINFORD{asset_id} not found in the sheet.")
            output_lines.append("")

    return "\n".join(output_lines).strip()