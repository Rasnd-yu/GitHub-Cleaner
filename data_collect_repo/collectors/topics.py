#!/usr/bin/env python3
"""
GitHub Topics collection module
Reads the index.md file of every topic from the local topics directory, extracts the topic name,
then retrieves the ten most starred repositories of each topic through the GitHub API.
Source identifier: "GitHub Topics_<topic name>_top 10"
"""

import logging
import time
from typing import List, Dict, Any, Optional
from pathlib import Path
import frontmatter  # Requires: pip install python-frontmatter
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Module configuration
MODULE_CONFIG = {
    'enabled': False,
    'force': False,
    'source_prefix': 'GitHub Topics',
    'token': 'xxx',
    'max_repos_per_topic': 10,
    'topics_path': 'topics',  # Path of the local topics folder
    'source': 'GitHub Topics',  # Data source identifier (overridden by the concrete topic name)
    'request_delay': 0.5,  # Delay between API requests, to stay clear of the rate limit
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
        'User-Agent': 'GitHub-Dataset-Collector'
    })

    if token:
        session.headers.update({'Authorization': f'token {token}'})

    return session


def collect(module_config: Optional[Dict[str, Any]] = None, **kwargs) -> List[Dict[str, Any]]:
    """
    Read every topic from the local topics directory and fetch its popular repositories through the GitHub API.
    Returns a list of dictionaries carrying the source, each with a full_name and a source field.

    Args:
        module_config: Module configuration
        **kwargs: Backward compatibility with the old calling convention

    Returns:
        List of repository dictionaries, each shaped as: {"full_name": "owner/repo", "source": "GitHub Topics_<topic name>_top 10"}
    """
    logger.info("Collecting popular GitHub Topics repositories from the local topics directory...")

    # Merge the configuration
    config = MODULE_CONFIG.copy()
    if module_config:
        config.update(module_config)

    limit = config.get('max_repos_per_topic', 10)
    topics_path = config.get('topics_path', 'topics')
    token = config.get('token')
    delay = config.get('request_delay', 0.5)

    # Check whether the topics directory exists
    base_path = Path(topics_path)
    if not base_path.exists() or not base_path.is_dir():
        logger.error(f"The topics directory does not exist or is not a directory: {topics_path}")
        return []

    # Discover every topic
    topics_to_process = _discover_all_topics(base_path)
    if not topics_to_process:
        logger.error("No valid topic was found under the topics directory (each topic directory must contain an index.md file)")
        return []

    logger.info(f"Found {len(topics_to_process)} topics, taking the top {limit} repositories of each")

    # Track the repositories already added, to avoid duplicates
    seen_repos = {}
    all_repos_to_collect = []

    # Create the session
    session = _create_session(token)

    try:
        # Iterate over every topic and fetch its popular repositories through the API
        for i, topic in enumerate(topics_to_process):
            logger.info(f"Processing topic [{i + 1}/{len(topics_to_process)}]: {topic}")

            # Build the source identifier of this topic
            topic_source = f"GitHub Topics_{topic}_top {limit}"

            repos_in_topic = _get_repos_from_topic_api(
                session,
                topic,
                limit
            )

            if not repos_in_topic:
                logger.warning(f"Topic '{topic}' returned no repository data; the API request may have failed or the topic may be empty.")
                continue

            added_count = 0
            for repo_full_name in repos_in_topic:
                # If a repository already exists but comes from a different topic, should every source be kept? Here it is deduplicated, keeping the first occurrence
                if repo_full_name not in seen_repos:
                    seen_repos[repo_full_name] = True
                    # Return a dictionary carrying the source
                    all_repos_to_collect.append({
                        "full_name": repo_full_name,
                        "source": topic_source
                    })
                    added_count += 1
                else:
                    logger.debug(f"Repository {repo_full_name} already appeared under another topic, skipping")

            logger.info(f"Topic '{topic}' contributed {added_count} new repositories ({len(repos_in_topic)} retrieved in total)")

            # Stay clear of the API rate limit
            if i < len(topics_to_process) - 1:
                time.sleep(delay)

    finally:
        session.close()

    logger.info(
        f"Collection complete, {len(topics_to_process)} topics yielded {len(all_repos_to_collect)} unique repositories")

    return all_repos_to_collect


def _discover_all_topics(base_path: Path) -> List[str]:
    """
    Discover every valid topic name in the local topics directory.
    Each subdirectory is a topic; it is considered valid only if it contains an index.md file.
    """
    topics = []

    try:
        # List every subdirectory of the topics directory
        all_items = sorted(base_path.iterdir())
        dirs = [item for item in all_items if item.is_dir()]

        for dir_path in dirs:
            topic = dir_path.name
            # Check whether the directory contains an index.md file
            index_file = dir_path / "index.md"
            if index_file.exists() and index_file.is_file():
                topics.append(topic)
                logger.debug(f"Found topic: {topic}")

        logger.info(f"Parsed {len(topics)} topics from the local directory")
        return topics

    except Exception as e:
        logger.error(f"Failed to parse the local topics directory: {e}")
        return []


def _get_repos_from_topic_api(session: requests.Session, topic: str, limit: int) -> List[str]:
    """
    Fetch the popular repositories of a topic through the GitHub API (ordered by stars)

    Args:
        session: requests Session object
        topic: Topic name
        limit: Maximum number of results

    Returns:
        List of repository full names
    """
    repos = []

    try:
        # Search repositories through the GitHub API, filtered by topic and ordered by stars
        url = "https://api.github.com/search/repositories"
        params = {
            'q': f'topic:{topic}',
            'sort': 'stars',
            'order': 'desc',
            'per_page': min(limit, 100)  # The API returns at most 100 entries per page
        }

        logger.debug(f"Requesting the API: {url}, parameters: {params}")

        response = session.get(url, params=params)

        # Check the API rate limit
        if response.headers.get('X-RateLimit-Remaining') == '0':
            reset_time = int(response.headers.get('X-RateLimit-Reset', 0))
            wait_time = max(reset_time - time.time(), 0) + 1
            logger.warning(f"API rate limit reached, waiting {wait_time} seconds")
            time.sleep(wait_time)
            # Retry the current request
            response = session.get(url, params=params)

        if response.status_code == 403:
            logger.error(f"API access denied, the rate limit may have been reached: {response.text}")
            return []
        elif response.status_code == 422:
            logger.error(f"Invalid API request parameters (topic: {topic}): {response.text}")
            return []

        response.raise_for_status()
        data = response.json()

        # Extract the repository full names
        items = data.get('items', [])
        for item in items[:limit]:
            full_name = item.get('full_name')
            if full_name:
                repos.append(full_name)

        logger.debug(f"Topic '{topic}' returned {len(repos)} repositories from the API")
        return repos[:limit]

    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed (topic: {topic}): {e}")
        return []
    except Exception as e:
        logger.error(f"Failed to parse the API response (topic: {topic}): {e}")
        return []


def _extract_topic_from_index(base_path: Path, topic: str) -> Optional[str]:
    """
    Extract the topic name from an index.md file (fallback, in case it has to come from the file content)
    The directory name is currently used as the topic name; this function is kept in case parsing the file becomes necessary
    """
    try:
        index_file = base_path / topic / "index.md"
        if not index_file.exists():
            return topic  # Fall back to the directory name when the file does not exist

        with open(index_file, 'r', encoding='utf-8') as f:
            post = frontmatter.load(f)

        # Try to read the topic from the frontmatter
        file_topic = post.get('topic')
        if file_topic:
            return file_topic

        # Return the directory name when there is no topic field
        return topic

    except Exception as e:
        logger.debug(f"Failed to parse the topic from index.md: {e}")
        return topic


def collect_with_details(module_config: Optional[Dict[str, Any]] = None, **kwargs) -> List[Dict[str, Any]]:
    """Kept for backward compatibility; returns the dictionary format"""
    logger.warning("collect_with_details is deprecated, use the collect function instead")
    return collect(module_config, **kwargs)


if __name__ == '__main__':
    # Test the module
    repos = collect()
    print(f"Found {len(repos)} repositories")
    if repos:
        print("\nSample of the collected repositories:")
        for i, repo in enumerate(repos[:20]):
            print(f"{i + 1}. {repo['full_name']} (source: {repo['source']}")