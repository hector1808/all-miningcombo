# MiningCombo Auto Publisher

A project that automatically retrieves data from MiningCombo and publishes articles to WordPress.

## Features

* Creates a new article every day for each game.
* Schedules articles to be published at **02:00 AM (GMT+7)** on the target date.
* Automatically checks for new answers and updates existing articles.
* Rewrites the Crypto Snapshot section using OpenAI.
* Records activity in Google Sheets to prevent duplicate article creation or updates.

---

# Project Structure

```text
config.yaml
main.py
games/
    spur-protocol.txt
    xenea-wallet.txt
    ...
```

* `config.yaml`: Contains the general settings and configuration for each game.
* `games/*.txt`: Contains the HTML template for each game.
* `main.py`: The main program.

---

# Adding a New Game

## Step 1

Create a template file:

```text
games/game-name.txt
```

The template must contain at least:

```html
<h2><strong>Game Quiz Answers Today - {{CURRENT_DATE_READABLE}}</strong></h2>

<p>
<strong>Question:</strong><br>
<strong>Correct Answer:</strong>
</p>

<h2><strong>Today's Crypto Market Update ({{CURRENT_DATE_READABLE}})</strong></h2>

{{CRYPTO_SNAPSHOT}}
```

---

## Step 2

Add the game to `config.yaml`.

Example:

```yaml
games:
  - game_key: spur_protocol

    enabled: true

    source_api_url: https://miningcombo.com/wp-json/wp/v2/pages/10799

    template_file: games/spur-protocol.txt

    featured_media_id: 12345

    check_answer: ""

    answer_heading_contains: "Spur Protocol Quiz Answers Today"

    question_selector: "p.has-text-align-left.wp-block-paragraph"
    answer_selector: "p.has-text-align-left.wp-block-paragraph"

    question_prefix: "Question:"
    answer_prefix: "Answer:"

    title_format: "Spur Protocol Quiz Answers Today - {{CURRENT_DATE_READABLE}}"

    slug_format: "spur-protocol-quiz-answer-{{CURRENT_DATE}}"

    seo_title_format: "Spur Protocol Quiz Answer {{CURRENT_DATE_READABLE}}"

    meta_description_format: "Today's Spur Protocol Quiz Answer for {{CURRENT_DATE_READABLE}}."
```

---

# Important Fields

## game_key

A unique identifier for the game.

Example:

```text
spur_protocol
```

---

## source_api_url

The MiningCombo WordPress API endpoint.

Example:

```text
https://miningcombo.com/wp-json/wp/v2/pages/10799
```

---

## template_file

The HTML template file.

Example:

```text
games/spur-protocol.txt
```

---

## featured_media_id

The media ID in WordPress.

This image will:

* Be used as the Featured Image.
* Be available for use inside the article content if the template includes it.

---

## check_answer

This value is only used during the first run for a game.

After the game has a corresponding log entry in Google Sheets, the project will use the `check_answer` value stored in the Sheet instead of the value in `config.yaml`.

---

## answer_heading_contains

Used to identify the H2 section containing the Question and Correct Answer.

Example:

```text
Spur Protocol Quiz Answers Today
```

The text does not need to match the H2 exactly.

The H2 only needs to contain this string.

---

## question_selector

The CSS selector used to retrieve the Question from MiningCombo.

Example:

```text
p.has-text-align-left.wp-block-paragraph
```

---

## answer_selector

The CSS selector used to retrieve the Answer.

Example:

```text
p.has-text-align-left.wp-block-paragraph
```

---

## question_prefix

The prefix used to identify the Question.

Example:

```text
Question:
```

---

## answer_prefix

The prefix used to identify the Answer.

Example:

```text
Answer:
```

---

# Google Sheets

Each row represents one game for one target date.

Important columns:

* `target_date`
* `game_key`
* `post_id`
* `check_answer`
* `status`

The project reads the Sheet to determine:

* Whether the article has already been created.
* Whether the answer needs to be updated.

---

# RUN_MODE

The project has two modes.

## create

* `target_date` is tomorrow.
* Creates a scheduled article.
* Publishes the article at 02:00 AM GMT+7.

## update

* `target_date` is today.
* Checks for the latest answer.
* Updates the article if the answer has changed.

---

# GitHub Secrets

The following secrets are required:

```text
WP_USERNAME

WP_APP_PASSWORD

OPENAI_API_KEY

GOOGLE_CREDENTIALS_BASE64

GOOGLE_SHEET_ID
```

---

# Notes

* Do not directly modify the H2 headings used to identify the Question and Answer sections.
* If the MiningCombo HTML structure changes, update the selectors or prefixes in `config.yaml`. There is no need to modify the code.
* If a game uses a special structure, prefer overriding its settings in the configuration instead of modifying `main.py`.
