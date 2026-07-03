import feedparser
import requests
import os
from datetime import datetime, timezone, timedelta

DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]
USERNAME = "trickcal_re"
VN_TZ = timezone(timedelta(hours=7))

RSS_SOURCES = [
    f"https://nitter.net/{USERNAME}/rss",
    f"https://rsshub.app/twitter/user/{USERNAME}",
    f"https://nitter.poast.org/{USERNAME}/rss",
    f"https://nitter.rawbit.ninja/{USERNAME}/rss",
    f"https://nitter.privacydev.net/{USERNAME}/rss",
    f"https://twitt.re/{USERNAME}/rss",
]

# Khung giờ an toàn (phút VN) — khớp lịch cron-job.org thực tế (tight+sparse: 9:00–19:40)
WINDOW = (8*60+55, 19*60+45)  # 8:55 - 19:45, có đệm 2 đầu

now_vn = datetime.now(VN_TZ)
total_min = now_vn.hour * 60 + now_vn.minute
in_window = WINDOW[0] <= total_min <= WINDOW[1]

if not in_window:
    print(f"Ngoài khung giờ ({now_vn.strftime('%H:%M')} VN) → thoát")
    exit()

print(f"Scan lúc {now_vn.strftime('%H:%M')} VN")

# Đọc last_id
try:
    with open("last_id.txt") as f:
        last_id = f.read().strip()
except:
    last_id = ""

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

# Tìm bài mới
new_posts = []
for entry in best_entries:
    if entry.id == last_id:
        break
    new_posts.append(entry)

print(f"Bài mới: {len(new_posts)}")

if new_posts:
    for post in reversed(new_posts):
        link = post.link
        for d in ["nitter.net", "nitter.poast.org", "nitter.rawbit.ninja", "nitter.privacydev.net"]:
            link = link.replace(d, "x.com")
        r = requests.post(DISCORD_WEBHOOK, json={
            "content": link,
            "username": "Heidi",
        })
        print(f"Discord: {r.status_code} → {link}")

    with open("last_id.txt", "w") as f:
        f.write(best_entries[0].id)
    print("✅ Đã lưu last_id")
else:
    print("Không có bài mới")
bot (2).py
