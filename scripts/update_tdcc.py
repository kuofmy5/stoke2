```python
from __future__ import annotations

 

import csv
import io
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

 

import requests

 

 

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config.json"
DATA_FILE = ROOT / "data" / "tdcc_history.json"

 

# TDCC 集保戶股權分散表開放資料
BASE_URL = "https://opendata.tdcc.com.tw/getOD.ashx"
DATASET_ID = "1-5"

 

# 由第 12 級開始，代表 400,001 股以上
BIG_HOLDER_FIRST_LEVEL = 12
BIG_HOLDER_LAST_LEVEL = 16

 

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 TDCC GitHub Pages chart"})

 

 

def normalize_header(value: str) -> str:
    return (
        str(value or "")
        .replace("\ufeff", "")
        .replace(" ", "")
        .replace("　", "")
        .replace("％", "%")
        .strip()
        .lower()
    )

 

 

def find_column(headers: list[str], aliases: set[str]) -> str | None:
    normalized = {normalize_header(h): h for h in headers}
    for alias in aliases:
        key = normalize_header(alias)
        if key in normalized:
            return normalized[key]
    return None

 

 

def parse_date(value: str) -> date | None:
    """支援西元 YYYYMMDD / YYYY-MM-DD，以及民國 YYYMMDD 格式。"""
    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)

 

    try:
        if len(digits) == 7:  # 民國年，例如 1150821
            year = int(digits[:3]) + 1911
            month = int(digits[3:5])
            day = int(digits[5:7])
            return date(year, month, day)

 

        if len(digits) >= 8:  # 西元年，例如 20260821
            year = int(digits[:4])
            month = int(digits[4:6])
            day = int(digits[6:8])
            return date(year, month, day)
    except ValueError:
        return None

 

    return None

 

 

def parse_number(value: str) -> float | None:
    text = str(value or "").strip()
    text = text.replace(",", "").replace("%", "").replace("％", "")

 

    if not text or text in {"-", "--", "—", "NA", "N/A"}:
        return None

 

    try:
        return float(text)
    except ValueError:
        return None

 

 

def is_big_holder_level(value: str) -> bool:
    """
    TDCC 持股分級若是 1～16 的級距代碼，12～16 為 400,001 股以上。
    若資料直接提供級距文字，則依級距起始股數判斷。
    """
    text = str(value or "").strip().replace(",", "")
    if not text:
        return False

 

    # 純數字且在 1～16 之間時，視為 TDCC 分級代碼。
    if re.fullmatch(r"\d+", text):
        level = int(text)
        if 1 <= level <= 16:
            return BIG_HOLDER_FIRST_LEVEL <= level <= BIG_HOLDER_LAST_LEVEL
        return level >= 400001

 

    # 例如「第12級」
    level_match = re.fullmatch(r"第?(\d+)級?", text)
    if level_match:
        level = int(level_match.group(1))
        if 1 <= level <= 16:
            return BIG_HOLDER_FIRST_LEVEL <= level <= BIG_HOLDER_LAST_LEVEL
        return level >= 400001

 

    # 例如「400001-600000」或「400,001～600,000」
    numbers = re.findall(r"\d+", text)
    if not numbers:
        return False

 

    return int(numbers[0]) >= 400001

 

 

def decode_csv(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp950", "big5"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")

 

 

def parse_tdcc_csv(content: bytes, wanted_codes: set[str]) -> list[dict]:
    text = decode_csv(content)

 

    # 沒有資料的歷史日期，有時會回傳空白或非 CSV 內容。
    if not text.strip() or text.lstrip().startswith("<"):
        return []

 

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []

 

    date_col = find_column(
        reader.fieldnames,
        {"資料日期", "dataDate", "date"},
    )
    code_col = find_column(
        reader.fieldnames,
        {"證券代號", "securityId", "stockId", "code"},
    )
    level_col = find_column(
        reader.fieldnames,
        {"持股分級", "持股級距", "level"},
    )
    ratio_col = find_column(
        reader.fieldnames,
        {
            "占集保庫存數比例%",
            "占集保庫存數比例",
            "percentage",
            "ratio",
        },
    )

 

    if not all([date_col, code_col, level_col, ratio_col]):
        # 若資料源欄位真的改名，讓錯誤明確出現在 Actions log。
        raise ValueError(f"找不到必要欄位；CSV 欄位為：{reader.fieldnames}")

 

    totals: dict[tuple[str, str], float] = {}

 

    for row in reader:
        code = str(row.get(code_col, "") or "").strip()
        if code not in wanted_codes:
            continue

 

        data_date = parse_date(row.get(date_col, ""))
        if not data_date:
            continue

 

        if not is_big_holder_level(row.get(level_col, "")):
            continue

 

        ratio = parse_number(row.get(ratio_col, ""))
        if ratio is None:
            continue

 

        key = (data_date.isoformat(), code)
        totals[key] = totals.get(key, 0.0) + ratio

 

    return [
        {
            "date": data_date,
            "stock_code": code,
            "large_holder_percent": round(percent, 6),
        }
        for (data_date, code), percent in sorted(totals.items())
    ]

 

 

def download_csv(params: dict[str, str]) -> bytes:
    response = SESSION.get(BASE_URL, params=params, timeout=60)
    response.raise_for_status()
    return response.content

 

 

def read_existing() -> dict:
    if not DATA_FILE.exists():
        return {"records": []}

 

    with DATA_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)

 

 

def taipei_today() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()

 

 

def friday_weeks(today: date, lookback_weeks: int):
    """產生回看期間內每週的星期一到星期五。"""
    start = today - timedelta(weeks=lookback_weeks)
    current_monday = start - timedelta(days=start.weekday())

 

    while current_monday <= today:
        friday = current_monday + timedelta(days=4)
        if current_monday <= today:
            yield current_monday, min(friday, today)
        current_monday += timedelta(days=7)

 

 

def fetch_historical_week(
    stock_code: str,
    monday: date,
    friday: date,
) -> list[dict]:
    """
    依週五到週一逐日查 TDCC 歷史資料。
    遇到休市日或沒有資料時往前試，抓到該週資料即停止。
    """
    for offset in range((friday - monday).days, -1, -1):
        query_date = monday + timedelta(days=offset)

 

        try:
            content = download_csv(
                {
                    "id": DATASET_ID,
                    "dataDate": query_date.strftime("%Y%m%d"),
                    "securityId": stock_code,
                }
            )
            rows = parse_tdcc_csv(content, {stock_code})
        except (requests.RequestException, ValueError) as error:
            print(f"歷史查詢略過 {stock_code} {query_date}: {error}")
            rows = []

 

        # 僅接受該週日期，避免把 API 回傳的其他日期誤當成該週。
        rows = [
            row
            for row in rows
            if monday.isoformat() <= row["date"] <= friday.isoformat()
        ]

 

        if rows:
            newest_date = max(row["date"] for row in rows)
            return [row for row in rows if row["date"] == newest_date]

 

        time.sleep(0.15)

 

    return []

 

 

def main() -> int:
    if not CONFIG_FILE.exists():
        print("找不到 config.json")
        return 1

 

    with CONFIG_FILE.open("r", encoding="utf-8") as file:
        config = json.load(file)

 

    stock_codes = {
        str(code).strip()
        for code in config.get("stock_codes", [])
        if str(code).strip()
    }
    lookback_weeks = int(config.get("lookback_weeks", 26))

 

    if not stock_codes:
        print("config.json 的 stock_codes 至少要填一個股票代號")
        return 1

 

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    stored = read_existing()
    records_by_key = {
        (row["date"], row["stock_code"]): row
        for row in stored.get("records", [])
        if "date" in row and "stock_code" in row
    }

 

    # 先讀最新的全市場 CSV，再只保留 config.json 指定的股票。
    try:
        latest_content = download_csv({"id": DATASET_ID})
        latest_rows = parse_tdcc_csv(latest_content, stock_codes)
        print(f"最新資料取得 {len(latest_rows)} 筆")
    except (requests.RequestException, ValueError) as error:
        print(f"下載 TDCC 最新資料失敗：{error}")
        latest_rows = []

 

    for row in latest_rows:
        records_by_key[(row["date"], row["stock_code"])] = row

 

    # 補抓缺少的歷史週資料。第一次執行會建立近 lookback_weeks 週資料。
    today = taipei_today()
    for code in sorted(stock_codes):
        for monday, friday in friday_weeks(today, lookback_weeks):
            already_have_this_week = any(
                row["stock_code"] == code
                and monday.isoformat() <= row["date"] <= friday.isoformat()
                for row in records_by_key.values()
            )
            if already_have_this_week:
                continue

 

            rows = fetch_historical_week(code, monday, friday)
            for row in rows:
                records_by_key[(row["date"], row["stock_code"])] = row

 

    new_records = sorted(
        records_by_key.values(),
        key=lambda row: (row["stock_code"], row["date"]),
    )

 

    old_records = stored.get("records", [])
    if new_records == old_records:
        print("沒有新資料，不需要更新 JSON。")
        return 0

 

    output = {
        "source": "TDCC 集保戶股權分散表開放資料",
        "threshold": "400,001 股以上（持股分級第 12～16 級）",
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "records": new_records,
    }

 

    with DATA_FILE.open("w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)

 

    print(f"已寫入 {len(new_records)} 筆資料至 {DATA_FILE}")
    return 0

 

 

if __name__ == "__main__":
    sys.exit(main())
```

 

---
