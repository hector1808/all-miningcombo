import os
import re
import json
import base64
import html
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yaml
import requests
import gspread
from bs4 import BeautifulSoup, NavigableString
from google.oauth2.service_account import Credentials
from openai import OpenAI


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


# def target_date_str(tz_name):
#     return (now_local(tz_name).date() + timedelta(days=1)).isoformat()


# def target_date_readable(tz_name):
#     d = now_local(tz_name).date() + timedelta(days=1)
#     return f"{d.strftime('%B')} {d.day}, {d.year}"

def scheduled_publish_datetime(tz_name):
    target_date = get_target_date(tz_name)

    publish_dt = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        2,
        0,
        0,
        tzinfo=ZoneInfo(tz_name),
    )

    return publish_dt.isoformat()

def target_date_str(tz_name):
    return get_target_date(tz_name).isoformat()


def target_date_readable(tz_name):
    d = get_target_date(tz_name)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


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


def replace_date_vars(text, date_str, readable_date=None):
    text = text.replace("{{CURRENT_DATE}}", date_str)
    if readable_date:
        text = text.replace("{{CURRENT_DATE_READABLE}}", readable_date)
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


def extract_question_answer(content_html, game_cfg, cfg):
    question_selector = game_cfg.get("question_selector") or cfg["defaults"]["question_selector"]
    answer_selector = game_cfg.get("answer_selector") or cfg["defaults"]["answer_selector"]

    soup = BeautifulSoup(content_html, "html.parser")

    question = ""
    answer = ""

    for p in soup.select(question_selector):
        text = p.get_text(" ", strip=True)
        if text.lower().startswith("question:"):
            question = re.sub(r"^question:\s*", "", text, flags=re.I).strip()
            break

    for p in soup.select(answer_selector):
        text = p.get_text(" ", strip=True)
        if text.lower().startswith("answer:"):
            answer = re.sub(r"^answer:\s*", "", text, flags=re.I).strip()
            break

    return question, answer


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
- Begin each paragraph exactly with the supplied text.
- After each first sentence, add only ONE short sentence describing the current market trend.
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


def build_content(game_cfg, cfg, date_str, question, answer, crypto_snapshot_html):
    readable_date = target_date_readable(cfg["timezone"])

    with open(game_cfg["template_file"], "r", encoding="utf-8") as f:
        template = f.read()

    content = replace_date_vars(template, date_str, readable_date)
    content = content.replace("{{CRYPTO_SNAPSHOT}}", crypto_snapshot_html)

    content = update_quiz_answer_block(
        content_html=content,
        game_cfg=game_cfg,
        question=question,
        answer=answer,
    )

    return auto_link_html(content, cfg)


# def create_wp_post(cfg, game_cfg, title, slug, content):
#     url = f"{cfg['wp']['site_url'].rstrip('/')}/wp-json/wp/v2/posts"

#     featured_media_id = game_cfg.get("featured_media_id") or cfg["wp"].get("featured_media_id")

#     payload = {
#         "title": title,
#         "slug": slug,
#         "lang": cfg["wp"]["language"],
#         "content": content,
#         "status": cfg["wp"]["status"],
#         "author": cfg["wp"]["author_id"],
#         "categories": cfg["wp"]["category_ids"],
#     }

#     if featured_media_id:
#         payload["featured_media"] = int(featured_media_id)

#     r = requests.post(
#         url,
#         headers={**wp_headers(cfg), "Content-Type": "application/json"},
#         json=payload,
#         timeout=120,
#     )

#     if r.status_code >= 400:
#         raise RuntimeError(f"Post create failed {r.status_code}: {r.text[:2000]}")

#     return r.json()


def create_wp_post(cfg, game_cfg, title, slug, content):
    url = f"{cfg['wp']['site_url'].rstrip('/')}/wp-json/wp/v2/posts"

    run_mode = os.getenv("RUN_MODE", "update").lower()
    featured_media_id = game_cfg.get("featured_media_id") or cfg["wp"].get("featured_media_id")

    payload = {
        "title": title,
        "slug": slug,
        "lang": cfg["wp"]["language"],
        "content": content,
        "author": cfg["wp"]["author_id"],
        "categories": cfg["wp"]["category_ids"],
    }

    if run_mode == "create":
        payload["status"] = "future"
        payload["date"] = scheduled_publish_datetime(cfg["timezone"])
    else:
        payload["status"] = "publish"

    if featured_media_id:
        payload["featured_media"] = int(featured_media_id)

    r = requests.post(
        url,
        headers={**wp_headers(cfg), "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )

    if r.status_code >= 400:
        raise RuntimeError(f"Post create failed {r.status_code}: {r.text[:2000]}")

    return r.json()


def update_wp_post(cfg, post_id, content):
    url = f"{cfg['wp']['site_url'].rstrip('/')}/wp-json/wp/v2/posts/{post_id}"

    r = requests.post(
        url,
        headers={**wp_headers(cfg), "Content-Type": "application/json"},
        json={"content": content},
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


def process_game(cfg, ws, game_cfg):
    if not game_cfg.get("enabled", True):
        print(f"Skip disabled game: {game_cfg['game_key']}")
        return

    if not should_run_game_now(cfg, game_cfg):
        print(f"Skip game by run_times: {game_cfg['game_key']}")
        return

    game_key = game_cfg["game_key"]
    date_str = target_date_str(cfg["timezone"])
    readable_date = target_date_readable(cfg["timezone"])
    timestamp = now_local(cfg["timezone"]).isoformat(timespec="seconds")

    title = replace_date_vars(game_cfg["title_format"], date_str, readable_date)
    slug = normalize_slug(replace_date_vars(game_cfg["slug_format"], date_str, readable_date))
    seo_title = replace_date_vars(game_cfg["seo_title_format"], date_str, readable_date)
    meta_description = replace_date_vars(game_cfg["meta_description_format"], date_str, readable_date)

    print(f"Processing {game_key} for {date_str}")

    source = fetch_source_page(game_cfg["source_api_url"])
    source_modified = source.get("modified") or source.get("date") or ""
    source_content = source.get("content", {}).get("rendered", "")

    question, answer = extract_question_answer(source_content, game_cfg, cfg)

    row_idx, row = find_log_row(ws, date_str, game_key)

    run_mode = os.getenv("RUN_MODE", "update").lower()

    if run_mode == "create" and row:
        print("Create mode: log already exists, skip.")
        return
    
    if run_mode == "update" and not row:
        print("Update mode: today's post log not found, skip.")
        return

    if not row:
        print("No sheet log found. First run for this game/date.")

        # initial_check_answer = game_cfg.get("check_answer", "")
        # answer_changed = should_update_answer(answer, initial_check_answer)

        latest_sheet_check_answer = get_latest_check_answer_for_game(ws, game_key)
        initial_check_answer = latest_sheet_check_answer or game_cfg.get("check_answer", "")
        
        answer_changed = should_update_answer(answer, initial_check_answer)

        crypto_data = fetch_crypto_data(cfg)
        base_snapshot = make_base_snapshot(crypto_data)
        crypto_snapshot_html = rewrite_snapshot_with_openai(cfg, game_key, base_snapshot)

        if answer_changed:
            publish_question = question
            publish_answer = answer
            log_status = "created_with_new_answer"
            new_check_answer = answer
        else:
            publish_question = "Updating soon."
            publish_answer = "Updating soon."
            log_status = "created_waiting_answer"
            new_check_answer = initial_check_answer

        content = build_content(
            game_cfg=game_cfg,
            cfg=cfg,
            date_str=date_str,
            question=publish_question,
            answer=publish_answer,
            crypto_snapshot_html=crypto_snapshot_html,
        )

        post = create_wp_post(cfg, game_cfg, title, slug, content)
        post_id = post["id"]
        post_url = post.get("link", "")

        update_rankmath_meta(cfg, post_id, seo_title, meta_description)

        append_log_row(ws, {
            "target_date": date_str,
            "game_key": game_key,
            "post_id": post_id,
            "post_url": post_url,
            "slug": slug,
            "source_modified": source_modified,
            "question": publish_question,
            "answer": publish_answer,
            "check_answer": new_check_answer,
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
        raise RuntimeError(f"Missing post_id in sheet for {game_key} {date_str}")

    sheet_check_answer = row.get("check_answer") or ""
    answer_changed = should_update_answer(answer, sheet_check_answer)

    if not answer_changed:
        print("Answer unchanged. No post update needed.")

        update_log_row(ws, row_idx, {
            "source_modified": source_modified,
            "status": "checked_no_new_answer",
            "updated_at": timestamp,
        })

        return

    print("New answer detected. Updating existing post.")

    existing_post = get_wp_post(cfg, post_id)
    existing_content = (
        existing_post.get("content", {}).get("raw")
        or existing_post.get("content", {}).get("rendered", "")
    )

    updated_content = update_quiz_answer_block(
        content_html=existing_content,
        game_cfg=game_cfg,
        question=question,
        answer=answer,
    )

    updated_content = auto_link_html(updated_content, cfg)

    update_wp_post(cfg, post_id, updated_content)

    update_log_row(ws, row_idx, {
        "source_modified": source_modified,
        "question": question,
        "answer": answer,
        "check_answer": answer,
        "status": "updated_with_new_answer",
        "updated_at": timestamp,
    })

    print(f"Updated post {post_id}")
    print("Status: updated_with_new_answer")


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
