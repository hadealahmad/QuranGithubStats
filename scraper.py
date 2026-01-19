#!/usr/bin/env python3
"""
GitHub Quran Repository Scraper

Searches GitHub for repositories containing Quran-related keywords in their
README files and descriptions. Creates a comprehensive list of repositories
with their metadata and downloads README files.
"""

import os
import json
import time
import requests
from requests.exceptions import RequestException, ConnectionError, Timeout
from datetime import datetime
from typing import Optional
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import re
import argparse

# GitHub API configuration
GITHUB_API_BASE = "https://api.github.com"
GITHUB_TOKEN = ""  # Your GitHub token

# Search keywords - English
ENGLISH_KEYWORDS = [
    "quran",
    "mushaf",
    "moshaf",
    "quran-app",
    "quran-api",
    "quran-data",
    "quran-text",
    "quran-dataset",
    "tajwid",
    "tajweed-coloring",
    "tajweed-rules",
    "learn-quran",
    "quran-learning",
    "quran-nlp",
    "tilawa",
    "tarteel",
    "qiraat",
    "quran-audio",
    "ayah-timing",
    # Additional keywords
    "hifz",
    "hifaz",
    "quran-translation",
    "quran-tafsir",
    "tafseer",
    "surah",
    "ayat",
    "ayah",
    "qari",
    "quran-memorization",
    "quran-offline",
    "quran-reader",
]

# Search keywords - Arabic
ARABIC_KEYWORDS = [
    "القرآن",
    "قرآن",
    "التلاوة",
    "تلاوة",
    "التجويد",
    "تجويد",
    "الترتيل",
    "ترتيل",
    # Additional Arabic keywords
    "حفظ",
    "أحكام التجويد",
    "ترجمة القرآن",
    "تفسير",
    "سورة",
    "آية",
    "قارئ",
    "حفظ القرآن",
]

ALL_KEYWORDS = ENGLISH_KEYWORDS + ARABIC_KEYWORDS

# =============================================================================
# EXCLUSION CONFIGURATION
# =============================================================================
# Terms that will EXCLUDE a repository from results if found in:
# - Repository name
# - Repository description
# - Repository topics
# Use lowercase for case-insensitive matching

EXCLUDE_TERMS = [
    # Original exclusions
    "course",
    "list of",
    "template",
    "collection of",
    "awesome",
    "interview",
    # Lists/Collections
    "awesome-",
    "curated-list",
    "curated list",
    "resources",
    "links",
    "cheatsheet",
    "learning-path",
    "roadmap",
    # UI Components
    "drawer",
    "navbar",
    "sidebar",
    "component",
    "widget",
    "ui-kit",
    "ui-library",
    "react-component",
    "vue-component",
    # Templates
    "boilerplate",
    "starter",
    "scaffold",
    "skeleton",
    "bootstrap",
    # Courses/Learning materials
    "tutorial",
    "bootcamp",
    "workshop",
    "learning-",
    "study-notes",
    "notes",
    # Generic Tools
    "dotfiles",
    "config",
    "setup",
    "installer",
    "cli-tool",
    # Non-Project Types
    "fork-of",
    "backup",
    "mirror",
    "deprecated",
    "appz",
    "research papers",
    "daftar",
    "teaching",
    "send messages",
    "raycast extensions",
    "paper list",
    "playlists",
    "the todo list",
    "obsidian plugins",
    "public-apis",
    "awsome",
    "mcp-",
    "apis_public",
    "top-",
    "Sacred-Texts",
    "prompts",
    "999-eBooks",
    "tenet",
    "TrollStore",
    "religious",
    "Userscripts",
]

# Exclude repositories containing these terms in their FULL NAME (owner/repo)
# Useful for excluding specific users or organizations
EXCLUDE_OWNERS = [
    # "example-user",
    # "spam-org",
]

# Exclude repositories with these exact names
EXCLUDE_REPOS = [
    # "owner/repo-name",
]

# Minimum star count to include (set to 0 to include all)
MIN_STARS = 0

# =============================================================================
# BATCH DOWNLOAD CONFIGURATION
# =============================================================================
# Number of concurrent workers for README downloads
README_DOWNLOAD_WORKERS = 10

# Delay between batches of README downloads (seconds)
README_BATCH_DELAY = 1.0

# Output directories and files
OUTPUT_DIR = Path(".")
README_DIR = OUTPUT_DIR / "readmes"
RESULTS_FILE = OUTPUT_DIR / "repositories.json"
RESULTS_CSV = OUTPUT_DIR / "repositories.csv"
CONTRIBUTORS_FILE = OUTPUT_DIR / "contributors.json"
CHECKPOINT_FILE = Path("output/checkpoint.json")
SEARCH_CACHE_FILE = Path("output/search_cache.json")


def get_headers() -> dict:
    """Get request headers with optional authentication."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def rate_limit_handler(response: requests.Response) -> None:
    """Handle GitHub API rate limiting."""
    if response.status_code == 403:
        reset_time = response.headers.get("X-RateLimit-Reset")
        if reset_time:
            wait_time = int(reset_time) - int(time.time()) + 1
            if wait_time > 0:
                print(f"⏳ Rate limited. Waiting {wait_time} seconds...")
                time.sleep(wait_time)
    elif response.status_code == 429:
        retry_after = response.headers.get("Retry-After", 60)
        print(f"⏳ Too many requests. Waiting {retry_after} seconds...")
        time.sleep(int(retry_after))


def retry_request(func, *args, max_retries: int = 3, **kwargs):
    """Wrapper to retry a function with exponential backoff."""
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except (ConnectionError, Timeout, RequestException) as e:
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) * 2  # 2, 4, 8 seconds
                print(f"      ⚠️ Connection error, retrying in {wait_time}s... ({attempt + 1}/{max_retries})")
                time.sleep(wait_time)
            else:
                print(f"      ❌ Failed after {max_retries} retries: {e}")
                raise
    return None


def should_exclude_repo(repo: dict) -> tuple[bool, str]:
    """
    Check if a repository should be excluded based on exclusion rules.
    
    Returns:
        tuple: (should_exclude: bool, reason: str)
    """
    full_name = repo.get("full_name", "").lower()
    name = repo.get("name", "").lower()
    description = (repo.get("description") or "").lower()
    topics = [t.lower() for t in repo.get("topics", [])]
    stars = repo.get("stargazers_count", 0)
    
    # Check minimum stars
    if stars < MIN_STARS:
        return True, f"stars ({stars}) below minimum ({MIN_STARS})"
    
    # Check exact repo exclusions
    for excluded_repo in EXCLUDE_REPOS:
        if full_name == excluded_repo.lower():
            return True, f"repo in EXCLUDE_REPOS: {excluded_repo}"
    
    # Check owner exclusions
    for excluded_owner in EXCLUDE_OWNERS:
        if full_name.startswith(excluded_owner.lower() + "/"):
            return True, f"owner in EXCLUDE_OWNERS: {excluded_owner}"
    
    # Check term exclusions in name, description, and topics
    for term in EXCLUDE_TERMS:
        term_lower = term.lower()
        
        # Check in name
        if term_lower in name:
            return True, f"term '{term}' found in name"
        
        # Check in description
        if term_lower in description:
            return True, f"term '{term}' found in description"
        
        # Check in topics
        for topic in topics:
            if term_lower in topic:
                return True, f"term '{term}' found in topic '{topic}'"
    
    return False, ""


def filter_repositories(repos: list[dict], verbose: bool = True) -> list[dict]:
    """
    Filter repositories based on exclusion rules.
    
    Args:
        repos: List of repository dictionaries
        verbose: Whether to print exclusion reasons
    
    Returns:
        Filtered list of repositories
    """
    filtered = []
    excluded_count = 0
    exclusion_reasons = defaultdict(int)
    
    for repo in repos:
        should_exclude, reason = should_exclude_repo(repo)
        if should_exclude:
            excluded_count += 1
            # Categorize the reason
            if "EXCLUDE_REPOS" in reason:
                exclusion_reasons["Specific repos"] += 1
            elif "EXCLUDE_OWNERS" in reason:
                exclusion_reasons["Excluded owners"] += 1
            elif "stars" in reason:
                exclusion_reasons["Below min stars"] += 1
            else:
                # Extract the term
                match = re.search(r"term '([^']+)'", reason)
                term = match.group(1) if match else "unknown"
                exclusion_reasons[f"Term: {term}"] += 1
        else:
            filtered.append(repo)
    
    if verbose and excluded_count > 0:
        print(f"\n🚫 Excluded {excluded_count} repositories:")
        for reason, count in sorted(exclusion_reasons.items(), key=lambda x: -x[1]):
            print(f"   - {reason}: {count}")
    
    return filtered


def search_repositories_with_star_range(keyword: str, star_range: str, date_filters: dict = None) -> list[dict]:
    """Search GitHub repositories for a specific keyword within a star range.
    
    Args:
        keyword: Search keyword
        star_range: Star count range (e.g., "0..5", ">2000")
        date_filters: Optional dict with keys 'created_since', 'created_until', 'updated_since'
    """
    repositories = []
    page = 1
    per_page = 100  # Maximum allowed by GitHub API
    
    while True:
        # Build query with star filter and optional date filters
        query = f"{keyword} in:name,description,readme stars:{star_range}"
        
        # Add date filters if provided
        if date_filters:
            if date_filters.get('created_since'):
                query += f" created:>={date_filters['created_since']}"
            if date_filters.get('created_until'):
                query += f" created:<={date_filters['created_until']}"
            if date_filters.get('updated_since'):
                query += f" pushed:>={date_filters['updated_since']}"
        
        url = f"{GITHUB_API_BASE}/search/repositories"
        params = {
            "q": query,
            "sort": "stars",
            "order": "desc",
            "per_page": per_page,
            "page": page,
        }
        
        response = requests.get(url, headers=get_headers(), params=params)
        
        if response.status_code != 200:
            rate_limit_handler(response)
            if response.status_code == 403 or response.status_code == 429:
                # Retry after rate limit handling
                response = requests.get(url, headers=get_headers(), params=params)
            else:
                print(f"      ❌ Error: {response.status_code}")
                break
        
        data = response.json()
        items = data.get("items", [])
        
        if not items:
            break
        
        repositories.extend(items)
        
        # Check if we've got all results
        total_count = data.get("total_count", 0)
        if len(repositories) >= total_count or len(items) < per_page:
            break
        
        # GitHub search API has a limit of 1000 results per query
        if len(repositories) >= 1000:
            break
        
        page += 1
        time.sleep(0.5)  # Be respectful to the API
    
    return repositories


def search_repositories(keyword: str, date_filters: dict = None) -> list[dict]:
    """Search GitHub repositories for a specific keyword using star range splitting.
    
    Args:
        keyword: Search keyword
        date_filters: Optional dict for date filtering
    """
    # Star ranges to search - this allows us to get up to 1000 results per range
    STAR_RANGES = [
        "0",          # 0 stars
        "1",          # 1 star
        "2",          # 2 stars exactly
        "3..5",       # Very small projects
        "6..10",      # Small projects
        "11..50",     # Growing projects
        "51..100",    # Medium projects
        "101..500",   # Popular projects
        ">500",       # Highly starred projects
    ]
    
    all_repos = []
    seen_ids = set()
    
    print(f"🔍 Searching for: {keyword}")
    
    for star_range in STAR_RANGES:
        print(f"   ⭐ Stars {star_range}...", end=" ", flush=True)
        
        repos = search_repositories_with_star_range(keyword, star_range, date_filters)
        
        # Deduplicate within this keyword search
        new_count = 0
        for repo in repos:
            repo_id = repo.get("id")
            if repo_id not in seen_ids:
                seen_ids.add(repo_id)
                all_repos.append(repo)
                new_count += 1
        
        print(f"found {len(repos)} ({new_count} new)")
        
        time.sleep(1)  # Delay between star range searches
    
    print(f"   📊 Total for '{keyword}': {len(all_repos)} unique repositories")
    return all_repos


def get_contributors(owner: str, repo: str, max_contributors: int = 30) -> list[dict]:
    """Get list of contributors for a repository with retry logic."""
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contributors"
    params = {"per_page": max_contributors}
    
    try:
        response = retry_request(requests.get, url, headers=get_headers(), params=params, timeout=30)
    except Exception:
        return []
    
    if response is None or response.status_code != 200:
        if response:
            rate_limit_handler(response)
        return []
    
    contributors = response.json()
    
    if isinstance(contributors, list):
        return [
            {
                "login": c.get("login"),
                "avatar_url": c.get("avatar_url"),
                "profile_url": c.get("html_url"),
                "contributions": c.get("contributions"),
            }
            for c in contributors
        ]
    return []


# =============================================================================
# BATCH CONTRIBUTOR FETCHING
# =============================================================================
# Number of concurrent workers for contributor fetching
CONTRIBUTOR_FETCH_WORKERS = 10


def fetch_single_contributor(repo_tuple: tuple) -> tuple:
    """
    Fetch contributors for a single repository.
    
    Args:
        repo_tuple: (full_name, owner, repo_name)
    
    Returns:
        Tuple of (full_name, contributors_list)
    """
    full_name, owner, repo_name = repo_tuple
    try:
        contributors = get_contributors(owner, repo_name)
        return (full_name, contributors)
    except Exception:
        return (full_name, [])


def batch_fetch_contributors(
    repos: list[dict], 
    max_workers: int = None
) -> dict:
    """
    Fetch contributors for multiple repositories concurrently.
    
    Args:
        repos: List of repository dictionaries (must have 'full_name' key)
        max_workers: Number of concurrent workers (default: CONTRIBUTOR_FETCH_WORKERS)
    
    Returns:
        Dict mapping full_name -> contributors list
    """
    if max_workers is None:
        max_workers = CONTRIBUTOR_FETCH_WORKERS
    
    total = len(repos)
    all_contributors = {}
    completed = 0
    
    print(f"\n👥 Fetching contributors for {total} repositories ({max_workers} concurrent workers)...")
    
    # Prepare repo tuples for the executor
    repo_tuples = []
    for repo in repos:
        full_name = repo.get("full_name", "")
        if "/" in full_name:
            owner, repo_name = full_name.split("/", 1)
            repo_tuples.append((full_name, owner, repo_name))
    
    # Process in batches to avoid rate limiting
    batch_size = max_workers * 10
    
    for batch_start in range(0, len(repo_tuples), batch_size):
        batch_end = min(batch_start + batch_size, len(repo_tuples))
        batch = repo_tuples[batch_start:batch_end]
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_repo = {
                executor.submit(fetch_single_contributor, repo_tuple): repo_tuple
                for repo_tuple in batch
            }
            
            for future in as_completed(future_to_repo):
                try:
                    full_name, contributors = future.result()
                    all_contributors[full_name] = contributors
                except Exception:
                    repo_tuple = future_to_repo[future]
                    all_contributors[repo_tuple[0]] = []
                
                completed += 1
                if completed % 100 == 0 or completed == total:
                    print(f"   👤 Progress: {completed}/{total}")
        
        # Small delay between batches to respect rate limits
        if batch_end < len(repo_tuples):
            time.sleep(0.5)
    
    success_count = sum(1 for c in all_contributors.values() if c)
    print(f"   ✅ Completed: {success_count}/{total} repos with contributors")
    
    return all_contributors


def get_readme_content(owner: str, repo: str) -> Optional[str]:
    """Download README content for a repository with retry logic."""
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/readme"
    
    try:
        response = retry_request(requests.get, url, headers=get_headers(), timeout=30)
    except Exception:
        return None
    
    if response is None or response.status_code != 200:
        if response:
            rate_limit_handler(response)
        return None
    
    data = response.json()
    download_url = data.get("download_url")
    
    if download_url:
        try:
            readme_response = retry_request(requests.get, download_url, timeout=30)
            if readme_response and readme_response.status_code == 200:
                return readme_response.text
        except Exception:
            pass
    
    return None


def save_readme(owner: str, repo: str, content: str) -> Path:
    """Save README content to a file."""
    # Create a safe filename
    safe_name = f"{owner}_{repo}.md".replace("/", "_")
    filepath = README_DIR / safe_name
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    
    return filepath


def download_single_readme(repo_info: dict) -> dict:
    """
    Download README for a single repository.
    Returns the repo_info dict updated with readme_saved and readme_path.
    """
    full_name = repo_info.get("full_name", "")
    if "/" in full_name:
        owner, repo_name = full_name.split("/", 1)
    else:
        return {**repo_info, "readme_saved": False, "readme_path": None}
    
    try:
        content = get_readme_content(owner, repo_name)
        if content:
            readme_path = save_readme(owner, repo_name, content)
            return {**repo_info, "readme_saved": True, "readme_path": str(readme_path)}
    except Exception as e:
        pass
    
    return {**repo_info, "readme_saved": False, "readme_path": None}


def batch_download_readmes(
    repos: list[dict], 
    max_workers: int = None,
    progress_callback=None
) -> list[dict]:
    """
    Download READMEs for multiple repositories concurrently.
    
    Args:
        repos: List of repository dictionaries (must have 'full_name' key)
        max_workers: Number of concurrent workers (default: README_DOWNLOAD_WORKERS)
        progress_callback: Optional callback function(completed, total, repo_name)
    
    Returns:
        List of repository dictionaries with readme_saved and readme_path updated
    """
    if max_workers is None:
        max_workers = README_DOWNLOAD_WORKERS
    
    total = len(repos)
    results = []
    completed = 0
    success_count = 0
    
    print(f"\n📥 Downloading READMEs for {total} repositories ({max_workers} concurrent workers)...")
    
    # Process in batches to avoid rate limiting
    batch_size = max_workers * 10  # Process 10 rounds per batch, then pause
    
    for batch_start in range(0, total, batch_size):
        batch_end = min(batch_start + batch_size, total)
        batch = repos[batch_start:batch_end]
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all tasks in this batch
            future_to_repo = {
                executor.submit(download_single_readme, repo): repo 
                for repo in batch
            }
            
            # Process completed futures
            for future in as_completed(future_to_repo):
                original_repo = future_to_repo[future]
                try:
                    result = future.result()
                    results.append(result)
                    if result.get("readme_saved"):
                        success_count += 1
                except Exception as e:
                    # If download failed, add with readme_saved=False
                    results.append({
                        **original_repo, 
                        "readme_saved": False, 
                        "readme_path": None
                    })
                
                completed += 1
                
                # Progress update
                if progress_callback:
                    progress_callback(completed, total, original_repo.get("full_name", ""))
                elif completed % 50 == 0 or completed == total:
                    print(f"   📄 Progress: {completed}/{total} ({success_count} successful)")
        
        # Delay between batches
        if batch_end < total:
            time.sleep(README_BATCH_DELAY)
    
    print(f"   ✅ Completed: {success_count}/{total} READMEs downloaded successfully")
    
    return results


def download_missing_readmes(repos: list[dict]) -> list[dict]:
    """
    Download READMEs only for repositories that don't have them yet.
    Useful for resuming interrupted downloads.
    
    Args:
        repos: List of repository dictionaries
    
    Returns:
        Updated list with README information
    """
    # Separate repos that need README download
    need_download = []
    already_have = []
    
    for repo in repos:
        if repo.get("readme_saved"):
            # Verify the file still exists
            readme_path = repo.get("readme_path")
            if readme_path and Path(readme_path).exists():
                already_have.append(repo)
            else:
                repo["readme_saved"] = False
                need_download.append(repo)
        else:
            need_download.append(repo)
    
    if not need_download:
        print(f"✅ All {len(repos)} repositories already have READMEs downloaded")
        return repos
    
    print(f"📥 {len(already_have)} repos already have READMEs, downloading {len(need_download)} missing...")
    
    # Download missing READMEs
    downloaded = batch_download_readmes(need_download)
    
    # Combine results
    return already_have + downloaded


def save_checkpoint(processed: list, all_contributors: dict, start_idx: int):
    """Save checkpoint to allow resuming."""
    checkpoint = {
        "processed": processed,
        "contributors": all_contributors,
        "last_index": start_idx,
        "timestamp": datetime.now().isoformat()
    }
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, ensure_ascii=False)
    print(f"      💾 Checkpoint saved at index {start_idx}")


def load_checkpoint() -> tuple[list, dict, int]:
    """Load checkpoint or existing results if they exist."""
    if CHECKPOINT_FILE.exists():
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                checkpoint = json.load(f)
            print(f"📂 Resuming from checkpoint (index {checkpoint['last_index']})")
            return checkpoint["processed"], checkpoint["contributors"], checkpoint["last_index"]
        except Exception as e:
            print(f"⚠️ Could not load checkpoint: {e}")
            
    # If no checkpoint, try loading from existing results file to use as cache
    if RESULTS_FILE.exists():
        try:
            with open(RESULTS_FILE, "r", encoding="utf-8") as f:
                existing_repos = json.load(f)
            
            # Load contributors as well
            existing_contributors = {}
            if CONTRIBUTORS_FILE.exists():
                with open(CONTRIBUTORS_FILE, "r", encoding="utf-8") as f:
                    existing_contributors = json.load(f)
            
            print(f"📂 Loaded {len(existing_repos)} existing repos from results (caching metadata)")
            return existing_repos, existing_contributors, 0
        except Exception as e:
            print(f"⚠️ Could not load existing results for cache: {e}")
            
    return [], {}, 0


def process_repositories(all_repos: list[dict], resume: bool = True, use_batch_readme: bool = True, skip_readme: bool = False) -> tuple[list[dict], dict]:
    """
    Process repositories to extract detailed information and contributors.
    
    Args:
        all_repos: List of repository dictionaries from search
        resume: Whether to try resuming from checkpoint
        use_batch_readme: Whether to use batch README downloading (faster)
        skip_readme: If True, skip README downloading entirely (fastest mode)
    
    Returns:
        Tuple of (processed_repos, all_contributors)
    """
    # Try to resume from checkpoint or load existing results as cache
    if resume:
        cached_processed, all_contributors, start_idx = load_checkpoint()
        
        # Filter cached results to only include those in the current search list
        # This ensures that repositories we no longer want (e.g. newly excluded) are removed
        search_ids = {r["id"] for r in all_repos}
        processed = [r for r in cached_processed if r["id"] in search_ids]
        
        seen_repos = {r["id"] for r in processed}
        
        if len(processed) < len(cached_processed):
            print(f"🧹 Cleaned up {len(cached_processed) - len(processed)} repositories from cache that are no longer in search results")
    else:
        processed = []
        all_contributors = {}
        seen_repos = set()
        start_idx = 0
    
    total = len(all_repos)
    print(f"\n📦 Processing {total} repositories (starting from {start_idx})...")
    
    # First pass: extract repo info (no API calls, fast)
    repos_to_process = []
    
    for idx, repo in enumerate(all_repos):
        # Skip already processed repos
        if idx < start_idx:
            continue
            
        repo_id = repo.get("id")
        full_name = repo.get("full_name", "")
        
        # Skip if already processed
        if repo_id in seen_repos:
            continue
        seen_repos.add(repo_id)
        
        owner, repo_name = full_name.split("/") if "/" in full_name else ("", full_name)
        
        # Extract repository info (no API calls here)
        repo_info = {
            "id": repo_id,
            "name": repo.get("name"),
            "full_name": full_name,
            "owner": owner,
            "description": repo.get("description", ""),
            "url": repo.get("html_url"),
            "clone_url": repo.get("clone_url"),
            "homepage": repo.get("homepage", ""),
            "language": repo.get("language"),
            "languages_url": repo.get("languages_url"),
            "stars": repo.get("stargazers_count", 0),
            "forks": repo.get("forks_count", 0),
            "watchers": repo.get("watchers_count", 0),
            "open_issues": repo.get("open_issues_count", 0),
            "topics": repo.get("topics", []),
            "license": repo.get("license", {}).get("name") if repo.get("license") else None,
            "created_at": repo.get("created_at"),
            "updated_at": repo.get("updated_at"),
            "pushed_at": repo.get("pushed_at"),
            "is_fork": repo.get("fork", False),
            "is_archived": repo.get("archived", False),
            "default_branch": repo.get("default_branch"),
        }
        
        repos_to_process.append(repo_info)
    
    print(f"   📋 Extracted metadata for {len(repos_to_process)} repos")
    
    # Second pass: Batch fetch contributors (parallel, fast)
    if repos_to_process:
        contributors_map = batch_fetch_contributors(repos_to_process)
        
        # Attach contributors to repo info
        for repo_info in repos_to_process:
            full_name = repo_info.get("full_name", "")
            contributors = contributors_map.get(full_name, [])
            repo_info["contributors"] = contributors
            all_contributors[full_name] = contributors
    
    # Second pass: Download READMEs (unless skipped)
    if skip_readme:
        # Skip README download entirely - mark all as not saved
        print(f"\n⏭️ Skipping README downloads (--no-readme flag)")
        for repo_info in repos_to_process:
            repo_info["readme_saved"] = False
            repo_info["readme_path"] = None
        processed.extend(repos_to_process)
    elif use_batch_readme and repos_to_process:
        print(f"\n📥 Starting batch README download...")
        repos_with_readme = batch_download_readmes(repos_to_process)
        processed.extend(repos_with_readme)
    else:
        # Sequential download (original behavior)
        for repo_info in repos_to_process:
            full_name = repo_info.get("full_name", "")
            owner = repo_info.get("owner", "")
            repo_name = repo_info.get("name", "")
            
            time.sleep(0.5)
            readme_content = get_readme_content(owner, repo_name)
            if readme_content:
                readme_path = save_readme(owner, repo_name, readme_content)
                repo_info["readme_saved"] = True
                repo_info["readme_path"] = str(readme_path)
            else:
                repo_info["readme_saved"] = False
                repo_info["readme_path"] = None
            
            processed.append(repo_info)
    
    # Clean up checkpoint after successful completion
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
        print("🗑️ Checkpoint removed (processing complete)")
    
    return processed, all_contributors


def save_results(repositories: list[dict], contributors: dict) -> None:
    """Save results to JSON and CSV files."""
    # Save as JSON
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(repositories, f, indent=2, ensure_ascii=False)
    print(f"✅ Saved repository data to: {RESULTS_FILE}")
    
    # Save contributors separately
    with open(CONTRIBUTORS_FILE, "w", encoding="utf-8") as f:
        json.dump(contributors, f, indent=2, ensure_ascii=False)
    print(f"✅ Saved contributors data to: {CONTRIBUTORS_FILE}")
    
    # Save as CSV for easy viewing
    import csv
    with open(RESULTS_CSV, "w", encoding="utf-8", newline="") as f:
        fieldnames = [
            "name", "full_name", "description", "url", "language",
            "stars", "forks", "license", "created_at", "updated_at",
            "topics", "contributors_count", "readme_saved"
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        
        for repo in repositories:
            row = {
                "name": repo["name"],
                "full_name": repo["full_name"],
                "description": (repo["description"] or "")[:200],  # Truncate long descriptions
                "url": repo["url"],
                "language": repo["language"],
                "stars": repo["stars"],
                "forks": repo["forks"],
                "license": repo["license"],
                "created_at": repo["created_at"],
                "updated_at": repo["updated_at"],
                "topics": ", ".join(repo.get("topics", [])),
                "contributors_count": len(repo.get("contributors", [])),
                "readme_saved": repo["readme_saved"],
            }
            writer.writerow(row)
    print(f"✅ Saved CSV summary to: {RESULTS_CSV}")


def generate_markdown_report(repositories: list[dict]) -> None:
    """Generate a markdown report of all repositories."""
    report_file = OUTPUT_DIR / "README.md"
    
    # Sort by stars
    sorted_repos = sorted(repositories, key=lambda x: x["stars"], reverse=True)
    
    with open(report_file, "w", encoding="utf-8") as f:
        f.write("# Quran-Related GitHub Repositories\n\n")
        f.write(f"*Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n\n")
        f.write(f"**Total Repositories Found:** {len(repositories)}\n\n")
        
        # Statistics
        total_stars = sum(r["stars"] for r in repositories)
        languages = {}
        for r in repositories:
            lang = r.get("language") or "Unknown"
            languages[lang] = languages.get(lang, 0) + 1
        
        f.write("## Statistics\n\n")
        f.write(f"- **Total Stars:** {total_stars:,}\n")
        f.write(f"- **Top Languages:** {', '.join(sorted(languages.keys(), key=lambda x: languages[x], reverse=True)[:5])}\n\n")
        
        # Table of contents by language
        f.write("## Repository List\n\n")
        f.write("| # | Repository | Description | ⭐ Stars | Language | License |\n")
        f.write("|---|------------|-------------|---------|----------|--------|\n")
        
        for idx, repo in enumerate(sorted_repos[:100], 1):  # Top 100
            desc = (repo["description"] or "")[:80].replace("|", "\\|").replace("\n", " ")
            f.write(f"| {idx} | [{repo['full_name']}]({repo['url']}) | {desc} | {repo['stars']} | {repo['language'] or 'N/A'} | {repo['license'] or 'N/A'} |\n")
        
        if len(sorted_repos) > 100:
            f.write(f"\n*...and {len(sorted_repos) - 100} more repositories. See full list in repositories.json*\n")
        
        # Top contributors section
        f.write("\n## Top Contributors\n\n")
        all_contributors_flat = []
        for repo_name, contributors in [(r["full_name"], r.get("contributors", [])) for r in repositories]:
            for c in contributors:
                all_contributors_flat.append({**c, "repo": repo_name})
        
        # Aggregate contributions per user
        contributor_aggregated = {}
        for c in all_contributors_flat:
            login = c.get("login")
            if login:
                if login not in contributor_aggregated:
                    contributor_aggregated[login] = {
                        "login": login,
                        "profile_url": c.get("profile_url"),
                        "total_contributions": 0,
                        "repos_count": 0
                    }
                contributor_aggregated[login]["total_contributions"] += c.get("contributions", 0)
                contributor_aggregated[login]["repos_count"] += 1
        
        top_contributors = sorted(
            contributor_aggregated.values(),
            key=lambda x: x["total_contributions"],
            reverse=True
        )[:20]
        
        f.write("| # | Contributor | Total Contributions | Repos Contributed |\n")
        f.write("|---|-------------|--------------------|-----------------|\n")
        for idx, c in enumerate(top_contributors, 1):
            f.write(f"| {idx} | [{c['login']}]({c['profile_url']}) | {c['total_contributions']} | {c['repos_count']} |\n")
    
    print(f"✅ Generated markdown report: {report_file}")


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="🕌 GitHub Quran Repository Scraper - Find Quran-related open source projects",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # Full scrape with README downloads
  %(prog)s --no-readme              # Fast mode: skip README downloads
  %(prog)s --since 2024-01-01       # Only repos created after 2024-01-01
  %(prog)s --updated-since 2024-06-01 --no-readme  # Recently updated, fast mode
        """
    )
    
    # README mode
    readme_group = parser.add_mutually_exclusive_group()
    readme_group.add_argument(
        '--no-readme', '--skip-readme',
        action='store_true',
        dest='skip_readme',
        help='Skip README downloads (faster, metadata only)'
    )
    readme_group.add_argument(
        '--with-readme',
        action='store_true',
        default=True,
        help='Download READMEs (default behavior)'
    )
    
    # Date filters
    parser.add_argument(
        '--since',
        type=str,
        metavar='YYYY-MM-DD',
        help='Only include repos created on or after this date'
    )
    parser.add_argument(
        '--until',
        type=str,
        metavar='YYYY-MM-DD',
        help='Only include repos created on or before this date'
    )
    parser.add_argument(
        '--updated-since',
        type=str,
        metavar='YYYY-MM-DD',
        help='Only include repos updated (pushed) on or after this date'
    )
    
    # Other options
    parser.add_argument(
        '--fresh',
        action='store_true',
        help='Ignore cache and do a fresh search'
    )
    
    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()
    
    print("=" * 60)
    print("🕌 GitHub Quran Repository Scraper")
    print("=" * 60)
    
    # Show mode
    if args.skip_readme:
        print("📋 Mode: Metadata only (README downloads skipped)")
    else:
        print("📋 Mode: Full scrape (with README downloads)")
    
    # Show date filters
    date_filters = {}
    if args.since:
        date_filters['created_since'] = args.since
        print(f"📅 Created since: {args.since}")
    if args.until:
        date_filters['created_until'] = args.until
        print(f"📅 Created until: {args.until}")
    if args.updated_since:
        date_filters['updated_since'] = args.updated_since
        print(f"📅 Updated since: {args.updated_since}")
    
    if not GITHUB_TOKEN:
        print("\n⚠️  Warning: GITHUB_TOKEN not set. Rate limits will be more restrictive.")
        print("   Set the GITHUB_TOKEN environment variable for better results.\n")
    
    # Show exclusion configuration
    if EXCLUDE_TERMS or EXCLUDE_OWNERS or EXCLUDE_REPOS or MIN_STARS > 0:
        print("\n🔧 Exclusion Configuration:")
        if EXCLUDE_TERMS:
            print(f"   - Excluded terms: {len(EXCLUDE_TERMS)} patterns")
        if EXCLUDE_OWNERS:
            print(f"   - Excluded owners: {', '.join(EXCLUDE_OWNERS)}")
        if EXCLUDE_REPOS:
            print(f"   - Excluded repos: {len(EXCLUDE_REPOS)}")
        if MIN_STARS > 0:
            print(f"   - Minimum stars: {MIN_STARS}")
    
    # Create output directories
    OUTPUT_DIR.mkdir(exist_ok=True)
    if not args.skip_readme:
        README_DIR.mkdir(exist_ok=True)
    
    # Check for cached search results (unless --fresh)
    all_repos = []
    if not args.fresh and SEARCH_CACHE_FILE.exists():
        try:
            with open(SEARCH_CACHE_FILE, "r", encoding="utf-8") as f:
                all_repos = json.load(f)
            print(f"📂 Loaded {len(all_repos)} repos from search cache")
        except Exception as e:
            print(f"⚠️ Could not load search cache: {e}")
            all_repos = []
    
    # If no cache, do the search with parallel keyword processing
    if not all_repos:
        print(f"\n🔎 Searching {len(ALL_KEYWORDS)} keywords in parallel...")
        
        # Parallel keyword search with limited workers to respect rate limits
        KEYWORD_SEARCH_WORKERS = 5
        keyword_results = []
        
        def search_keyword_wrapper(keyword):
            """Wrapper for parallel keyword search."""
            return search_repositories(keyword, date_filters if date_filters else None)
        
        with ThreadPoolExecutor(max_workers=KEYWORD_SEARCH_WORKERS) as executor:
            future_to_keyword = {
                executor.submit(search_keyword_wrapper, kw): kw 
                for kw in ALL_KEYWORDS
            }
            
            completed = 0
            for future in as_completed(future_to_keyword):
                keyword = future_to_keyword[future]
                try:
                    repos = future.result()
                    keyword_results.extend(repos)
                except Exception as e:
                    print(f"   ⚠️ Error searching '{keyword}': {e}")
                
                completed += 1
                if completed % 10 == 0:
                    print(f"   🔄 Keywords completed: {completed}/{len(ALL_KEYWORDS)}")
        
        # Deduplicate across all keywords
        seen_ids = set()
        for repo in keyword_results:
            if repo["id"] not in seen_ids:
                seen_ids.add(repo["id"])
                all_repos.append(repo)
        
        # Save search cache
        with open(SEARCH_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(all_repos, f, ensure_ascii=False)
        print(f"💾 Saved search results to cache ({len(all_repos)} repos)")
    
    print(f"\n📊 Total: {len(all_repos)} unique repositories across all keywords")
    
    if not all_repos:
        print("❌ No repositories found. Exiting.")
        return
    
    # Apply exclusion filtering
    filtered_repos = filter_repositories(all_repos)
    print(f"📋 After filtering: {len(filtered_repos)} repositories to process")
    
    if not filtered_repos:
        print("❌ All repositories were filtered out. Check your exclusion settings.")
        return
    
    # Process repositories (get details, contributors, optionally READMEs)
    processed_repos, contributors = process_repositories(
        filtered_repos, 
        skip_readme=args.skip_readme
    )
    
    # Save results
    save_results(processed_repos, contributors)
    
    # Generate markdown report
    generate_markdown_report(processed_repos)
    
    # Keep search cache for faster re-runs (use --fresh to force new search)
    print(f"� Search cache preserved at: {SEARCH_CACHE_FILE}")
    
    print("\n" + "=" * 60)
    print("✨ Scraping complete!")
    print(f"   - Repositories found: {len(processed_repos)}")
    if not args.skip_readme:
        print(f"   - READMEs downloaded: {sum(1 for r in processed_repos if r.get('readme_saved'))}")
    print(f"   - Output directory: {OUTPUT_DIR.absolute()}")
    print("=" * 60)


if __name__ == "__main__":
    main()

