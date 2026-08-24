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
    date = readable_date(d)

    # Fetch source
    source = fetch_json(DROPEE_SOURCE)
    source_html = source.get("content", {}).get("rendered", "")
    source_soup = BeautifulSoup(source_html, "html.parser")

    source_question = extract_prefixed(source_soup, "Question")
    source_answer = extract_prefixed(source_soup, "Answer")

    source_ready = (
        source_modified_date(source) == d
        and bool(source_question)
        and bool(source_answer)
    )

    # Fetch current MEXC post
    post = get_wp_post(DROPEE_POST_ID)
    content = post.get("content", {}).get("raw", "")

    if not content:
        raise RuntimeError("Dropee post content is empty.")

    soup = BeautifulSoup(content, "html.parser")

    target = soup.find(
        "div",
        id="dropee-answer-area",
    )

    if not target:
        raise RuntimeError(
            "#dropee-answer-area not found."
        )

    # Keep current Q/A if MiningCombo has not updated yet
    current_question = extract_prefixed(
        target,
        "Question",
    )
    current_answer = extract_prefixed(
        target,
        "Answer",
    )

    if source_ready:
        question = source_question
        answer = source_answer
    else:
        question = current_question
        answer = current_answer

    if not question or not answer:
        print(
            "Dropee: no usable Q/A in source "
            "or current post. Skip."
        )
        return

    area = f"""
<h2 class="wp-block-heading">Dropee Question of the Day Answer Today – {html.escape(date)}</h2>
<p class="wp-block-paragraph"><strong>Question: <em>{html.escape(question)}</em></strong></p>
<p class="wp-block-paragraph"><strong>Answer: <em>{html.escape(answer)}</em></strong></p>
<p class="wp-block-paragraph"><strong>Last updated: {html.escape(date)}</strong></p>
""".strip()

    if not replace_area(
        soup,
        "dropee-answer-area",
        area,
    ):
        print("Dropee: already up to date.")
        return

    update_wp_post(
        DROPEE_POST_ID,
        str(soup),
    )

    if source_ready:
        print(
            f"Dropee updated: {date} | "
            f"new source answer: {answer}"
        )
    else:
        print(
            f"Dropee date refreshed: {date} | "
            "source has not updated yet, kept current Q/A."
        )


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


def find_wotd_table(soup):
    heading = soup.find(
        "h3",
        id="binance-word-of-the-day-for-this-week",
    )

    if not heading:
        raise RuntimeError(
            "WOTD table heading not found: "
            "#binance-word-of-the-day-for-this-week"
        )

    # Find the first table after this heading,
    # stopping before the next heading.
    for node in heading.next_elements:
        name = getattr(node, "name", None)

        if node is not heading and name in {"h2", "h3"}:
            break

        if name == "table":
            return node

    raise RuntimeError("WOTD answer table not found.")


def update_wotd_table(soup, groups):
    table = find_wotd_table(soup)
    changed = False
    found_lengths = set()

    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])

        if len(cells) < 2:
            continue

        label = clean(cells[0].get_text(" ", strip=True))

        match = re.fullmatch(
            r"([3-8])\s+letters?",
            label,
            re.I,
        )

        if not match:
            continue

        length = match.group(1)
        found_lengths.add(length)

        answers = groups.get(length, []) or [
            "Updating soon."
        ]

        new_text = ", ".join(answers)
        current_text = clean(
            cells[1].get_text(" ", strip=True)
        )

        if current_text == new_text:
            continue

        cells[1].clear()
        cells[1].string = new_text
        changed = True

    missing = {
        str(length)
        for length in range(3, 9)
    } - found_lengths

    if missing:
        raise RuntimeError(
            "WOTD table rows missing for: "
            + ", ".join(
                f"{length} letters"
                for length in sorted(missing)
            )
        )

    return changed


def extract_wotd_current_meta(area):
    theme = ""
    reward = ""

    for p in area.find_all("p"):
        text = clean(
            p.get_text(" ", strip=True)
        )

        match = re.match(
            r"^Theme\s*:\s*(.+)$",
            text,
            re.I,
        )

        if match:
            theme = match.group(1).strip()
            continue

        match = re.match(
            r"^Prize Pool\s*:\s*(.+)$",
            text,
            re.I,
        )

        if match:
            reward = match.group(1).strip()

    return theme, reward

def update_wotd():
    d = today()
    date = readable_date(d)

    expected_start, expected_end = (
        expected_wotd_campaign(d)
    )

    # -----------------------------------------------------
    # 1. Fetch MiningCombo
    # -----------------------------------------------------

    source = fetch_json(WOTD_SOURCE)
    source_html = source.get(
        "content",
        {},
    ).get("rendered", "")

    theme, reward, start, end, groups = extract_wotd(
        source_html
    )

    campaign_ready = (
        start == expected_start
        and end == expected_end
    )

    answers_ready = (
        campaign_ready
        and any(groups.values())
    )

    # -----------------------------------------------------
    # 2. Fetch current MEXC post
    # -----------------------------------------------------

    post = get_wp_post(WOTD_POST_ID)
    content = post.get(
        "content",
        {},
    ).get("raw", "")

    if not content:
        raise RuntimeError(
            "WOTD post content is empty."
        )

    soup = BeautifulSoup(
        content,
        "html.parser",
    )

    current_area = soup.find(
        "div",
        id="binance-wotd-answer-area",
    )

    if not current_area:
        raise RuntimeError(
            "#binance-wotd-answer-area not found."
        )

    current_theme, current_reward = (
        extract_wotd_current_meta(
            current_area
        )
    )

    # -----------------------------------------------------
    # 3. Theme / Prize only use new source
    #    when current campaign is ready
    # -----------------------------------------------------

    if campaign_ready:
        display_theme = (
            theme
            or current_theme
            or "Updating soon."
        )

        display_reward = (
            reward
            or current_reward
            or "Updating soon."
        )

    else:
        display_theme = (
            current_theme
            or "Updating soon."
        )

        display_reward = (
            current_reward
            or "Updating soon."
        )

    # -----------------------------------------------------
    # 4. Always refresh current date + current week
    # -----------------------------------------------------

    heading_id = (
        "binance-word-of-the-day-answers-today-"
        + d.strftime("%B-%d-%Y").lower()
    )

    area = f"""
<h2 id="{heading_id}" class="wp-block-heading">Binance Word of the Day Answers Today – {html.escape(date)}</h2>
<p class="wp-block-paragraph"><strong>Theme:</strong> {html.escape(display_theme)}</p>
<p class="wp-block-paragraph"><strong>Activity Dates: </strong>{expected_start} to {expected_end}</p>
<p class="wp-block-paragraph"><strong>Last updated: </strong>{html.escape(date)}</p>
<p class="wp-block-paragraph"><strong>Prize Pool:</strong> {html.escape(display_reward)}</p>
""".strip()

    changed = replace_area(
        soup,
        "binance-wotd-answer-area",
        area,
    )

    # -----------------------------------------------------
    # 5. Always refresh Rank Math TOC date
    # -----------------------------------------------------

    toc = soup.find(
        "a",
        href="#binance-wotd-answer-area",
    )

    new_toc_text = (
        "Binance Word of the Day "
        f"Answers Today – {date}"
    )

    if (
        toc
        and clean(
            toc.get_text(
                " ",
                strip=True,
            )
        ) != new_toc_text
    ):
        toc.string = new_toc_text
        changed = True

    # -----------------------------------------------------
    # 6. Only update answers when MiningCombo
    #    is on the current campaign
    # -----------------------------------------------------

    if answers_ready:

        # Summary table
        if update_wotd_table(
            soup,
            groups,
        ):
            changed = True

        # Detailed UL answers
        for length in range(3, 9):
            h3_id = (
                "binance-word-of-the-day-"
                f"{length}-letter-answers"
            )

            h3 = soup.find(
                "h3",
                id=h3_id,
            )

            if not h3:
                raise RuntimeError(
                    "WOTD heading not found: "
                    f"#{h3_id}"
                )

            ul = find_ul_after_heading(h3)

            if not ul:
                raise RuntimeError(
                    "WOTD UL not found after "
                    f"#{h3_id}"
                )

            new_answers = groups.get(
                str(length),
                [],
            ) or ["Updating soon."]

            old_answers = [
                clean(
                    li.get_text(
                        " ",
                        strip=True,
                    )
                )
                for li in ul.find_all(
                    "li",
                    recursive=False,
                )
            ]

            if old_answers == new_answers:
                continue

            ul.clear()

            for answer in new_answers:
                li = soup.new_tag("li")
                li.string = answer
                ul.append(li)

            changed = True

    # -----------------------------------------------------
    # 7. Logging when source is still old
    # -----------------------------------------------------

    elif not campaign_ready:
        print(
            "WOTD: MiningCombo campaign not ready yet. "
            f"Expected {expected_start} to {expected_end}, "
            f"got {start} to {end}. "
            "Refreshing date only."
        )

    else:
        print(
            "WOTD: current campaign detected, "
            "but no answers yet. "
            "Refreshing date/meta only."
        )

    # -----------------------------------------------------
    # 8. Save
    # -----------------------------------------------------

    if not changed:
        print("WOTD: already up to date.")
        return

    update_wp_post(
        WOTD_POST_ID,
        str(soup),
    )

    if answers_ready:
        print(
            f"WOTD updated: {date} | "
            f"{expected_start} to {expected_end} | "
            "answers updated."
        )
    else:
        print(
            f"WOTD refreshed: {date} | "
            f"{expected_start} to {expected_end} | "
            "waiting for new answers."
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
    # 1. Fetch codes from MiningCombo
    source = fetch_json(RED_PACKET_SOURCE)
    source_html = source.get("content", {}).get("rendered", "")

    source_codes = extract_red_packet_codes(source_html)

    if not source_codes:
        print("Red Packet: no source codes found. Skip.")
        return

    # 2. Current date = GMT+7
    current_date = readable_date(today())

    # 3. Fetch current MEXC post
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

    # 4. Compare current codes
    current_codes = extract_red_packet_codes(str(target))

    # 5. Check whether displayed date is already today
    current_text = clean(target.get_text(" ", strip=True))
    date_is_current = current_date in current_text

    if current_codes == source_codes and date_is_current:
        print(
            f"Red Packet: unchanged "
            f"({len(source_codes)} codes, {current_date}). Skip."
        )
        return

    # 6. Rebuild using current GMT+7 date
    new_area = build_red_packet_area(
        source_codes,
        current_date,
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
        f"Date: {current_date}"
    )


# =========================================================
# RUN
# =========================================================

def main():
    if os.getenv("RUN_MODE", "update").lower() != "update":
        print("feature.py: skip non-update run.")
        return

    tasks = [
        # ("Dropee", update_dropee),
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
