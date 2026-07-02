"""
Vercel Serverless Function：/api/scrape

由 vercel.json 裡設定的 Cron Job 定時呼叫這個路徑（GET 請求）。
每次執行會：
  1. 向維基百科抓「32強／16強~季軍賽／冠軍賽」三個頁面的 wikitext
  2. 解析賽事樣板，取出日期/時間/隊伍，換算成台灣時間
  3. 透過 GitHub Contents API 把結果寫回 matches.json

需要在 Vercel 的 Environment Variables 設定：
  GITHUB_TOKEN   - GitHub Personal Access Token（repo 的 Contents 需要 Read/Write 權限）
  GITHUB_REPO    - 例如 "eli-101/FIFASchedule2026"
  GITHUB_PATH    - 預設 "matches.json"（可不填）
  GITHUB_BRANCH  - 預設 "main"（可不填）
  CRON_SECRET    - 選填，設定後可防止別人隨意觸發這個網址（見下方說明）
"""

import base64
import json
import os
import re
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler

import requests

WIKI_API = "https://en.wikipedia.org/w/api.php"
UA = {"User-Agent": "fifa-calendar-bot/1.0 (personal project; contact via GitHub)"}

TEAM_NAMES = {
    "MEX": "墨西哥", "RSA": "南非", "KOR": "南韓", "CZE": "捷克",
    "CAN": "加拿大", "BIH": "波赫", "USA": "美國", "PAR": "巴拉圭",
    "HAI": "海地", "SCO": "蘇格蘭", "AUS": "澳洲", "TUR": "土耳其",
    "BRA": "巴西", "MAR": "摩洛哥", "QAT": "卡達", "SUI": "瑞士",
    "CIV": "象牙海岸", "ECU": "厄瓜多", "GER": "德國", "CUW": "庫拉索",
    "NED": "荷蘭", "JPN": "日本", "SWE": "瑞典", "TUN": "突尼西亞",
    "KSA": "沙烏地阿拉伯", "SAU": "沙烏地阿拉伯", "URU": "烏拉圭",
    "ESP": "西班牙", "CPV": "維德角", "IRN": "伊朗", "NZL": "紐西蘭",
    "BEL": "比利時", "EGY": "埃及", "FRA": "法國", "SEN": "塞內加爾",
    "IRQ": "伊拉克", "NOR": "挪威", "ARG": "阿根廷", "ALG": "阿爾及利亞",
    "AUT": "奧地利", "JOR": "約旦", "GHA": "迦納", "PAN": "巴拿馬",
    "ENG": "英格蘭", "CRO": "克羅埃西亞", "POR": "葡萄牙", "COD": "剛果民主共和國",
    "UZB": "烏茲別克", "COL": "哥倫比亞",
}

DATE_TO_ROUND = {}
for d in range(28, 31):
    DATE_TO_ROUND[(2026, 6, d)] = "32"
for d in range(1, 4):
    DATE_TO_ROUND[(2026, 7, d)] = "32"
for d in range(4, 9):
    DATE_TO_ROUND[(2026, 7, d)] = "16"
for d in range(9, 13):
    DATE_TO_ROUND[(2026, 7, d)] = "8"
for d in (14, 15, 16):
    DATE_TO_ROUND[(2026, 7, d)] = "4"
DATE_TO_ROUND[(2026, 7, 18)] = "3RD"
DATE_TO_ROUND[(2026, 7, 19)] = "FINAL"
DATE_TO_ROUND[(2026, 7, 20)] = "FINAL"


def fetch_wikitext(title: str) -> str:
    params = {
        "action": "query", "prop": "revisions", "rvprop": "content",
        "rvslots": "main", "format": "json", "titles": title,
    }
    r = requests.get(WIKI_API, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    data = r.json()
    page = next(iter(data["query"]["pages"].values()))
    if "revisions" not in page:
        raise RuntimeError(f"找不到頁面內容：{title}")
    return page["revisions"][0]["slots"]["main"]["*"]


def fetch_match_section_indices(title: str):
    """回傳頁面裡所有『對戰』小節的 section index（標題含 " vs " 的那些）。"""
    params = {"action": "parse", "page": title, "prop": "sections", "format": "json"}
    r = requests.get(WIKI_API, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    sections = r.json()["parse"]["sections"]
    return [s["index"] for s in sections if " vs " in s["line"]]


def fetch_wikitext_section(title: str, section_index) -> str:
    params = {
        "action": "query", "prop": "revisions", "rvprop": "content",
        "rvslots": "main", "rvsection": section_index, "format": "json",
        "titles": title,
    }
    r = requests.get(WIKI_API, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    page = next(iter(r.json()["query"]["pages"].values()))
    if "revisions" not in page:
        return ""
    return page["revisions"][0]["slots"]["main"]["*"]


def fetch_wikitext_by_sections(title: str) -> str:
    """逐一抓取頁面中每個『對戰』小節後合併。
    用於像 round of 32 這種整頁太大、一次抓取會逾時或被截斷的頁面：
    每個小節只有幾 KB，分開抓既快又穩定。"""
    indices = fetch_match_section_indices(title)
    parts = []
    for idx in indices:
        try:
            parts.append(fetch_wikitext_section(title, idx))
        except Exception as e:
            print(f"[警告] 抓取「{title}」第 {idx} 小節失敗：{e}")
    return "\n----\n".join(parts)


def strip_comments(s: str) -> str:
    return re.sub(r"<!--.*?-->", "", s, flags=re.S)


def find_football_box_blocks(wikitext: str):
    """掃描 wikitext，找出所有 {{#invoke:football box|main ... }} 樣板區塊
    （不分大小寫比對，因為不同頁面的編輯者有時會寫成 "Football box"），
    用括號深度計數來正確處理巢狀的 {{ }}。"""
    pattern = re.compile(r"\{\{#invoke:football box\|main", re.IGNORECASE)
    blocks, idx = [], 0
    while True:
        m = pattern.search(wikitext, idx)
        if not m:
            break
        start = m.start()
        depth, i, n = 0, start, len(wikitext)
        while i < n:
            if wikitext[i:i + 2] == "{{":
                depth += 1
                i += 2
                continue
            if wikitext[i:i + 2] == "}}":
                depth -= 1
                i += 2
                if depth == 0:
                    break
                continue
            i += 1
        blocks.append(wikitext[start:i])
        idx = i
    return blocks


def split_template_params(block: str):
    # 開頭的 "{{#invoke:football box|main" 長度固定，不分大小寫都一樣長，
    # 用不分大小寫的比對找出真正的前綴長度即可安全去除。
    prefix_match = re.match(r"\{\{#invoke:football box\|main", block, re.IGNORECASE)
    prefix_len = prefix_match.end() if prefix_match else len("{{#invoke:football box|main")
    inner = block[prefix_len:-2]
    params, current = [], ""
    brace_depth, bracket_depth = 0, 0
    i, n = 0, len(inner)
    while i < n:
        two = inner[i:i + 2]
        if two == "{{":
            brace_depth += 1
            current += two
            i += 2
            continue
        if two == "}}":
            brace_depth -= 1
            current += two
            i += 2
            continue
        if two == "[[":
            bracket_depth += 1
            current += two
            i += 2
            continue
        if two == "]]":
            bracket_depth -= 1
            current += two
            i += 2
            continue
        c = inner[i]
        if c == "|" and brace_depth == 0 and bracket_depth == 0:
            params.append(current)
            current = ""
            i += 1
            continue
        current += c
        i += 1
    if current.strip():
        params.append(current)
    result = {}
    for p in params:
        if "=" in p:
            k, v = p.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def parse_team(raw: str) -> str:
    raw = strip_comments(raw).strip()
    m = re.search(r"\{\{#invoke:flag\|fb(?:-rt)?\|([A-Za-z]+)\}\}", raw)
    if m:
        code = m.group(1).upper()
        return TEAM_NAMES.get(code, code)
    m = re.search(r"Winner Match (\d+)", raw)
    if m:
        return f"M{m.group(1)}勝隊"
    m = re.search(r"Loser Match (\d+)", raw)
    if m:
        return f"M{m.group(1)}敗隊"
    return raw.strip() or "待定"


def parse_datetime(date_field: str, time_field: str):
    m = re.search(r"\{\{Start date\|(\d+)\|(\d+)\|(\d+)\}\}", date_field)
    if not m:
        return None
    year, month, day = (int(x) for x in m.groups())

    t = time_field.replace("&nbsp;", " ")
    m = re.search(r"(\d{1,2}):(\d{2})\s*(a\.m\.|p\.m\.)", t, re.I)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ampm == "p.m." and hour != 12:
        hour += 12
    if ampm == "a.m." and hour == 12:
        hour = 0

    m = re.search(r"UTC[−\-+]?(\d+)", t)
    offset = -int(m.group(1)) if m else 0
    if "UTC+" in t:
        offset = int(m.group(1)) if m else 0
    return year, month, day, hour, minute, offset


def to_taipei(year, month, day, hour, minute, offset):
    local_dt = datetime(year, month, day, hour, minute)
    utc_dt = local_dt - timedelta(hours=offset)
    return utc_dt + timedelta(hours=8)


def format_time_label(dt: datetime) -> str:
    h = dt.hour
    if h == 0 or h < 6:
        label = "凌晨"
    elif h < 12:
        label = "上午"
    elif h == 12:
        label = "中午"
    elif h < 18:
        label = "下午"
    else:
        label = "晚上"
    return f"{label} {dt.strftime('%H:%M')}"


def parse_page(wikitext: str):
    matches = []
    for block in find_football_box_blocks(wikitext):
        params = split_template_params(block)
        if "date" not in params or "time" not in params:
            continue
        parsed = parse_datetime(params["date"], params["time"])
        if not parsed:
            continue
        year, month, day, hour, minute, offset = parsed
        round_code = DATE_TO_ROUND.get((year, month, day))
        if not round_code:
            continue
        taipei_dt = to_taipei(year, month, day, hour, minute, offset)
        if taipei_dt.month != 7:
            continue
        team1 = parse_team(params.get("team1", ""))
        team2 = parse_team(params.get("team2", ""))
        matches.append({
            "date": taipei_dt.day, "group": round_code,
            "team1": team1, "team2": team2,
            "time": format_time_label(taipei_dt),
            "_sort": taipei_dt.isoformat(),
        })
    return matches


def scrape_all():
    # 每個頁面標一個抓取策略：
    #   "whole"    = 一次抓整頁（適合較小的頁面）
    #   "sections" = 逐一小節抓取後合併（適合 round of 32 這種超大頁面，
    #                整頁一次抓會逾時或被截斷）
    pages = [
        ("2026 FIFA World Cup round of 32", "sections"),
        ("2026 FIFA World Cup knockout stage", "whole"),
        ("2026 FIFA World Cup final", "whole"),
    ]
    all_matches, seen = [], set()
    for title, strategy in pages:
        try:
            if strategy == "sections":
                wikitext = fetch_wikitext_by_sections(title)
            else:
                wikitext = fetch_wikitext(title)
                # 保險：如果整頁抓到卻找不到任何 football box，
                # 可能是頁面太大被截斷，改用小節方式再試一次。
                if not find_football_box_blocks(wikitext):
                    print(f"[資訊]「{title}」整頁未找到賽事樣板，改用小節方式重試")
                    wikitext = fetch_wikitext_by_sections(title)
        except Exception as e:
            print(f"[警告] 抓取「{title}」失敗：{e}")
            continue
        for m in parse_page(wikitext):
            key = (m["date"], m["team1"], m["team2"], m["time"])
            if key in seen:
                continue
            seen.add(key)
            all_matches.append(m)
    all_matches.sort(key=lambda m: m["_sort"])
    for m in all_matches:
        del m["_sort"]
    return all_matches


def push_to_github(matches):
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPO"]
    path = os.environ.get("GITHUB_PATH", "matches.json")
    branch = os.environ.get("GITHUB_BRANCH", "main")

    api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": UA["User-Agent"],
    }
    content_str = json.dumps(matches, ensure_ascii=False, indent=2)
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("ascii")

    sha = None
    r = requests.get(api_url, headers=headers, params={"ref": branch}, timeout=20)
    if r.status_code == 200:
        sha = r.json().get("sha")

    payload = {
        "message": f"自動更新賽程 {datetime.utcnow().isoformat()}Z",
        "content": content_b64,
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(api_url, headers=headers, json=payload, timeout=20)
    r.raise_for_status()


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 如果設定了 CRON_SECRET，就檢查 Vercel Cron 帶來的授權標頭，
        # 避免任何人只要知道網址就能亂觸發這個端點（會浪費 GitHub API 額度）。
        expected_secret = os.environ.get("CRON_SECRET")
        if expected_secret:
            auth_header = self.headers.get("Authorization", "")
            if auth_header != f"Bearer {expected_secret}":
                self._respond(401, {"status": "error", "message": "unauthorized"})
                return

        try:
            matches = scrape_all()
            if not matches:
                self._respond(500, {"status": "error", "message": "沒有解析到任何比賽資料，已中止，避免覆蓋成空檔案"})
                return
            push_to_github(matches)
            self._respond(200, {"status": "ok", "count": len(matches)})
        except Exception as e:
            self._respond(500, {"status": "error", "message": str(e)})

    def _respond(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
