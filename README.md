# MiningCombo Auto Publisher

Project tự động lấy dữ liệu từ MiningCombo và đăng bài lên WordPress.

## Chức năng

- Tạo bài viết mới mỗi ngày cho từng game.
- Bài viết được schedule publish lúc **02:00 (GMT+7)** của ngày mục tiêu.
- Tự động kiểm tra đáp án mới và update bài viết.
- Rewrite phần Crypto Snapshot bằng OpenAI.
- Ghi log vào Google Sheet để tránh tạo/update trùng.

---

# Cấu trúc project

```
config.yaml
main.py
games/
    spur-protocol.txt
    xenea-wallet.txt
    ...
```

- `config.yaml`: cấu hình chung và cấu hình từng game.
- `games/*.txt`: template HTML của từng game.
- `main.py`: chương trình chính.

---

# Thêm game mới

## Bước 1

Tạo file template:

```
games/ten-game.txt
```

Template phải có tối thiểu:

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

## Bước 2

Thêm game vào `config.yaml`

Ví dụ:

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

# Các field quan trọng

## game_key

Tên duy nhất của game.

Ví dụ:

```
spur_protocol
```

---

## source_api_url

API WordPress của MiningCombo.

Ví dụ:

```
https://miningcombo.com/wp-json/wp/v2/pages/10799
```

---

## template_file

File HTML template.

Ví dụ:

```
games/spur-protocol.txt
```

---

## featured_media_id

Media ID trên WordPress.

Ảnh này sẽ:

- làm Featured Image
- có thể dùng trong nội dung nếu template sử dụng.

---

## check_answer

Chỉ dùng cho lần chạy đầu tiên của game.

Sau khi game đã có log trong Google Sheet, project sẽ sử dụng `check_answer` trong Sheet thay vì giá trị này.

---

## answer_heading_contains

Dùng để xác định H2 chứa phần Question / Correct Answer.

Ví dụ:

```
Spur Protocol Quiz Answers Today
```

Không cần đúng 100%.

Chỉ cần H2 chứa chuỗi này.

---

## question_selector

CSS Selector để lấy Question từ MiningCombo.

Ví dụ:

```
p.has-text-align-left.wp-block-paragraph
```

---

## answer_selector

CSS Selector để lấy Answer.

Ví dụ:

```
p.has-text-align-left.wp-block-paragraph
```

---

## question_prefix

Tiền tố dùng để nhận biết Question.

Ví dụ:

```
Question:
```

---

## answer_prefix

Tiền tố dùng để nhận biết Answer.

Ví dụ:

```
Answer:
```

---

# Google Sheet

Mỗi dòng tương ứng một game của một ngày.

Các cột quan trọng:

- target_date
- game_key
- post_id
- check_answer
- status

Project sẽ đọc Sheet để quyết định:

- đã tạo bài chưa
- có cần update đáp án không

---

# RUN_MODE

Project có 2 mode.

## create

- target_date = ngày mai
- tạo bài Scheduled
- publish lúc 02:00 GMT+7

## update

- target_date = hôm nay
- kiểm tra đáp án
- update bài nếu có thay đổi

---

# GitHub Secrets

Các secret cần có:

```
WP_USERNAME

WP_APP_PASSWORD

OPENAI_API_KEY

GOOGLE_CREDENTIALS_BASE64

GOOGLE_SHEET_ID
```

---

# Lưu ý

- Không sửa trực tiếp các H2 dùng để xác định Question/Answer.
- Khi thay đổi cấu trúc HTML của MiningCombo, chỉ cần sửa selector hoặc prefix trong `config.yaml`, không cần sửa code.
- Nếu game có cấu trúc đặc biệt, ưu tiên override trong config thay vì sửa `main.py`.
