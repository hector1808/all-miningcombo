import os
import re
import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


# =========================================================
# CONFIG
# =========================================================

SITE_URL = "https://blog.mexc.com"
TZ = ZoneInfo("Asia/Ho_Chi_Minh")

DROPEE_POST_ID = 340602
DROPEE_SOURCE = "https://miningcombo.com/wp-json/wp/v2/pages/6939"

WOTD_POST_ID = 340608
WOTD_SOURCE = "https://miningcombo.com/wp-json/wp/v2/pages/25514"

RED_PACKET_POST_ID = 340688
RED_PACKET_SOURCE = "https://miningcombo.com/wp-json/wp/v2/pages/26104"


# =========================================================
# COMMON
# =========================================================

def auth():
    return (
        os.environ["WP_USERNAME"],
        os.environ["WP_APP_PASSWORD"],
    )


def today():
    return datetime.now(TZ).date()


def readable_date(d):
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def fetch_json(url):
    r = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def source_modified_date(source):
    value = source.get("modified_gmt")

    if value:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))

        return dt.astimezone(TZ).date()

    value = source.get("modified") or source.get("date")

    if not value:
        return None

    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)

    return dt.astimezone(TZ).date()


def get_wp_post(post_id):
    r = requests.get(
        f"{SITE_URL}/wp-json/wp/v2/posts/{post_id}?context=edit",
        auth=auth(),
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def update_wp_post(post_id, content):
    r = requests.post(
        f"{SITE_URL}/wp-json/wp/v2/posts/{post_id}",
        auth=auth(),
        json={"content": content},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def replace_area(soup, area_id, new_html):
    target = soup.find("div", id=area_id)

    if not target:
        raise RuntimeError(f"#{area_id} not found.")

    fragment = BeautifulSoup(new_html, "html.parser")

    old_text = clean(target.get_text(" ", strip=True))
    new_text = clean(fragment.get_text(" ", strip=True))

    if old_text == new_text:
        return False

    target.clear()

    for node in list(fragment.contents):
        target.append(node)

    return True


# =========================================================
# DROPEE
# =========================================================

def extract_prefixed(soup, prefix):
    pattern = re.compile(
        rf"^{re.escape(prefix)}\s*:\s*(.+)$",
        re.I,
    )

    for p in soup.find_all("p"):
        match = pattern.match(
            clean(p.get_text(" ", strip=True))
        )

        if match:
            return match.group(1).strip()

    return ""


def update_dropee():
    d = today()
    source = fetch_json(DROPEE_SOURCE)

    if source_modified_date(source) != d:
        print("Dropee: source is not updated today. Skip.")
        return

    source_html = source.get("content", {}).get("rendered", "")
    source_soup = BeautifulSoup(source_html, "html.parser")

    question = extract_prefixed(source_soup, "Question")
    answer = extract_prefixed(source_soup, "Answer")

    if not question or not answer:
        print("Dropee: Q/A missing. Skip.")
        return

    date = readable_date(d)

    area = f"""
<h2 class="wp-block-heading">Dropee Question of the Day Answer Today – {html.escape(date)}</h2>
<p class="wp-block-paragraph"><strong>Question: <em>{html.escape(question)}</em></strong></p>
<p class="wp-block-paragraph"><strong>Answer: <em>{html.escape(answer)}</em></strong></p>
<p class="wp-block-paragraph"><strong>Last updated: {html.escape(date)}</strong></p>
""".strip()

    post = get_wp_post(DROPEE_POST_ID)
    content = post.get("content", {}).get("raw", "")

    if not content:
        raise RuntimeError("Dropee post content is empty.")

    soup = BeautifulSoup(content, "html.parser")

    if not replace_area(soup, "dropee-answer-area", area):
        print("Dropee: already up to date.")
        return

    update_wp_post(DROPEE_POST_ID, str(soup))

    print(f"Dropee updated: {date} | {answer}")


# =========================================================
# BINANCE WOTD
# =========================================================

def expected_wotd_campaign(d):
    days_since_sunday = (d.weekday() + 1) % 7
    sunday = d - timedelta(days=days_since_sunday)

    return (
        sunday + timedelta(days=1),
        sunday + timedelta(days=7),
    )


def extract_wotd(content):
    soup = BeautifulSoup(content, "html.parser")

    theme = ""
    reward = ""
    start = None
    end = None
    groups = {}

    # Theme + Date + Prize Pool
    for p in soup.find_all("p"):
        text = clean(p.get_text(" ", strip=True))

        if not (
            re.search(r"\bTheme\s*:", text, re.I)
            and re.search(r"\bDate\s*:", text, re.I)
        ):
            continue

        match = re.search(
            r"Theme\s*:\s*(.*?)\s+Date\s*:",
            text,
            re.I,
        )

        if match:
            theme = match.group(1).strip()

        match = re.search(
            r"Date\s*:\s*(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})",
            text,
            re.I,
        )

        if match:
            start = datetime.fromisoformat(match.group(1)).date()
            end = datetime.fromisoformat(match.group(2)).date()

        for mark in p.find_all("mark"):
            mark_text = clean(mark.get_text(" ", strip=True))

            if "to be shared" in mark_text.lower():
                reward = mark_text
                break

        if not reward:
            match = re.search(
                r"([0-9][A-Za-z0-9 .,+-]*?to be shared!?)",
                text,
                re.I,
            )

            if match:
                reward = match.group(1).strip()

        break

    # 3-8 letter answers
    for h3 in soup.find_all("h3"):
        match = re.search(
            r"Answer\s+(\d+)\s+letters",
            clean(h3.get_text(" ", strip=True)),
            re.I,
        )

        if not match:
            continue

        length = match.group(1)
        ul = h3.find_next_sibling("ul") or h3.find_next("ul")

        groups[length] = (
            [
                clean(li.get_text(" ", strip=True))
                for li in ul.find_all("li", recursive=False)
                if clean(li.get_text(" ", strip=True))
            ]
            if ul
            else []
        )

    return theme, reward, start, end, groups


def find_ul_after_heading(h3):
    for node in h3.next_siblings:
        name = getattr(node, "name", None)

        if name == "ul":
            return node

        if name in {"h2", "h3"}:
            break

    return None


def update_wotd():
    d = today()
    expected_start, expected_end = expected_wotd_campaign(d)

    source = fetch_json(WOTD_SOURCE)
    source_html = source.get("content", {}).get("rendered", "")

    theme, reward, start, end, groups = extract_wotd(source_html)

    if start != expected_start or end != expected_end:
        print(
            f"WOTD: wrong campaign. "
            f"Expected {expected_start} to {expected_end}, "
            f"got {start} to {end}. Skip."
        )
        return

    if not any(groups.values()):
        print("WOTD: no answers found. Skip.")
        return

    post = get_wp_post(WOTD_POST_ID)
    content = post.get("content", {}).get("raw", "")

    if not content:
        raise RuntimeError("WOTD post content is empty.")

    soup = BeautifulSoup(content, "html.parser")
    date = readable_date(d)

    heading_id = (
        "binance-word-of-the-day-answers-today-"
        + d.strftime("%B-%d-%Y").lower()
    )

    area = f"""
<h2 id="{heading_id}" class="wp-block-heading">Binance Word of the Day Answers Today – {html.escape(date)}</h2>
<p class="wp-block-paragraph"><strong>Theme:</strong> {html.escape(theme or "Updating soon.")}</p>
<p class="wp-block-paragraph"><strong>Activity Dates: </strong>{start} to {end}</p>
<p class="wp-block-paragraph"><strong>Last updated: </strong>{html.escape(date)}</p>
<p class="wp-block-paragraph"><strong>Prize Pool:</strong> {html.escape(reward or "Updating soon.")}</p>
""".strip()

    changed = replace_area(
        soup,
        "binance-wotd-answer-area",
        area,
    )

    # Update Rank Math TOC text
    toc = soup.find(
        "a",
        href="#binance-wotd-answer-area",
    )

    new_toc_text = (
        f"Binance Word of the Day Answers Today – {date}"
    )

    if toc and clean(toc.get_text(" ", strip=True)) != new_toc_text:
        toc.string = new_toc_text
        changed = True

    # Update answer ULs
    for length in range(3, 9):
        h3_id = (
            f"binance-word-of-the-day-"
            f"{length}-letter-answers"
        )

        h3 = soup.find("h3", id=h3_id)

        if not h3:
            raise RuntimeError(
                f"WOTD heading not found: #{h3_id}"
            )

        ul = find_ul_after_heading(h3)

        if not ul:
            raise RuntimeError(
                f"WOTD UL not found after #{h3_id}"
            )

        new_answers = groups.get(str(length), []) or [
            "Updating soon."
        ]

        old_answers = [
            clean(li.get_text(" ", strip=True))
            for li in ul.find_all("li", recursive=False)
        ]

        if old_answers == new_answers:
            continue

        ul.clear()

        for answer in new_answers:
            li = soup.new_tag("li")
            li.string = answer
            ul.append(li)

        changed = True

    if not changed:
        print("WOTD: already up to date.")
        return

    update_wp_post(WOTD_POST_ID, str(soup))

    print(
        f"WOTD updated: {date} | "
        f"{start} to {end}"
    )


# =========================================================
# BINANCE RED PACKET
# =========================================================

def extract_red_packet_codes(content):
    soup = BeautifulSoup(content, "html.parser")

    pattern = re.compile(
        r"^\s*#\s*(\d+)\s+(?:No\s+)?Code\s+Is\s*:?",
        re.I,
    )

    codes = {}

    for p in soup.find_all("p"):
        text = clean(p.get_text(" ", strip=True))
        match = pattern.search(text)

        if not match:
            continue

        number = int(match.group(1))
        code_element = p.select_one("span.copy-text")

        if code_element:
            code = (
                code_element.get("data-original-text")
                or code_element.get_text(" ", strip=True)
            )
        else:
            code = pattern.sub("", text).strip()

        code = clean(code)

        if code:
            codes[number] = code

    return [
        {
            "number": number,
            "code": codes[number],
        }
        for number in sorted(codes, reverse=True)
    ]


def extract_red_packet_date(source):
    content = source.get("content", {}).get("rendered", "")
    text = clean(BeautifulSoup(content, "html.parser").get_text(" ", strip=True))

    match = re.search(
        r"Date\s*:\s*(\d{1,2})/(\d{1,2})/(\d{4})",
        text,
        re.I,
    )

    if not match:
        return ""

    day, month, year = map(int, match.groups())

    return readable_date(
        datetime(year, month, day).date()
    )


def build_red_packet_area(codes, date):
    parts = [
        (
            '<h2 class="wp-block-heading">'
            "Binance Red Packet Codes Today "
            f"<strong>for {html.escape(date)} "
            "(Updated Hourly)</strong>"
            "</h2>"
        ),
        (
            '<p class="wp-block-paragraph">'
            "<strong>Last updated:</strong> "
            f"{html.escape(date)}"
            "</p>"
        ),
    ]

    for item in codes:
        parts.append(
            '<p class="wp-block-paragraph">'
            f"#{item['number']:02d} Code Is:&nbsp;"
            f"{html.escape(item['code'])}"
            "</p>"
        )

    return "\n".join(parts)


def update_red_packet():
    # 1. Fetch MiningCombo
    source = fetch_json(RED_PACKET_SOURCE)
    source_html = source.get("content", {}).get("rendered", "")

    source_codes = extract_red_packet_codes(source_html)
    source_date = extract_red_packet_date(source)

    if not source_codes:
        print("Red Packet: no source codes found. Skip.")
        return

    if not source_date:
        print("Red Packet: source date not found. Skip.")
        return

    # 2. Fetch current MEXC post
    post = get_wp_post(RED_PACKET_POST_ID)
    content = post.get("content", {}).get("raw", "")

    if not content:
        raise RuntimeError("Red Packet post content is empty.")

    soup = BeautifulSoup(content, "html.parser")

    target = soup.find(
        "div",
        id="red-packet-answer-area",
    )

    if not target:
        raise RuntimeError(
            "#red-packet-answer-area not found."
        )

    # 3. Extract current codes from MEXC
    current_codes = extract_red_packet_codes(str(target))

    # 4. Compare answers only
    if current_codes == source_codes:
        print(
            f"Red Packet: codes unchanged "
            f"({len(source_codes)} codes). Skip."
        )
        return

    # 5. Codes changed → rebuild using source date
    new_area = build_red_packet_area(
        source_codes,
        source_date,
    )

    target.clear()

    fragment = BeautifulSoup(
        new_area,
        "html.parser",
    )

    for node in list(fragment.contents):
        target.append(node)

    update_wp_post(
        RED_PACKET_POST_ID,
        str(soup),
    )

    print(
        f"Red Packet updated: "
        f"{len(current_codes)} → {len(source_codes)} codes | "
        f"Source date: {source_date}"
    )


# =========================================================
# RUN
# =========================================================

def main():
    if os.getenv("RUN_MODE", "update").lower() != "update":
        print("feature.py: skip non-update run.")
        return

    tasks = [
        ("Dropee", update_dropee),
        ("Binance WOTD", update_wotd),
        ("Binance Red Packet", update_red_packet),
    ]

    for name, task in tasks:
        try:
            task()
        except Exception as e:
            print(f"ERROR {name}: {e}")


if __name__ == "__main__":
    main()
