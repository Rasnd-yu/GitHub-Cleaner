#!/usr/bin/env python3
"""
OSSF Criticality Score dataset collection module
Randomly samples repositories from the local file 2025.07.25_010355_all.txt, taking the first 10,000 of each.
Source identifier: "ossf-criticality-score-2025.07.25"
"""

import logging
import random
import csv
from typing import List, Dict, Any, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# Module configuration
MODULE_CONFIG = {
    'enabled': False,
    'force': False,
    'source_prefix': 'ossf-criticality-score',
    'token': 'xxx',
    'max_repos_to_collect': 1000,
    'data_file': '2025.07.25_010355_all.txt',  # Data file name (same directory as collector.py)
    'source': 'ossf-criticality-score-2025.07.25',  # Data source identifier
}


def collect(module_config: Optional[Dict[str, Any]] = None, **kwargs) -> List[Dict[str, Any]]:
    """
    Collect repository information from the local data file.
    Returns a list of dictionaries carrying the source, each with a full_name and a source field.

    Args:
        module_config: Module configuration
        **kwargs: Backward compatibility with the old calling convention

    Returns:
        List of repository dictionaries, each shaped as: {"full_name": "owner/repo", "source": "ossf-criticality-score-2025.07.25"}
    """
    logger.info("Collecting repositories from the OSSF Criticality Score data file...")

    # Merge the configuration
    config = MODULE_CONFIG.copy()
    if module_config:
        config.update(module_config)

    max_repos = config.get('max_repos_to_collect', 10000)
    data_file = config.get('data_file', '2025.07.25_010355_all.txt')
    source = config.get('source', 'ossf-criticality-score-2025.07.25')

    # Resolve the data file path (an absolute path wins, otherwise the current working directory is used)
    data_path = Path(data_file)
    if not data_path.exists():
        # Fall back to the directory that contains collector.py
        data_path = Path(__file__).parent.parent / data_file

    # Check whether the data file exists
    if not data_path.exists() or not data_path.is_file():
        logger.error(f"Data file does not exist or is not a file: {data_path}")
        return []

    logger.info(f"Using data file: {data_path}")

    # Extract every repository from the file
    all_repos = _extract_repos_from_file(data_path)

    if not all_repos:
        logger.error("No repository could be extracted from the data file")
        return []

    logger.info(f"Extracted {len(all_repos)} repositories from the data file")

    # Randomly sample the configured number of repositories
    sample_size = min(max_repos, len(all_repos))
    sampled_repos = random.sample(all_repos, sample_size)

    logger.info(f"Randomly sampled {sample_size} repositories")

    # Build the result
    result = []
    for repo_full_name in sampled_repos:
        result.append({
            "full_name": repo_full_name,
            "source": source
        })

    logger.info(f"Collection complete, returning {len(result)} repository records")

    return result


def _extract_repos_from_file(file_path: Path) -> List[str]:
    """
    Extract the full_name of every repository from the data file.
    The file is CSV with a header row; the first field of each line is repo.url (format: https://github.com/owner/repo)

    Args:
        file_path: Path to the data file

    Returns:
        List of repository full names
    """
    repos = []

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            # Read the header row
            reader = csv.reader(f)
            try:
                header = next(reader)
                logger.debug(f"File header row: {header}")
            except StopIteration:
                logger.error("The file is empty")
                return []

            # Determine the index of the repo.url column
            url_col_index = 0  # The first column is assumed to be repo.url
            for i, col_name in enumerate(header):
                if col_name.strip() == 'repo.url':
                    url_col_index = i
                    break

            # Process the file line by line
            line_count = 0
            for row in reader:
                line_count += 1
                if len(row) <= url_col_index:
                    logger.debug(f"Malformed line {line_count + 1}, skipping")
                    continue

                repo_url = row[url_col_index].strip()
                if not repo_url:
                    continue

                # Extract owner/repo from the URL
                # Format: https://github.com/owner/repo
                if repo_url.startswith('https://github.com/'):
                    parts = repo_url.replace('https://github.com/', '').split('/')
                    if len(parts) >= 2:
                        owner = parts[0]
                        repo = parts[1]
                        # Strip a possible .git suffix
                        if repo.endswith('.git'):
                            repo = repo[:-4]
                        full_name = f"{owner}/{repo}"
                        repos.append(full_name)

                if line_count % 100000 == 0:
                    logger.debug(f"Processed {line_count} lines, extracted {len(repos)} repositories")

        logger.info(f"File processed, {line_count} lines read, {len(repos)} repositories extracted")
        return repos

    except Exception as e:
        logger.error(f"Failed to read the data file: {e}")
        return []


def _parse_repo_url_manual(file_path: Path, limit: int) -> List[str]:
    """
    Fallback that parses repository URLs manually (used by the simple method)
    """
    repos = []

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            # Skip the header row
            next(f)

            for line in f:
                if len(repos) >= limit:
                    break

                # Split naively on commas
                parts = line.strip().split(',')
                if parts:
                    repo_url = parts[0].strip()
                    if repo_url.startswith('https://github.com/'):
                        full_name = repo_url.replace('https://github.com/', '')
                        # Strip a possible .git suffix
                        if full_name.endswith('.git'):
                            full_name = full_name[:-4]
                        repos.append(full_name)

        return repos

    except Exception as e:
        logger.error(f"Manual file parsing failed: {e}")
        return []


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
            print(f"{i + 1}. {repo['full_name']} (source: {repo['source']})")