"""Daily image-quiz publisher for MiningCombo.

This module intentionally lives outside main.py/feature.py. It reuses their
stable date, WordPress, Google Sheets, and formatting helpers while keeping
image-specific extraction, cropping, media upload, and latest-news generation
isolated here.
"""

import hashlib
import html
import os
import time
from io import BytesIO
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from PIL import Image

import main as core


DEFAULT_IMAGE_SELECTOR = "img.daily-combo-image"
DEFAULT_CROP_TOP_RATIO = 0.10
DEFAULT_LATEST_NEWS_MODEL = "gpt-5-nano-2025-08-07"


def get_image_quiz_settings(cfg):
    section = cfg.get("img_quiz", {})
    defaults = section.get("defaults", {})
    games = section.get("games", [])
    return defaults, games


def extract_source_image_url(source_html, source_api_url, selector):
    soup = BeautifulSoup(source_html or "", "html.parser")
    image = soup.select_one(selector)

    if not image:
        raise RuntimeError(f"Image not found with selector: {selector}")

    image_url = (
        image.get("src")
        or image.get("data-src")
        or image.get("data-lazy-src")
    )

    if not image_url:
        raise RuntimeError(f"Image found but URL is missing: {selector}")

    return urljoin(source_api_url, html.unescape(image_url.strip()))


def download_and_crop_image(image_url, crop_top_ratio):
    try:
        ratio = float(crop_top_ratio)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid crop_top_ratio: {crop_top_ratio}") from exc

    if not 0 <= ratio < 0.5:
        raise RuntimeError("crop_top_ratio must be between 0 and 0.5")

    response = requests.get(
        image_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://miningcombo.com/",
        },
        timeout=60,
    )
    response.raise_for_status()

    original = Image.open(BytesIO(response.content))
    original.load()
    width, height = original.size
    crop_top_px = round(height * ratio)
    cropped = original.crop((0, crop_top_px, width, height))

    if cropped.mode not in {"RGB", "RGBA"}:
        cropped = cropped.convert("RGBA" if "transparency" in cropped.info else "RGB")

    output = BytesIO()
    cropped.save(output, format="PNG", optimize=True)
    image_bytes = output.getvalue()

    return {
        "bytes": image_bytes,
        "sha256": hashlib.sha256(image_bytes).hexdigest(),
        "width": cropped.width,
        "height": cropped.height,
        "crop_top_px": crop_top_px,
    }


def upload_wp_media(cfg, image_bytes, filename, title, alt_text):
    url = f"{cfg['wp']['site_url'].rstrip('/')}/wp-json/wp/v2/media"
    response = requests.post(
        url,
        headers={
            **core.wp_headers(cfg),
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "image/png",
        },
        data=image_bytes,
        timeout=120,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Media upload failed {response.status_code}: {response.text[:2000]}"
        )

    media = response.json()
    media_id = media.get("id")

    if not media_id or not media.get("source_url"):
        raise RuntimeError(f"Invalid WordPress media response: {media}")

    metadata_response = requests.post(
        f"{url}/{media_id}",
        headers={
            **core.wp_headers(cfg),
            "Content-Type": "application/json",
        },
        json={"title": title, "alt_text": alt_text},
        timeout=60,
    )

    if metadata_response.status_code >= 400:
        print(
            f"WARNING media metadata update failed {metadata_response.status_code}: "
            f"{metadata_response.text[:500]}"
        )

    return media


def normalize_latest_news_html(raw_text):
    soup = BeautifulSoup(raw_text or "", "html.parser")
    paragraphs = soup.find_all("p")

    if not paragraphs:
        blocks = [block.strip() for block in (raw_text or "").split("\n\n") if block.strip()]
        paragraphs = [BeautifulSoup(f"<p>{html.escape(block)}</p>", "html.parser").p for block in blocks]

    if not 2 <= len(paragraphs) <= 4:
        raise RuntimeError(
            f"Latest news must contain 2-4 paragraphs; received {len(paragraphs)}"
        )

    return "\n".join(str(paragraph) for paragraph in paragraphs)


def generate_latest_news(cfg, defaults, game_cfg):
    model = game_cfg.get(
        "latest_news_model",
        defaults.get("latest_news_model", DEFAULT_LATEST_NEWS_MODEL),
    )
    game_name = game_cfg.get("game_name") or game_cfg["game_key"].replace("_", " ")
    run_date = core.now_local(cfg["timezone"]).date().isoformat()
    prompt = f"""
Research the latest reliable news and official updates about {game_name} as of {run_date}.

Write a concise Latest News section for a daily answer article.

Requirements:
- Return HTML only, using 2 to 4 <p> paragraphs and no heading.
- Do not include a date in the output because the article may publish later.
- Focus on current gameplay, features, events, or official project updates.
- Do not invent announcements, rewards, token listings, or roadmap claims.
- Prefer official project sources; use reputable secondary sources only when needed.
- Keep the tone neutral and useful, not promotional.
- End with a short reminder that game events, card requirements, and conditions can change and players should verify in the game or official channels.
""".strip()

    client = OpenAI(api_key=core.get_env("OPENAI_API_KEY"))
    last_error = None

    for attempt in range(1, 4):
        try:
            response = client.responses.create(
                model=model,
                tools=[{"type": "web_search"}],
                input=prompt,
            )
            return normalize_latest_news_html(response.output_text.strip())
        except Exception as exc:
            last_error = exc
            print(f"Latest news attempt {attempt}/3 failed: {exc}")
            if attempt < 3:
                time.sleep(2 * attempt)

    raise RuntimeError(f"Could not generate latest news: {last_error}")


def build_waiting_answer_area():
    return "<p><strong>Updating soon.</strong></p>"


def build_image_answer_area(media_url, alt_text, width, height):
    return (
        '<figure class="wp-block-image size-full">'
        f'<img src="{html.escape(media_url, quote=True)}" '
        f'alt="{html.escape(alt_text, quote=True)}" '
        f'width="{int(width)}" height="{int(height)}" />'
        "</figure>"
    )


def build_template_content(cfg, defaults, game_cfg, identity, latest_news_html):
    with open(game_cfg["template_file"], "r", encoding="utf-8") as file:
        content = file.read()

    content = core.replace_date_vars(
        content,
        identity["date_str"],
        identity["readable_date"],
        identity["slug_date"],
    )

    if content.count("{{LATEST_NEWS}}") != 1:
        raise RuntimeError(
            f"Template for {game_cfg['game_key']} must contain exactly one {{LATEST_NEWS}}."
        )

    content = content.replace("{{LATEST_NEWS}}", latest_news_html, 1)
    content = core.replace_template_answer_area(
        content,
        game_cfg,
        build_waiting_answer_area(),
    )
    return core.auto_link_html(content, cfg)


def create_image_quiz_post(cfg, ws, defaults, game_cfg):
    game_key = game_cfg["game_key"]
    target_date = core.get_target_date(cfg["timezone"])
    identity = core.build_game_identity(game_cfg, target_date)
    row_idx, row = core.find_log_row(ws, identity["date_str"], game_key)

    if row:
        print(f"Create mode: {game_key} already exists; no duplicate or reschedule.")
        return

    matches = core.find_wp_posts_by_slug(cfg, identity["slug"])
    if len(matches) > 1:
        raise RuntimeError(f"Multiple WordPress posts found for slug {identity['slug']}")

    timestamp = core.now_local(cfg["timezone"]).isoformat(timespec="seconds")
    previous_hash = core.get_latest_check_answer_for_game(ws, game_key) or ""

    if matches:
        post = core.get_wp_post(cfg, matches[0]["id"])
        actual_slug = post.get("slug") or identity["slug"]
        core.append_log_row(
            ws,
            {
                "target_date": identity["date_str"],
                "game_key": game_key,
                "post_id": post["id"],
                "post_url": f"{cfg['wp']['site_url'].rstrip('/')}/{actual_slug.strip('/')}/",
                "slug": actual_slug,
                "answer": "Updating soon",
                "check_answer": previous_hash,
                "status": f"recovered_existing_{post.get('status', '')}",
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
        print(f"Recovered existing WordPress post {post['id']} into Sheet.")
        return

    latest_news_html = generate_latest_news(cfg, defaults, game_cfg)
    content = build_template_content(cfg, defaults, game_cfg, identity, latest_news_html)
    post = core.create_wp_post(
        cfg,
        game_cfg,
        identity["title"],
        identity["slug"],
        content,
        target_date,
    )
    core.update_rankmath_meta(
        cfg,
        post["id"],
        identity["seo_title"],
        identity["meta_description"],
    )
    post_url = f"{cfg['wp']['site_url'].rstrip('/')}/{identity['slug'].strip('/')}/"
    core.append_log_row(
        ws,
        {
            "target_date": identity["date_str"],
            "game_key": game_key,
            "post_id": post["id"],
            "post_url": post_url,
            "slug": identity["slug"],
            "answer": "Updating soon",
            "check_answer": previous_hash,
            "status": f"created_{post.get('status', 'unknown')}",
            "created_at": timestamp,
            "updated_at": timestamp,
        },
    )
    print(f"Created {game_key} post {post['id']} with status {post.get('status')}.")


def update_image_quiz_post(cfg, ws, defaults, game_cfg):
    game_key = game_cfg["game_key"]
    target_date = core.get_target_date(cfg["timezone"])
    date_str = target_date.isoformat()
    timestamp = core.now_local(cfg["timezone"]).isoformat(timespec="seconds")
    row_idx, row = core.find_log_row(ws, date_str, game_key)

    if not row:
        print(f"Update mode: no Sheet row for {game_key} {date_str}. Skip.")
        return

    post_id = str(row.get("post_id") or "").strip()
    if not post_id:
        raise RuntimeError(f"Missing post_id in Sheet for {game_key} {date_str}")

    source = core.fetch_source_page(game_cfg["source_api_url"])
    source_modified = source.get("modified") or source.get("date") or ""

    if not core.source_modified_matches_target(
        source_modified,
        cfg["timezone"],
        target_date,
    ):
        core.update_log_row(
            ws,
            row_idx,
            {
                "source_modified": source_modified,
                "status": "checked_source_not_target_date",
                "updated_at": timestamp,
            },
        )
        print(f"{game_key}: source is not for target {date_str}. Skip.")
        return

    selector = game_cfg.get(
        "image_selector",
        defaults.get("image_selector", DEFAULT_IMAGE_SELECTOR),
    )
    crop_ratio = game_cfg.get(
        "crop_top_ratio",
        defaults.get("crop_top_ratio", DEFAULT_CROP_TOP_RATIO),
    )
    source_html = source.get("content", {}).get("rendered", "")
    image_url = extract_source_image_url(source_html, game_cfg["source_api_url"], selector)
    cropped = download_and_crop_image(image_url, crop_ratio)
    current_hash = cropped["sha256"]
    previous_hash = str(row.get("check_answer") or "").strip()
    row_answer = str(row.get("answer") or "").strip().lower()

    if current_hash == previous_hash and row_answer not in {"", "updating soon"}:
        core.update_log_row(
            ws,
            row_idx,
            {
                "source_modified": source_modified,
                "status": "checked_no_new_image",
                "updated_at": timestamp,
            },
        )
        print(f"{game_key}: image unchanged.")
        return

    if current_hash == previous_hash:
        core.update_log_row(
            ws,
            row_idx,
            {
                "source_modified": source_modified,
                "status": "checked_waiting_for_new_image",
                "updated_at": timestamp,
            },
        )
        print(f"{game_key}: source still contains the previous image. Skip.")
        return

    readable_date = core.format_date_readable(target_date)
    alt_text = core.replace_date_vars(
        game_cfg.get("answer_image_alt", f"{game_cfg.get('game_name', game_key)} answer"),
        date_str,
        readable_date,
        core.format_date_slug(target_date),
    )
    filename = f"{core.normalize_slug(game_key)}-{date_str}.png"
    media = upload_wp_media(
        cfg,
        cropped["bytes"],
        filename,
        alt_text,
        alt_text,
    )
    answer_html = build_image_answer_area(
        media["source_url"],
        alt_text,
        cropped["width"],
        cropped["height"],
    )
    post = core.get_wp_post(cfg, post_id)
    existing_content = (
        post.get("content", {}).get("raw")
        or post.get("content", {}).get("rendered", "")
    )
    updated_content = core.replace_answer_area(existing_content, game_cfg, answer_html)
    updated_content = core.auto_link_html(updated_content, cfg)
    updated_post = core.patch_wp_post(cfg, post_id, {"content": updated_content})
    core.update_log_row(
        ws,
        row_idx,
        {
            "source_modified": source_modified,
            "answer": media["source_url"],
            "check_answer": current_hash,
            "status": "updated_with_new_image",
            "updated_at": timestamp,
        },
    )
    print(
        f"{game_key}: post {updated_post.get('id')} updated with media {media.get('id')}; "
        f"cropped_top={cropped['crop_top_px']}px."
    )


def process_image_quiz(cfg, ws, defaults, game_cfg):
    if not game_cfg.get("enabled", True):
        print(f"Skip disabled image quiz: {game_cfg['game_key']}")
        return

    if not core.should_run_game_now(cfg, game_cfg):
        print(f"Skip image quiz by run_times: {game_cfg['game_key']}")
        return

    run_mode = os.getenv("RUN_MODE", "update").lower()
    if run_mode == "create":
        create_image_quiz_post(cfg, ws, defaults, game_cfg)
    elif run_mode == "update":
        update_image_quiz_post(cfg, ws, defaults, game_cfg)
    else:
        raise RuntimeError(f"Invalid RUN_MODE: {run_mode}")


def main():
    cfg = core.load_config()
    defaults, games = get_image_quiz_settings(cfg)
    run_mode = os.getenv("RUN_MODE", "update").lower()
    print("=" * 60)
    print(f"IMG QUIZ RUN_MODE: {run_mode}")
    print(f"LOCAL TIME: {core.now_local(cfg['timezone'])}")
    print("=" * 60)

    if not games:
        print("No img_quiz.games configured.")
        return

    ws = core.get_sheet(cfg)
    for game_cfg in games:
        try:
            process_image_quiz(cfg, ws, defaults, game_cfg)
            time.sleep(2)
        except Exception as exc:
            print(f"ERROR image_game={game_cfg.get('game_key')}: {exc}")


if __name__ == "__main__":
    main()
