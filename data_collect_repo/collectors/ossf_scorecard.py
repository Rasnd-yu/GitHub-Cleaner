#!/usr/bin/env python3
"""
OSSF Scorecard dataset collection module
Extracts repository information from the JSON files in the local OpenSSF-scorecard folder.
Source identifier: "ossf-scorecard-2026.03.16"
"""

import logging
import json
from typing import List, Dict, Any, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# Module configuration
MODULE_CONFIG = {
    'enabled': False,
    'force': False,
    'source_prefix': 'ossf-scorecard',
    'token': 'xxx',
    'max_repos_to_collect': 12000,  # Maximum number of repositories to collect, 0 means unlimited
    'data_folder': 'OpenSSF-scorecard',  # Data folder name (sibling of collector.py)
    'data_files': [
        'scorecard_export-20260316-000000000000.json',
        'scorecard_export-20260316-000000000001.json',
        'scorecard_export-20260316-000000000002.json'
    ],
    'source': 'ossf-scorecard-2026.03.16',  # Data source identifier
}


def collect(module_config: Optional[Dict[str, Any]] = None, **kwargs) -> List[Dict[str, Any]]:
    """
    Collect repository information from the local OpenSSF-scorecard data files.
    Returns a list of dictionaries carrying the source, each with a full_name and a source field.

    Args:
        module_config: Module configuration
        **kwargs: Backward compatibility with the old calling convention

    Returns:
        List of repository dictionaries, each shaped as: {"full_name": "owner/repo", "source": "ossf-scorecard-2026.03.16"}
    """
    logger.info("Collecting repositories from the OSSF Scorecard data files...")

    # Merge the configuration
    config = MODULE_CONFIG.copy()
    if module_config:
        config.update(module_config)

    max_repos = config.get('max_repos_to_collect', 10000)
    data_folder = config.get('data_folder', 'OpenSSF-scorecard')
    data_files = config.get('data_files', [])
    source = config.get('source', 'ossf-scorecard-2026.03.16')

    # Resolve the data folder path
    data_path = Path(data_folder)
    if not data_path.exists():
        # Fall back to the directory that contains collector.py
        data_path = Path(__file__).parent.parent / data_folder

    # Check whether the data folder exists
    if not data_path.exists() or not data_path.is_dir():
        logger.error(f"Data folder does not exist or is not a directory: {data_path}")
        return []

    logger.info(f"Using data folder: {data_path}")

    # Extract repositories from every configured JSON file
    all_repos = []
    for file_name in data_files:
        file_path = data_path / file_name
        if not file_path.exists():
            logger.warning(f"Data file does not exist, skipping: {file_path}")
            continue

        logger.info(f"Processing file: {file_name}")
        repos_from_file = _extract_repos_from_json(file_path)
        logger.info(f"File {file_name} yielded {len(repos_from_file)} repositories")
        all_repos.extend(repos_from_file)

    if not all_repos:
        logger.error("No repository could be extracted from the data files")
        return []

    # Deduplicate (preserving the original order)
    unique_repos = []
    seen = set()
    for repo in all_repos:
        if repo not in seen:
            seen.add(repo)
            unique_repos.append(repo)

    logger.info(f"Before deduplication: {len(all_repos)} repositories, after: {len(unique_repos)} repositories")

    # Cap the number of repositories
    if max_repos > 0 and len(unique_repos) > max_repos:
        logger.info(f"Limiting the collection to {max_repos} repositories (original count: {len(unique_repos)})")
        unique_repos = unique_repos[:max_repos]

    # Build the result
    result = []
    for repo_full_name in unique_repos:
        result.append({
            "full_name": repo_full_name,
            "source": source
        })

    logger.info(f"Collection complete, returning {len(result)} repository records")

    return result


def _extract_repos_from_json(file_path: Path) -> List[str]:
    """
    Extract the full_name of every repository from a JSON file.
    The file holds one JSON object per line, each containing a repo.name field (format: github.com/owner/repo)

    Args:
        file_path: Path to the JSON file

    Returns:
        List of repository full names (format: owner/repo)
    """
    repos = []
    line_count = 0
    error_count = 0

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue

                line_count += 1

                try:
                    data = json.loads(line)

                    # Extract the repo.name field
                    repo_name = data.get('repo', {}).get('name')

                    if not repo_name:
                        logger.debug(f"Line {line_num} lacks the repo.name field, skipping")
                        error_count += 1
                        continue

                    # Convert the format: github.com/owner/repo -> owner/repo
                    if repo_name.startswith('github.com/'):
                        full_name = repo_name.replace('github.com/', '')
                        repos.append(full_name)
                    else:
                        # If the standard github.com prefix is absent, use the value as it is
                        logger.debug(f"Unexpected repository name format on line {line_num}: {repo_name}")
                        error_count += 1

                    # Report progress periodically
                    if line_count % 100000 == 0:
                        logger.debug(f"Processed {line_count} lines, extracted {len(repos)} repositories")

                except json.JSONDecodeError as e:
                    logger.debug(f"JSON parsing failed on line {line_num}: {e}")
                    error_count += 1
                except Exception as e:
                    logger.debug(f"Error while processing line {line_num}: {e}")
                    error_count += 1

        logger.info(f"Finished file {file_path.name}, processed {line_count} lines, "
                    f"extracted {len(repos)} repositories, {error_count} errors")

        return repos

    except Exception as e:
        logger.error(f"Failed to read the JSON file {file_path}: {e}")
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