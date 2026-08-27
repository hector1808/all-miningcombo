import os
import re
import json
import base64
import html
import time
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yaml
import requests
import gspread
from bs4 import BeautifulSoup, NavigableString
from google.oauth2.service_account import Credentials
from openai import OpenAI


def parse_wp_datetime_local(value, tz_name):
    if not value:
        return None

    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if dt.tzinfo is None:
        # WordPress field "modified" thường là local time của site.
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    else:
        dt = dt.astimezone(ZoneInfo(tz_name))

    return dt

def parse_wp_datetime_gmt(value, tz_name):
    """
    WordPress modified_gmt không có timezone suffix,
    nhưng giá trị này phải được hiểu là UTC.
    """
    if not value:
        return None

    dt = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    if dt.tzinfo is None:
        dt = dt.replace(
            tzinfo=ZoneInfo("UTC")
        )
    else:
        dt = dt.astimezone(
            ZoneInfo("UTC")
        )

    return dt.astimezone(
        ZoneInfo(tz_name)
    )

def target_date_start_datetime(tz_name):
    d = get_target_date(tz_name)

    return datetime(
        d.year,
        d.month,
        d.day,
        0,
        0,
        0,
        tzinfo=ZoneInfo(tz_name),
    )


def is_source_for_target_date(source_modified, tz_name):
    source_dt = parse_wp_datetime_local(source_modified, tz_name)

    if not source_dt:
        return False

    return source_dt >= target_date_start_datetime(tz_name)


def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def now_local(tz_name):
    return datetime.now(ZoneInfo(tz_name))


def get_target_date(tz_name):
    run_mode = os.getenv("RUN_MODE", "update").lower()
    today = now_local(tz_name).date()

    if run_mode == "create":
        return today + timedelta(days=1)

    if run_mode == "update":
        return today

    raise RuntimeError(f"Invalid RUN_MODE: {run_mode}")

def scheduled_publish_datetime(
    tz_name,
    game_cfg,
    target_date,
):
    post_cycle = game_cfg.get(
        "post_cycle",
        "daily",
    )

    publish_time = game_cfg.get(
        "publish_time",
        "22:00",
    )

    hour, minute = map(
        int,
        publish_time.split(":"),
    )

    if post_cycle == "weekly":
        publish_date = target_date
    else:
        # Giữ nguyên hành vi daily hiện tại:
        # create vào sáng hôm nay,
        # publish lúc 22:00 hôm nay.
        publish_date = now_local(
            tz_name
        ).date()

    return datetime(
        publish_date.year,
        publish_date.month,
        publish_date.day,
        hour,
        minute,
        0,
        tzinfo=ZoneInfo(tz_name),
    )

def target_date_str(tz_name):
    return get_target_date(tz_name).isoformat()


def target_date_readable(tz_name):
    d = get_target_date(tz_name)
    return f"{d.strftime('%B')} {d.day}, {d.year}"

def target_date_slug(tz_name):
    d = get_target_date(tz_name)

    return f"{d.day}-{d.strftime('%B').lower()}-{d.year}"

def format_datetime_readable(datetime_value):
    offset = datetime_value.strftime("%z")

    if offset:
        offset = f"{offset[:3]}:{offset[3:]}"
        timezone_text = f"UTC{offset}"
    else:
        timezone_text = ""

    result = (
        f"{datetime_value.strftime('%B')} "
        f"{datetime_value.day}, "
        f"{datetime_value.year} at "
        f"{datetime_value.strftime('%H:%M')}"
    )

    if timezone_text:
        result += f" ({timezone_text})"

    return result

def format_date_readable(date_value):
    return (
        f"{date_value.strftime('%B')} "
        f"{date_value.day}, "
        f"{date_value.year}"
    )


def format_date_slug(date_value):
    return (
        f"{date_value.day}-"
        f"{date_value.strftime('%B').lower()}-"
        f"{date_value.year}"
    )


def get_recent_sunday(tz_name):
    """
    Trả về ngày Chủ nhật gần nhất.

    Nếu hôm nay là Chủ nhật:
        trả về chính hôm nay.

    Nếu hôm nay là thứ Hai đến thứ Bảy:
        trả về Chủ nhật vừa qua.
    """
    today = now_local(tz_name).date()

    # weekday():
    # Monday = 0
    # Sunday = 6
    days_since_sunday = (today.weekday() + 1) % 7

    return today - timedelta(days=days_since_sunday)


def get_game_target_date(tz_name, game_cfg):
    post_cycle = game_cfg.get("post_cycle", "daily")

    if post_cycle == "weekly":
        return get_recent_sunday(tz_name)

    return get_target_date(tz_name)


def get_week_campaign_dates(publish_sunday):
    """
    Ví dụ:
    publish_sunday = 2026-07-12

    campaign_start = 2026-07-13
    campaign_end   = 2026-07-19
    """
    campaign_start = publish_sunday + timedelta(days=1)
    campaign_end = publish_sunday + timedelta(days=7)

    return campaign_start, campaign_end


def format_date_range_readable(start_date, end_date):
    if start_date.year == end_date.year:
        return (
            f"{start_date.strftime('%B')} {start_date.day} "
            f"to {end_date.strftime('%B')} {end_date.day}, "
            f"{end_date.year}"
        )

    return (
        f"{start_date.strftime('%B')} {start_date.day}, "
        f"{start_date.year} to "
        f"{end_date.strftime('%B')} {end_date.day}, "
        f"{end_date.year}"
    )


def format_date_range_slug(start_date, end_date):
    return (
        f"{start_date.day}-"
        f"{start_date.strftime('%B').lower()}-"
        f"{start_date.year}-to-"
        f"{end_date.day}-"
        f"{end_date.strftime('%B').lower()}-"
        f"{end_date.year}"
    )

def make_city_combo_signature(combo_lines):
    if not combo_lines:
        return ""

    raw = json.dumps(
        combo_lines,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def get_latest_city_combo_signature(
    ws,
    game_key,
    exclude_target_date=None,
):
    records = ws.get_all_records()

    for row in reversed(records):
        if str(
            row.get("game_key")
        ) != game_key:
            continue

        if (
            exclude_target_date
            and str(row.get("target_date"))
            == exclude_target_date
        ):
            continue

        raw_answer = str(
            row.get("answer")
            or ""
        ).strip()

        if not raw_answer:
            continue

        try:
            payload = json.loads(
                raw_answer
            )
        except (
            json.JSONDecodeError,
            TypeError,
        ):
            continue

        combo_lines = payload.get(
            "combo_lines",
            [],
        )

        signature = make_city_combo_signature(
            combo_lines
        )

        if signature:
            return signature

    return ""


def get_latest_check_answer_for_game(ws, game_key):
    records = ws.get_all_records()

    latest_row = None

    for row in records:
        if str(row.get("game_key")) == game_key and str(row.get("check_answer") or "").strip():
            latest_row = row

    if latest_row:
        return latest_row.get("check_answer") or ""

    return ""


def current_time_hhmm(tz_name):
    return now_local(tz_name).strftime("%H:%M")


def replace_date_vars(text, date_str, readable_date=None, slug_date=None):
    text = text.replace("{{CURRENT_DATE}}", date_str)

    if readable_date:
        text = text.replace("{{CURRENT_DATE_READABLE}}", readable_date)

    if slug_date:
        text = text.replace("{{CURRENT_DATE_SLUG}}", slug_date)

    return text

def replace_game_vars(
    text,
    date_str,
    readable_date,
    slug_date,
    extra_vars=None,
):
    text = replace_date_vars(
        text=text,
        date_str=date_str,
        readable_date=readable_date,
        slug_date=slug_date,
    )

    for key, value in (extra_vars or {}).items():
        placeholder = "{{" + key + "}}"
        text = text.replace(placeholder, str(value))

    return text


def normalize_slug(slug):
    slug = slug.lower().strip()
    slug = re.sub(r"[^a-z0-9\-]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


def normalize_answer(value):
    if not value:
        return ""
    value = html.unescape(str(value))
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def wp_headers(cfg):
    username = get_env(cfg["wp"]["username_env"])
    app_password = get_env(cfg["wp"]["app_password_env"])
    token = base64.b64encode(f"{username}:{app_password}".encode()).decode()

    return {
        "Authorization": f"Basic {token}",
        "User-Agent": "Mozilla/5.0",
    }


def get_sheet(cfg):
    raw = get_env("GOOGLE_CREDENTIALS_BASE64")
    service_account_info = json.loads(base64.b64decode(raw).decode("utf-8"))

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = Credentials.from_service_account_info(
        service_account_info,
        scopes=scopes,
    )

    gc = gspread.authorize(creds)
    sheet_id = get_env(cfg["google_sheet"]["sheet_id_env"])
    sh = gc.open_by_key(sheet_id)

    worksheet_name = cfg["google_sheet"]["worksheet_name"]

    headers = [
        "target_date",
        "game_key",
        "post_id",
        "post_url",
        "slug",
        "source_modified",
        "question",
        "answer",
        "check_answer",
        "verified_date",
        "status",
        "created_at",
        "updated_at",
    ]

    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=20)
        ws.append_row(headers)
        return ws

    existing_headers = ws.row_values(1)
    missing_headers = [h for h in headers if h not in existing_headers]

    if missing_headers:
        new_headers = existing_headers + missing_headers
        ws.update("1:1", [new_headers])

    return ws


def find_log_row(ws, target_date, game_key):
    records = ws.get_all_records()

    for idx, row in enumerate(records, start=2):
        if str(row.get("target_date")) == target_date and str(row.get("game_key")) == game_key:
            return idx, row

    return None, None


def update_log_row(ws, row_idx, data):
    headers = ws.row_values(1)
    updates = []

    for key, value in data.items():
        if key in headers:
            col = headers.index(key) + 1
            updates.append({
                "range": gspread.utils.rowcol_to_a1(row_idx, col),
                "values": [[value]],
            })

    if updates:
        ws.batch_update(updates)


def append_log_row(ws, data):
    headers = ws.row_values(1)
    ws.append_row([data.get(h, "") for h in headers])


def fetch_source_page(url):
    r = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()

def strip_prefix(text, prefix):
    text = text.strip()
    prefix = prefix.strip().rstrip(":")

    pattern = rf"^\s*{re.escape(prefix)}\s*:?\s*"
    match = re.match(pattern, text, flags=re.I)

    if not match:
        return ""

    return text[match.end():].strip()


def extract_by_selector_and_prefix(soup, selector, prefix):
    for el in soup.select(selector):
        text = el.get_text(" ", strip=True)
        value = strip_prefix(text, prefix)

        if value:
            return value

    return ""


def extract_question_answer(content_html, game_cfg, cfg):
    question_selector = game_cfg.get("question_selector") or cfg["defaults"]["question_selector"]
    answer_selector = game_cfg.get("answer_selector") or cfg["defaults"]["answer_selector"]

    question_prefix = game_cfg.get("question_prefix") or cfg["defaults"].get("question_prefix", "Question:")
    answer_prefix = game_cfg.get("answer_prefix") or cfg["defaults"].get("answer_prefix", "Answer:")

    soup = BeautifulSoup(content_html, "html.parser")

    question = extract_by_selector_and_prefix(
        soup=soup,
        selector=question_selector,
        prefix=question_prefix,
    )

    answer = extract_by_selector_and_prefix(
        soup=soup,
        selector=answer_selector,
        prefix=answer_prefix,
    )

    return question, answer

def extract_binance_wotd(content_html):
    soup = BeautifulSoup(content_html, "html.parser")

    theme = ""
    reward = ""
    campaign_start = None
    campaign_end = None
    answer_groups = {}

    # Tìm paragraph chứa Theme và Date.
    summary_p = None

    for p in soup.select("p.wp-block-paragraph"):
        text = p.get_text(" ", strip=True)

        if (
            re.search(r"\bTheme\s*:", text, flags=re.I)
            and re.search(r"\bDate\s*:", text, flags=re.I)
        ):
            summary_p = p
            break

    if summary_p:
        summary_text = html.unescape(
            summary_p.get_text(" ", strip=True)
        )

        theme_match = re.search(
            r"Theme\s*:\s*(.*?)\s+Date\s*:",
            summary_text,
            flags=re.I,
        )

        if theme_match:
            theme = theme_match.group(1).strip()

        date_match = re.search(
            r"Date\s*:\s*"
            r"(\d{4}-\d{2}-\d{2})"
            r"\s+to\s+"
            r"(\d{4}-\d{2}-\d{2})",
            summary_text,
            flags=re.I,
        )

        if date_match:
            campaign_start = datetime.fromisoformat(
                date_match.group(1)
            ).date()

            campaign_end = datetime.fromisoformat(
                date_match.group(2)
            ).date()

        # Ưu tiên tìm mark chứa "to be shared".
        for mark in summary_p.find_all("mark"):
            mark_text = mark.get_text(" ", strip=True)

            if "to be shared" in mark_text.lower():
                reward = mark_text
                break

        # Fallback nếu reward không nằm trong mark.
        if not reward:
            reward_match = re.search(
                r"([0-9][A-Za-z0-9 .,+-]*?"
                r"\bto be shared!?)",
                summary_text,
                flags=re.I,
            )

            if reward_match:
                reward = reward_match.group(1).strip()

    # Lấy các nhóm đáp án 3-8 letters.
    for h3 in soup.find_all("h3"):
        heading_text = html.unescape(
            h3.get_text(" ", strip=True)
        )

        length_match = re.search(
            r"Answer\s+(\d+)\s+letters",
            heading_text,
            flags=re.I,
        )

        if not length_match:
            continue

        word_length = length_match.group(1)

        answer_list = h3.find_next_sibling("ul")

        if not answer_list:
            answer_list = h3.find_next("ul")

        answers = []

        if answer_list:
            answers = [
                li.get_text(" ", strip=True)
                for li in answer_list.find_all(
                    "li",
                    recursive=False,
                )
                if li.get_text(" ", strip=True)
            ]

        answer_groups[word_length] = answers

    return {
        "theme": theme,
        "reward": reward,
        "campaign_start": campaign_start,
        "campaign_end": campaign_end,
        "answer_groups": answer_groups,
    }

def make_binance_wotd_signature(answer_data):
    campaign_start = answer_data.get(
        "campaign_start"
    )
    campaign_end = answer_data.get(
        "campaign_end"
    )

    payload = {
        "theme": answer_data.get("theme") or "",
        "reward": answer_data.get("reward") or "",
        "campaign_start": (
            campaign_start.isoformat()
            if campaign_start
            else ""
        ),
        "campaign_end": (
            campaign_end.isoformat()
            if campaign_end
            else ""
        ),
        "answer_groups": answer_data.get(
            "answer_groups",
            {},
        ),
    }

    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        serialized.encode("utf-8")
    ).hexdigest()

def find_label_element(soup, selector, prefix):
    """
    Tìm element chỉ chứa label, ví dụ:
    <p><strong>Simplified:</strong></p>
    """
    clean_prefix = prefix.strip().rstrip(":")

    pattern = re.compile(
        rf"^\s*{re.escape(clean_prefix)}\s*:?\s*$",
        flags=re.I,
    )

    for el in soup.select(selector):
        text = el.get_text(" ", strip=True)

        if pattern.match(text):
            return el

    return None

def extract_hamster_cipher(content_html, game_cfg):
    soup = BeautifulSoup(content_html, "html.parser")

    word_selector = game_cfg.get(
        "word_selector",
        "p.wp-block-paragraph",
    )
    word_prefix = game_cfg.get(
        "word_prefix",
        "Word",
    )

    simplified_label_selector = game_cfg.get(
        "simplified_label_selector",
        "p.wp-block-paragraph",
    )
    simplified_prefix = game_cfg.get(
        "simplified_prefix",
        "Simplified",
    )

    word = ""
    morse_lines = []
    simplified_lines = []

    # =====================================================
    # 1. Tìm paragraph Word và lấy Morse ngay phía sau
    # =====================================================
    word_element = None

    for el in soup.select(word_selector):
        text = el.get_text(" ", strip=True)
        value = strip_prefix(text, word_prefix)

        if value:
            word = value
            word_element = el
            break

    if word_element:
        # Morse thường là paragraph kế tiếp trong cùng column.
        morse_element = word_element.find_next_sibling("p")

        if not morse_element:
            morse_element = word_element.find_next("p")

        if morse_element:
            raw_morse = morse_element.get_text(
                "\n",
                strip=True,
            )

            morse_lines = [
                line.strip()
                for line in raw_morse.splitlines()
                if line.strip()
            ]

    # =====================================================
    # 2. Tìm label Simplified và paragraph kế tiếp
    # =====================================================
    simplified_label = find_label_element(
        soup=soup,
        selector=simplified_label_selector,
        prefix=simplified_prefix,
    )

    if simplified_label:
        simplified_element = (
            simplified_label.find_next_sibling("p")
        )

        if not simplified_element:
            simplified_element = (
                simplified_label.find_next("p")
            )

        if simplified_element:
            raw_simplified = simplified_element.get_text(
                "\n",
                strip=True,
            )

            simplified_lines = [
                line.strip()
                for line in raw_simplified.splitlines()
                if line.strip()
            ]

    return word, morse_lines, simplified_lines

def extract_quote_author(content_html):
    soup = BeautifulSoup(
        content_html,
        "html.parser",
    )

    source_date = None
    quote = ""
    author = ""
    question_element = None

    # =====================================================
    # 1. Lấy Date và tìm paragraph câu hỏi
    # =====================================================
    for p in soup.select("p.wp-block-paragraph"):
        text = html.unescape(
            p.get_text(" ", strip=True)
        )

        if source_date is None:
            date_match = re.search(
                r"\bDate\s*:\s*"
                r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                text,
                flags=re.I,
            )

            if date_match:
                try:
                    source_date = datetime.strptime(
                        date_match.group(1),
                        "%B %d, %Y",
                    ).date()
                except ValueError:
                    source_date = None

        if re.search(
            r"who\s+is\s+the\s+author\s+"
            r"of\s+the\s+quote",
            text,
            flags=re.I,
        ):
            question_element = p

    # =====================================================
    # 2. Quote nằm ngay trước paragraph câu hỏi
    # =====================================================
    if question_element:
        quote_element = (
            question_element.find_previous_sibling("p")
        )

        if quote_element:
            quote = html.unescape(
                quote_element.get_text(
                    " ",
                    strip=True,
                )
            )

    # =====================================================
    # 3. Ưu tiên lấy author từ span.copy-text
    # =====================================================
    author_element = soup.select_one(
        "span.copy-text"
    )

    if author_element:
        author = html.unescape(
            author_element.get_text(
                " ",
                strip=True,
            )
        )

    # Fallback nếu source bỏ span.copy-text.
    if not author:
        for p in soup.select("p.wp-block-paragraph"):
            text = html.unescape(
                p.get_text(" ", strip=True)
            )

            value = strip_prefix(
                text,
                "Answer",
            )

            if value:
                author = value
                break

    return source_date, quote, author

def extract_red_packet_codes(content_html):
    soup = BeautifulSoup(
        content_html,
        "html.parser",
    )

    # Chấp nhận cả:
    # #12 Code Is:
    # #02 No Code Is:
    code_pattern = re.compile(
        r"^\s*#\s*(\d+)\s+"
        r"(?:No\s+)?"
        r"Code\s+Is\s*:?",
        flags=re.I,
    )

    codes_by_number = {}

    for p in soup.select(
        "p.wp-block-paragraph"
    ):
        paragraph_text = html.unescape(
            p.get_text(" ", strip=True)
        )

        match = code_pattern.search(
            paragraph_text
        )

        if not match:
            continue

        number = int(
            match.group(1)
        )

        code_element = p.select_one(
            "span.copy-text"
        )

        if not code_element:
            continue

        # Ưu tiên giá trị dùng cho chức năng copy.
        code = (
            code_element.get(
                "data-original-text"
            )
            or code_element.get_text(
                " ",
                strip=True,
            )
        )

        code = html.unescape(
            str(code)
        ).strip()

        if not code:
            continue

        # Nếu source vô tình có cùng một số nhiều lần,
        # giá trị xuất hiện sau cùng sẽ được sử dụng.
        codes_by_number[number] = code

    # Luôn chuẩn hóa thứ tự:
    # code có số lớn nhất nằm trên cùng.
    return [
        {
            "number": number,
            "code": codes_by_number[number],
        }
        for number in sorted(
            codes_by_number,
            reverse=True,
        )
    ]

def extract_money_bux_codes(content_html):
    soup = BeautifulSoup(
        content_html,
        "html.parser",
    )

    # Chấp nhận:
    # 03. No Code Is:
    # 3. No Code Is:
    # 03. Code Is:
    code_pattern = re.compile(
        r"^\s*(\d+)\.\s+"
        r"(?:No\s+)?"
        r"Code\s+Is\s*:?",
        flags=re.I,
    )

    codes_by_number = {}

    # copy-text chỉ được dùng để nhận dạng
    # vị trí chứa code trong source.
    for code_element in soup.select(
        "span.copy-text"
    ):
        paragraph = code_element.find_parent(
            "p"
        )

        if not paragraph:
            continue

        paragraph_text = html.unescape(
            paragraph.get_text(
                " ",
                strip=True,
            )
        )

        match = code_pattern.search(
            paragraph_text
        )

        if not match:
            continue

        number = int(
            match.group(1)
        )

        # Ưu tiên data-original-text.
        # Nếu không có thì lấy text trong span.
        code = (
            code_element.get(
                "data-original-text"
            )
            or code_element.get_text(
                " ",
                strip=True,
            )
        )

        code = html.unescape(
            str(code)
        ).strip()

        if not code:
            continue

        # Nếu cùng số xuất hiện nhiều lần,
        # lấy giá trị cuối cùng trong source.
        codes_by_number[number] = code

    # Chuẩn hóa thứ tự lớn đến nhỏ:
    # 03, 02, 01
    return [
        {
            "number": number,
            "code": codes_by_number[number],
        }
        for number in sorted(
            codes_by_number,
            reverse=True,
        )
    ]

def find_city_combo_element(soup):
    for h2 in soup.find_all("h2"):
        heading = html.unescape(
            h2.get_text(" ", strip=True)
        ).lower()

        if "city holder daily combo" not in heading:
            continue

        for sibling in h2.next_siblings:
            tag = getattr(sibling, "name", None)

            # Hỗ trợ cả format cũ và mới
            if tag in {"pre", "ol"}:
                return sibling

            # Không tìm tràn sang section kế tiếp
            if tag == "h2":
                break

    return None

def find_pre_after_heading(
    soup,
    heading_match,
):
    """
    Tìm pre nằm sau H2 phù hợp.
    Không dùng #tw-target-text vì source có nhiều ID trùng nhau.
    """
    for h2 in soup.find_all("h2"):
        heading_text = html.unescape(
            h2.get_text(" ", strip=True)
        ).lower()

        if not heading_match(heading_text):
            continue

        for sibling in h2.next_siblings:
            tag_name = getattr(
                sibling,
                "name",
                None,
            )

            if tag_name == "pre":
                return sibling

            # Không tìm tràn sang section tiếp theo.
            if tag_name == "h2":
                break

    return None


def extract_pre_lines(pre_element):
    if not pre_element:
        return []

    raw_text = html.unescape(
        pre_element.get_text("\n")
    )

    lines = []

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()

        if line:
            lines.append(line)

        # Giữ một dòng trống để phân cách RU và EN.
        elif lines and lines[-1] != "":
            lines.append("")

    while lines and lines[-1] == "":
        lines.pop()

    return lines


def extract_numbered_answers(lines):
    answers = []

    for line in lines:
        match = re.match(
            r"^\s*\d+\.\s*(.+?)\s*$",
            line,
        )

        if match:
            answers.append(
                match.group(1).strip()
            )

    return answers


def is_waiting_content(lines):
    text = " ".join(lines).lower()

    waiting_phrases = [
        "we will update",
        "please wait",
        "updating soon",
        "update all answers",
    ]

    return any(
        phrase in text
        for phrase in waiting_phrases
    )

def extract_city_quiz_date(soup):
    for h2 in soup.find_all("h2"):
        heading = normalize_answer(
            h2.get_text(" ", strip=True)
        ).lower()

        if heading != "city holder daily quiz answer":
            continue

        for sibling in h2.next_siblings:
            tag = getattr(sibling, "name", None)

            if tag == "h2":
                break

            if tag != "p":
                continue

            text = html.unescape(
                sibling.get_text(" ", strip=True)
            )

            match = re.search(
                r"\bDate\s*:\s*"
                r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                text,
                flags=re.I,
            )

            if match:
                try:
                    return datetime.strptime(
                        match.group(1),
                        "%B %d, %Y",
                    ).date()
                except ValueError:
                    return None

    return None

def extract_city_quiz_en(soup):
    for h2 in soup.find_all("h2"):
        heading = normalize_answer(
            h2.get_text(" ", strip=True)
        ).lower()

        if heading != "city holder daily quiz answer":
            continue

        for sibling in h2.next_siblings:
            tag = getattr(sibling, "name", None)

            if tag == "h2":
                break

            # Trường hợp source dùng <ol><li>
            if tag == "ol":
                answers = [
                    normalize_answer(
                        li.get_text(" ", strip=True)
                    )
                    for li in sibling.find_all(
                        "li",
                        recursive=False,
                    )
                    if normalize_answer(
                        li.get_text(" ", strip=True)
                    )
                ]

                if answers:
                    return answers

            # Trường hợp source dùng <p> + <br>
            if tag == "p":
                raw = html.unescape(
                    sibling.get_text(
                        "\n",
                        strip=True,
                    )
                )

                answers = []

                for line in raw.splitlines():
                    match = re.match(
                        r"^\s*\d+\s*[.)-]?\s+(.+?)\s*$",
                        line,
                    )

                    if match:
                        answer = normalize_answer(
                            match.group(1)
                        )

                        if answer:
                            answers.append(answer)

                if answers:
                    return answers

    return []

def extract_city_quiz_ru(soup):
    for h2 in soup.find_all("h2"):
        heading = normalize_answer(
            h2.get_text(" ", strip=True)
        ).lower()

        if (
            "city holder daily quiz answer for russia"
            not in heading
        ):
            continue

        for sibling in h2.next_siblings:
            tag = getattr(sibling, "name", None)

            if tag == "h2":
                break

            if tag == "ol":
                return [
                    normalize_answer(
                        li.get_text(
                            " ",
                            strip=True,
                        )
                    )
                    for li in sibling.find_all(
                        "li",
                        recursive=False,
                    )
                    if normalize_answer(
                        li.get_text(
                            " ",
                            strip=True,
                        )
                    )
                ]

    return []

        
def extract_city_holder_data(content_html):
    soup = BeautifulSoup(
        content_html,
        "html.parser",
    )

    # =====================================================
    # 1. COMBO
    # =====================================================

    combo_element = find_city_combo_element(
        soup
    )
    
    combo_lines = []
    
    if combo_element:
        if combo_element.name == "pre":
            # Format cũ
            combo_lines = extract_pre_lines(
                combo_element
            )
    
        elif combo_element.name == "ol":
            # Format mới
            combo_lines = [
                f"{i}. {html.unescape(li.get_text(' ', strip=True))}"
                for i, li in enumerate(
                    combo_element.find_all(
                        "li",
                        recursive=False,
                    ),
                    start=1,
                )
                if li.get_text(" ", strip=True)
            ]
    
    if is_waiting_content(combo_lines):
        combo_lines = []

    # =====================================================
    # 2. QUIZ
    # =====================================================

    quiz_date = extract_city_quiz_date(
        soup
    )

    quiz_en = extract_city_quiz_en(
        soup
    )

    quiz_ru = extract_city_quiz_ru(
        soup
    )

    print(
        "City Holder parsed: "
        f"combo={len(combo_lines)}, "
        f"quiz_date={quiz_date}, "
        f"quiz_en={len(quiz_en)}, "
        f"quiz_ru={len(quiz_ru)}"
    )

    return {
        "combo_lines": combo_lines,
        "quiz_date": quiz_date,
        "quiz_en": quiz_en,
        "quiz_ru": quiz_ru,
    }

def extract_game_answer_data(content_html, game_cfg, cfg):
    answer_type = game_cfg.get(
        "answer_type",
        "question_answer",
    )

    if answer_type == "money_bux_codes":
        codes = extract_money_bux_codes(
            content_html
        )

        # Chuẩn hóa toàn bộ danh sách thành JSON.
        answer_json = json.dumps(
            codes,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        # Chỉ tạo hash khi lấy được ít nhất một code.
        if codes:
            check_value = hashlib.sha256(
                answer_json.encode("utf-8")
            ).hexdigest()
        else:
            check_value = ""

        return {
            "answer_type": "money_bux_codes",

            # Google Sheet.
            "question": "",
            "answer": (
                answer_json
                if codes
                else ""
            ),
            "check_value": check_value,

            # Dữ liệu dùng để tạo HTML.
            "codes": codes,
        }

    if answer_type == "city_holder":
        city_data = extract_city_holder_data(
            content_html
        )

        combo_lines = city_data.get(
            "combo_lines",
            [],
        )

        quiz_date = city_data.get(
            "quiz_date"
        )

        quiz_en = city_data.get(
            "quiz_en",
            [],
        )

        quiz_ru = city_data.get(
            "quiz_ru",
            [],
        )

        answer_payload = {
            "combo_lines": combo_lines,

            "quiz_date": (
                quiz_date.isoformat()
                if quiz_date
                else ""
            ),

            "quiz_en": quiz_en,
            "quiz_ru": quiz_ru,
        }

        has_combo = bool(
            combo_lines
        )

        has_quiz = bool(
            quiz_en
            or quiz_ru
        )

        has_data = bool(
            has_combo
            or has_quiz
        )

        answer_json = json.dumps(
            answer_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        if has_data:
            check_value = hashlib.sha256(
                answer_json.encode("utf-8")
            ).hexdigest()
        else:
            check_value = ""

        # Signature riêng cho Combo.
        # Dùng để phân biệt Combo mới/cũ,
        # không bị Quiz làm ảnh hưởng.
        combo_json = json.dumps(
            combo_lines,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        combo_check_value = (
            hashlib.sha256(
                combo_json.encode("utf-8")
            ).hexdigest()
            if combo_lines
            else ""
        )

        return {
            "answer_type": "city_holder",

            "question": "",

            "answer": (
                answer_json
                if has_data
                else ""
            ),

            "check_value": check_value,

            "combo_check_value": (
                combo_check_value
            ),

            "combo_lines": combo_lines,

            "quiz_date": quiz_date,

            "quiz_en": quiz_en,

            "quiz_ru": quiz_ru,

            "has_combo": has_combo,

            "has_quiz": has_quiz,

            "has_data": has_data,
        }

    if answer_type == "red_packet_codes":
        codes = extract_red_packet_codes(
            content_html
        )

        # JSON được chuẩn hóa để lưu Sheet
        # và tạo hash ổn định.
        answer_json = json.dumps(
            codes,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        # Không tạo hash nếu source chưa có code.
        if codes:
            check_value = hashlib.sha256(
                answer_json.encode("utf-8")
            ).hexdigest()
        else:
            check_value = ""

        return {
            "answer_type": "red_packet_codes",

            # Dữ liệu chung dùng cho Google Sheet.
            "question": "",
            "answer": answer_json if codes else "",
            "check_value": check_value,

            # Dữ liệu dùng để render HTML.
            "codes": codes,
        }

    if answer_type == "quote_author":
        source_date, quote, author = (
            extract_quote_author(
                content_html
            )
        )

        has_data = bool(
            source_date
            and (
                quote
                or author
            )
        )
        
        is_complete = bool(
            source_date
            and quote
            and author
        )

        answer_payload = {
            "source_date": (
                source_date.isoformat()
                if source_date
                else ""
            ),
            "quote": quote,
            "author": author,
        }

        # Chỉ tạo check_value khi dữ liệu đầy đủ.
        # Tránh update post khi source mới chỉ có Date
        # nhưng chưa có quote hoặc author.
        if has_data:
            check_value = json.dumps(
                answer_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            check_value = ""

        return {
            "answer_type": "quote_author",

            # Dữ liệu chung cho Google Sheet.
            "question": quote,
            "answer": author,
            "check_value": check_value,

            # Dữ liệu riêng của Hrum.
            "source_date": source_date,
            "quote": quote,
            "author": author,
            "is_complete": is_complete,
            "has_data": has_data,
        }

    if answer_type == "hamster_cipher":
        word, morse_lines, simplified_lines = (
            extract_hamster_cipher(
                content_html=content_html,
                game_cfg=game_cfg,
            )
        )
    
        answer_payload = {
            "word": word,
            "morse_lines": morse_lines,
            "simplified_lines": simplified_lines,
        }
    
        # Signature giúp phát hiện thay đổi không chỉ ở Word,
        # mà cả Morse và Hold/Tap.
        check_value = json.dumps(
            answer_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    
        answer_text_parts = []
    
        if morse_lines:
            answer_text_parts.append(
                "Morse:\n" + "\n".join(morse_lines)
            )
    
        if simplified_lines:
            answer_text_parts.append(
                "Simplified:\n"
                + "\n".join(simplified_lines)
            )
    
        return {
            "answer_type": "hamster_cipher",
    
            # Dùng các cột Sheet hiện tại.
            "question": word,
            "answer": "\n\n".join(answer_text_parts),
            "check_value": check_value,
    
            # Dữ liệu riêng để render HTML.
            "word": word,
            "morse_lines": morse_lines,
            "simplified_lines": simplified_lines,
        }

    if answer_type == "binance_wotd":
        wotd_data = extract_binance_wotd(
            content_html
        )
    
        signature = make_binance_wotd_signature(
            wotd_data
        )
    
        answer_groups_json = json.dumps(
            wotd_data.get("answer_groups", {}),
            ensure_ascii=False,
            sort_keys=True,
        )
    
        return {
            "answer_type": "binance_wotd",
    
            # Dữ liệu chung dùng cho Sheet.
            "question": wotd_data.get("theme", ""),
            "answer": answer_groups_json,
            "check_value": signature,
    
            # Dữ liệu riêng của Binance WOTD.
            "theme": wotd_data.get("theme", ""),
            "reward": wotd_data.get("reward", ""),
            "campaign_start": wotd_data.get(
                "campaign_start"
            ),
            "campaign_end": wotd_data.get(
                "campaign_end"
            ),
            "answer_groups": wotd_data.get(
                "answer_groups",
                {},
            ),
        }

    if answer_type == "question_answer":
        question, answer = extract_question_answer(
            content_html=content_html,
            game_cfg=game_cfg,
            cfg=cfg,
        )

        return {
            "answer_type": "question_answer",
            "question": question,
            "answer": answer,
            "check_value": answer,
        }

    raise RuntimeError(
        f"Unsupported answer_type: {answer_type}"
    )

def make_waiting_answer_data(
    game_cfg,
    campaign_start=None,
    campaign_end=None,
):
    answer_type = game_cfg.get(
        "answer_type",
        "question_answer",
    )

    if answer_type == "money_bux_codes":
        return {
            "answer_type": "money_bux_codes",
            "question": "",
            "answer": "Updating soon.",
            "check_value": "",
            "codes": [],
        }

    if answer_type == "city_holder":
        return {
            "answer_type": "city_holder",
            "question": "",
            "answer": "Updating soon.",
            "check_value": "",
            "combo_lines": [],
            "quiz_en": [],
            "quiz_ru": [],
            "has_data": False,
        }

    if answer_type == "red_packet_codes":
        return {
            "answer_type": "red_packet_codes",
            "question": "",
            "answer": "Updating soon.",
            "check_value": "",
            "codes": [],
        }

    if answer_type == "quote_author":
        return {
            "answer_type": "quote_author",
            "question": "Updating soon.",
            "answer": "Updating soon.",
            "check_value": "",
            "source_date": None,
            "quote": "Updating soon.",
            "author": "Updating soon.",
            "is_complete": False,
            "has_data": False,
        }

    if answer_type == "binance_wotd":
        empty_groups = {
            str(length): []
            for length in range(3, 9)
        }

        return {
            "answer_type": "binance_wotd",
            "question": "Updating soon.",
            "answer": json.dumps(
                empty_groups,
                ensure_ascii=False,
                sort_keys=True,
            ),
            "check_value": "",
            "theme": "Updating soon.",
            "reward": "Updating soon.",
            "campaign_start": campaign_start,
            "campaign_end": campaign_end,
            "answer_groups": empty_groups,
        }

    if answer_type == "hamster_cipher":
        return {
            "answer_type": "hamster_cipher",
            "question": "Updating soon.",
            "answer": "Updating soon.",
            "check_value": "",
            "word": "Updating soon.",
            "morse_lines": [],
            "simplified_lines": [],
        }

    return {
        "answer_type": "question_answer",
        "question": "Updating soon.",
        "answer": "Updating soon.",
        "check_value": "",
    }


def fetch_crypto_data(cfg):
    url = cfg["crypto_snapshot"]["coingecko_url"]

    r = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=60,
    )
    r.raise_for_status()

    data = {}

    for item in r.json():
        data[item["id"]] = {
            "name": item["name"],
            "symbol": item["symbol"].upper(),
            "price": item.get("current_price"),
            "market_cap": item.get("market_cap"),
            "change_24h": item.get("price_change_percentage_24h"),
        }

    return data


def fmt_price(x):
    if x is None:
        return "N/A"
    return f"${x:,.0f}" if x >= 100 else f"${x:,.2f}"


def fmt_pct(x):
    if x is None:
        return "N/A"
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.2f}%"


def make_base_snapshot(crypto_data):
    btc = crypto_data["bitcoin"]
    eth = crypto_data["ethereum"]

    btc_direction = "up" if btc["change_24h"] >= 0 else "down"
    eth_direction = "up" if eth["change_24h"] >= 0 else "down"

    btc_market_cap = btc["market_cap"] or 0
    eth_market_cap = eth["market_cap"] or 0

    return (
        f"Bitcoin (BTC): ~{fmt_price(btc['price'])}, {btc_direction} {fmt_pct(btc['change_24h'])} in the last 24h. "
        f"Bitcoin price remains an important market benchmark for BTC/USDT traders, with BTC market cap around ${btc_market_cap:,.0f}.\n\n"
        f"Ethereum (ETH): ~{fmt_price(eth['price'])}, {eth_direction} {fmt_pct(eth['change_24h'])} in the last 24h. "
        f"Ethereum price continues to guide ETH/USDT liquidity and broader altcoin sentiment."
    )


def rewrite_snapshot_with_openai(cfg, game_key, base_snapshot):
    client = OpenAI(api_key=get_env("OPENAI_API_KEY"))

    phrases = ", ".join(cfg["crypto_snapshot"]["required_phrases"])

    prompt = f"""
You are a professional cryptocurrency market editor.

Rewrite the following crypto market update.

Requirements:

- Preserve every number exactly as provided.
- Do not change prices or percentages.
- Do not invent any information.
- Do not mention market capitalization.
- Do not mention trading volume.
- Do not mention predictions, forecasts, or future price movements.
- Do not mention the game name, edition, article title, or any branding.
- Keep exactly two paragraphs:
  - one for Bitcoin
  - one for Ethereum
- Begin each paragraph exactly with the supplied first sentence.
- After the first sentence, write TWO or THREE additional sentences describing only the current market situation.
- The additional sentences should discuss topics such as short-term momentum, market sentiment, trading activity, or ecosystem strength.
- Do not mention any information that cannot be reasonably inferred from the provided price movement.
- Never invent news, partnerships, ETF approvals, on-chain metrics, or macroeconomic events.
- Naturally include these phrases:
  - Bitcoin price
  - BTC/USDT
  - Ethereum price
  - ETH/USDT
- Return HTML only using two <p> elements.

Input:

{base_snapshot}
""".strip()
    resp = client.responses.create(
        model=cfg["openai"]["model"],
        input=prompt,
    )

    return resp.output_text.strip()


def link_text_once_in_soup(soup, phrase, url):
    existing = soup.find("a", string=lambda s: s and phrase.lower() in s.lower())
    if existing:
        return

    pattern = re.compile(re.escape(phrase), re.I)

    for text_node in soup.find_all(string=pattern):
        if text_node.find_parent("a"):
            continue

        original = str(text_node)
        match = pattern.search(original)

        if not match:
            continue

        before = original[:match.start()]
        matched = original[match.start():match.end()]
        after = original[match.end():]

        a = soup.new_tag("a", href=url)
        a.string = matched

        new_nodes = []

        if before:
            new_nodes.append(NavigableString(before))

        new_nodes.append(a)

        if after:
            new_nodes.append(NavigableString(after))

        text_node.replace_with(*new_nodes)
        return


def auto_link_html(content_html, cfg):
    soup = BeautifulSoup(content_html, "html.parser")

    items = sorted(
        cfg["auto_links"].items(),
        key=lambda x: len(x[0]),
        reverse=True,
    )

    for phrase, url in items:
        link_text_once_in_soup(soup, phrase, url)

    return str(soup)


def update_quiz_answer_block(content_html, game_cfg, question, answer):
    soup = BeautifulSoup(content_html, "html.parser")

    heading_contains = game_cfg.get("answer_heading_contains", "Quiz Answers Today").lower()

    target_h2 = None

    for h2 in soup.find_all("h2"):
        h2_text = h2.get_text(" ", strip=True).lower()
        if heading_contains in h2_text:
            target_h2 = h2
            break

    if not target_h2:
        raise RuntimeError(f"Quiz answer H2 not found: {heading_contains}")

    target_p = target_h2.find_next("p")

    if not target_p:
        target_p = soup.new_tag("p")
        target_h2.insert_after(target_p)

    target_p.clear()

    q_label = soup.new_tag("strong")
    q_label.string = "Question:"
    target_p.append(q_label)
    target_p.append(soup.new_tag("br"))
    target_p.append(question or "Updating soon.")

    target_p.append(soup.new_tag("br"))
    target_p.append(soup.new_tag("br"))

    a_label = soup.new_tag("strong")
    a_label.string = "Correct Answer:"
    target_p.append(a_label)
    target_p.append(soup.new_tag("br"))
    target_p.append(answer or "Updating soon.")

    return str(soup)

def build_quote_author_answer_area(
    readable_date,
    quote,
    author,
):
    safe_date = html.escape(
        str(readable_date or "")
    )

    safe_quote = html.escape(
        str(quote or "Updating soon.")
    )

    safe_author = html.escape(
        str(author or "Updating soon.")
    )

    return (
        f"<p><strong>Date:</strong> "
        f"{safe_date}</p>\n"

        f"<p>{safe_quote}</p>\n"

        "<p>"
        "Who is the author of the quote?"
        "</p>\n"

        "<p><strong>Answer:</strong> "
        f"{safe_author}</p>"
    )

def build_money_bux_answer_area(
    codes,
):
    if not codes:
        return "<p>Updating soon.</p>"

    html_parts = []

    for item in codes:
        number = int(
            item["number"]
        )

        code = str(
            item["code"]
        ).strip()

        safe_code = html.escape(
            code
        )

        html_parts.append(
            f"<p>"
            f"{number:02d}. No Code Is: "
            f"<strong>{safe_code}</strong>"
            f"</p>"
        )

    return "\n".join(
        html_parts
    )

def build_red_packet_answer_area(
    codes,
    last_updated_text,
):
    safe_updated_text = html.escape(
        str(last_updated_text)
    )

    html_parts = [
        (
            "<p><strong>Last updated:</strong> "
            f"{safe_updated_text}</p>"
        )
    ]

    if not codes:
        html_parts.append(
            "<p>Updating soon.</p>"
        )

        return "\n".join(html_parts)

    for item in codes:
        number = int(
            item["number"]
        )

        code = str(
            item["code"]
        ).strip()

        safe_code_text = html.escape(
            code
        )

        safe_code_attr = html.escape(
            code,
            quote=True,
        )

        html_parts.append(
            f"<p>#{number:02d} Code Is: "
            f'<span class="copy-text" '
            f'data-original-text="{safe_code_attr}">'
            f"{safe_code_text}"
            "</span>"
            "</p>"
        )

    return "\n".join(html_parts)

def build_city_holder_answer_area(
    readable_date,
    combo_lines,
    quiz_en,
    quiz_ru,
):
    safe_date = html.escape(
        str(readable_date or "")
    )

    html_parts = []

    # =====================================================
    # COMBO
    # =====================================================
    html_parts.append(
        "<h2><strong>"
        "City Holder Daily Combo Today Answers for "
        f"{safe_date}"
        "</strong></h2>"
    )

    html_parts.append(
        "<p>Today’s Combo:</p>"
    )

    if combo_lines:
        combo_text = "\n".join(
            html.escape(str(line))
            for line in combo_lines
        )

        html_parts.append(
            '<pre class="wp-block-preformatted">'
            f"{combo_text}"
            "</pre>"
        )
    else:
        html_parts.append(
            "<p>Updating soon.</p>"
        )

    html_parts.append(
        "<p>"
        "Open the combo section inside the official "
        "City Holder Telegram mini app and upgrade "
        "these buildings in the order shown. "
        "The exact category mix (residential, commercial, "
        "or industrial) changes day to day — don't assume "
        "it's always split evenly across zones, just match "
        "today's list above."
        "</p>"
    )

    html_parts.append(
        "<p><em>"
        "Reward: up to 5 million in-game coins "
        "for a correct combo."
        "</em></p>"
    )

    # =====================================================
    # QUIZ ENGLISH
    # =====================================================
    html_parts.append(
        "<h2><strong>"
        "City Holder Daily Quiz Answer - "
        f"{safe_date}"
        "</strong></h2>"
    )

    if quiz_en:
        quiz_en_items = [
            f"<li>{html.escape(str(answer))}</li>"
            for answer in quiz_en
        ]

        html_parts.append(
            "<ol>\n"
            + "\n".join(quiz_en_items)
            + "\n</ol>"
        )
    else:
        html_parts.append(
            "<p>Updating soon.</p>"
        )

    # =====================================================
    # QUIZ RUSSIA
    # =====================================================
    html_parts.append(
        "<h2><strong>"
        "City Holder Daily Quiz Answer For Russia - "
        f"{safe_date}"
        "</strong></h2>"
    )

    if quiz_ru:
        quiz_ru_items = [
            f"<li>{html.escape(str(answer))}</li>"
            for answer in quiz_ru
        ]

        html_parts.append(
            "<ol>\n"
            + "\n".join(quiz_ru_items)
            + "\n</ol>"
        )
    else:
        html_parts.append(
            "<p>Updating soon.</p>"
        )

    html_parts.append(
        "<p><em>"
        "Reward: up to 2.5 million in-game coins "
        "for a correct quiz. Combined with the combo, "
        "that's the full 7.5 million coin daily cap."
        "</em></p>"
    )

    return "\n".join(html_parts)

def build_hamster_cipher_answer_area(
    readable_date,
    word,
    morse_lines,
    simplified_lines,
):
    safe_date = html.escape(
        str(readable_date or "")
    )

    safe_word = html.escape(
        str(word or "Updating soon.")
    )

    html_parts = []

    html_parts.append(
        "<p>"
        f"<strong>Date:</strong> {safe_date}"
        " &nbsp; "
        f"<strong>Word:</strong> {safe_word}"
        "</p>"
    )

    # =====================================================
    # Morse gốc
    # =====================================================
    if morse_lines:
        morse_html = "<br>\n".join(
            html.escape(str(line))
            for line in morse_lines
            if str(line).strip()
        )

        html_parts.append(
            "<p>"
            "Each letter in today’s cipher is represented "
            "by the following dot-and-dash sequence:"
            "</p>"
        )

        html_parts.append(
            f"<p>{morse_html}</p>"
        )

    # =====================================================
    # Hold/Tap simplified, chỉ hiển thị nếu source có dữ liệu
    # =====================================================
    if simplified_lines:
        simplified_html = "<br>\n".join(
            html.escape(str(line))
            for line in simplified_lines
            if str(line).strip()
        )

        html_parts.append(
            "<p>"
            "Use the following Hold/Tap pattern to enter "
            "the cipher in the GameDev mini-game:"
            "</p>"
        )

        html_parts.append(
            f"<p>{simplified_html}</p>"
        )

    # Chỉ hiện Updating soon nếu cả Morse và Simplified đều trống.
    if not morse_lines and not simplified_lines:
        html_parts.append(
            "<p>Updating soon.</p>"
        )

    return "\n".join(html_parts)

def build_binance_wotd_answer_area(
    answer_data,
    last_verified_date=None,
):
    campaign_start = answer_data.get(
        "campaign_start"
    )
    campaign_end = answer_data.get(
        "campaign_end"
    )

    if campaign_start and campaign_end:
        campaign_range = format_date_range_readable(
            campaign_start,
            campaign_end,
        )
    else:
        campaign_range = "Updating soon."

    theme = html.escape(
        str(
            answer_data.get("theme")
            or "Updating soon."
        )
    )

    reward = html.escape(
        str(
            answer_data.get("reward")
            or "Updating soon."
        )
    )

    if last_verified_date:
        verified_text = format_date_readable(
            last_verified_date
        )
    else:
        verified_text = "Updating soon."

    answer_groups = answer_data.get(
        "answer_groups",
        {},
    )

    html_parts = []

    html_parts.append(
        "<h2><strong>"
        "Binance Word of the Day (WOTD) "
        f"Answer Today – {html.escape(campaign_range)}"
        "</strong></h2>"
    )

    html_parts.append(
        "<p><strong>Campaign Theme:</strong> "
        f"{theme}</p>"
    )

    html_parts.append(
        "<p><strong>Activity Dates:</strong> "
        f"{html.escape(campaign_range)}</p>"
    )

    html_parts.append(
        "<p><strong>Last Verified:</strong> "
        f"{html.escape(verified_text)}</p>"
    )

    html_parts.append(
        "<p><strong>Prize pool:</strong> "
        f"{reward}</p>"
    )

    html_parts.append(
        "<p><strong>"
        "How Many Letters Is the Word "
        "You Are Searching For?"
        "</strong></p>"
    )

    # Danh sách link nhảy xuống heading.
    navigation_items = []

    for length in range(3, 9):
        anchor_id = (
            f"binance-wotd-{length}-letters"
        )

        navigation_items.append(
            "<li>"
            f'<a href="#{anchor_id}">'
            "<strong>"
            "Binance Word of the Day "
            f"{length} letters today"
            "</strong>"
            "</a>"
            "</li>"
        )

    html_parts.append(
        "<ul>\n"
        + "\n".join(navigation_items)
        + "\n</ul>"
    )

    # Heading + answers.
    for length in range(3, 9):
        length_key = str(length)

        anchor_id = (
            f"binance-wotd-{length}-letters"
        )

        html_parts.append(
            f'<h3 id="{anchor_id}">'
            "<strong>"
            "Binance Word of the Day "
            f"{length} Letters Answers"
            "</strong>"
            "</h3>"
        )

        answers = answer_groups.get(
            length_key,
            [],
        )

        if answers:
            list_items = [
                f"<li>{html.escape(str(answer))}</li>"
                for answer in answers
            ]
        else:
            list_items = [
                "<li>Updating soon.</li>"
            ]

        html_parts.append(
            "<ul>\n"
            + "\n".join(list_items)
            + "\n</ul>"
        )

    return "\n".join(html_parts)


def replace_answer_area(
    content_html,
    game_cfg,
    answer_area_html,
):
    soup = BeautifulSoup(
        content_html,
        "html.parser",
    )

    answer_area_id = game_cfg.get(
        "answer_area_id",
        "answer-area",
    )

    target = soup.find(
        "div",
        id=answer_area_id,
    )

    if not target:
        raise RuntimeError(
            f"Answer area div not found: #{answer_area_id}"
        )

    # Xóa toàn bộ nội dung cũ bên trong div,
    # nhưng giữ lại chính div và id của nó.
    target.clear()

    fragment = BeautifulSoup(
        answer_area_html,
        "html.parser",
    )

    # Dùng list() vì các node bị di chuyển khỏi fragment
    # trong lúc append.
    for node in list(fragment.contents):
        target.append(node)

    return str(soup)

def update_existing_answer_content(
    content_html,
    game_cfg,
    answer_data,
    readable_date=None,
    last_verified_date=None,
    last_updated_text=None,
):
    answer_type = answer_data.get(
        "answer_type",
        "question_answer",
    )

    if answer_type == "money_bux_codes":
        answer_area_html = (
            build_money_bux_answer_area(
                codes=answer_data.get(
                    "codes",
                    [],
                ),
            )
        )

        return replace_answer_area(
            content_html=content_html,
            game_cfg=game_cfg,
            answer_area_html=answer_area_html,
        )

    if answer_type == "city_holder":
        answer_area_html = (
            build_city_holder_answer_area(
                readable_date=readable_date,
                combo_lines=answer_data.get(
                    "combo_lines",
                    [],
                ),
                quiz_en=answer_data.get(
                    "quiz_en",
                    [],
                ),
                quiz_ru=answer_data.get(
                    "quiz_ru",
                    [],
                ),
            )
        )

        return replace_answer_area(
            content_html=content_html,
            game_cfg=game_cfg,
            answer_area_html=answer_area_html,
        )

    if answer_type == "red_packet_codes":
        answer_area_html = (
            build_red_packet_answer_area(
                codes=answer_data.get(
                    "codes",
                    [],
                ),
                last_updated_text=(
                    last_updated_text
                    or "Updating soon."
                ),
            )
        )

        return replace_answer_area(
            content_html=content_html,
            game_cfg=game_cfg,
            answer_area_html=answer_area_html,
        )

    if answer_type == "quote_author":
        answer_area_html = (
            build_quote_author_answer_area(
                readable_date=readable_date,
                quote=answer_data.get("quote"),
                author=answer_data.get("author"),
            )
        )

        return replace_answer_area(
            content_html=content_html,
            game_cfg=game_cfg,
            answer_area_html=answer_area_html,
        )

    if answer_type == "binance_wotd":
        answer_area_html = build_binance_wotd_answer_area(
            answer_data=answer_data,
            last_verified_date=last_verified_date,
        )

        return replace_answer_area(
            content_html=content_html,
            game_cfg=game_cfg,
            answer_area_html=answer_area_html,
        )

    if answer_type == "hamster_cipher":
        answer_area_html = (
            build_hamster_cipher_answer_area(
                readable_date=readable_date,
                word=answer_data.get("word"),
                morse_lines=answer_data.get(
                    "morse_lines",
                    [],
                ),
                simplified_lines=answer_data.get(
                    "simplified_lines",
                    [],
                ),
            )
        )

        return replace_answer_area(
            content_html=content_html,
            game_cfg=game_cfg,
            answer_area_html=answer_area_html,
        )

    return update_quiz_answer_block(
        content_html=content_html,
        game_cfg=game_cfg,
        question=answer_data.get("question"),
        answer=answer_data.get("answer"),
    )

def build_content(
    game_cfg,
    cfg,
    date_str,
    answer_data,
    crypto_snapshot_html,
    readable_date=None,
    slug_date=None,
    extra_vars=None,
):
    """
    Build toàn bộ content từ template.

    Hỗ trợ:
    - question_answer
    - hamster_cipher
    - binance_wotd
    - red_packet_codes
    - city_holder
    - money_bux_codes
    """

    # -----------------------------------------------------
    # 1. Fallback cho game daily cũ
    # -----------------------------------------------------
    if readable_date is None:
        readable_date = target_date_readable(
            cfg["timezone"]
        )

    if slug_date is None:
        slug_date = target_date_slug(
            cfg["timezone"]
        )

    # -----------------------------------------------------
    # 2. Đọc template của game
    # -----------------------------------------------------
    template_file = game_cfg["template_file"]

    with open(
        template_file,
        "r",
        encoding="utf-8",
    ) as f:
        template = f.read()

    # -----------------------------------------------------
    # 3. Thay toàn bộ biến ngày
    # -----------------------------------------------------
    content = replace_game_vars(
        text=template,
        date_str=date_str,
        readable_date=readable_date,
        slug_date=slug_date,
        extra_vars=extra_vars,
    )

    # -----------------------------------------------------
    # 4. Chèn crypto snapshot
    # -----------------------------------------------------
    content = content.replace(
        "{{CRYPTO_SNAPSHOT}}",
        crypto_snapshot_html or "",
    )

    # -----------------------------------------------------
    # 5. Xác định loại answer
    # -----------------------------------------------------
    answer_type = answer_data.get(
        "answer_type",
        game_cfg.get(
            "answer_type",
            "question_answer",
        ),
    )

    # =====================================================
    # BINANCE WORD OF THE DAY
    # =====================================================
    if answer_type == "binance_wotd":
        placeholder = game_cfg.get(
            "answer_placeholder",
            "{{ANSWER_AREA}}",
        )

        placeholder_count = content.count(
            placeholder
        )

        if placeholder_count != 1:
            raise RuntimeError(
                f"Binance WOTD template must contain "
                f"exactly one {placeholder}. "
                f"Found: {placeholder_count}"
            )

        answer_area_html = (
            build_binance_wotd_answer_area(
                answer_data=answer_data,
                last_verified_date=answer_data.get(
                    "last_verified_date"
                ),
            )
        )

        content = content.replace(
            placeholder,
            answer_area_html,
            1,
        )

    # =====================================================
    # MONEY BUX CODES
    # =====================================================
    elif answer_type == "money_bux_codes":
        placeholder = game_cfg.get(
            "answer_placeholder",
            "{{ANSWER_AREA}}",
        )

        placeholder_count = content.count(
            placeholder
        )

        if placeholder_count != 1:
            raise RuntimeError(
                f"Money Bux template must contain "
                f"exactly one {placeholder}. "
                f"Found: {placeholder_count}"
            )

        answer_area_html = (
            build_money_bux_answer_area(
                codes=answer_data.get(
                    "codes",
                    [],
                ),
            )
        )

        content = content.replace(
            placeholder,
            answer_area_html,
            1,
        )

    # =====================================================
    # CITY HOLDER
    # =====================================================
    elif answer_type == "city_holder":
        placeholder = game_cfg.get(
            "answer_placeholder",
            "{{ANSWER_AREA}}",
        )

        placeholder_count = content.count(
            placeholder
        )

        if placeholder_count != 1:
            raise RuntimeError(
                f"City Holder template must contain "
                f"exactly one {placeholder}. "
                f"Found: {placeholder_count}"
            )

        answer_area_html = (
            build_city_holder_answer_area(
                readable_date=readable_date,
                combo_lines=answer_data.get(
                    "combo_lines",
                    [],
                ),
                quiz_en=answer_data.get(
                    "quiz_en",
                    [],
                ),
                quiz_ru=answer_data.get(
                    "quiz_ru",
                    [],
                ),
            )
        )

        content = content.replace(
            placeholder,
            answer_area_html,
            1,
        )

    # =====================================================
    # BINANCE RED PACKET CODES
    # =====================================================
    elif answer_type == "red_packet_codes":
        placeholder = game_cfg.get(
            "answer_placeholder",
            "{{ANSWER_AREA}}",
        )

        placeholder_count = content.count(
            placeholder
        )

        if placeholder_count != 1:
            raise RuntimeError(
                f"Red Packet template must contain "
                f"exactly one {placeholder}. "
                f"Found: {placeholder_count}"
            )

        last_updated_text = (
            format_datetime_readable(
                now_local(
                    cfg["timezone"]
                )
            )
        )

        answer_area_html = (
            build_red_packet_answer_area(
                codes=answer_data.get(
                    "codes",
                    [],
                ),
                last_updated_text=(
                    last_updated_text
                ),
            )
        )

        content = content.replace(
            placeholder,
            answer_area_html,
            1,
        )

    # =====================================================
    # HAMSTER CIPHER
    # =====================================================
    elif answer_type == "hamster_cipher":
        placeholder = game_cfg.get(
            "answer_placeholder",
            "{{ANSWER_AREA}}",
        )

        placeholder_count = content.count(
            placeholder
        )

        if placeholder_count != 1:
            raise RuntimeError(
                f"Hamster Cipher template must contain "
                f"exactly one {placeholder}. "
                f"Found: {placeholder_count}"
            )

        answer_area_html = (
            build_hamster_cipher_answer_area(
                readable_date=readable_date,
                word=answer_data.get("word"),
                morse_lines=answer_data.get(
                    "morse_lines",
                    [],
                ),
                simplified_lines=answer_data.get(
                    "simplified_lines",
                    [],
                ),
            )
        )

        content = content.replace(
            placeholder,
            answer_area_html,
            1,
        )

    # =====================================================
    # HRUM QUOTE OF THE DAY
    # =====================================================
    elif answer_type == "quote_author":
        placeholder = game_cfg.get(
            "answer_placeholder",
            "{{ANSWER_AREA}}",
        )

        placeholder_count = content.count(
            placeholder
        )

        if placeholder_count != 1:
            raise RuntimeError(
                f"Hrum template must contain "
                f"exactly one {placeholder}. "
                f"Found: {placeholder_count}"
            )

        answer_area_html = (
            build_quote_author_answer_area(
                readable_date=readable_date,
                quote=answer_data.get("quote"),
                author=answer_data.get("author"),
            )
        )

        content = content.replace(
            placeholder,
            answer_area_html,
            1,
        )

    # =====================================================
    # GAME QUESTION / ANSWER THÔNG THƯỜNG
    # =====================================================
    elif answer_type == "question_answer":
        content = update_quiz_answer_block(
            content_html=content,
            game_cfg=game_cfg,
            question=answer_data.get(
                "question"
            ),
            answer=answer_data.get(
                "answer"
            ),
        )

    else:
        raise RuntimeError(
            f"Unsupported answer_type "
            f"in build_content: {answer_type}"
        )

    # -----------------------------------------------------
    # 6. Chèn internal links tự động
    # -----------------------------------------------------
    return auto_link_html(
        content,
        cfg,
    )

def get_tag_ids_for_post_url(
    cfg,
    slug,
):
    site_url = cfg["wp"]["site_url"].rstrip("/")

    # URL dự kiến dùng để phân loại.
    # URL thật có thể chứa thêm category,
    # nhưng slug vẫn là phần nhận dạng chính.
    post_url = (
        f"{site_url}/"
        f"{str(slug).strip('/')}/"
    ).lower()

    matched_tag_ids = []

    for rule in cfg.get(
        "tag_rules",
        [],
    ):
        keyword = str(
            rule.get(
                "url_contains",
                "",
            )
        ).strip().lower()

        if not keyword:
            continue

        if keyword not in post_url:
            continue

        for tag_id in rule.get(
            "tag_ids",
            [],
        ):
            try:
                tag_id = int(tag_id)
            except (TypeError, ValueError):
                print(
                    "Invalid tag ID in config: "
                    f"{tag_id}"
                )
                continue

            if tag_id not in matched_tag_ids:
                matched_tag_ids.append(
                    tag_id
                )

    print(
        f"Tag matching URL: {post_url}"
    )

    print(
        f"Matched tag IDs: "
        f"{matched_tag_ids}"
    )

    return matched_tag_ids

def create_wp_post(cfg, game_cfg, title, slug, content, target_date,):
    url = f"{cfg['wp']['site_url'].rstrip('/')}/wp-json/wp/v2/posts"

    run_mode = os.getenv("RUN_MODE", "update").lower()
    featured_media_id = game_cfg.get("featured_media_id") or cfg["wp"].get("featured_media_id")

    payload = {
        "title": title,
        "slug": slug,
        "lang": cfg["wp"]["language"],
        "content": content,
        "author": cfg["wp"]["author_id"],
        "categories": game_cfg.get(
            "category_ids",
            cfg["wp"]["category_ids"],
        ),
    }

    tag_ids = get_tag_ids_for_post_url(
        cfg=cfg,
        slug=slug,
    )
    
    if tag_ids:
        payload["tags"] = tag_ids
    else:
        print(
            "No tag rule matched. "
            "Post will be created without tags."
    )

    if run_mode == "create":
        # Tất cả post mới đều tạo dưới dạng draft.
        # Chỉ publish khi update job lấy được answer hợp lệ.
        payload["status"] = "draft"
    else:
        payload["status"] = "publish"

    if featured_media_id:
        payload["featured_media"] = int(featured_media_id)

    print(
        f"Creating post with tags: "
        f"{payload.get('tags', [])}"
    )

    r = requests.post(
        url,
        headers={**wp_headers(cfg), "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )

    if r.status_code >= 400:
        raise RuntimeError(f"Post create failed {r.status_code}: {r.text[:2000]}")

    return r.json()


def update_wp_post(
    cfg,
    post_id,
    content,
    publish_now=False,
):
    url = f"{cfg['wp']['site_url'].rstrip('/')}/wp-json/wp/v2/posts/{post_id}"

    payload = {
        "content": content,
    }

    if publish_now:
        payload["status"] = "publish"
        payload["date"] = now_local(
            cfg["timezone"]
        ).isoformat()

    r = requests.post(
        url,
        headers={**wp_headers(cfg), "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )

    if r.status_code >= 400:
        raise RuntimeError(f"Post update failed {r.status_code}: {r.text[:2000]}")

    return r.json()


def get_wp_post(cfg, post_id):
    url = f"{cfg['wp']['site_url'].rstrip('/')}/wp-json/wp/v2/posts/{post_id}?context=edit"

    r = requests.get(
        url,
        headers=wp_headers(cfg),
        timeout=60,
    )

    if r.status_code >= 400:
        raise RuntimeError(f"Post fetch failed {r.status_code}: {r.text[:2000]}")

    return r.json()

def find_wp_posts_by_slug(
    cfg,
    slug,
):
    url = (
        f"{cfg['wp']['site_url'].rstrip('/')}"
        f"/wp-json/wp/v2/posts"
    )

    clean_slug = normalize_slug(
        slug
    )

    # Phải check tất cả status vì mặc định
    # WordPress REST API chỉ trả published posts.
    params = [
        ("context", "edit"),
        ("slug", clean_slug),
        ("per_page", 100),
        (
            "_fields",
            "id,slug,status,link,date,modified",
        ),
        ("status[]", "publish"),
        ("status[]", "future"),
        ("status[]", "draft"),
        ("status[]", "pending"),
        ("status[]", "private"),
    ]

    r = requests.get(
        url,
        headers=wp_headers(cfg),
        params=params,
        timeout=60,
    )

    if r.status_code >= 400:
        raise RuntimeError(
            f"WordPress slug check failed "
            f"{r.status_code}: "
            f"{r.text[:2000]}"
        )

    posts = r.json()

    # Double-check exact slug.
    exact_matches = [
        post
        for post in posts
        if normalize_slug(
            post.get("slug", "")
        ) == clean_slug
    ]

    return exact_matches


def update_rankmath_meta(cfg, post_id, seo_title, meta_description):
    url = f"{cfg['wp']['site_url'].rstrip('/')}/wp-json/rankmath/v1/updateMeta"

    payload = {
        "objectID": post_id,
        "objectType": "post",
        "meta": {
            "rank_math_title": seo_title,
            "rank_math_description": meta_description,
            "rank_math_focus_keyword": seo_title,
        },
    }

    r = requests.post(
        url,
        headers=wp_headers(cfg),
        json=payload,
        timeout=60,
    )

    print("RankMath update:", r.status_code, r.text[:300])


def should_run_game_now(cfg, game_cfg):
    run_times = game_cfg.get("run_times")

    if not run_times:
        return True

    current = current_time_hhmm(cfg["timezone"])
    return current in run_times


def should_update_answer(current_answer, check_answer):
    current_answer_norm = normalize_answer(current_answer)
    check_answer_norm = normalize_answer(check_answer)

    if not current_answer_norm:
        return False

    return current_answer_norm != check_answer_norm


def has_publishable_answer(answer_data):
    answer_type = answer_data.get(
        "answer_type",
        "question_answer",
    )

    if answer_type == "city_holder":
        # Chỉ Combo được phép quyết định publish.
        # Quiz EN/RU không phải tín hiệu publish.
        return bool(
            answer_data.get("combo_lines")
        )

    if answer_type in {
        "money_bux_codes",
        "red_packet_codes",
    }:
        return bool(
            answer_data.get("codes")
        )

    if answer_type == "quote_author":
        # Hrum chỉ publish khi đã có đáp án author.
        return bool(
            answer_data.get("source_date")
            and normalize_answer(
                answer_data.get("author")
            )
        )

    if answer_type == "hamster_cipher":
        word = normalize_answer(
            answer_data.get("word")
        ).lower().rstrip(".")

        return bool(
            word
            and word != "updating soon"
        )

    if answer_type == "binance_wotd":
        answer_groups = answer_data.get(
            "answer_groups",
            {},
        )

        # Chỉ cần ít nhất một nhóm 3-8 letters
        # có answer thật là đủ điều kiện publish.
        return any(
            bool(answers)
            for answers in answer_groups.values()
        )

    if answer_type == "question_answer":
        answer = normalize_answer(
            answer_data.get("answer")
        ).lower().rstrip(".")

        return bool(
            answer
            and answer != "updating soon"
        )

    return True


def process_game(cfg, ws, game_cfg):
    if not game_cfg.get("enabled", True):
        print(f"Skip disabled game: {game_cfg['game_key']}")
        return

    if not should_run_game_now(cfg, game_cfg):
        print(f"Skip game by run_times: {game_cfg['game_key']}")
        return

    game_key = game_cfg["game_key"]
    run_mode = os.getenv(
        "RUN_MODE",
        "update",
    ).lower()
    # date_str = target_date_str(cfg["timezone"])
    # readable_date = target_date_readable(cfg["timezone"])
    # slug_date = target_date_slug(cfg["timezone"])
    target_date_obj = get_game_target_date(
        cfg["timezone"],
        game_cfg,
    )
    
    date_str = target_date_obj.isoformat()
    readable_date = format_date_readable(
        target_date_obj
    )
    slug_date = format_date_slug(
        target_date_obj
    )
    
    post_cycle = game_cfg.get(
        "post_cycle",
        "daily",
    )
    
    campaign_start = None
    campaign_end = None
    extra_vars = {}
    
    if post_cycle == "weekly":
        campaign_start, campaign_end = (
            get_week_campaign_dates(
                target_date_obj
            )
        )
    
        extra_vars = {
            "CAMPAIGN_START_DATE": (
                campaign_start.isoformat()
            ),
            "CAMPAIGN_END_DATE": (
                campaign_end.isoformat()
            ),
            "CAMPAIGN_DATE_READABLE": (
                format_date_range_readable(
                    campaign_start,
                    campaign_end,
                )
            ),
            "CAMPAIGN_DATE_SLUG": (
                format_date_range_slug(
                    campaign_start,
                    campaign_end,
                )
            ),
        }
    timestamp = now_local(cfg["timezone"]).isoformat(timespec="seconds")

    # title = replace_date_vars(game_cfg["title_format"], date_str, readable_date)
    # slug = normalize_slug(replace_date_vars(game_cfg["slug_format"], date_str, readable_date))
    # seo_title = replace_date_vars(game_cfg["seo_title_format"], date_str, readable_date)
    # meta_description = replace_date_vars(game_cfg["meta_description_format"], date_str, readable_date)

    title = replace_game_vars(
        text=game_cfg["title_format"],
        date_str=date_str,
        readable_date=readable_date,
        slug_date=slug_date,
        extra_vars=extra_vars,
    )
    
    slug = normalize_slug(
        replace_game_vars(
            text=game_cfg["slug_format"],
            date_str=date_str,
            readable_date=readable_date,
            slug_date=slug_date,
            extra_vars=extra_vars,
        )
    )
    
    seo_title = replace_game_vars(
        text=game_cfg["seo_title_format"],
        date_str=date_str,
        readable_date=readable_date,
        slug_date=slug_date,
        extra_vars=extra_vars,
    )
    
    meta_description = replace_game_vars(
        text=game_cfg["meta_description_format"],
        date_str=date_str,
        readable_date=readable_date,
        slug_date=slug_date,
        extra_vars=extra_vars,
    )
    
    print(f"Processing {game_key} for {date_str}")

    # source = fetch_source_page(game_cfg["source_api_url"])
    # source_modified = source.get("modified") or source.get("date") or ""
    
    # source_is_today_target = is_source_for_target_date(
    #     source_modified,
    #     cfg["timezone"],
    # )

    source = fetch_source_page(
        game_cfg["source_api_url"]
    )

    source_modified = (
        source.get("modified")
        or source.get("date")
        or ""
    )

    source_modified_gmt = (
        source.get("modified_gmt")
        or ""
    )

    source_is_today_target = (
        is_source_for_target_date(
            source_modified,
            cfg["timezone"],
        )
    )

    # =====================================================
    # CITY HOLDER:
    # modified_gmt được hiểu là UTC,
    # sau đó đổi sang giờ Việt Nam và so sánh đúng ngày.
    # =====================================================
    if (
        game_cfg.get("answer_type")
        == "city_holder"
    ):
        source_modified_local = (
            parse_wp_datetime_gmt(
                source_modified_gmt,
                cfg["timezone"],
            )
        )

        # Fallback nếu API không có modified_gmt.
        if not source_modified_local:
            source_modified_local = (
                parse_wp_datetime_local(
                    source_modified,
                    cfg["timezone"],
                )
            )

        if source_modified_local:
            source_date_local = (
                source_modified_local.date()
            )
        
            day_difference = (
                source_date_local
                - target_date_obj
            ).days
        
            if run_mode == "update":
                # Update được phép lệch sớm/chậm 1 ngày.
                source_is_today_target = (
                    abs(day_difference) <= 1
                )
            else:
                # Create chỉ lấy đáp án nếu source
                # thực sự thuộc đúng ngày target.
                source_is_today_target = (
                    day_difference == 0
                )
        else:
            source_date_local = None
            day_difference = None
            source_is_today_target = False

        # Lưu modified_gmt vào Sheet nếu có.
        if source_modified_gmt:
            source_modified = (
                source_modified_gmt
            )

        print(
            "City Holder modified local: "
            f"{source_modified_local}"
        )

        print(
            "City Holder source matches target: "
            f"{source_is_today_target}"
        )
    
    source_content = source.get("content", {}).get("rendered", "")

    # question, answer = extract_question_answer(source_content, game_cfg, cfg)

    answer_data = extract_game_answer_data(
        content_html=source_content,
        game_cfg=game_cfg,
        cfg=cfg,
    )
    
    question = answer_data.get(
        "question",
        "",
    )
    
    answer = answer_data.get(
        "answer",
        "",
    )
    
    current_check_value = answer_data.get(
        "check_value",
        "",
    )

    # =====================================================
    # HRUM: UPDATE THEO DATE TRONG SOURCE
    # =====================================================
    if (
        answer_data.get("answer_type")
        == "quote_author"
    ):
        hrum_source_date = answer_data.get(
            "source_date"
        )

        hrum_has_data = answer_data.get(
            "has_data",
            False,
        )

        # Trong create mode vẫn giữ target ngày mai.
        # Chỉ update mode mới dùng Date trong source.
        if run_mode == "update":
            if not hrum_source_date:
                print(
                    "Hrum source date not found. Skip."
                )
                return

            if not hrum_has_data:
                print(
                    "Hrum source has no quote "
                    "or author yet. Skip."
                )
                return

            today_date = now_local(
                cfg["timezone"]
            ).date()

            yesterday_date = (
                today_date
                - timedelta(days=1)
            )

            # Chỉ chấp nhận source của hôm nay
            # hoặc hôm qua. Tránh source quá cũ
            # update nhầm một post cũ.
            if hrum_source_date not in {
                today_date,
                yesterday_date,
            }:
                print(
                    "Hrum source date is too old "
                    "or invalid. Skip."
                )
                print(
                    f"Hrum source date: "
                    f"{hrum_source_date}"
                )
                print(
                    f"Allowed dates: "
                    f"{yesterday_date}, "
                    f"{today_date}"
                )
                return

            # Dùng Date trong HTML để tìm đúng row.
            target_date_obj = hrum_source_date
            date_str = target_date_obj.isoformat()

            readable_date = format_date_readable(
                target_date_obj
            )

            slug_date = format_date_slug(
                target_date_obj
            )

            print(
                "Hrum update target overridden "
                "by source Date."
            )

            print(
                f"Hrum source target: "
                f"{date_str}"
            )

        # Create:
        # source date phải bằng target ngày mai
        # thì mới dùng đáp án ngay.
        #
        # Update:
        # target đã được đổi thành source date.
        source_is_today_target = bool(
            hrum_source_date
            and hrum_has_data
            and hrum_source_date
            == target_date_obj
        )

    source_matches_target_week = False

    if post_cycle == "weekly":
        source_campaign_start = answer_data.get(
            "campaign_start"
        )
        source_campaign_end = answer_data.get(
            "campaign_end"
        )
    
        source_matches_target_week = (
            source_campaign_start == campaign_start
            and source_campaign_end == campaign_end
        )
    
    print(
        f"Extracted answer type: "
        f"{answer_data.get('answer_type')}"
    )
    print(f"Extracted check value: {current_check_value}")

    row_idx, row = find_log_row(ws, date_str, game_key)

    # run_mode = os.getenv("RUN_MODE", "update").lower()

    if run_mode == "create" and row:
        print("Create mode: log already exists, skip.")
        return
    
    if run_mode == "update" and not row:
        print("Update mode: today's post log not found, skip.")
        return

    # =====================================================
    # CREATE DUPLICATE GUARD:
    # Sheet không có row thì check tiếp WordPress.
    # =====================================================
    if (
        run_mode == "create"
        and not row
    ):
        wp_matches = find_wp_posts_by_slug(
            cfg=cfg,
            slug=slug,
        )
    
        if len(wp_matches) > 1:
            duplicate_info = [
                {
                    "id": post.get("id"),
                    "status": post.get("status"),
                    "slug": post.get("slug"),
                }
                for post in wp_matches
            ]
    
            raise RuntimeError(
                "Multiple WordPress posts found "
                f"for slug '{slug}': "
                f"{duplicate_info}"
            )
    
        if len(wp_matches) == 1:
            existing_wp_post = wp_matches[0]
    
            existing_post_id = (
                existing_wp_post["id"]
            )
    
            existing_status = (
                existing_wp_post.get(
                    "status",
                    "",
                )
            )
    
            actual_slug = (
                existing_wp_post.get("slug")
                or slug
            )
    
            print(
                "WordPress post already exists."
            )
    
            print(
                f"Existing ID: "
                f"{existing_post_id}"
            )
    
            print(
                f"Existing status: "
                f"{existing_status}"
            )
    
            print(
                f"Existing slug: "
                f"{actual_slug}"
            )

            # =============================================
            # Rebuild Sheet row
            # =============================================
        
            if post_cycle == "weekly":
                recovered_data = (
                    make_waiting_answer_data(
                        game_cfg=game_cfg,
                        campaign_start=campaign_start,
                        campaign_end=campaign_end,
                    )
                )
        
                recovered_check_answer = ""
                recovered_verified_date = ""
        
            else:
                latest_sheet_check_answer = (
                    get_latest_check_answer_for_game(
                        ws,
                        game_key,
                    )
                )
        
                recovered_check_answer = (
                    latest_sheet_check_answer
                    or game_cfg.get(
                        "check_answer",
                        "",
                    )
                )
        
                recovered_data = (
                    make_waiting_answer_data(
                        game_cfg
                    )
                )
        
                recovered_verified_date = ""
        
            recovered_post_url = (
                f"{cfg['wp']['site_url'].rstrip('/')}/"
                f"{actual_slug.strip('/')}/"
            )
        
            append_log_row(
                ws,
                {
                    "target_date": date_str,
                    "game_key": game_key,
                    "post_id": existing_post_id,
                    "post_url": recovered_post_url,
                    "slug": actual_slug,
                    "source_modified": (
                        source_modified
                    ),
                    "question": recovered_data.get(
                        "question",
                        "",
                    ),
                    "answer": recovered_data.get(
                        "answer",
                        "",
                    ),
                    "check_answer": (
                        recovered_check_answer
                    ),
                    "verified_date": (
                        recovered_verified_date
                    ),
                    "status": (
                        "recovered_existing_wp_post"
                    ),
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
        
            print(
                "Recovered missing Sheet row. "
                "No new WordPress post created."
            )
        
            return

    # City Holder:
    # Nếu cả Combo, Quiz EN và Quiz RU đều chưa có,
    # không update lại bài thành toàn bộ Updating soon.
    if (
        run_mode == "update"
        and answer_data.get("answer_type")
        == "city_holder"
        and not answer_data.get("has_data")
    ):
        print(
            "City Holder source has no real "
            "answer data yet. Skip."
        )

        update_log_row(
            ws,
            row_idx,
            {
                "source_modified": source_modified,
                "status": (
                    "checked_city_holder_no_data"
                ),
                "updated_at": timestamp,
            },
        )

        return

    # Money Bux:
    # Nếu source tạm thời không lấy được code,
    # giữ nguyên nội dung WordPress hiện tại.
    if (
        run_mode == "update"
        and answer_data.get("answer_type")
        == "money_bux_codes"
        and not answer_data.get("codes")
    ):
        print(
            "No Money Bux codes found "
            "in source. Skip update."
        )

        update_log_row(
            ws,
            row_idx,
            {
                "source_modified": source_modified,
                "status": (
                    "checked_no_money_bux_codes"
                ),
                "updated_at": timestamp,
            },
        )

        return

    # Red Packet:
    # Không update nếu source chưa lấy được code nào.
    # Chỉ áp dụng riêng cho game này.
    if (
        run_mode == "update"
        and answer_data.get("answer_type")
        == "red_packet_codes"
        and not answer_data.get("codes")
    ):
        print(
            "No Red Packet codes found "
            "in source. Skip update."
        )

        update_log_row(
            ws,
            row_idx,
            {
                "source_modified": source_modified,
                "status": (
                    "checked_no_red_packet_codes"
                ),
                "updated_at": timestamp,
            },
        )

        return

    if not row:
        print("No sheet log found. First run for this game/date.")
    
        # =========================================================
        # WEEKLY GAME CREATE
        # =========================================================
        if post_cycle == "weekly":
            # Weekly cũng luôn create dưới dạng draft/waiting.
            # Update job sẽ publish khi campaign đúng
            # và source có ít nhất một answer thật.
            publish_data = make_waiting_answer_data(
                game_cfg=game_cfg,
                campaign_start=campaign_start,
                campaign_end=campaign_end,
            )

            publish_data["last_verified_date"] = None

            new_check_answer = ""
            verified_date = ""
            log_status = "created_draft_waiting_weekly_answer"
    
            crypto_data = fetch_crypto_data(cfg)
            base_snapshot = make_base_snapshot(crypto_data)
    
            crypto_snapshot_html = rewrite_snapshot_with_openai(
                cfg,
                game_key,
                base_snapshot,
            )
    
            content = build_content(
                game_cfg=game_cfg,
                cfg=cfg,
                date_str=date_str,
                answer_data=publish_data,
                crypto_snapshot_html=crypto_snapshot_html,
                readable_date=readable_date,
                slug_date=slug_date,
                extra_vars=extra_vars,
            )
    
            post = create_wp_post(
                cfg=cfg,
                game_cfg=game_cfg,
                title=title,
                slug=slug,
                content=content,
                target_date=target_date_obj,
            )
    
            post_id = post["id"]
            
            post_url = (
                f"{cfg['wp']['site_url'].rstrip('/')}/"
                f"{slug.strip('/')}/"
            )
    
            update_rankmath_meta(
                cfg,
                post_id,
                seo_title,
                meta_description,
            )
    
            append_log_row(ws, {
                "target_date": date_str,
                "game_key": game_key,
                "post_id": post_id,
                "post_url": post_url,
                "slug": slug,
                "source_modified": source_modified,
                "question": publish_data.get(
                    "question",
                    "",
                ),
                "answer": publish_data.get(
                    "answer",
                    "",
                ),
                "check_answer": new_check_answer,
                "verified_date": verified_date,
                "status": log_status,
                "created_at": timestamp,
                "updated_at": timestamp,
            })
    
            print(
                f"Created weekly post {post_id}: "
                f"{post_url}"
            )
            print(f"Status: {log_status}")
            return
    
        # =========================================================
        # DAILY GAME CREATE
        # =========================================================
        latest_sheet_check_answer = (
            get_latest_check_answer_for_game(
                ws,
                game_key,
            )
        )
    
        initial_check_answer = (
            latest_sheet_check_answer
            or game_cfg.get("check_answer", "")
        )
    
        crypto_data = fetch_crypto_data(cfg)
        base_snapshot = make_base_snapshot(crypto_data)
    
        crypto_snapshot_html = rewrite_snapshot_with_openai(
            cfg,
            game_key,
            base_snapshot,
        )

        # Daily posts luôn được create dưới dạng draft
        # với answer area ở trạng thái waiting.
        # Update job sẽ publish khi có dữ liệu hợp lệ.
        publish_data = make_waiting_answer_data(
            game_cfg
        )
        log_status = "created_draft_waiting_answer"
        new_check_answer = initial_check_answer
    
        # Phần này phải nằm ngoài else phía trên.
        content = build_content(
            game_cfg=game_cfg,
            cfg=cfg,
            date_str=date_str,
            answer_data=publish_data,
            crypto_snapshot_html=crypto_snapshot_html,
            readable_date=readable_date,
            slug_date=slug_date,
            extra_vars=extra_vars,
        )
    
        post = create_wp_post(
            cfg=cfg,
            game_cfg=game_cfg,
            title=title,
            slug=slug,
            content=content,
            target_date=target_date_obj,
        )
    
        post_id = post["id"]
        
        post_url = (
            f"{cfg['wp']['site_url'].rstrip('/')}/"
            f"{slug.strip('/')}/"
        )
    
        update_rankmath_meta(
            cfg,
            post_id,
            seo_title,
            meta_description,
        )
    
        append_log_row(ws, {
            "target_date": date_str,
            "game_key": game_key,
            "post_id": post_id,
            "post_url": post_url,
            "slug": slug,
            "source_modified": source_modified,
            "question": publish_data.get(
                "question",
                "",
            ),
            "answer": publish_data.get(
                "answer",
                "",
            ),
            "check_answer": new_check_answer,
            "verified_date": "",
            "status": log_status,
            "created_at": timestamp,
            "updated_at": timestamp,
        })
    
        print(f"Created post {post_id}: {post_url}")
        print(f"Status: {log_status}")
        return

    print("Sheet log exists. Checking answer against check_answer from log.")
    
    post_id = str(row.get("post_id") or "").strip()
    
    if not post_id:
        raise RuntimeError(
            f"Missing post_id in sheet for {game_key} {date_str}"
        )
        
    # =========================================================
    # WEEKLY GAME UPDATE
    # =========================================================
    if post_cycle == "weekly":
        print(
            f"Weekly update mode: checking {game_key} "
            f"for campaign {campaign_start} to {campaign_end}"
        )
    
        # -----------------------------------------------------
        # 1. Kiểm tra source MiningCombo có đúng tuần hay không
        # -----------------------------------------------------
        if not source_matches_target_week:
            source_campaign_start = answer_data.get(
                "campaign_start"
            )
    
            source_campaign_end = answer_data.get(
                "campaign_end"
            )
    
            print(
                "Weekly source campaign does not match "
                "the current target week."
            )
    
            print(
                f"Expected campaign: "
                f"{campaign_start} to {campaign_end}"
            )
    
            print(
                f"Source campaign: "
                f"{source_campaign_start} to "
                f"{source_campaign_end}"
            )
    
            update_log_row(
                ws,
                row_idx,
                {
                    "source_modified": source_modified,
                    "status": "checked_weekly_source_mismatch",
                    "updated_at": timestamp,
                },
            )
    
            return

        # -----------------------------------------------------
        # 2. Chỉ publish khi source có ít nhất một answer thật
        # -----------------------------------------------------
        publishable_answer = has_publishable_answer(
            answer_data
        )

        if not publishable_answer:
            print(
                "Weekly source matches campaign "
                "but has no publishable answers yet. "
                "Keep post as draft."
            )

            update_log_row(
                ws,
                row_idx,
                {
                    "source_modified": source_modified,
                    "status": (
                        "checked_weekly_waiting_answer"
                    ),
                    "updated_at": timestamp,
                },
            )

            return
    
        # -----------------------------------------------------
        # 3. Lấy ngày hiện tại để cập nhật Last Verified
        # -----------------------------------------------------
        today_date = now_local(
            cfg["timezone"]
        ).date()
    
        today_str = today_date.isoformat()
    
        # -----------------------------------------------------
        # 3. Đọc dữ liệu hiện tại trong Google Sheet
        # -----------------------------------------------------
        sheet_check_answer = str(
            row.get("check_answer")
            or ""
        ).strip()
    
        sheet_source_modified = str(
            row.get("source_modified")
            or ""
        ).strip()
    
        sheet_verified_date = str(
            row.get("verified_date")
            or ""
        ).strip()

        existing_post = get_wp_post(
            cfg,
            post_id,
        )

        existing_status = str(
            existing_post.get("status")
            or ""
        ).lower()

        publish_now = existing_status in {
            "draft",
            "future",
            "pending",
        }

        print(
            f"Weekly WordPress status: "
            f"{existing_status}"
        )

        print(
            f"Weekly publish now: "
            f"{publish_now}"
        )
    
        # -----------------------------------------------------
        # 4. Kiểm tra lý do cần update
        # -----------------------------------------------------
    
        # Theme, reward hoặc answer groups có thay đổi.
        signature_changed = (
            current_check_value
            != sheet_check_answer
        )
    
        # MiningCombo modified lại page.
        source_modified_changed = (
            str(source_modified).strip()
            != sheet_source_modified
        )
    
        # Hôm nay bài chưa được cập nhật Last Verified.
        not_verified_today = (
            sheet_verified_date
            != today_str
        )
    
        print(
            f"Weekly signature changed: "
            f"{signature_changed}"
        )
    
        print(
            f"Weekly source_modified changed: "
            f"{source_modified_changed}"
        )
    
        print(
            f"Weekly verified today: "
            f"{not not_verified_today}"
        )
    
        # -----------------------------------------------------
        # 5. Nếu hôm nay đã verify và source không đổi thì skip
        # -----------------------------------------------------
        if (
            not signature_changed
            and not source_modified_changed
            and not not_verified_today
            and not publish_now
        ):
            print(
                "Weekly post already verified today "
                "and source has not changed. Skip."
            )
    
            update_log_row(
                ws,
                row_idx,
                {
                    "status": "checked_weekly_no_change",
                    "updated_at": timestamp,
                },
            )
    
            return
    
        # -----------------------------------------------------
        # 6. Lấy nội dung bài WordPress hiện tại
        # -----------------------------------------------------
        print(
            "Weekly data requires an update. "
            "Fetching existing WordPress post."
        )
    
        existing_content = (
            existing_post.get(
                "content",
                {},
            ).get("raw")
            or existing_post.get(
                "content",
                {},
            ).get("rendered", "")
        )
    
        if not existing_content:
            raise RuntimeError(
                f"Empty WordPress content for post {post_id}"
            )
    
        # -----------------------------------------------------
        # 7. Tạo lại toàn bộ ANSWER_AREA
        # -----------------------------------------------------
        updated_content = update_existing_answer_content(
            content_html=existing_content,
            game_cfg=game_cfg,
            answer_data=answer_data,
            readable_date=readable_date,
            last_verified_date=today_date,
        )
    
        # -----------------------------------------------------
        # 8. Chạy lại auto-link
        # -----------------------------------------------------
        updated_content = auto_link_html(
            updated_content,
            cfg,
        )
    
        # -----------------------------------------------------
        # 9. Gửi nội dung mới lên WordPress
        # -----------------------------------------------------
        update_wp_post(
            cfg,
            post_id,
            updated_content,
            publish_now=publish_now,
        )

        if publish_now:
            final_status = "published_weekly_wotd"
        else:
            final_status = "updated_weekly_wotd"
    
        # -----------------------------------------------------
        # 10. Update Google Sheet
        # -----------------------------------------------------
        update_log_row(
            ws,
            row_idx,
            {
                "source_modified": source_modified,
                "question": answer_data.get(
                    "question",
                    "",
                ),
                "answer": answer_data.get(
                    "answer",
                    "",
                ),
                "check_answer": current_check_value,
                "verified_date": today_str,
                "status": final_status,
                "updated_at": timestamp,
            },
        )
    
        print(
            f"Updated weekly WordPress post {post_id}"
        )
    
        print(
            f"Weekly campaign: "
            f"{campaign_start} to {campaign_end}"
        )
    
        print(
            f"Last Verified: "
            f"{format_date_readable(today_date)}"
        )
    
        print(
            f"Status: {final_status}"
        )
    
        return
    
    # =========================================================
    # DAILY GAME UPDATE
    # =========================================================
    sheet_check_answer = row.get("check_answer") or ""
    
    answer_changed = should_update_answer(
        current_check_value,
        sheet_check_answer,
    )
    
    row_answer_norm = normalize_answer(
        row.get("answer") or ""
    ).lower().rstrip(".")
    
    row_is_waiting = row_answer_norm in {
        "",
        "updating soon",
    }

    # if not answer_changed:
    #     print("Answer unchanged. No post update needed.")

    #     update_log_row(ws, row_idx, {
    #         "source_modified": source_modified,
    #         "status": "checked_no_new_answer",
    #         "updated_at": timestamp,
    #     })

    #     return

    if not source_is_today_target:
        print("Source content is not updated for target date yet. Skip.")
        update_log_row(ws, row_idx, {
            "source_modified": source_modified,
            "status": "checked_source_not_target_date",
            "updated_at": timestamp,
        })
        return

    publishable_answer = has_publishable_answer(
        answer_data
    )

    # Draft chưa có dữ liệu thật thì tuyệt đối không publish.
    if row_is_waiting and not publishable_answer:
        print(
            "No publishable answer yet. "
            "Keep post as draft."
        )
        update_log_row(ws, row_idx, {
            "source_modified": source_modified,
            "status": "checked_draft_waiting_answer",
            "updated_at": timestamp,
        })
        return

    # City Holder cho phép source lệch +/-1 ngày khi update.
    # Nếu post vẫn đang waiting và source không đúng chính xác
    # target date, cần có ít nhất một tín hiệu mới so với lúc create:
    # answer/hash đổi hoặc source_modified đổi.
    if (
        row_is_waiting
        and answer_data.get("answer_type")
        == "city_holder"
        and day_difference != 0
    ):
        current_combo_check = (
            answer_data.get(
                "combo_check_value",
                "",
            )
        )

        previous_combo_check = (
            get_latest_city_combo_signature(
                ws=ws,
                game_key=game_key,
                exclude_target_date=date_str,
            )
        )

        # Với source lệch +/-1 ngày,
        # chỉ Combo mới được coi là tín hiệu mới.
        combo_changed = bool(
            current_combo_check
            and previous_combo_check
            and current_combo_check
            != previous_combo_check
        )

        print(
            "City Holder current combo signature: "
            f"{current_combo_check}"
        )

        print(
            "City Holder previous combo signature: "
            f"{previous_combo_check}"
        )

        print(
            "City Holder combo changed: "
            f"{combo_changed}"
        )

        if not combo_changed:
            print(
                "City Holder source is within +/-1 day "
                "but Combo has not changed. "
                "Quiz data is ignored for publish detection. "
                "Keep post as draft."
            )

            update_log_row(
                ws,
                row_idx,
                {
                    "source_modified": source_modified,
                    "status": (
                        "checked_city_holder_draft_"
                        "no_new_combo"
                    ),
                    "updated_at": timestamp,
                },
            )

            return


    if not answer_changed and not row_is_waiting:
        print("Answer unchanged. No post update needed.")
        update_log_row(ws, row_idx, {
            "source_modified": source_modified,
            "status": "checked_no_new_answer",
            "updated_at": timestamp,
        })
        return

    print("Valid answer detected. Updating existing post.")

    existing_post = get_wp_post(cfg, post_id)
    existing_content = (
        existing_post.get("content", {}).get("raw")
        or existing_post.get("content", {}).get("rendered", "")
    )

    last_updated_text = None

    if (
        answer_data.get("answer_type")
        == "red_packet_codes"
    ):
        last_updated_text = (
            format_datetime_readable(
                now_local(
                    cfg["timezone"]
                )
            )
        )
    
    updated_content = update_existing_answer_content(
        content_html=existing_content,
        game_cfg=game_cfg,
        answer_data=answer_data,
        readable_date=readable_date,
        last_updated_text=last_updated_text,
    )

    updated_content = auto_link_html(updated_content, cfg)

    existing_status = str(
        existing_post.get("status")
        or ""
    ).lower()

    publish_now = existing_status in {
        "draft",
        "future",
        "pending",
    }

    update_wp_post(
        cfg,
        post_id,
        updated_content,
        publish_now=publish_now,
    )

    if publish_now:
        final_status = "published_with_new_answer"
    else:
        final_status = "updated_with_new_answer"

    update_log_row(ws, row_idx, {
        "source_modified": source_modified,
        "question": answer_data.get(
            "question",
            "",
        ),
        "answer": answer_data.get(
            "answer",
            "",
        ),
        "check_answer": current_check_value,
        "status": final_status,
        "updated_at": timestamp,
    })

    if publish_now:
        print(
            f"Published post {post_id} "
            "with valid answer."
        )
    else:
        print(f"Updated post {post_id}")

    print(f"Status: {final_status}")


def main():
    cfg = load_config()
    ws = get_sheet(cfg)

    for game_cfg in cfg["games"]:
        try:
            process_game(cfg, ws, game_cfg)
            time.sleep(2)
        except Exception as e:
            print(f"ERROR game={game_cfg.get('game_key')}: {e}")


if __name__ == "__main__":
    main()
