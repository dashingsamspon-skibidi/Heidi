import feedparser
import requests
import random
import time
import os
import re
import html
import socket
from datetime import datetime, timezone, timedelta

socket.setdefaulttimeout(10)  # chặn treo khi fetch RSS/Discord quá 10s
DISCORD_WEBHOOKS = [w.strip() for w in os.environ["DISCORD_WEBHOOK"].split(",") if w.strip()]
USERNAME = "trickcal_en"
VN_TZ = timezone(timedelta(hours=7))

# ── Bỏ qua bài có từ khóa (KHÔNG gửi lên Discord, nhưng vẫn ghi nhớ là đã xử lý) ──
SKIP_WORDS = ["[안내]"
    # "BOGO",             # bài có chữ này (không phân biệt hoa/thường) sẽ bị bỏ qua
    # "re:\\bart\\b",     # tiền tố "re:" = regex (không phân biệt hoa/thường)
]


def entry_text(entry):
    """Gom chữ của 1 bài trong feed (tiêu đề + nội dung), bỏ thẻ HTML — để thẻ <a href=...> không khớp nhầm vào URL."""
    parts = [getattr(entry, "title", ""), getattr(entry, "summary", ""), getattr(entry, "description", "")]
    for c in getattr(entry, "content", None) or []:
        parts.append(c.get("value", "") if isinstance(c, dict) else str(c))
    text = re.sub(r"<[^>]+>", " ", " ".join(p for p in parts if p))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def skip_reason(entry):
    text = entry_text(entry)
    for w in SKIP_WORDS:
        if w.startswith("re:"):
            if re.search(w[3:], text, re.I):
                return w
        elif w.casefold() in text.casefold():
            return w
    return None

RSS_SOURCES = [
    f"https://fixupx.com/{USERNAME}/feed.xml",
]

KEEP_LAST_N = 3  # trickcal_en thỉnh thoảng xoá/sửa 1 tweet trước khi đăng bài mới -> giữ dư vài id làm mốc dự phòng

# Khung giờ an toàn (phút VN) — khớp lịch cron-job.org thực tế (tight+sparse: 9:00–19:40)
WINDOW = (8*60+55, 19*60+45)  # 8:55 - 19:45, có đệm 2 đầu — không đổi, không liên quan tới việc đổi RSS source

now_vn = datetime.now(VN_TZ)
total_min = now_vn.hour * 60 + now_vn.minute
in_window = WINDOW[0] <= total_min <= WINDOW[1]

if not in_window:
    print(f"Ngoài khung giờ ({now_vn.strftime('%H:%M')} VN) → thoát")
    exit()

print(f"Scan lúc {now_vn.strftime('%H:%M')} VN")

# Jitter — chỉ chờ SAU KHI đã xác nhận trong khung giờ, tránh gọi API đồng loạt đúng giây 0
jitter = random.uniform(5, 20)
print(f"Chờ {jitter:.1f}s trước khi fetch")
time.sleep(jitter)

# Đọc các id gần nhất — vẫn 1 file last_id.txt như cũ, chỉ khác là có thể có tới KEEP_LAST_N dòng thay vì 1
try:
    with open("last_id.txt") as f:
        last_ids = [line.strip() for line in f if line.strip()]
except FileNotFoundError:
    last_ids = []

# Lấy RSS — chọn nguồn tươi nhất
def entry_time(entry):
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)

best_entries = []
best_time = datetime.min.replace(tzinfo=timezone.utc)
for url in RSS_SOURCES:
    print(f"Thử: {url}")
    try:
        feed = feedparser.parse(url)
        if not feed.entries:
            print("❌ 0 bài")
            continue
        t = entry_time(feed.entries[0])
        print(f"✅ {len(feed.entries)} bài | mới nhất: {t.strftime('%m-%d %H:%M UTC')}")
        if t > best_time:
            best_time = t
            best_entries = feed.entries
    except Exception as e:
        print(f"❌ {e}")

if not best_entries:
    print("Tất cả nguồn thất bại")
    exit()

lag = (datetime.now(timezone.utc) - best_time).total_seconds()
print(f"DEBUG: bài mới nhất cũ hơn hiện tại {lag:.0f} giây")  # theo dõi vài lần để xem cache CDN có làm stale không — như đã bàn ở tin trước

print(f"DEBUG raw link: {best_entries[0].link} | raw id: {best_entries[0].id}")  # xoá sau khi đã xác nhận format ổn định

# Tìm bài mới: dừng khi gặp BẤT KỲ id nào trong các id gần nhất đã lưu (không chỉ đúng 1 id cuối như trước)
new_posts = []
for entry in best_entries:
    if entry.id in last_ids:
        break
    new_posts.append(entry)
else:
    # for...else: chỉ chạy khi vòng lặp đi hết feed mà KHÔNG break, tức không khớp id nào cả
    if last_ids:
        print(f"⚠️ Không khớp id nào trong {len(last_ids)} id đã lưu — coi toàn bộ feed là bài mới")
print(f"Bài mới: {len(new_posts)}")

if new_posts:
    for post in reversed(new_posts):
        reason = skip_reason(post)
        if reason:
            print(f"⏭️ Bỏ qua (khớp {reason!r}): {post.link}")
            continue
        if SKIP_WORDS:
            print(f"   (chữ dùng để lọc: {entry_text(post)[:80]!r})")
        link = post.link
        if "vxtwitter.com" not in link:
            for d in ["twitter.com", "x.com"]:
                link = link.replace(f"://{d}", "://vxtwitter.com")
        for i, webhook in enumerate(DISCORD_WEBHOOKS, 1):
            r = requests.post(webhook, json={
                "content": link,
                "username": "Heidi",
            }, timeout=10)
            print(f"Discord #{i}: {r.status_code} → {link}")

    updated_ids = ([e.id for e in new_posts] + last_ids)[:KEEP_LAST_N]
    with open("last_id.txt", "w") as f:
        f.write("\n".join(updated_ids))
    print(f"✅ Đã lưu {len(updated_ids)} id gần nhất")
else:
    print("Không có bài mới")
