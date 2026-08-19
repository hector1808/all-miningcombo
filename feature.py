import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


SITE_URL = "https://blog.mexc.com"
TZ = ZoneInfo("Asia/Ho_Chi_Minh")

DROPEE_POST_ID = 340602
DROPEE_SOURCE_URL = "https://miningcombo.com/wp-json/wp/v2/pages/6939"
DROPEE_AREA_ID = "dropee-answer-area"

WOTD_POST_ID = 340608
WOTD_SOURCE_URL = "https://miningcombo.com/wp-json/wp/v2/pages/25514"
WOTD_AREA_ID = "binance-wotd-answer-area"


# =========================================================
# COMMON
# =========================================================

def wp_auth():
    return (
        os.environ["WP_USERNAME"],
        os.environ["WP_APP_PASSWORD"],
    )


def today():
    return datetime.now(TZ).date()


def date_text(d):
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


def get_wp_post(post_id):
    url = f"{SITE_URL}/wp-json/wp/v2/posts/{post_id}?context=edit"

    r = requests.get(
        url,
        auth=wp_auth(),
        timeout=60,
    )
    r.raise_for_status()

    return r.json()


def update_wp_post(post_id, content):
    url = f"{SITE_URL}/wp-json/wp/v2/posts/{post_id}"

    r = requests.post(
        url,
        auth=wp_auth(),
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

    if clean(target.get_text(" ", strip=True)) == clean(
        fragment.get_text(" ", strip=True)
    ):
        return False

    target.clear()

    for node in list(fragment.contents):
        target.append(node)

    return True


# =========================================================
# DROPEE
# =========================================================

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
    current_date = today()
    source = fetch_json(DROPEE_SOURCE_URL)

    if source_modified_date(source) != current_date:
        print("Dropee: source is not updated for today. Skip.")
        return

    source_html = source.get("content", {}).get("rendered", "")
    source_soup = BeautifulSoup(source_html, "html.parser")

    question = extract_prefixed(source_soup, "Question")
    answer = extract_prefixed(source_soup, "Answer")

    if not question or not answer:
        print("Dropee: question or answer missing. Skip.")
        return

    d = date_text(current_date)

    new_area = f"""
<h2 class="wp-block-heading">Dropee Question of the Day Answer Today – {d}</h2>
<p class="wp-block-paragraph"><strong>Question: <em>{question}</em></strong></p>
<p class="wp-block-paragraph"><strong>Answer: <em>{answer}</em></strong></p>
<p class="wp-block-paragraph"><strong>Last updated: {d}</strong></p>
""".strip()

    post = get_wp_post(DROPEE_POST_ID)
    content = post.get("content", {}).get("raw", "")

    if not content:
        raise RuntimeError("Dropee post content is empty.")

    soup = BeautifulSoup(content, "html.parser")

    if not replace_area(soup, DROPEE_AREA_ID, new_area):
        print("Dropee: already up to date.")
        return

    update_wp_post(DROPEE_POST_ID, str(soup))

    print(
        f"Dropee updated: {d} | "
        f"Question: {question} | "
        f"Answer: {answer}"
    )


# =========================================================
# BINANCE WOTD
# =========================================================

def expected_wotd_campaign(current_date):
    days_since_sunday = (current_date.weekday() + 1) % 7
    sunday = current_date - timedelta(days=days_since_sunday)

    return (
        sunday + timedelta(days=1),
        sunday + timedelta(days=7),
    )


def extract_wotd(content):
    soup = BeautifulSoup(content, "html.parser")

    theme = ""
    reward = ""
    campaign_start = None
    campaign_end = None
    groups = {}

    summary = None

    for p in soup.find_all("p"):
        text = clean(p.get_text(" ", strip=True))

        if re.search(r"\bTheme\s*:", text, re.I) and re.search(
            r"\bDate\s*:", text, re.I
        ):
            summary = p
            break

    if summary:
        text = clean(summary.get_text(" ", strip=True))

        match = re.search(
            r"Theme\s*:\s*(.*?)\s+Date\s*:",
            text,
            re.I,
        )

        if match:
            theme = match.group(1).strip()

        date_match = re.search(
            r"Date\s*:\s*(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})",
            text,
            re.I,
        )

        if date_match:
            campaign_start = datetime.fromisoformat(
                date_match.group(1)
            ).date()

            campaign_end = datetime.fromisoformat(
                date_match.group(2)
            ).date()

            after_dates = text[date_match.end():]

            reward_match = re.search(
                r"([0-9][A-Za-z0-9 .,+-]*?to be shared!?)",
                after_dates,
                re.I,
            )

            if reward_match:
                reward = reward_match.group(1).strip()

        if not reward:
            for mark in summary.find_all("mark"):
                mark_text = clean(
                    mark.get_text(" ", strip=True)
                )

                if "to be shared" in mark_text.lower():
                    reward = mark_text
                    break

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

    return theme, reward, campaign_start, campaign_end, groups


def find_ul_after_heading(h3):
    for node in h3.next_siblings:
        name = getattr(node, "name", None)

        if name == "ul":
            return node

        if name in {"h2", "h3"}:
            break

    return None


def update_wotd():
    current_date = today()
    expected_start, expected_end = expected_wotd_campaign(current_date)

    source = fetch_json(WOTD_SOURCE_URL)
    source_html = source.get("content", {}).get("rendered", "")

    theme, reward, campaign_start, campaign_end, groups = extract_wotd(
        source_html
    )

    if (
        campaign_start != expected_start
        or campaign_end != expected_end
    ):
        print(
            "WOTD: source campaign does not match current campaign. "
            f"Expected {expected_start} to {expected_end}, "
            f"got {campaign_start} to {campaign_end}. Skip."
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
    d = date_text(current_date)

    h2_id = (
        "binance-word-of-the-day-answers-today-"
        + current_date.strftime("%B-%d-%Y").lower()
    )

    top_area = f"""
<h2 id="{h2_id}" class="wp-block-heading">Binance Word of the Day Answers Today – {d}</h2>
<p class="wp-block-paragraph"><strong>Theme:</strong> {theme or "Updating soon."}</p>
<p class="wp-block-paragraph"><strong>Activity Dates: </strong>{campaign_start} to {campaign_end}</p>
<p class="wp-block-paragraph"><strong>Last updated: </strong>{d}</p>
<p class="wp-block-paragraph"><strong>Prize Pool:</strong> {reward or "Updating soon."}</p>
""".strip()

    changed = replace_area(
        soup,
        WOTD_AREA_ID,
        top_area,
    )

    toc_link = soup.find(
        "a",
        href="#binance-wotd-answer-area",
    )
    
    if toc_link:
        new_toc_text = (
            "Binance Word of the Day "
            f"Answers Today – {d}"
        )
    
        if clean(
            toc_link.get_text(
                " ",
                strip=True,
            )
        ) != new_toc_text:
            toc_link.string = new_toc_text
            changed = True

    for length in range(3, 9):
        heading_id = (
            f"binance-word-of-the-day-"
            f"{length}-letter-answers"
        )

        h3 = soup.find("h3", id=heading_id)

        if not h3:
            raise RuntimeError(
                f"WOTD heading not found: #{heading_id}"
            )

        ul = find_ul_after_heading(h3)

        if not ul:
            raise RuntimeError(
                f"WOTD answer list not found after #{heading_id}"
            )

        new_answers = groups.get(str(length), []) or [
            "Updating soon."
        ]

        current_answers = [
            clean(li.get_text(" ", strip=True))
            for li in ul.find_all("li", recursive=False)
        ]

        if current_answers == new_answers:
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
        f"WOTD updated: {d} | "
        f"Campaign: {campaign_start} to {campaign_end}"
    )

    for length in range(3, 9):
        print(
            f"{length} letters: "
            f"{groups.get(str(length), [])}"
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
    ]

    for name, task in tasks:
        try:
            task()
        except Exception as e:
            print(f"ERROR {name}: {e}")


if __name__ == "__main__":
    main()
