#!/usr/bin/env python3
"""
Add core developer information to a GitHub repository dataset
Identification strategy: based on commit counts, developers accumulating 80% of the commits whose individual share is >= 5%
Uses several GitHub APIs to obtain accurate contributor statistics
"""

import os
import json
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Optional, Any, Tuple
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Bot detection configuration
BOT_KEYWORDS = [
    'bot', 'github-actions', 'dependabot', 'renovate', 'snyk-bot',
    'codecov', 'coveralls', 'travis-ci', 'jenkins', 'circleci',
    'gitlab-ci', 'mergify', 'stale', 'pyup', 'pre-commit-ci',
    'lgtm-com', 'sonarcloud', 'netlify', 'vercel', 'bot'
]

BOT_SUFFIX = ['[bot]', '(bot)', '-bot', '_bot', 'bot']


def is_bot_user(login: str, author_dict: Optional[Dict] = None) -> bool:
    """
    Determine whether the user is a bot

    Args:
        login: User login name
        author_dict: Author information dictionary (optional)

    Returns:
        Whether the user is a bot
    """
    if not login:
        return True  # An entry without a login is anonymous and is not counted as a developer

    login_lower = login.lower()

    # Check the account type
    if author_dict and author_dict.get('type') == 'Bot':
        return True

    # Check the common bot keywords
    for keyword in BOT_KEYWORDS:
        if keyword in login_lower:
            return True

    # Check the bot suffix
    for suffix in BOT_SUFFIX:
        if login.endswith(suffix) or login_lower.endswith(suffix):
            return True

    # GitHub bot accounts usually end with [bot]
    if login.endswith('[bot]'):
        return True

    return False

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('core_developers.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Module configuration
MODULE_CONFIG = {
    'enabled': False,
    'batch_size': 50,  # Number of repositories per batch
    'delay_between_batches': 2,  # Delay between batches (seconds)
    'delay_between_repos': 0.5,  # Delay between repositories (seconds)
    'max_retries': 3,
    'token': 'xxx',
    'force': False,  # Whether to reprocess every repository
    'min_commit_threshold': 5,  # Minimum commit share of a core developer (percentage)
    'cumulative_threshold': 80,  # Cumulative commit share threshold (percentage)
    'output_field': 'core_developers',  # Name of the output field
    'stats_retry_delay': 5,  # Time to wait while the statistics are generated (seconds)
    'api_strategy': 'auto',  # Select the API strategy automatically
    'enable_fallback': True,  # Enable the degrading strategy
    'exclude_bots': True,  # Whether to exclude bot accounts
}


def _create_session(token: Optional[str] = None) -> requests.Session:
    """Create a session with a retry strategy"""
    session = requests.Session()

    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.headers.update({
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'GitHub-Core-Developers-Collector'
    })

    if token:
        session.headers.update({'Authorization': f'token {token}'})

    return session


def get_contributors_stats(full_name: str, token: Optional[str] = None,
                           max_retries: int = 3, retry_delay: int = 5) -> List[Dict]:
    """
    Fetch the contributor statistics of a repository (matching the GitHub Insights page)
    Uses the GitHub Statistics API: /repos/{owner}/{repo}/stats/contributors

    Args:
        full_name: Full repository name (owner/repo)
        token: GitHub API Token
        max_retries: Maximum number of retries
        retry_delay: Retry delay (seconds)

    Returns:
        List of contributor statistics, each carrying the commit total and the user information
    """
    url = f"https://api.github.com/repos/{full_name}/stats/contributors"
    session = _create_session(token)

    for attempt in range(max_retries):
        try:
            response = session.get(url)

            # Check the API rate limit
            if response.headers.get('X-RateLimit-Remaining') == '0':
                reset_time = int(response.headers.get('X-RateLimit-Reset', 0))
                wait_time = max(reset_time - time.time(), 0) + 1
                logger.warning(f"API rate limit reached, waiting {wait_time} seconds")
                time.sleep(wait_time)
                continue

            # This API may require Git to compute the statistics first, so the first request can return 202 Accepted
            if response.status_code == 202:
                logger.info(f"GitHub is computing the statistics of {full_name}, retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                continue

            if response.status_code == 200:
                contributors = response.json()
                logger.info(f"Fetched the contributor statistics of {full_name}, {len(contributors)} contributors in total")
                return contributors
            elif response.status_code == 404:
                logger.warning(f"Repository {full_name} does not exist or is not accessible")
                return []
            elif response.status_code == 403:
                logger.error(f"Access denied for {full_name}, permissions may be insufficient")
                return []
            else:
                logger.warning(f"Failed to fetch the contributors of {full_name}: {response.status_code}")
                return []

        except Exception as e:
            logger.error(f"Error while fetching the contributors of {full_name} (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                return []
        finally:
            session.close()

    return []


def get_contributors_via_contributors_api(full_name: str, token: Optional[str] = None) -> List[Dict]:
    """
    Fetch contributor information through the /repos/{owner}/{repo}/contributors API
    This API responds faster but may not carry the complete statistics

    Args:
        full_name: Full repository name (owner/repo)
        token: GitHub API Token

    Returns:
        List of contributors
    """
    url = f"https://api.github.com/repos/{full_name}/contributors"
    session = _create_session(token)

    try:
        # Add the parameters needed to fetch every contributor
        params = {
            'per_page': 100,
            'anon': 'true'  # Include anonymous contributors
        }

        all_contributors = []
        page = 1

        while True:
            params['page'] = page
            response = session.get(url, params=params)

            if response.status_code == 404:
                logger.warning(f"Repository {full_name} does not exist or is not accessible")
                return []
            elif response.status_code == 403:
                logger.error(f"Access denied for {full_name}, permissions may be insufficient")
                return []
            elif response.status_code != 200:
                logger.warning(f"Failed to fetch the contributors of {full_name}: {response.status_code}")
                return []

            contributors = response.json()
            if not contributors:
                break

            all_contributors.extend(contributors)

            # Check whether there is a next page
            if 'next' not in response.links:
                break

            page += 1
            time.sleep(0.5)  # Avoid issuing requests too quickly

        # Convert to a format similar to the stats API
        formatted_contributors = []
        for c in all_contributors:
            formatted_contributors.append({
                'author': {
                    'login': c.get('login'),
                    'html_url': c.get('html_url'),
                    'avatar_url': c.get('avatar_url'),
                    'type': c.get('type', 'User')
                },
                'total': c.get('contributions', 0)
            })

        logger.info(f"Fetched the contributors of {full_name} through the contributors API, {len(formatted_contributors)} in total")
        return formatted_contributors

    except Exception as e:
        logger.error(f"Error while fetching the contributors of {full_name}: {e}")
        return []
    finally:
        session.close()


def get_contributors_via_commits_api(full_name: str, token: Optional[str] = None, max_commits: int = 100) -> List[Dict]:
    """
    Fetch the most recent commits through the /repos/{owner}/{repo}/commits API and derive the contributors from them
    Used as the last fallback

    Args:
        full_name: Full repository name (owner/repo)
        token: GitHub API Token
        max_commits: Maximum number of commits to fetch

    Returns:
        List of contributor statistics
    """
    url = f"https://api.github.com/repos/{full_name}/commits"
    session = _create_session(token)

    try:
        params = {
            'per_page': 100,
            'page': 1
        }

        contributors_map = {}
        total_fetched = 0

        while total_fetched < max_commits:
            response = session.get(url, params=params)

            if response.status_code != 200:
                logger.warning(f"Failed to fetch the commits of {full_name}: {response.status_code}")
                break

            commits = response.json()
            if not commits:
                break

            for commit in commits:
                total_fetched += 1
                author = commit.get('author') or commit.get('commit', {}).get('author', {})

                if author:
                    login = author.get('login')
                    if login:
                        contributors_map[login] = contributors_map.get(login, 0) + 1
                    else:
                        # Anonymous contributor
                        name = author.get('name', 'anonymous')
                        contributors_map[f"anon:{name}"] = contributors_map.get(f"anon:{name}", 0) + 1

            # Check whether there is a next page
            if 'next' not in response.links or total_fetched >= max_commits:
                break

            params['page'] += 1
            time.sleep(0.5)

        # Normalize to the standard format
        formatted_contributors = []
        for login, commits in contributors_map.items():
            is_anon = login.startswith('anon:')
            formatted_contributors.append({
                'author': {
                    'login': login.replace('anon:', '') if is_anon else login,
                    'html_url': None if is_anon else f"https://github.com/{login}",
                    'avatar_url': None if is_anon else f"https://github.com/{login}.png",
                    'type': 'Anonymous' if is_anon else 'User'
                },
                'total': commits
            })

        logger.info(
            f"Fetched the commits of {full_name} through the commits API, {len(formatted_contributors)} contributors from {total_fetched} commits")
        return formatted_contributors

    except Exception as e:
        logger.error(f"Error while fetching the commits of {full_name}: {e}")
        return []
    finally:
        session.close()


def get_contributors_with_fallback(full_name: str, config: Dict[str, Any]) -> List[Dict]:
    """
    Fetch contributor information with several strategies, degrading automatically

    Args:
        full_name: Full repository name
        config: Configuration

    Returns:
        List of contributor statistics
    """
    token = config.get('token')
    strategy = config.get('api_strategy', 'auto')
    max_commits = config.get('max_commits_to_fetch', 100)
    enable_fallback = config.get('enable_fallback', True)

    strategies = []

    if strategy == 'stats':
        strategies = ['stats']
    elif strategy == 'contributors':
        strategies = ['contributors']
    elif strategy == 'commits':
        strategies = ['commits']
    else:  # The auto strategy
        if enable_fallback:
            strategies = ['stats', 'contributors', 'commits']
        else:
            strategies = ['stats']

    for s in strategies:
        logger.info(f"Trying the {s} API to fetch the information of {full_name}")

        if s == 'stats':
            contributors = get_contributors_stats(full_name, token)
            if contributors:
                return contributors

        elif s == 'contributors':
            contributors = get_contributors_via_contributors_api(full_name, token)
            if contributors:
                return contributors

        elif s == 'commits':
            contributors = get_contributors_via_commits_api(full_name, token, max_commits)
            if contributors:
                return contributors

        # Short delay before trying the next strategy
        time.sleep(1)

    return []


def identify_core_developers_from_stats(contributors: List[Dict],
                                        min_threshold: float = 5.0,
                                        cumulative_threshold: float = 80.0,
                                        exclude_bots: bool = True) -> List[Dict[str, Any]]:
    """
    Identify the core developers from the contributor statistics

    Args:
        contributors: Contributor statistics returned by the GitHub API
        min_threshold: Minimum commit share threshold (percentage)
        cumulative_threshold: Cumulative commit share threshold (percentage)
        exclude_bots: Whether to exclude bot accounts

    Returns:
        List of detailed core developer records
    """
    if not contributors:
        return []

    # Process the contributor data, discarding the invalid entries
    developer_stats = []
    total_commits = 0

    # Compute the valid commit total first
    valid_contributors = []
    for contributor in contributors:
        # Check whether the contributor is None
        if contributor is None:
            continue

        # Read the total field, making sure it is a number
        commits = contributor.get('total', 0)
        if not isinstance(commits, (int, float)) or commits <= 0:
            continue

        # Check the author field
        author = contributor.get('author')
        if author is None or not isinstance(author, dict):
            # Build a minimal record when the author information is missing
            author = {
                'login': 'unknown',
                'html_url': None,
                'avatar_url': None,
                'type': 'Unknown'
            }

        # Read the login name
        login = author.get('login') if author else None

        # Exclude bot accounts
        if exclude_bots and login and is_bot_user(login, author):
            logger.debug(f"Skipping bot account: {login}")
            continue

        valid_contributors.append({
            'contributor': contributor,
            'author': author,
            'commits': commits
        })
        total_commits += commits

    if total_commits == 0:
        return []

    # Build the developer statistics
    for item in valid_contributors:
        contributor = item['contributor']
        author = item['author']
        commits = item['commits']

        percentage = (commits / total_commits) * 100

        # Read the author information defensively
        login = author.get('login') if author else None
        if not login:
            # Try another identifier when there is no login
            login = f"user_{len(developer_stats)}"

        developer_stats.append({
            'login': login,
            'name': login,
            'html_url': author.get('html_url') if author else None,
            'avatar_url': author.get('avatar_url') if author else None,
            'commits': commits,
            'percentage': round(percentage, 2),
            'type': author.get('type', 'github_user') if author else 'unknown'
        })

    # Sort by commit count (descending)
    developer_stats.sort(key=lambda x: x['commits'], reverse=True)

    # Identify the core developers: cumulative commit share of 80% with an individual share >= 5%
    cumulative = 0
    core_developers = []

    for dev in developer_stats:
        percentage = dev['percentage']

        # When the current developer is below 5%, check whether the 80% cumulative threshold is already reached
        if percentage < min_threshold:
            # Stop once the cumulative threshold is reached; otherwise continue (skipping this developer)
            if cumulative >= cumulative_threshold:
                break
            continue

        # Check whether adding this developer would exceed the cumulative threshold
        if cumulative + percentage <= cumulative_threshold:
            # Entirely within the threshold, add it directly
            core_developers.append(dev)
            cumulative += percentage
        elif cumulative < cumulative_threshold:
            # Adding it exceeds the threshold, but the cumulative share has not reached it yet
            core_developers.append(dev)
            cumulative += percentage
            break
        else:
            break

        # Stop once the cumulative threshold is reached
        if cumulative >= cumulative_threshold:
            break

    # Record the debugging information
    if core_developers:
        logger.debug(f"Core developers: {[d['login'] for d in core_developers]}, "
                     f"cumulative share: {cumulative:.2f}%, total commits: {total_commits}")
    else:
        logger.debug(f"No core developer found, total commits: {total_commits}")

    return core_developers


def format_quoted_list(items: List[str]) -> str:
    """
    Format a list of strings as a comma-separated string
    For example: ["torvalds", "gregkh", "akpm"] -> "torvalds, gregkh, akpm"
    """
    if not items:
        return ""

    # Drop None values and empty strings
    valid_items = [item for item in items if item]

    # Join with a comma and a space, without quotes
    return ", ".join(valid_items)


def process_repository(repo: Dict[str, Any], config: Dict[str, Any]) -> bool:
    """
    Process a single repository and add the core developer information
    - core_developers: List of core developer names, formatted as "name1, name2, name3"
    """
    full_name = repo.get('full_name')
    if not full_name:
        logger.warning("The repository lacks a full_name field, skipping")
        return False

    output_field = config.get('output_field', 'core_developers')
    exclude_bots = config.get('exclude_bots', True)

    # Check whether core developer information is already present
    if not config.get('force', False):
        if output_field in repo:
            if repo.get(output_field):  # Skip when a non-empty value is already present
                logger.debug(f"Repository {full_name} already carries core developer information, skipping")
                return False

    # Fetch contributor information with the degrading strategy
    contributors = get_contributors_with_fallback(full_name, config)

    if not contributors:
        logger.warning(f"No contributor statistics were obtained for repository {full_name}")
        repo[output_field] = ""
        return True

    # Identify the core developers (excluding bots)
    min_threshold = config.get('min_commit_threshold', 5)
    cumulative_threshold = config.get('cumulative_threshold', 80)

    core_developers = identify_core_developers_from_stats(
        contributors,
        min_threshold=min_threshold,
        cumulative_threshold=cumulative_threshold,
        exclude_bots=exclude_bots
    )

    if core_developers:
        # Collect the core developer names
        names = []
        for dev in core_developers:
            if dev.get('login'):
                names.append(dev['login'])

        # Format as a comma-separated list
        repo[output_field] = format_quoted_list(names)

        logger.info(f"Repository {full_name}: {len(core_developers)} core developers identified: {names}")
    else:
        repo[output_field] = ""
        logger.info(f"Repository {full_name}: no core developer identified")

    return True


def collect(module_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Main collection function - invoked from collector.py

    Returns:
        Returns an empty list, because the original data file is modified in place
    """
    logger.info("collect was called, but the core_developers module modifies the data file directly through update_dataset")
    return []


def update_dataset(data_file: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Update the dataset by adding the core developer information

    Args:
        data_file: Path to the dataset file
        config: Configuration

    Returns:
        Statistics of the processing result
    """
    data_file = Path(data_file)
    if not data_file.exists():
        logger.error(f"The data file {data_file} does not exist")
        return {
            'status': 'failed',
            'message': f'File does not exist: {data_file}',
            'processed': 0,
            'updated': 0,
            'failed': 0,
            'skipped': 0
        }

    # Load the data
    try:
        with open(data_file, 'r', encoding='utf-8') as f:
            repos = json.load(f)
        logger.info(f"Loaded {len(repos)} repositories")
    except Exception as e:
        logger.error(f"Failed to load the data file: {e}")
        return {
            'status': 'failed',
            'message': f'Loading failed: {e}',
            'processed': 0,
            'updated': 0,
            'failed': 0,
            'skipped': 0
        }

    output_field = config.get('output_field', 'core_developers')

    # Process every repository
    updated_count = 0
    failed_count = 0
    skipped_count = 0

    batch_size = config.get('batch_size', 50)
    delay_between_batches = config.get('delay_between_batches', 2)
    delay_between_repos = config.get('delay_between_repos', 0.5)

    for i, repo in enumerate(repos, 1):
        try:
            full_name = repo.get('full_name', 'unknown')

            # Check whether core developer information is already present
            if not config.get('force', False):
                if output_field in repo:
                    if repo.get(output_field):  # Skip when a non-empty value is already present
                        logger.debug(f"Repository {full_name} has already been processed, skipping")
                        skipped_count += 1
                        continue

            if process_repository(repo, config):
                updated_count += 1
            else:
                failed_count += 1

            # Save periodically, to survive an unexpected interruption
            if i % 10 == 0:
                temp_file = data_file.with_suffix('.tmp')
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(repos, f, ensure_ascii=False, indent=2)
                temp_file.replace(data_file)
                logger.info(f"Progress saved: {i}/{len(repos)}")

            # Avoid issuing requests too quickly
            time.sleep(delay_between_repos)

            # Delay between batches
            if i % batch_size == 0 and i < len(repos):
                logger.info(f"Finished repository {i}/{len(repos)}, waiting {delay_between_batches} seconds...")
                time.sleep(delay_between_batches)

        except Exception as e:
            logger.error(f"Error while processing repository {repo.get('full_name', 'unknown')}: {e}")
            failed_count += 1

    # Final save
    try:
        temp_file = data_file.with_suffix('.tmp')
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(repos, f, ensure_ascii=False, indent=2)
        temp_file.replace(data_file)
        logger.info(f"Final result saved to {data_file}")
    except Exception as e:
        logger.error(f"Failed to save the data file: {e}")
        return {
            'status': 'partial',
            'message': f'Saving failed: {e}',
            'processed': len(repos),
            'updated': updated_count,
            'failed': failed_count,
            'skipped': skipped_count
        }

    # Result statistics
    status = 'success' if failed_count == 0 else 'partial' if updated_count > 0 else 'failed'

    return {
        'status': status,
        'message': f'Processing complete: {updated_count} repositories updated, {failed_count} failed, {skipped_count} skipped',
        'processed': len(repos),
        'updated': updated_count,
        'failed': failed_count,
        'skipped': skipped_count
    }


def main():
    """Command line entry point"""
    import argparse

    parser = argparse.ArgumentParser(description='Add core developer information to a GitHub repository dataset')
    parser.add_argument('data_file', help='Path to the GitHub repository dataset JSON file')
    parser.add_argument('--token', '-t', help='GitHub API Token')
    parser.add_argument('--force', '-f', action='store_true', help='Reprocess every repository')
    parser.add_argument('--batch-size', type=int, default=50, help='Number of repositories per batch')
    parser.add_argument('--min-threshold', type=float, default=5.0, help='Minimum commit share of a core developer (percentage)')
    parser.add_argument('--cumulative-threshold', type=float, default=80.0, help='Cumulative commit share threshold (percentage)')
    parser.add_argument('--api-strategy', choices=['stats', 'contributors', 'commits', 'auto'],
                        default='auto', help='API fetch strategy')
    parser.add_argument('--include-bots', action='store_true', help='Include bot accounts (they are excluded by default)')

    args = parser.parse_args()

    # Configuration
    config = MODULE_CONFIG.copy()
    config.update({
        'token': args.token,
        'force': args.force,
        'batch_size': args.batch_size,
        'min_commit_threshold': args.min_threshold,
        'cumulative_threshold': args.cumulative_threshold,
        'api_strategy': args.api_strategy,
        'exclude_bots': not args.include_bots,  # Bots are excluded by default
    })

    # Run the update
    result = update_dataset(args.data_file, config)

    logger.info(f"Processing result: {result['message']}")

    # Return the status code
    if result['status'] == 'success':
        return 0
    elif result['status'] == 'partial':
        return 1
    else:
        return 2


if __name__ == '__main__':
    exit(main())