import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


# =========================================================
# CONFIG
# =========================================================

SITE_URL = "https://blog.mexc.com"
SOURCE_URL = "https://miningcombo.com/wp-json/wp/v2/pages/6939"
POST_ID = 340602  # <-- thay bằng Dropee post ID thật
ANSWER_AREA_ID = "dropee-answer-area"

TZ = ZoneInfo("Asia/Ho_Chi_Minh")


# =========================================================
# HELPERS
# =========================================================

def wp_auth():
    return (
        os.environ["WP_USERNAME"],
        os.environ["WP_APP_PASSWORD"],
    )


def readable_date(date):
    return f"{date.strftime('%B')} {date.day}, {date.year}"


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


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


def extract_qa(content):
    soup = BeautifulSoup(content, "html.parser")

    question = ""
    answer = ""

    for p in soup.select("p.has-text-align-center.wp-block-paragraph"):
        text = clean(p.get_text(" ", strip=True))

        if not question:
            match = re.match(r"^Question\s*:\s*(.+)$", text, re.I)
            if match:
                question = match.group(1).strip()

        if not answer:
            match = re.match(r"^Answer\s*:\s*(.+)$", text, re.I)
            if match:
                answer = match.group(1).strip()

    return question, answer


def build_answer_area(question, answer, date_text):
    return f"""
<h2 class="wp-block-heading">Dropee Question of the Day Answer Today – {date_text}</h2>

<p class="wp-block-paragraph"><strong>Question: <em>{question}</em></strong></p>

<p class="wp-block-paragraph"><strong>Answer: <em>{answer}</em></strong></p>

<p class="wp-block-paragraph"><strong>Last updated: {date_text}</strong></p>
""".strip()


# =========================================================
# MAIN
# =========================================================

def main():
    if os.getenv("RUN_MODE", "update").lower() != "update":
        print("Dropee: skip non-update run.")
        return

    today = datetime.now(TZ).date()
    date_text = readable_date(today)

    # 1. Fetch MiningCombo source
    source = requests.get(
        SOURCE_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=60,
    )
    source.raise_for_status()
    source = source.json()

    modified_date = source_modified_date(source)

    if modified_date != today:
        print(f"Dropee: source date is {modified_date}, target is {today}. Skip.")
        return

    # 2. Extract today's Q/A
    source_html = source.get("content", {}).get("rendered", "")
    question, answer = extract_qa(source_html)

    if not question or not answer:
        print("Dropee: question or answer not found. Skip.")
        return

    print(f"Dropee question: {question}")
    print(f"Dropee answer: {answer}")

    # 3. Fetch fixed MEXC post
    post_url = f"{SITE_URL}/wp-json/wp/v2/posts/{POST_ID}"

    response = requests.get(
        f"{post_url}?context=edit",
        auth=wp_auth(),
        timeout=60,
    )
    response.raise_for_status()

    post = response.json()
    content = post.get("content", {}).get("raw", "")

    if not content:
        raise RuntimeError("Dropee post content is empty.")

    # 4. Find answer area
    soup = BeautifulSoup(content, "html.parser")
    target = soup.find("div", id=ANSWER_AREA_ID)

    if not target:
        raise RuntimeError(f"#{ANSWER_AREA_ID} not found in Dropee post.")

    new_html = build_answer_area(question, answer, date_text)
    fragment = BeautifulSoup(new_html, "html.parser")

    # Không update lại nếu content đã giống.
    if clean(target.get_text(" ", strip=True)) == clean(fragment.get_text(" ", strip=True)):
        print("Dropee: already up to date.")
        return

    # 5. Replace answer area
    target.clear()

    for node in list(fragment.contents):
        target.append(node)

    # 6. Update WordPress
    response = requests.post(
        post_url,
        auth=wp_auth(),
        json={"content": str(soup)},
        timeout=120,
    )
    response.raise_for_status()

    print(f"Dropee updated successfully: post {POST_ID}")
    print(f"Date: {date_text}")


if __name__ == "__main__":
    main()
