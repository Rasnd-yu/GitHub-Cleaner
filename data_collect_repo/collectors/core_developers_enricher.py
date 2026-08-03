#!/usr/bin/env python3
"""
Module that enriches an existing GitHub repository dataset with core developer information
This module can be invoked from collector.py
Uses several API strategies to obtain contributor statistics, with incremental writes
"""

import os
import json
import time
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional, Set, Tuple, Union
from datetime import datetime

# Import the core developer identification helpers
from .core_developers import process_repository, MODULE_CONFIG as BASE_CONFIG, format_quoted_list, \
    get_contributors_stats, identify_core_developers_from_stats

logger = logging.getLogger(__name__)

# Module configuration
MODULE_CONFIG = {
    **BASE_CONFIG,
    'name': 'core_developers_enricher',
    'description': 'Add core developer information to existing repositories (several API strategies supported)',
    'enabled': False,
    'source': 'GitHub Statistics API / Contributors API / Commits API',
    'api_strategy': 'contributors',  # Allowed values: auto, stats, contributors, commits
    'max_commits_to_fetch': 100,  # Maximum number of commits fetched when the commits API is used
    'enable_real_time_write': True,  # Enable incremental writes
    'enable_checkpoint': True,  # Enable checkpointing
    'checkpoint_interval': 10,  # Number of repositories between two checkpoints
    'processed_repos_file': 'processed_repos.json',  # File recording the repositories already processed
    'exclude_bots': True,  # Whether to exclude bot accounts
    'check_empty': True,  # Whether to revisit repositories whose core_developers field is empty
    'only_empty': False,  # Whether to process only the repositories whose core_developers field is empty
}


def get_contributors_via_contributors_api(full_name: str, token: Optional[str] = None) -> List[Dict]:
    """
    Fetch contributor information through the /repos/{owner}/{repo}/contributors API
    """
    url = f"https://api.github.com/repos/{full_name}/contributors"
    session = None
    try:
        from .core_developers import _create_session
        session = _create_session(token)

        params = {
            'per_page': 100,
            'anon': 'true'
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

            if 'next' not in response.links:
                break

            page += 1
            time.sleep(0.5)

        # Convert to a format similar to the stats API, guarding against missing values
        formatted_contributors = []
        for c in all_contributors:
            if c is None:
                continue

            # Make sure the author field is present
            author = {
                'login': c.get('login'),
                'html_url': c.get('html_url'),
                'avatar_url': c.get('avatar_url'),
                'type': c.get('type', 'User')
            }

            # Drop the records without any contribution count
            contributions = c.get('contributions', 0)
            if contributions <= 0:
                continue

            formatted_contributors.append({
                'author': author,
                'total': contributions
            })

        logger.info(f"Fetched the contributors of {full_name} through the contributors API, {len(formatted_contributors)} in total")
        return formatted_contributors

    except Exception as e:
        logger.error(f"Error while fetching the contributors of {full_name}: {e}")
        return []
    finally:
        if session:
            session.close()


def get_contributors_via_commits_api(full_name: str, token: Optional[str] = None, max_commits: int = 100) -> List[Dict]:
    """
    Fetch the most recent commits through the /repos/{owner}/{repo}/commits API
    """
    url = f"https://api.github.com/repos/{full_name}/commits"
    session = None
    try:
        from .core_developers import _create_session
        session = _create_session(token)

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
                if commit is None:
                    continue

                total_fetched += 1

                # Try to read the author information, tolerating every possible None value
                author = None
                if commit.get('author') and isinstance(commit['author'], dict):
                    author = commit['author']
                elif commit.get('commit') and isinstance(commit['commit'], dict):
                    author = commit['commit'].get('author')

                if author and isinstance(author, dict):
                    login = author.get('login')
                    if login:
                        contributors_map[login] = contributors_map.get(login, 0) + 1
                    else:
                        # Anonymous contributor, fall back to the name
                        name = author.get('name', 'anonymous')
                        if name:
                            contributors_map[f"anon:{name}"] = contributors_map.get(f"anon:{name}", 0) + 1

            if 'next' not in response.links or total_fetched >= max_commits:
                break

            params['page'] += 1
            time.sleep(0.5)

        # Normalize to the standard format
        formatted_contributors = []
        for login, commits in contributors_map.items():
            is_anon = login.startswith('anon:')
            clean_login = login.replace('anon:', '') if is_anon else login

            formatted_contributors.append({
                'author': {
                    'login': clean_login,
                    'html_url': None if is_anon else f"https://github.com/{clean_login}",
                    'avatar_url': None if is_anon else f"https://github.com/{clean_login}.png",
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
        if session:
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

    strategies = []

    if strategy == 'stats':
        strategies = ['stats']
    elif strategy == 'contributors':
        strategies = ['contributors']
    elif strategy == 'commits':
        strategies = ['commits']
    else:  # The auto strategy
        strategies = ['stats', 'contributors', 'commits']

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


def load_processed_repos(progress_file: Path) -> Set[str]:
    """Load the record of the repositories already processed"""
    if progress_file.exists():
        try:
            with open(progress_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data.get('processed_repos', []))
        except Exception as e:
            logger.warning(f"Failed to load the processing record: {e}")
    return set()


def save_processed_repos(progress_file: Path, processed_repos: Set[str], stats: Dict):
    """Save the record of the repositories already processed"""
    try:
        temp_file = progress_file.with_suffix('.tmp')
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump({
                'last_updated': datetime.now().isoformat(),
                'processed_repos': list(processed_repos),
                'total_processed': len(processed_repos),
                'stats': stats
            }, f, ensure_ascii=False, indent=2)
        temp_file.replace(progress_file)
        return True
    except Exception as e:
        logger.error(f"Failed to save the processing record: {e}")
        return False


def should_process_repo(repo: Dict[str, Any], config: Dict[str, Any],
                        processed_repos: Set[str]) -> bool:
    """
    Decide whether the repository should be processed

    Args:
        repo: Repository dictionary
        config: Configuration
        processed_repos: Set of the repositories already processed

    Returns:
        True when the repository must be processed, False otherwise
    """
    full_name = repo.get('full_name', 'unknown')
    output_field = config.get('output_field', 'core_developers')
    force = config.get('force', False)
    check_empty = config.get('check_empty', False)
    only_empty = config.get('only_empty', False)

    # Force mode: process everything
    if force:
        return True

    # Check whether the repository is in the processed list
    if full_name in processed_repos:
        # When the empty-field check is enabled and the field is empty, process it again
        if check_empty:
            core_developers = repo.get(output_field)
            if not core_developers or core_developers == "":
                logger.info(f"Repository {full_name} has an empty {output_field} field, reprocessing")
                return True
        return False

    # Empty-field-only mode
    if only_empty:
        core_developers = repo.get(output_field)
        if not core_developers or core_developers == "":
            return True
        return False

    # Normal mode: process everything not yet processed
    return True


def process_repository_real_time(repo: Dict[str, Any], config: Dict[str, Any]) -> Tuple[bool, bool]:
    """
    Process a single repository, with incremental writes

    Returns:
        (success, updated) - whether it succeeded, and whether anything was updated
    """
    full_name = repo.get('full_name')
    if not full_name:
        logger.warning("The repository lacks a full_name field, skipping")
        return False, False

    output_field = config.get('output_field', 'core_developers')
    exclude_bots = config.get('exclude_bots', True)
    check_empty = config.get('check_empty', False)

    # Check whether core developer information is already present
    if not config.get('force', False):
        if output_field in repo:
            # When the empty-field check is enabled and the field is not empty, skip it
            if not check_empty:
                if repo.get(output_field):
                    logger.debug(f"Repository {full_name} already carries core developer information, skipping")
                    return True, False
            else:
                # In empty-field check mode, only repositories with an empty field are processed
                if repo.get(output_field):
                    logger.debug(f"Repository {full_name} already carries non-empty core developer information, skipping")
                    return True, False

    # Fetch contributor information with the degrading strategy
    contributors = get_contributors_with_fallback(full_name, config)

    if not contributors:
        logger.warning(f"No contributor information was obtained for repository {full_name}")
        repo[output_field] = ""
        # Report failure without marking the repository as processed
        return False, False

    # Identify the core developers
    min_threshold = config.get('min_commit_threshold', 5)
    cumulative_threshold = config.get('cumulative_threshold', 80)

    core_developers = identify_core_developers_from_stats(
        contributors,
        min_threshold=min_threshold,
        cumulative_threshold=cumulative_threshold,
        exclude_bots=exclude_bots
    )

    if core_developers:
        names = []
        for dev in core_developers:
            if dev.get('login'):
                names.append(dev['login'])

        repo[output_field] = format_quoted_list(names)
        logger.info(f"Repository {full_name}: {len(core_developers)} core developers identified: {names}")
    else:
        repo[output_field] = ""
        logger.info(f"Repository {full_name}: no core developer identified")

    return True, True  # Succeeded and updated


def collect(module_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Collect core developer information and update the dataset (with incremental writes)
    """
    logger.info("Starting the core developer enrichment module (with incremental writes)")

    # Read the configuration
    output_file = module_config.get('output_file', 'github_repos.json')
    output_field = module_config.get('output_field', 'core_developers')
    force = module_config.get('force', False)
    check_empty = module_config.get('check_empty', False)
    only_empty = module_config.get('only_empty', False)
    enable_real_time_write = module_config.get('enable_real_time_write', True)
    enable_checkpoint = module_config.get('enable_checkpoint', True)
    checkpoint_interval = module_config.get('checkpoint_interval', 10)

    # Check whether the file exists
    if not os.path.exists(output_file):
        logger.error(f"The data file {output_file} does not exist")
        return []

    # Load the data
    try:
        with open(output_file, 'r', encoding='utf-8') as f:
            repos = json.load(f)
        logger.info(f"Loaded {len(repos)} repositories")
    except Exception as e:
        logger.error(f"Failed to load the data file: {e}")
        return []

    # In empty-field-only mode, count the empty fields first
    if only_empty:
        empty_count = sum(1 for repo in repos
                          if not repo.get(output_field) or repo.get(output_field) == "")
        logger.info(f"Found {empty_count} repositories whose {output_field} field is empty")

    # Set up the progress file
    output_path = Path(output_file)
    progress_file = output_path.parent / f"core_developers_progress.json"
    processed_repos_file = output_path.parent / module_config.get('processed_repos_file', 'processed_repos.json')

    # Load the repositories already processed
    processed_repos = load_processed_repos(processed_repos_file) if enable_checkpoint else set()

    # Statistics
    stats = {
        'total': len(repos),
        'updated': 0,
        'failed': 0,
        'skipped': 0,
        'already_processed': len(processed_repos),
        'empty_found': 0,
        'empty_fixed': 0
    }

    # When the empty-field check is enabled, count the empty fields
    if check_empty:
        stats['empty_found'] = sum(1 for repo in repos
                                   if not repo.get(output_field) or repo.get(output_field) == "")

    # Process every repository
    for i, repo in enumerate(repos, 1):
        full_name = repo.get('full_name', 'unknown')

        # Decide whether the repository must be processed
        if not should_process_repo(repo, module_config, processed_repos):
            stats['skipped'] += 1
            continue

        try:
            success, updated = process_repository_real_time(repo, module_config)

            if success:
                if updated:
                    stats['updated'] += 1
                    processed_repos.add(full_name)

                    # Count the repairs of empty fields
                    if check_empty:
                        stats['empty_fixed'] += 1
                else:
                    stats['skipped'] += 1
                    processed_repos.add(full_name)
            else:
                stats['failed'] += 1

            # Incremental write
            if enable_real_time_write and updated:
                # Persist the current repository
                temp_file = output_path.with_suffix('.tmp')
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(repos, f, ensure_ascii=False, indent=2)
                temp_file.replace(output_path)
                logger.debug(f"Incrementally wrote the update of repository {full_name}")

            # Save a checkpoint
            if enable_checkpoint and i % checkpoint_interval == 0:
                save_processed_repos(processed_repos_file, processed_repos, stats)

                # Persist the data as well
                temp_file = output_path.with_suffix('.tmp')
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(repos, f, ensure_ascii=False, indent=2)
                temp_file.replace(output_path)

                logger.info(
                    f"Checkpoint: {i}/{len(repos)} updated: {stats['updated']} failed: {stats['failed']} skipped: {stats['skipped']}")

            # Stay clear of the API rate limit
            time.sleep(module_config.get('delay_between_repos', 0.5))

        except Exception as e:
            logger.error(f"Error while processing repository {full_name}: {e}")
            stats['failed'] += 1

    # Final save
    try:
        temp_file = output_path.with_suffix('.tmp')
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(repos, f, ensure_ascii=False, indent=2)
        temp_file.replace(output_path)
        logger.info(f"Final result saved to {output_path}")
    except Exception as e:
        logger.error(f"Failed to save the data file: {e}")

    # Remove the progress file (only once everything has been processed)
    if not force and not check_empty and not only_empty:
        if processed_repos_file.exists():
            processed_repos_file.unlink()

    # Print the summary
    logger.info("=" * 60)
    logger.info("Core developer enrichment complete")
    logger.info(f"Total repositories: {stats['total']}")
    logger.info(f"Updated: {stats['updated']}")
    logger.info(f"Failed: {stats['failed']}")
    logger.info(f"Skipped: {stats['skipped']}")

    if check_empty:
        logger.info(f"Empty-field check mode: {stats['empty_found']} empty fields found, {stats['empty_fixed']} repaired")

    if only_empty:
        logger.info(f"Empty-field-only mode: {stats['updated']} repositories with an empty field processed")

    logger.info("=" * 60)

    return []


# A standalone run function is provided for compatibility
def run(output_file: str = "github_repos.json", token: Optional[str] = None,
        force: bool = False, check_empty: bool = False, only_empty: bool = False):
    """
    Standalone run function
    """
    config = MODULE_CONFIG.copy()
    config['token'] = token
    config['force'] = force
    config['check_empty'] = check_empty
    config['only_empty'] = only_empty
    config['output_file'] = output_file

    return collect(config)