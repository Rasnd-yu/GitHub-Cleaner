#!/usr/bin/env python3
"""
2024 GitHub Trending list collection module
Parses the per-language trending lists under archive/repository/2024/
Supports limiting the collection to the top k repositories
"""

import json
import glob
from pathlib import Path
from typing import List, Union, Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)

# Module configuration (every setting is centralized here)
MODULE_CONFIG = {
    'enabled': False,  # Whether the module is enabled
    'force': False,  # Whether to force execution
    'source': '2024 GitHub Trending',  # Data source identifier
    'token': 'xxx',
    'archive_path': 'archive',  # Archive path
    'max_retries': 3,
    'timeout': 30,
    'limit_per_language': 5  # Keep only the top 5 repositories per language
}


def collect(module_config: Optional[Dict[str, Any]] = None, **kwargs) -> List[str]:
    """
    Collect every repository listed in the trending files under archive/repository/2024/

    Args:
        module_config: Module configuration (archive_path, limit_per_language, ...)
        **kwargs: Backward compatibility with the old calling convention

    Returns:
        List of repository full names (strings only, without any other information)
    """
    logger.info("Collecting 2024 GitHub Trending repositories...")

    # Merge the configuration
    config = MODULE_CONFIG.copy()
    if module_config:
        config.update(module_config)

    # Backward compatibility (a github_token supplied through kwargs)
    if kwargs.get('github_token'):
        config['token'] = kwargs['github_token']
        logger.info("Overriding the module configuration with the supplied token")

    # Read the limit from the configuration, defaulting to 5
    limit_per_language = config.get('limit_per_language', 5)
    logger.info(f"Collecting at most the top {limit_per_language} repositories per language")

    archive_path = config.get('archive_path', 'archive')
    token = config.get('token')

    if token:
        logger.info(f"Using token: {token[:8]}...")

    year_path = Path(archive_path) / "repository" / "2024"
    if not year_path.exists():
        logger.error(f"Path does not exist: {year_path}")
        return []

    # Temporary set used for deduplication (module level, to avoid returning duplicates)
    seen_repos = set()
    repos_to_collect = []

    # Get the directory of every date
    date_dirs = sorted(glob.glob(str(year_path / "2024-*-*")))
    logger.info(f"Found {len(date_dirs)} date directories")

    for date_dir in date_dirs:
        date = Path(date_dir).name
        logger.debug(f"Processing date: {date}")

        # Get the json file of every language for that date
        json_files = glob.glob(str(Path(date_dir) / "*.json"))

        for json_file in json_files:
            language = Path(json_file).stem
            logger.debug(f"  Processing language: {language}")

            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                # Parse the different data formats
                repo_list = []
                if isinstance(data, dict):
                    repo_list = data.get('list', [])
                elif isinstance(data, list):
                    repo_list = data

                # Keep only the first limit_per_language repositories
                if limit_per_language > 0:
                    repo_list = repo_list[:limit_per_language]
                    logger.debug(f"  Keeping the top {limit_per_language} repositories")

                # Process every repository
                for repo_item in repo_list:
                    full_name = _extract_full_name(repo_item)

                    if full_name and full_name not in seen_repos:
                        seen_repos.add(full_name)
                        # Return full_name only; the main program performs the API calls
                        repos_to_collect.append(full_name)
                    elif full_name:
                        logger.debug(f"Skipping duplicate repository: {full_name}")

            except Exception as e:
                logger.error(f"Failed to process file {json_file}: {e}")

    logger.info(f"Collection complete, {len(repos_to_collect)} unique repositories found")
    return repos_to_collect


def _extract_full_name(repo_item: Union[str, Dict[str, Any]]) -> Optional[str]:
    """
    Extract full_name from a repository item

    Args:
        repo_item: Either a string or a dictionary

    Returns:
        full_name, or None
    """
    if isinstance(repo_item, dict):
        # Try to read full_name directly
        full_name = repo_item.get('full_name')
        if full_name:
            return full_name

        # Try to combine owner and name
        owner = None
        name = repo_item.get('name')

        if 'owner' in repo_item:
            owner_data = repo_item['owner']
            if isinstance(owner_data, dict):
                owner = owner_data.get('login')
            else:
                owner = owner_data

        if owner and name:
            return f"{owner}/{name}"

    elif isinstance(repo_item, str):
        # A string is expected to follow the owner/repo format
        if '/' in repo_item:
            return repo_item

    return None


# Compatibility function retained (calling collect directly is preferred)
def collect_with_details(module_config: Optional[Dict[str, Any]] = None, **kwargs) -> List[Dict[str, Any]]:
    """
    Kept for backward compatibility; returns the dictionary format
    Prefer the collect function and let the main program normalize the result
    """
    logger.warning("collect_with_details is deprecated, use collect instead")
    repos = collect(module_config, **kwargs)
    return [{'full_name': repo} for repo in repos]


if __name__ == '__main__':
    # Test the module
    repos = collect()
    print(f"Found {len(repos)} repositories")
    if repos:
        print("First 10:", repos[:10])