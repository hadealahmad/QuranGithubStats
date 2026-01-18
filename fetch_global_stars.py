#!/usr/bin/env python3
import json
import time
import os
import requests
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configuration
CONTRIBUTORS_FILE = "contributors.json"
GLOBAL_DATA_FILE = "contributors_global_stats.json"
GITHUB_TOKEN = "" # Can be set here or retrieved from scraper.py
MAX_WORKERS = 10 # Number of parallel threads

# Thread safety
stats_lock = threading.Lock()
rate_limit_event = threading.Event()
rate_limit_event.set() # Set initially (not rate limited)

def get_token_from_scraper():
    if os.path.exists("scraper.py"):
        with open("scraper.py", "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("GITHUB_TOKEN ="):
                    # Extract string between quotes
                    import re
                    match = re.search(r'["\']([^"\']+)["\']', line)
                    if match:
                        return match.group(1)
    return os.environ.get("GITHUB_TOKEN", "")

# Try to get token
token = GITHUB_TOKEN or get_token_from_scraper()
headers = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if token:
    headers["Authorization"] = f"Bearer {token}"
    print("🔑 Using GitHub token for authentication.")
else:
    print("⚠️ No GitHub token found. Rate limits will be very restrictive.")

def get_user_total_stars(login):
    """Fetch total star count for a user across all their public repos."""
    total_stars = 0
    page = 1
    while True:
        # Wait if globally rate limited
        rate_limit_event.wait()
        
        url = f"https://api.github.com/users/{login}/repos"
        params = {"per_page": 100, "page": page}
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
        except Exception as e:
            print(f"\n❌ Request error for {login}: {e}")
            return None
        
        if response.status_code == 200:
            repos = response.json()
            if not repos:
                break
            total_stars += sum(repo.get("stargazers_count", 0) for repo in repos)
            if len(repos) < 100:
                break
            page += 1
            time.sleep(0.05) # Tiny delay to be polite
        elif response.status_code == 403:
            reset_time = int(response.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait_time = max(0, reset_time - int(time.time())) + 5
            
            # Check if we should be the one to set the global rate limit
            if rate_limit_event.is_set():
                rate_limit_event.clear()
                print(f"\n⏳ Rate limit hit! Global pause for {wait_time}s...")
                time.sleep(wait_time)
                rate_limit_event.set()
                print("\n🚀 Resuming fetching...")
            else:
                # Just wait for the event to be set by the other thread
                rate_limit_event.wait()
            
            continue # Retry
        elif response.status_code == 404:
            print(f"\n🚫 User {login} not found (404).")
            return 0
        else:
            print(f"\n❌ Error {response.status_code} for {login}")
            return None
            
    return total_stars

def main():
    # 1. Load existing contributors
    if not os.path.exists(CONTRIBUTORS_FILE):
        print(f"❌ {CONTRIBUTORS_FILE} not found!")
        return

    with open(CONTRIBUTORS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 2. Extract unique logins
    unique_logins = set()
    for repo_contribs in data.values():
        for c in repo_contribs:
            unique_logins.add(c["login"])
    
    print(f"👥 Found {len(unique_logins)} unique contributors.")

    # 3. Load or initialize global stats
    global_stats = {}
    if os.path.exists(GLOBAL_DATA_FILE):
        try:
            with open(GLOBAL_DATA_FILE, "r", encoding="utf-8") as f:
                global_stats = json.load(f)
            print(f"📁 Loaded {len(global_stats)} existing global stats.")
        except Exception:
            print("⚠️ Could not load existing global stats. Starting fresh.")

    # 4. Filter missing stats
    to_fetch = [l for l in unique_logins if l not in global_stats]
    total_to_fetch = len(to_fetch)
    print(f"📥 {total_to_fetch} users to fetch.")

    if total_to_fetch == 0:
        print("✅ All users already have global stats.")
        return

    # Use ThreadPoolExecutor for parallel fetching
    count = 0
    print(f"⚡ Starting parallel fetch with {MAX_WORKERS} workers...")
    
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_login = {executor.submit(get_user_total_stars, login): login for login in to_fetch}
            
            for future in as_completed(future_to_login):
                login = future_to_login[future]
                try:
                    stars = future.result()
                    if stars is not None:
                        with stats_lock:
                            global_stats[login] = {"global_stars": stars}
                            count += 1
                            
                            if count % 10 == 0:
                                print(f"\r📊 Progress: {count}/{total_to_fetch} users fetched...", end="", flush=True)
                            
                            # Save every 50 users
                            if count % 50 == 0:
                                with open(GLOBAL_DATA_FILE, "w", encoding="utf-8") as f:
                                    json.dump(global_stats, f, indent=2)
                except Exception as e:
                    print(f"\n⚠️ Unexpected error for {login}: {e}")
            
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user. Saving progress...")
    
    # 5. Final save
    with open(GLOBAL_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(global_stats, f, indent=2)
    
    print(f"\n✨ Finished! Total users with global stats: {len(global_stats)}")

if __name__ == "__main__":
    main()
