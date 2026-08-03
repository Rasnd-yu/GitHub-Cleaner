import requests
from bs4 import BeautifulSoup
import json
import os

headers = {
    "User-Agent": "Mozilla/5.0"
}

users = []

# Crawl the first 10 pages (10 entries per page)
for page in range(1, 11):
    url = f"https://gitstar-ranking.com/users?page={page}"

    resp = requests.get(url, headers=headers)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    items = soup.find_all("a", class_="list-group-item")

    for item in items:
        name_span = item.find("span", class_="hidden-xs")
        if name_span:
            username = name_span.get_text(strip=True)
            users.append(username)

# Deduplicate (defensively)
users = list(dict.fromkeys(users))[:100]

# Save
os.makedirs("data", exist_ok=True)
output_path = "data/gitstar_ranking_users_top100.json"

with open(output_path, "w", encoding="utf-8") as f:
    json.dump(users, f, indent=2, ensure_ascii=False)

print(f"Done: {len(users)} users")