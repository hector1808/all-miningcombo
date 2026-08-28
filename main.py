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


def source_modified_matches_target(
    source_modified,
    tz_name,
    target_date,
):
    source_dt = parse_wp_datetime_local(
        source_modified,
        tz_name,
    )

    if not source_dt:
        return False

    target_start = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        tzinfo=ZoneInfo(tz_name),
    )

    return source_dt >= target_start


def get_publish_settings(cfg, game_cfg):
    defaults = cfg.get("publishing", {})

    mode = str(
        game_cfg.get(
            "publish_mode",
            defaults.get("default_mode", "scheduled"),
        )
    ).strip().lower()

    if mode not in {"scheduled", "answer"}:
        raise RuntimeError(
            f"Invalid publish_mode for {game_cfg['game_key']}: {mode}"
        )

    publish_time = str(
        game_cfg.get(
            "publish_time",
            defaults.get("default_time", "22:00"),
        )
    ).strip()

    try:
        hour, minute = map(int, publish_time.split(":"))
    except (TypeError, ValueError):
        raise RuntimeError(
            f"Invalid publish_time for {game_cfg['game_key']}: "
            f"{publish_time}. Expected HH:MM."
        )

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise RuntimeError(
            f"Invalid publish_time for {game_cfg['game_key']}: "
            f"{publish_time}."
        )

    try:
        day_offset = int(
            game_cfg.get(
                "publish_day_offset",
                defaults.get("default_day_offset", -1),
            )
        )
    except (TypeError, ValueError):
        raise RuntimeError(
            f"Invalid publish_day_offset for {game_cfg['game_key']}"
        )

    return mode, publish_time, day_offset


def scheduled_publish_datetime(
    cfg,
    game_cfg,
    target_date,
):
    _, publish_time, day_offset = get_publish_settings(
        cfg,
        game_cfg,
    )

    hour, minute = map(int, publish_time.split(":"))
    publish_date = target_date + timedelta(days=day_offset)

    return datetime(
        publish_date.year,
        publish_date.month,
        publish_date.day,
        hour,
        minute,
        tzinfo=ZoneInfo(cfg["timezone"]),
    )


def wp_date_matches(value, expected_dt, tz_name):
    current_dt = parse_wp_datetime_local(value, tz_name)

    if not current_dt:
        return False

    return abs((current_dt - expected_dt).total_seconds()) < 60


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


def extract_by_selector_and_prefix(
    soup,
    selector,
    prefix,
    text_separator=" ",
    exclude_prefixes=None,
):
    exclude_prefixes = exclude_prefixes or []

    for el in soup.select(selector):
        text = html.unescape(
            el.get_text(
                text_separator,
                strip=True,
            )
        )

        text = text.strip()

        if not text:
            continue

        # Dùng cho trường hợp prefix rỗng,
        # ví dụ Fomo Fighters:
        # bỏ Date và Answer, lấy paragraph Riddle.
        should_skip = False

        for excluded in exclude_prefixes:
            clean_excluded = (
                str(excluded)
                .strip()
                .rstrip(":")
            )

            if re.match(
                rf"^\s*{re.escape(clean_excluded)}\s*:?",
                text,
                flags=re.I,
            ):
                should_skip = True
                break

        if should_skip:
            continue

        # Prefix rỗng = lấy nguyên text.
        # Giữ xuống dòng cho các list như TiCkTOM.
        if prefix == "":
            if text_separator == "\n":
                return "\n".join(
                    normalize_answer(line)
                    for line in text.splitlines()
                    if normalize_answer(line)
                )

            return normalize_answer(text)

        value = strip_prefix(
            text,
            prefix,
        )

        if value:
            return value

    return ""


def find_source_section_scope(
    soup,
    heading_contains,
):
    if not heading_contains:
        return soup

    heading_contains = (
        heading_contains
        .strip()
        .lower()
    )

    for h2 in soup.find_all("h2"):
        heading_text = normalize_answer(
            h2.get_text(
                " ",
                strip=True,
            )
        ).lower()

        if heading_contains not in heading_text:
            continue

        # MiningCombo hiện đang đặt từng answer
        # trong wp-block-cover.
        cover = h2.find_parent(
            "div",
            class_=lambda classes: (
                classes
                and "wp-block-cover" in classes
            ),
        )

        if cover:
            return cover

        # Fallback nếu MiningCombo bỏ wp-block-cover.
        if h2.parent:
            return h2.parent

    return soup


def extract_source_date_from_scope(
    scope,
    selector="p",
    prefix="Date",
):
    raw_date = extract_by_selector_and_prefix(
        soup=scope,
        selector=selector,
        prefix=prefix,
    )

    raw_date = normalize_answer(
        raw_date
    )

    if not raw_date:
        return None

    match = re.search(
        r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        raw_date,
    )

    if not match:
        return None

    try:
        return datetime.strptime(
            match.group(1),
            "%B %d, %Y",
        ).date()
    except ValueError:
        return None


def extract_question_answer(
    content_html,
    game_cfg,
    cfg,
):
    soup = BeautifulSoup(
        content_html,
        "html.parser",
    )

    # Chỉ parse trong đúng section của game.
    # Nếu config không có thì behavior cũ.
    scope = find_source_section_scope(
        soup,
        game_cfg.get(
            "source_heading_contains",
            "",
        ),
    )

    question_selector = (
        game_cfg.get("question_selector")
        or cfg["defaults"]["question_selector"]
    )

    answer_selector = (
        game_cfg.get("answer_selector")
        or cfg["defaults"]["answer_selector"]
    )

    # Quan trọng:
    # cho phép config explicitly đặt prefix = ""
    if "question_prefix" in game_cfg:
        question_prefix = game_cfg[
            "question_prefix"
        ]
    else:
        question_prefix = cfg[
            "defaults"
        ].get(
            "question_prefix",
            "Question:",
        )

    if "answer_prefix" in game_cfg:
        answer_prefix = game_cfg[
            "answer_prefix"
        ]
    else:
        answer_prefix = cfg[
            "defaults"
        ].get(
            "answer_prefix",
            "Answer:",
        )

    question = extract_by_selector_and_prefix(
        soup=scope,
        selector=question_selector,
        prefix=question_prefix,
        text_separator=game_cfg.get(
            "question_text_separator",
            " ",
        ),
        exclude_prefixes=game_cfg.get(
            "question_exclude_prefixes",
            [],
        ),
    )

    answer = extract_by_selector_and_prefix(
        soup=scope,
        selector=answer_selector,
        prefix=answer_prefix,
        text_separator=game_cfg.get(
            "answer_text_separator",
            " ",
        ),
    )

    source_date = None

    if game_cfg.get(
        "use_content_date",
        False,
    ):
        source_date = (
            extract_source_date_from_scope(
                scope=scope,
                selector=game_cfg.get(
                    "source_date_selector",
                    "p",
                ),
                prefix=game_cfg.get(
                    "source_date_prefix",
                    "Date",
                ),
            )
        )

    return (
        question,
        answer,
        source_date,
    )


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


def extract_city_combo_lines(soup):
    combo_element = find_city_combo_element(soup)

    if not combo_element:
        return []

    if combo_element.name == "pre":
        combo_lines = extract_pre_lines(combo_element)
    else:
        combo_lines = [
            f"{index}. {normalize_answer(li.get_text(' ', strip=True))}"
            for index, li in enumerate(
                combo_element.find_all("li", recursive=False),
                start=1,
            )
            if normalize_answer(li.get_text(" ", strip=True))
        ]

    if is_waiting_content(combo_lines):
        return []

    return combo_lines


def extract_city_holder_data(content_html, log_result=True):
    soup = BeautifulSoup(content_html, "html.parser")

    combo_lines = extract_city_combo_lines(soup)
    quiz_date = extract_city_quiz_date(soup)
    quiz_en = extract_city_quiz_en(soup)
    quiz_ru = extract_city_quiz_ru(soup)

    if log_result:
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
        codes = extract_money_bux_codes(content_html)
        answer_json = json.dumps(
            codes,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        return {
            "answer_type": answer_type,
            "question": "",
            "answer": answer_json if codes else "",
            "check_value": (
                hashlib.sha256(answer_json.encode("utf-8")).hexdigest()
                if codes
                else ""
            ),
            "codes": codes,
        }

    if answer_type == "city_holder":
        city_data = extract_city_holder_data(content_html)
        combo_lines = city_data["combo_lines"]
        quiz_date = city_data["quiz_date"]
        quiz_en = city_data["quiz_en"]
        quiz_ru = city_data["quiz_ru"]

        answer_payload = {
            "combo_lines": combo_lines,
            "quiz_date": quiz_date.isoformat() if quiz_date else "",
            "quiz_en": quiz_en,
            "quiz_ru": quiz_ru,
        }

        has_combo = bool(combo_lines)
        has_quiz = bool(quiz_en or quiz_ru)
        has_data = has_combo or has_quiz

        answer_json = json.dumps(
            answer_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        return {
            "answer_type": answer_type,
            "question": "",
            "answer": answer_json if has_data else "",
            "check_value": (
                hashlib.sha256(answer_json.encode("utf-8")).hexdigest()
                if has_data
                else ""
            ),
            "combo_check_value": make_city_combo_signature(combo_lines),
            **answer_payload,
            "quiz_date": quiz_date,
            "has_combo": has_combo,
            "has_quiz": has_quiz,
            "has_data": has_data,
        }

    if answer_type == "quote_author":
        source_date, quote, author = extract_quote_author(content_html)
        has_data = bool(source_date and (quote or author))

        answer_payload = {
            "source_date": source_date.isoformat() if source_date else "",
            "quote": quote,
            "author": author,
        }

        check_value = (
            json.dumps(
                answer_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if has_data
            else ""
        )

        return {
            "answer_type": answer_type,
            "question": quote,
            "answer": author,
            "check_value": check_value,
            "source_date": source_date,
            "quote": quote,
            "author": author,
            "has_data": has_data,
        }

    if answer_type == "hamster_cipher":
        word, morse_lines, simplified_lines = extract_hamster_cipher(
            content_html=content_html,
            game_cfg=game_cfg,
        )

        answer_payload = {
            "word": word,
            "morse_lines": morse_lines,
            "simplified_lines": simplified_lines,
        }

        answer_text = []

        if morse_lines:
            answer_text.append("Morse:\n" + "\n".join(morse_lines))

        if simplified_lines:
            answer_text.append(
                "Simplified:\n" + "\n".join(simplified_lines)
            )

        return {
            "answer_type": answer_type,
            "question": word,
            "answer": "\n\n".join(answer_text),
            "check_value": json.dumps(
                answer_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            **answer_payload,
        }

    if answer_type == "question_answer":
        question, answer, source_date = extract_question_answer(
            content_html=content_html,
            game_cfg=game_cfg,
            cfg=cfg,
        )

        return {
            "answer_type": answer_type,
            "question": question,
            "answer": answer,
            "check_value": answer,
            "source_date": source_date,
        }

    raise RuntimeError(
        f"Unsupported answer_type in main.py: {answer_type}"
    )


def make_waiting_answer_data(game_cfg):
    answer_type = game_cfg.get(
        "answer_type",
        "question_answer",
    )

    if answer_type == "money_bux_codes":
        return {
            "answer_type": answer_type,
            "question": "",
            "answer": "Updating soon.",
            "check_value": "",
            "codes": [],
        }

    if answer_type == "city_holder":
        return {
            "answer_type": answer_type,
            "question": "",
            "answer": "Updating soon.",
            "check_value": "",
            "combo_check_value": "",
            "combo_lines": [],
            "quiz_date": None,
            "quiz_en": [],
            "quiz_ru": [],
            "has_combo": False,
            "has_quiz": False,
            "has_data": False,
        }

    if answer_type == "quote_author":
        return {
            "answer_type": answer_type,
            "question": "Updating soon.",
            "answer": "Updating soon.",
            "check_value": "",
            "source_date": None,
            "quote": "Updating soon.",
            "author": "Updating soon.",
            "has_data": False,
        }

    if answer_type == "hamster_cipher":
        return {
            "answer_type": answer_type,
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
        "source_date": None,
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
    q_label.string = game_cfg.get(
        "question_label",
        "Question:",
    )
    target_p.append(q_label)
    target_p.append(soup.new_tag("br"))
    target_p.append(question or "Updating soon.")

    target_p.append(soup.new_tag("br"))
    target_p.append(soup.new_tag("br"))

    a_label = soup.new_tag("strong")
    a_label.string = game_cfg.get(
        "answer_label",
        "Correct Answer:",
    )
    target_p.append(a_label)
    target_p.append(soup.new_tag("br"))
    answer_text = (
        answer
        or "Updating soon."
    )

    answer_lines = str(
        answer_text
    ).splitlines()

    for idx, line in enumerate(
        answer_lines
    ):
        if idx > 0:
            target_p.append(
                soup.new_tag("br")
            )

        target_p.append(
            line.strip()
        )

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
    readable_date,
):
    answer_type = answer_data.get(
        "answer_type",
        "question_answer",
    )

    if answer_type == "money_bux_codes":
        answer_area_html = build_money_bux_answer_area(
            answer_data.get("codes", [])
        )

    elif answer_type == "city_holder":
        answer_area_html = build_city_holder_answer_area(
            readable_date=readable_date,
            combo_lines=answer_data.get("combo_lines", []),
            quiz_en=answer_data.get("quiz_en", []),
            quiz_ru=answer_data.get("quiz_ru", []),
        )

    elif answer_type == "quote_author":
        answer_area_html = build_quote_author_answer_area(
            readable_date=readable_date,
            quote=answer_data.get("quote"),
            author=answer_data.get("author"),
        )

    elif answer_type == "hamster_cipher":
        answer_area_html = build_hamster_cipher_answer_area(
            readable_date=readable_date,
            word=answer_data.get("word"),
            morse_lines=answer_data.get("morse_lines", []),
            simplified_lines=answer_data.get(
                "simplified_lines",
                [],
            ),
        )

    else:
        return update_quiz_answer_block(
            content_html=content_html,
            game_cfg=game_cfg,
            question=answer_data.get("question"),
            answer=answer_data.get("answer"),
        )

    return replace_answer_area(
        content_html=content_html,
        game_cfg=game_cfg,
        answer_area_html=answer_area_html,
    )


def build_content(
    game_cfg,
    cfg,
    date_str,
    answer_data,
    crypto_snapshot_html,
    readable_date,
    slug_date,
):
    with open(
        game_cfg["template_file"],
        "r",
        encoding="utf-8",
    ) as file:
        content = file.read()

    content = replace_date_vars(
        content,
        date_str,
        readable_date,
        slug_date,
    )

    content = content.replace(
        "{{CRYPTO_SNAPSHOT}}",
        crypto_snapshot_html or "",
    )

    answer_type = answer_data.get(
        "answer_type",
        game_cfg.get("answer_type", "question_answer"),
    )

    if answer_type == "question_answer":
        content = update_quiz_answer_block(
            content_html=content,
            game_cfg=game_cfg,
            question=answer_data.get("question"),
            answer=answer_data.get("answer"),
        )

    elif answer_type == "money_bux_codes":
        answer_area_html = build_money_bux_answer_area(
            answer_data.get("codes", [])
        )
        content = replace_template_answer_area(
            content,
            game_cfg,
            answer_area_html,
        )

    elif answer_type == "city_holder":
        answer_area_html = build_city_holder_answer_area(
            readable_date=readable_date,
            combo_lines=answer_data.get("combo_lines", []),
            quiz_en=answer_data.get("quiz_en", []),
            quiz_ru=answer_data.get("quiz_ru", []),
        )
        content = replace_template_answer_area(
            content,
            game_cfg,
            answer_area_html,
        )

    elif answer_type == "quote_author":
        answer_area_html = build_quote_author_answer_area(
            readable_date=readable_date,
            quote=answer_data.get("quote"),
            author=answer_data.get("author"),
        )
        content = replace_template_answer_area(
            content,
            game_cfg,
            answer_area_html,
        )

    elif answer_type == "hamster_cipher":
        answer_area_html = build_hamster_cipher_answer_area(
            readable_date=readable_date,
            word=answer_data.get("word"),
            morse_lines=answer_data.get("morse_lines", []),
            simplified_lines=answer_data.get(
                "simplified_lines",
                [],
            ),
        )
        content = replace_template_answer_area(
            content,
            game_cfg,
            answer_area_html,
        )

    else:
        raise RuntimeError(
            f"Unsupported answer_type in build_content: {answer_type}"
        )

    return auto_link_html(content, cfg)


def replace_template_answer_area(
    content,
    game_cfg,
    answer_area_html,
):
    placeholder = game_cfg.get(
        "answer_placeholder",
        "{{ANSWER_AREA}}",
    )

    if content.count(placeholder) != 1:
        raise RuntimeError(
            f"Template for {game_cfg['game_key']} must contain "
            f"exactly one {placeholder}."
        )

    return content.replace(
        placeholder,
        answer_area_html,
        1,
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


def patch_wp_post(cfg, post_id, payload):
    url = (
        f"{cfg['wp']['site_url'].rstrip('/')}"
        f"/wp-json/wp/v2/posts/{post_id}"
    )

    response = requests.post(
        url,
        headers={
            **wp_headers(cfg),
            "Content-Type": "application/json",
        },
        params={"context": "edit"},
        json=payload,
        timeout=120,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Post update failed {response.status_code}: "
            f"{response.text[:2000]}"
        )

    return response.json()


def create_wp_post(
    cfg,
    game_cfg,
    title,
    slug,
    content,
    target_date,
):
    url = (
        f"{cfg['wp']['site_url'].rstrip('/')}"
        "/wp-json/wp/v2/posts"
    )

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

    tag_ids = get_tag_ids_for_post_url(cfg, slug)

    if tag_ids:
        payload["tags"] = tag_ids

    featured_media_id = (
        game_cfg.get("featured_media_id")
        or cfg["wp"].get("featured_media_id")
    )

    if featured_media_id:
        payload["featured_media"] = int(featured_media_id)

    publish_mode, _, _ = get_publish_settings(cfg, game_cfg)

    if publish_mode == "answer":
        payload["status"] = "draft"
    else:
        publish_dt = scheduled_publish_datetime(
            cfg,
            game_cfg,
            target_date,
        )

        if publish_dt > now_local(cfg["timezone"]):
            payload["status"] = "future"
        else:
            payload["status"] = "publish"

        payload["date"] = publish_dt.isoformat()

    print(
        f"Create {game_cfg['game_key']}: "
        f"publish_mode={publish_mode}, "
        f"status={payload['status']}, "
        f"date={payload.get('date')}"
    )

    response = requests.post(
        url,
        headers={
            **wp_headers(cfg),
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Post create failed {response.status_code}: "
            f"{response.text[:2000]}"
        )

    return response.json()


def get_wp_post(cfg, post_id):
    url = (
        f"{cfg['wp']['site_url'].rstrip('/')}"
        f"/wp-json/wp/v2/posts/{post_id}?context=edit"
    )

    response = requests.get(
        url,
        headers=wp_headers(cfg),
        timeout=60,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Post fetch failed {response.status_code}: "
            f"{response.text[:2000]}"
        )

    return response.json()


def reconcile_publish_state(
    cfg,
    game_cfg,
    post,
    target_date,
):
    publish_mode, _, _ = get_publish_settings(cfg, game_cfg)
    current_status = str(post.get("status") or "").lower()

    if publish_mode == "answer":
        if current_status in {"future", "pending"}:
            updated_post = patch_wp_post(
                cfg,
                post["id"],
                {"status": "draft"},
            )
            return updated_post, True, "switched_to_answer_draft"

        return post, False, ""

    publish_dt = scheduled_publish_datetime(
        cfg,
        game_cfg,
        target_date,
    )
    desired_status = (
        "future"
        if publish_dt > now_local(cfg["timezone"])
        else "publish"
    )

    if current_status == "publish":
        return post, False, ""

    if current_status not in {"draft", "future", "pending"}:
        return post, False, ""

    if (
        desired_status == "future"
        and current_status == "future"
        and wp_date_matches(
            post.get("date"),
            publish_dt,
            cfg["timezone"],
        )
    ):
        return post, False, ""

    updated_post = patch_wp_post(
        cfg,
        post["id"],
        {
            "status": desired_status,
            "date": publish_dt.isoformat(),
        },
    )

    label = (
        "scheduled_future"
        if desired_status == "future"
        else "published_scheduled_time_passed"
    )

    return updated_post, True, label


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


def has_answer_data(answer_data):
    answer_type = answer_data.get(
        "answer_type",
        "question_answer",
    )

    if answer_type == "city_holder":
        return bool(answer_data.get("has_data"))

    if answer_type == "money_bux_codes":
        return bool(answer_data.get("codes"))

    if answer_type == "quote_author":
        return bool(answer_data.get("has_data"))

    if answer_type == "hamster_cipher":
        word = normalize_answer(answer_data.get("word"))
        return bool(word and word.lower().rstrip(".") != "updating soon")

    answer = normalize_answer(answer_data.get("answer"))
    return bool(answer and answer.lower().rstrip(".") != "updating soon")


def can_publish_in_answer_mode(answer_data):
    answer_type = answer_data.get(
        "answer_type",
        "question_answer",
    )

    if answer_type == "city_holder":
        return bool(answer_data.get("combo_lines"))

    if answer_type == "quote_author":
        return bool(
            answer_data.get("source_date")
            and normalize_answer(answer_data.get("author"))
        )

    return has_answer_data(answer_data)


def build_game_identity(game_cfg, target_date):
    date_str = target_date.isoformat()
    readable_date = format_date_readable(target_date)
    slug_date = format_date_slug(target_date)

    title = replace_date_vars(
        game_cfg["title_format"],
        date_str,
        readable_date,
        slug_date,
    )

    slug = normalize_slug(
        replace_date_vars(
            game_cfg["slug_format"],
            date_str,
            readable_date,
            slug_date,
        )
    )

    seo_title = replace_date_vars(
        game_cfg["seo_title_format"],
        date_str,
        readable_date,
        slug_date,
    )

    meta_description = replace_date_vars(
        game_cfg["meta_description_format"],
        date_str,
        readable_date,
        slug_date,
    )

    return {
        "date_str": date_str,
        "readable_date": readable_date,
        "slug_date": slug_date,
        "title": title,
        "slug": slug,
        "seo_title": seo_title,
        "meta_description": meta_description,
    }


def create_game_post(cfg, ws, game_cfg):
    game_key = game_cfg["game_key"]
    target_date = get_target_date(cfg["timezone"])
    identity = build_game_identity(game_cfg, target_date)

    row_idx, row = find_log_row(
        ws,
        identity["date_str"],
        game_key,
    )

    if row:
        post_id = str(row.get("post_id") or "").strip()

        if not post_id:
            raise RuntimeError(
                f"Missing post_id in existing Sheet row for {game_key}"
            )

        post = get_wp_post(cfg, post_id)
        post, state_changed, state_status = reconcile_publish_state(
            cfg,
            game_cfg,
            post,
            target_date,
        )

        if state_changed:
            update_log_row(
                ws,
                row_idx,
                {
                    "status": state_status,
                    "updated_at": now_local(
                        cfg["timezone"]
                    ).isoformat(timespec="seconds"),
                },
            )

        print(
            f"Create mode: {game_key} already exists "
            f"as post {post_id}; no duplicate created."
        )
        return

    wp_matches = find_wp_posts_by_slug(
        cfg,
        identity["slug"],
    )

    if len(wp_matches) > 1:
        raise RuntimeError(
            f"Multiple WordPress posts found for slug "
            f"'{identity['slug']}': {wp_matches}"
        )

    initial_check_answer = (
        get_latest_check_answer_for_game(ws, game_key)
        or game_cfg.get("check_answer", "")
    )

    waiting_data = make_waiting_answer_data(game_cfg)
    timestamp = now_local(cfg["timezone"]).isoformat(
        timespec="seconds"
    )

    if wp_matches:
        post = get_wp_post(cfg, wp_matches[0]["id"])
        post, _, _ = reconcile_publish_state(
            cfg,
            game_cfg,
            post,
            target_date,
        )

        actual_slug = post.get("slug") or identity["slug"]
        post_url = (
            f"{cfg['wp']['site_url'].rstrip('/')}"
            f"/{actual_slug.strip('/')}/"
        )

        append_log_row(
            ws,
            {
                "target_date": identity["date_str"],
                "game_key": game_key,
                "post_id": post["id"],
                "post_url": post_url,
                "slug": actual_slug,
                "question": waiting_data.get("question", ""),
                "answer": waiting_data.get("answer", ""),
                "check_answer": initial_check_answer,
                "status": f"recovered_existing_{post.get('status', '')}",
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )

        print(
            f"Recovered existing WordPress post {post['id']} "
            "into Sheet; no duplicate created."
        )
        return

    crypto_data = fetch_crypto_data(cfg)
    crypto_snapshot_html = rewrite_snapshot_with_openai(
        cfg,
        game_key,
        make_base_snapshot(crypto_data),
    )

    content = build_content(
        game_cfg=game_cfg,
        cfg=cfg,
        date_str=identity["date_str"],
        answer_data=waiting_data,
        crypto_snapshot_html=crypto_snapshot_html,
        readable_date=identity["readable_date"],
        slug_date=identity["slug_date"],
    )

    post = create_wp_post(
        cfg=cfg,
        game_cfg=game_cfg,
        title=identity["title"],
        slug=identity["slug"],
        content=content,
        target_date=target_date,
    )

    update_rankmath_meta(
        cfg,
        post["id"],
        identity["seo_title"],
        identity["meta_description"],
    )

    post_url = (
        f"{cfg['wp']['site_url'].rstrip('/')}"
        f"/{identity['slug'].strip('/')}/"
    )

    append_log_row(
        ws,
        {
            "target_date": identity["date_str"],
            "game_key": game_key,
            "post_id": post["id"],
            "post_url": post_url,
            "slug": identity["slug"],
            "question": waiting_data.get("question", ""),
            "answer": waiting_data.get("answer", ""),
            "check_answer": initial_check_answer,
            "status": f"created_{post.get('status', 'unknown')}",
            "created_at": timestamp,
            "updated_at": timestamp,
        },
    )

    print(
        f"Created {game_key} post {post['id']} "
        f"with status {post.get('status')}."
    )


def update_game_post(cfg, ws, game_cfg):
    game_key = game_cfg["game_key"]
    target_date = get_target_date(cfg["timezone"])
    readable_date = format_date_readable(target_date)
    date_str = target_date.isoformat()
    timestamp = now_local(cfg["timezone"]).isoformat(
        timespec="seconds"
    )

    source = fetch_source_page(game_cfg["source_api_url"])
    source_modified = (
        source.get("modified")
        or source.get("date")
        or ""
    )
    source_modified_gmt = source.get("modified_gmt") or ""
    source_content = source.get("content", {}).get("rendered", "")

    answer_data = extract_game_answer_data(
        source_content,
        game_cfg,
        cfg,
    )

    source_matches_target = source_modified_matches_target(
        source_modified,
        cfg["timezone"],
        target_date,
    )
    day_difference = 0

    if answer_data.get("answer_type") == "city_holder":
        source_local = parse_wp_datetime_gmt(
            source_modified_gmt,
            cfg["timezone"],
        ) or parse_wp_datetime_local(
            source_modified,
            cfg["timezone"],
        )

        tolerance_days = int(
            game_cfg.get("source_date_tolerance_days", 1)
        )

        if source_local:
            day_difference = (
                source_local.date() - target_date
            ).days
            source_matches_target = (
                abs(day_difference) <= tolerance_days
            )
        else:
            source_matches_target = False

        if source_modified_gmt:
            source_modified = source_modified_gmt

    if game_cfg.get("use_content_date", False):
        source_matches_target = (
            answer_data.get("source_date") == target_date
        )

    if answer_data.get("answer_type") == "quote_author":
        source_date = answer_data.get("source_date")
        today_date = now_local(cfg["timezone"]).date()
        yesterday_date = today_date - timedelta(days=1)

        if (
            not source_date
            or not answer_data.get("has_data")
            or source_date not in {today_date, yesterday_date}
        ):
            print(
                f"Hrum source date/data invalid: {source_date}. Skip."
            )
            return

        target_date = source_date
        date_str = source_date.isoformat()
        readable_date = format_date_readable(source_date)
        source_matches_target = True

    row_idx, row = find_log_row(ws, date_str, game_key)

    if not row:
        print(
            f"Update mode: no Sheet row for {game_key} {date_str}. Skip."
        )
        return

    post_id = str(row.get("post_id") or "").strip()

    if not post_id:
        raise RuntimeError(
            f"Missing post_id in Sheet for {game_key} {date_str}"
        )

    post = get_wp_post(cfg, post_id)
    post, state_changed, state_status = reconcile_publish_state(
        cfg,
        game_cfg,
        post,
        target_date,
    )

    if not source_matches_target:
        update_log_row(
            ws,
            row_idx,
            {
                "source_modified": source_modified,
                "status": state_status or "checked_source_not_target_date",
                "updated_at": timestamp,
            },
        )
        print(
            f"{game_key}: source is not for target {date_str}. Skip content."
        )
        return

    if not has_answer_data(answer_data):
        update_log_row(
            ws,
            row_idx,
            {
                "source_modified": source_modified,
                "status": state_status or "checked_no_answer_data",
                "updated_at": timestamp,
            },
        )
        print(f"{game_key}: no usable answer data. Skip content.")
        return

    current_check_value = answer_data.get("check_value", "")
    sheet_check_answer = row.get("check_answer") or ""
    answer_changed = should_update_answer(
        current_check_value,
        sheet_check_answer,
    )

    row_answer_norm = normalize_answer(
        row.get("answer") or ""
    ).lower().rstrip(".")
    row_is_waiting = row_answer_norm in {"", "updating soon"}

    if not answer_changed and not row_is_waiting:
        update_log_row(
            ws,
            row_idx,
            {
                "source_modified": source_modified,
                "status": state_status or "checked_no_new_answer",
                "updated_at": timestamp,
            },
        )
        print(f"{game_key}: answer unchanged.")
        return

    existing_content = (
        post.get("content", {}).get("raw")
        or post.get("content", {}).get("rendered", "")
    )

    if not existing_content:
        raise RuntimeError(
            f"Empty WordPress content for post {post_id}"
        )

    render_data = answer_data
    answer_publish_allowed = can_publish_in_answer_mode(answer_data)

    if answer_data.get("answer_type") == "city_holder":
        combo_is_new = True

        if day_difference != 0:
            current_combo_check = answer_data.get(
                "combo_check_value",
                "",
            )
            previous_combo_check = get_latest_city_combo_signature(
                ws,
                game_key,
                exclude_target_date=date_str,
            )
            combo_is_new = bool(
                current_combo_check
                and previous_combo_check
                and current_combo_check != previous_combo_check
            )

        if not combo_is_new:
            current_city_data = extract_city_holder_data(
                existing_content,
                log_result=False,
            )
            render_data = answer_data.copy()
            render_data["combo_lines"] = current_city_data.get(
                "combo_lines",
                [],
            )

        answer_publish_allowed = bool(
            answer_data.get("combo_lines")
            and combo_is_new
        )

    updated_content = update_existing_answer_content(
        content_html=existing_content,
        game_cfg=game_cfg,
        answer_data=render_data,
        readable_date=readable_date,
    )
    updated_content = auto_link_html(updated_content, cfg)

    publish_mode, _, _ = get_publish_settings(cfg, game_cfg)
    publish_now = bool(
        publish_mode == "answer"
        and answer_publish_allowed
        and str(post.get("status") or "").lower()
        in {"draft", "future", "pending"}
    )

    payload = {"content": updated_content}

    if publish_now:
        payload.update(
            {
                "status": "publish",
                "date": now_local(cfg["timezone"]).isoformat(),
            }
        )

    updated_post = patch_wp_post(cfg, post_id, payload)
    final_status = (
        "published_with_new_answer"
        if publish_now
        else "updated_with_new_answer"
    )

    update_log_row(
        ws,
        row_idx,
        {
            "source_modified": source_modified,
            "question": answer_data.get("question", ""),
            "answer": answer_data.get("answer", ""),
            "check_answer": current_check_value,
            "status": final_status,
            "updated_at": timestamp,
        },
    )

    print(
        f"{game_key}: post {updated_post.get('id')} updated; "
        f"status={updated_post.get('status')}."
    )


def process_game(cfg, ws, game_cfg):
    if not game_cfg.get("enabled", True):
        print(f"Skip disabled game: {game_cfg['game_key']}")
        return

    if not should_run_game_now(cfg, game_cfg):
        print(f"Skip game by run_times: {game_cfg['game_key']}")
        return

    run_mode = os.getenv("RUN_MODE", "update").lower()

    if run_mode == "create":
        create_game_post(cfg, ws, game_cfg)
        return

    if run_mode == "update":
        update_game_post(cfg, ws, game_cfg)
        return

    raise RuntimeError(f"Invalid RUN_MODE: {run_mode}")


def main():
    cfg = load_config()
    ws = get_sheet(cfg)

    for game_cfg in cfg["games"]:
        try:
            process_game(cfg, ws, game_cfg)
            time.sleep(2)
        except Exception as e:
            print(f"ERROR game={game_cfg.get('game_key')}: {e}")
