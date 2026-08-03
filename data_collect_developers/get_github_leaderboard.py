from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import json
import os
import time

# Initialize the driver (the critical fix)
service = Service(ChromeDriverManager().install())
driver = webdriver.Chrome(service=service)

driver.get("https://leaderboard.github.io/users")
time.sleep(5)  # Wait for the JavaScript to load

users = []
elements = driver.find_elements(By.CSS_SELECTOR, "a[href^='https://github.com/']")

for e in elements:
    href = e.get_attribute("href")
    if href:
        username = href.rstrip("/").split("/")[-1]
        users.append(username)

driver.quit()

# Deduplicate
users = list(set(users))

# Save
os.makedirs("data", exist_ok=True)
with open("data/github_leaderboard.json", "w", encoding="utf-8") as f:
    json.dump(users, f, indent=2, ensure_ascii=False)

print(f"Crawl complete: {len(users)} users")