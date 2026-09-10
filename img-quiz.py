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


def extract_source_image_url(
    source_html,
    source_api_url,
    selector,
):
    soup = BeautifulSoup(
        source_html or "",
        "html.parser",
    )

    image = soup.select_one(selector)

    if not image:
        raise RuntimeError(
            f"Image not found with selector: {selector}"
        )

    image_url = (
        image.get("src")
        or image.get("data-src")
        or image.get("data-lazy-src")
    )

    if not image_url:
        raise RuntimeError(
            f"Image found but URL is missing: {selector}"
        )

    return urljoin(
        source_api_url,
        html.unescape(image_url.strip()),
    )


def download_and_crop_image(
    image_url,
    crop_top_ratio,
):
    try:
        ratio = float(crop_top_ratio)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid crop_top_ratio: {crop_top_ratio}"
        ) from exc

    if not 0 <= ratio < 0.5:
        raise RuntimeError(
            "crop_top_ratio must be between 0 and 0.5"
        )

    response = requests.get(
        image_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://miningcombo.com/",
        },
        timeout=60,
    )

    response.raise_for_status()

    original = Image.open(
        BytesIO(response.content)
    )

    original.load()

    width, height = original.size
    crop_top_px = round(height * ratio)

    cropped = original.crop(
        (
            0,
            crop_top_px,
            width,
            height,
        )
    )

    if cropped.mode not in {
        "RGB",
        "RGBA",
    }:
        cropped = cropped.convert(
            "RGBA"
            if "transparency" in cropped.info
            else "RGB"
        )

    output = BytesIO()

    cropped.save(
        output,
        format="PNG",
        optimize=True,
    )

    image_bytes = output.getvalue()

    return {
        "bytes": image_bytes,
        "sha256": hashlib.sha256(
            image_bytes
        ).hexdigest(),
        "width": cropped.width,
        "height": cropped.height,
        "crop_top_px": crop_top_px,
    }


def upload_wp_media(
    cfg,
    image_bytes,
    filename,
    title,
    alt_text,
):
    url = (
        f"{cfg['wp']['site_url'].rstrip('/')}"
        "/wp-json/wp/v2/media"
    )

    response = requests.post(
        url,
        headers={
            **core.wp_headers(cfg),
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            ),
            "Content-Type": "image/png",
        },
        data=image_bytes,
        timeout=120,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Media upload failed "
            f"{response.status_code}: "
            f"{response.text[:2000]}"
        )

    media = response.json()
    media_id = media.get("id")

    if (
        not media_id
        or not media.get("source_url")
    ):
        raise RuntimeError(
            f"Invalid WordPress media response: {media}"
        )

    metadata_response = requests.post(
        f"{url}/{media_id}",
        headers={
            **core.wp_headers(cfg),
            "Content-Type": "application/json",
        },
        json={
            "title": title,
            "alt_text": alt_text,
        },
        timeout=60,
    )

    if metadata_response.status_code >= 400:
        print(
            "WARNING media metadata update failed "
            f"{metadata_response.status_code}: "
            f"{metadata_response.text[:500]}"
        )

    return media


def normalize_latest_news_html(raw_text):
    soup = BeautifulSoup(
        raw_text or "",
        "html.parser",
    )

    paragraphs = soup.find_all("p")

    if not paragraphs:
        blocks = [
            block.strip()
            for block in (
                raw_text or ""
            ).split("\n\n")
            if block.strip()
        ]

        paragraphs = [
            BeautifulSoup(
                f"<p>{html.escape(block)}</p>",
                "html.parser",
            ).p
            for block in blocks
        ]

    if not 2 <= len(paragraphs) <= 4:
        raise RuntimeError(
            "Latest news must contain 2-4 paragraphs; "
            f"received {len(paragraphs)}"
        )

    return "\n".join(
        str(paragraph)
        for paragraph in paragraphs
    )


def generate_latest_news(
    cfg,
    defaults,
    game_cfg,
):
    model = game_cfg.get(
        "latest_news_model",
        defaults.get(
            "latest_news_model",
            DEFAULT_LATEST_NEWS_MODEL,
        ),
    )

    game_name = (
        game_cfg.get("game_name")
        or game_cfg["game_key"].replace(
            "_",
            " ",
        )
    )

    run_date = core.now_local(
        cfg["timezone"]
    ).date().isoformat()

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

    client = OpenAI(
        api_key=core.get_env(
            "OPENAI_API_KEY"
        )
    )

    last_error = None

    for attempt in range(1, 4):
        try:
            response = client.responses.create(
                model=model,
                tools=[
                    {
                        "type": "web_search",
                    }
                ],
                input=prompt,
            )

            return normalize_latest_news_html(
                response.output_text.strip()
            )

        except Exception as exc:
            last_error = exc

            print(
                f"Latest news attempt "
                f"{attempt}/3 failed: {exc}"
            )

            if attempt < 3:
                time.sleep(
                    2 * attempt
                )

    raise RuntimeError(
        f"Could not generate latest news: {last_error}"
    )


def build_waiting_answer_area():
    return (
        "<p><strong>"
        "Updating soon."
        "</strong></p>"
    )


def build_image_answer_area(
    media_url,
    alt_text,
    width,
    height,
):
    return (
        '<figure class="wp-block-image size-full">'
        f'<img src="{html.escape(media_url, quote=True)}" '
        f'alt="{html.escape(alt_text, quote=True)}" '
        f'width="{int(width)}" '
        f'height="{int(height)}" />'
        "</figure>"
    )


def build_template_content(
    cfg,
    defaults,
    game_cfg,
    identity,
    latest_news_html,
):
    with open(
        game_cfg["template_file"],
        "r",
        encoding="utf-8",
    ) as file:
        content = file.read()

    content = core.replace_date_vars(
        content,
        identity["date_str"],
        identity["readable_date"],
        identity["slug_date"],
    )

    if content.count(
        "{{LATEST_NEWS}}"
    ) != 1:
        raise RuntimeError(
            f"Template for "
            f"{game_cfg['game_key']} "
            "must contain exactly one "
            "{{LATEST_NEWS}}."
        )

    content = content.replace(
        "{{LATEST_NEWS}}",
        latest_news_html,
        1,
    )

    content = (
        core.replace_template_answer_area(
            content,
            game_cfg,
            build_waiting_answer_area(),
        )
    )

    return core.auto_link_html(
        content,
        cfg,
    )


def ensure_post_tags(
    cfg,
    post_id,
    slug,
):
    """
    Read matching tag rules from config.yaml and ensure that
    the WordPress post contains those tags.

    Existing tags are preserved.
    """

    required_tag_ids = (
        core.get_tag_ids_for_post_url(
            cfg,
            slug,
        )
    )

    if not required_tag_ids:
        print(
            "No configured tags matched "
            f"slug: {slug}"
        )
        return

    post = core.get_wp_post(
        cfg,
        post_id,
    )

    current_tag_ids = []

    for tag_id in post.get(
        "tags",
        [],
    ):
        try:
            tag_id = int(tag_id)
        except (
            TypeError,
            ValueError,
        ):
            continue

        if tag_id not in current_tag_ids:
            current_tag_ids.append(
                tag_id
            )

    merged_tag_ids = (
        current_tag_ids.copy()
    )

    for tag_id in required_tag_ids:
        tag_id = int(tag_id)

        if tag_id not in merged_tag_ids:
            merged_tag_ids.append(
                tag_id
            )

    if (
        merged_tag_ids
        == current_tag_ids
    ):
        print(
            f"Post {post_id} already has "
            f"required tags: "
            f"{required_tag_ids}"
        )
        return

    updated_post = core.patch_wp_post(
        cfg,
        post_id,
        {
            "tags": merged_tag_ids,
        },
    )

    print(
        f"Post {post_id} tags updated: "
        f"{updated_post.get('tags', merged_tag_ids)}"
    )


def create_image_quiz_post(
    cfg,
    ws,
    defaults,
    game_cfg,
):
    game_key = game_cfg["game_key"]

    target_date = core.get_target_date(
        cfg["timezone"]
    )

    identity = core.build_game_identity(
        game_cfg,
        target_date,
    )

    # =====================================================
    # SAFETY CHECK 1:
    # Check Google Sheets before touching WordPress.
    # =====================================================

    row_idx, row = core.find_log_row(
        ws,
        identity["date_str"],
        game_key,
    )

    if row:
        post_id = str(
            row.get("post_id")
            or ""
        ).strip()

        if not post_id:
            raise RuntimeError(
                "Missing post_id in existing "
                f"Sheet row for {game_key}"
            )

        existing_slug = (
            str(
                row.get("slug")
                or ""
            ).strip()
            or identity["slug"]
        )

        # This also repairs missing tags on an existing post.
        ensure_post_tags(
            cfg,
            post_id,
            existing_slug,
        )

        # Important:
        # Do not recreate or reschedule an existing post.
        print(
            f"Create mode: {game_key} "
            f"already exists as post {post_id}; "
            "tags checked, no duplicate "
            "and no reschedule."
        )

        return

    # =====================================================
    # SAFETY CHECK 2:
    # Sheet has no row, so check WordPress by exact slug.
    #
    # This covers:
    # - First run created the post successfully.
    # - Rank Math or Sheet writing failed afterward.
    # - Second create run happens 20 minutes later.
    # =====================================================

    matches = core.find_wp_posts_by_slug(
        cfg,
        identity["slug"],
    )

    if len(matches) > 1:
        raise RuntimeError(
            "Multiple WordPress posts found "
            f"for slug '{identity['slug']}': "
            f"{matches}"
        )

    timestamp = core.now_local(
        cfg["timezone"]
    ).isoformat(
        timespec="seconds"
    )

    previous_hash = (
        core.get_latest_check_answer_for_game(
            ws,
            game_key,
        )
        or ""
    )

    # =====================================================
    # RECOVERY:
    # WordPress already has the post but Sheet does not.
    # Never create another post.
    # =====================================================

    if matches:
        post = core.get_wp_post(
            cfg,
            matches[0]["id"],
        )

        actual_slug = (
            post.get("slug")
            or identity["slug"]
        )

        post_url = (
            f"{cfg['wp']['site_url'].rstrip('/')}"
            f"/{actual_slug.strip('/')}/"
        )

        # Repair tags in case the tag rule was added later
        # or the original create operation was incomplete.
        ensure_post_tags(
            cfg,
            post["id"],
            actual_slug,
        )

        # Repair Rank Math metadata in case the first run
        # created the post but failed before completing SEO.
        core.update_rankmath_meta(
            cfg,
            post["id"],
            identity["seo_title"],
            identity["meta_description"],
        )

        # Recover the existing WordPress post into Sheet.
        # If this write fails, the next create run will find
        # the same WP slug again and retry recovery.
        core.append_log_row(
            ws,
            {
                "target_date": (
                    identity["date_str"]
                ),
                "game_key": game_key,
                "post_id": post["id"],
                "post_url": post_url,
                "slug": actual_slug,
                "answer": "Updating soon",
                "check_answer": previous_hash,
                "status": (
                    "recovered_existing_"
                    f"{post.get('status', '')}"
                ),
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )

        print(
            "Recovered existing WordPress "
            f"post {post['id']} into Sheet; "
            "no duplicate created and "
            "publish date was not changed."
        )

        return

    # =====================================================
    # CREATE:
    # No Sheet row and no matching WordPress post.
    # Only now is a new post allowed to be created.
    # =====================================================

    latest_news_html = (
        generate_latest_news(
            cfg,
            defaults,
            game_cfg,
        )
    )

    content = build_template_content(
        cfg,
        defaults,
        game_cfg,
        identity,
        latest_news_html,
    )

    # core.create_wp_post() already:
    # - applies category IDs;
    # - matches tag_rules by slug;
    # - applies featured media;
    # - calculates scheduled publish time;
    # - creates the post.
    post = core.create_wp_post(
        cfg,
        game_cfg,
        identity["title"],
        identity["slug"],
        content,
        target_date,
    )

    # Verify/repair tags without deleting existing tags.
    ensure_post_tags(
        cfg,
        post["id"],
        identity["slug"],
    )

    core.update_rankmath_meta(
        cfg,
        post["id"],
        identity["seo_title"],
        identity["meta_description"],
    )

    post_url = (
        f"{cfg['wp']['site_url'].rstrip('/')}"
        f"/{identity['slug'].strip('/')}/"
    )

    # This is deliberately the final step.
    #
    # If the post was created but this Sheet write fails,
    # the next create run will find the post by exact slug
    # and recover it instead of creating a duplicate.
    core.append_log_row(
        ws,
        {
            "target_date": (
                identity["date_str"]
            ),
            "game_key": game_key,
            "post_id": post["id"],
            "post_url": post_url,
            "slug": identity["slug"],
            "answer": "Updating soon",
            "check_answer": previous_hash,
            "status": (
                "created_"
                f"{post.get('status', 'unknown')}"
            ),
            "created_at": timestamp,
            "updated_at": timestamp,
        },
    )

    print(
        f"Created {game_key} "
        f"post {post['id']} "
        f"with status {post.get('status')}."
    )


def update_image_quiz_post(
    cfg,
    ws,
    defaults,
    game_cfg,
):
    game_key = game_cfg["game_key"]

    target_date = core.get_target_date(
        cfg["timezone"]
    )

    date_str = target_date.isoformat()

    timestamp = core.now_local(
        cfg["timezone"]
    ).isoformat(
        timespec="seconds"
    )

    row_idx, row = core.find_log_row(
        ws,
        date_str,
        game_key,
    )

    if not row:
        print(
            "Update mode: no Sheet row "
            f"for {game_key} {date_str}. "
            "Skip."
        )
        return

    post_id = str(
        row.get("post_id")
        or ""
    ).strip()

    if not post_id:
        raise RuntimeError(
            "Missing post_id in Sheet "
            f"for {game_key} {date_str}"
        )

    source = core.fetch_source_page(
        game_cfg["source_api_url"]
    )

    source_modified = (
        source.get("modified")
        or source.get("date")
        or ""
    )

    if not core.source_modified_matches_target(
        source_modified,
        cfg["timezone"],
        target_date,
    ):
        core.update_log_row(
            ws,
            row_idx,
            {
                "source_modified": (
                    source_modified
                ),
                "status": (
                    "checked_source_not_target_date"
                ),
                "updated_at": timestamp,
            },
        )

        print(
            f"{game_key}: source is not "
            f"for target {date_str}. Skip."
        )

        return

    selector = game_cfg.get(
        "image_selector",
        defaults.get(
            "image_selector",
            DEFAULT_IMAGE_SELECTOR,
        ),
    )

    crop_ratio = game_cfg.get(
        "crop_top_ratio",
        defaults.get(
            "crop_top_ratio",
            DEFAULT_CROP_TOP_RATIO,
        ),
    )

    source_html = (
        source.get(
            "content",
            {},
        ).get(
            "rendered",
            "",
        )
    )

    image_url = (
        extract_source_image_url(
            source_html,
            game_cfg["source_api_url"],
            selector,
        )
    )

    cropped = download_and_crop_image(
        image_url,
        crop_ratio,
    )

    current_hash = cropped["sha256"]

    previous_hash = str(
        row.get("check_answer")
        or ""
    ).strip()

    row_answer = str(
        row.get("answer")
        or ""
    ).strip().lower()

    # Image is already updated in this post.
    if (
        current_hash == previous_hash
        and row_answer
        not in {
            "",
            "updating soon",
        }
    ):
        core.update_log_row(
            ws,
            row_idx,
            {
                "source_modified": (
                    source_modified
                ),
                "status": (
                    "checked_no_new_image"
                ),
                "updated_at": timestamp,
            },
        )

        print(
            f"{game_key}: image unchanged."
        )

        return

    # The post is still waiting, and MiningCombo still
    # contains the previous day's image.
    if current_hash == previous_hash:
        core.update_log_row(
            ws,
            row_idx,
            {
                "source_modified": (
                    source_modified
                ),
                "status": (
                    "checked_waiting_for_new_image"
                ),
                "updated_at": timestamp,
            },
        )

        print(
            f"{game_key}: source still contains "
            "the previous image. Skip."
        )

        return

    readable_date = (
        core.format_date_readable(
            target_date
        )
    )

    slug_date = (
        core.format_date_slug(
            target_date
        )
    )

    default_alt_text = (
        f"{game_cfg.get('game_name', game_key)} "
        "answer"
    )

    alt_text = core.replace_date_vars(
        game_cfg.get(
            "answer_image_alt",
            default_alt_text,
        ),
        date_str,
        readable_date,
        slug_date,
    )

    filename = (
        f"{core.normalize_slug(game_key)}"
        f"-{date_str}.png"
    )

    media = upload_wp_media(
        cfg,
        cropped["bytes"],
        filename,
        alt_text,
        alt_text,
    )

    answer_html = (
        build_image_answer_area(
            media["source_url"],
            alt_text,
            cropped["width"],
            cropped["height"],
        )
    )

    post = core.get_wp_post(
        cfg,
        post_id,
    )

    existing_content = (
        post.get(
            "content",
            {},
        ).get(
            "raw",
        )
        or post.get(
            "content",
            {},
        ).get(
            "rendered",
            "",
        )
    )

    if not existing_content:
        raise RuntimeError(
            f"Empty WordPress content "
            f"for post {post_id}"
        )

    updated_content = (
        core.replace_answer_area(
            existing_content,
            game_cfg,
            answer_html,
        )
    )

    updated_content = (
        core.auto_link_html(
            updated_content,
            cfg,
        )
    )

    updated_post = core.patch_wp_post(
        cfg,
        post_id,
        {
            "content": updated_content,
        },
    )

    core.update_log_row(
        ws,
        row_idx,
        {
            "source_modified": (
                source_modified
            ),
            "answer": (
                media["source_url"]
            ),
            "check_answer": (
                current_hash
            ),
            "status": (
                "updated_with_new_image"
            ),
            "updated_at": timestamp,
        },
    )

    print(
        f"{game_key}: post "
        f"{updated_post.get('id')} "
        "updated with media "
        f"{media.get('id')}; "
        "cropped_top="
        f"{cropped['crop_top_px']}px."
    )


def process_image_quiz(
    cfg,
    ws,
    defaults,
    game_cfg,
):
    if not game_cfg.get(
        "enabled",
        True,
    ):
        print(
            "Skip disabled image quiz: "
            f"{game_cfg['game_key']}"
        )
        return

    if not core.should_run_game_now(
        cfg,
        game_cfg,
    ):
        print(
            "Skip image quiz by run_times: "
            f"{game_cfg['game_key']}"
        )
        return

    run_mode = os.getenv(
        "RUN_MODE",
        "update",
    ).lower()

    if run_mode == "create":
        create_image_quiz_post(
            cfg,
            ws,
            defaults,
            game_cfg,
        )
        return

    if run_mode == "update":
        update_image_quiz_post(
            cfg,
            ws,
            defaults,
            game_cfg,
        )
        return

    raise RuntimeError(
        f"Invalid RUN_MODE: {run_mode}"
    )


def main():
    cfg = core.load_config()

    defaults, games = (
        get_image_quiz_settings(
            cfg
        )
    )

    run_mode = os.getenv(
        "RUN_MODE",
        "update",
    ).lower()

    print("=" * 60)
    print(
        f"IMG QUIZ RUN_MODE: {run_mode}"
    )
    print(
        "LOCAL TIME: "
        f"{core.now_local(cfg['timezone'])}"
    )
    print("=" * 60)

    if not games:
        print(
            "No img_quiz.games configured."
        )
        return

    ws = core.get_sheet(cfg)

    for game_cfg in games:
        try:
            process_image_quiz(
                cfg,
                ws,
                defaults,
                game_cfg,
            )

            time.sleep(2)

        except Exception as exc:
            print(
                "ERROR image_game="
                f"{game_cfg.get('game_key')}: "
                f"{exc}"
            )


if __name__ == "__main__":
    main()
