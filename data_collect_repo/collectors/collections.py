#!/usr/bin/env python3
"""
GitHub Collections (official curated lists) collection module
Reads the repositories of the official collections from the local collections directory, keeping the top 10 of each.
Source identifier: "GitHub Collections_<collection name>_top 10"
"""

import logging
from typing import List, Dict, Any, Optional
from pathlib import Path
import frontmatter  # Requires: pip install python-frontmatter

logger = logging.getLogger(__name__)

# Module configuration
MODULE_CONFIG = {
    'enabled': False,
    'force': False,
    'source_prefix': 'GitHub Collections',
    'token': 'xxx',
    'max_repos_per_collection': 10,
    'collections_path': 'collections',  # Path of the local collections folder
    'source': 'GitHub Collections',  # Data source identifier (overridden by the concrete collection name)
}


def collect(module_config: Optional[Dict[str, Any]] = None, **kwargs) -> List[Dict[str, Any]]:
    """
    Collect the repositories of every collection from the local collections directory.
    Returns a list of dictionaries carrying the source, each with a full_name and a source field.

    Args:
        module_config: Module configuration
        **kwargs: Backward compatibility with the old calling convention

    Returns:
        List of repository dictionaries, each shaped as: {"full_name": "owner/repo", "source": "GitHub Collections_<collection name>_top 10"}
    """
    logger.info("Collecting GitHub Collections repositories from the local collections directory...")

    # Merge the configuration
    config = MODULE_CONFIG.copy()
    if module_config:
        config.update(module_config)

    limit = config.get('max_repos_per_collection', 10)
    collections_path = config.get('collections_path', 'collections')

    # Check whether the collections directory exists
    base_path = Path(collections_path)
    if not base_path.exists() or not base_path.is_dir():
        logger.error(f"The collections directory does not exist or is not a directory: {collections_path}")
        return []

    # Discover every collection
    collections_to_process = _discover_all_collections(base_path)
    if not collections_to_process:
        logger.error("No valid collection was found under the collections directory (each collection directory must contain an index.md file)")
        return []

    logger.info(f"Found {len(collections_to_process)} collections, taking the top {limit} repositories of each")

    # Track the repositories already added, to avoid duplicates
    seen_repos = {}
    all_repos_to_collect = []

    # Iterate over every collection and read its repositories
    for i, slug in enumerate(collections_to_process):
        logger.info(f"Processing collection [{i + 1}/{len(collections_to_process)}]: {slug}")

        # Build the source identifier of this collection
        collection_source = f"GitHub Collections_{slug}_top {limit}"

        repos_in_collection = _get_repos_from_collection_local(
            base_path,
            slug,
            limit
        )

        if not repos_in_collection:
            logger.warning(f"Collection '{slug}' returned no repository data; its index.md may be malformed.")
            continue

        added_count = 0
        for repo_full_name in repos_in_collection:
            # If a repository already exists but comes from a different collection, should every source be kept? Here it is deduplicated, keeping the first occurrence
            if repo_full_name not in seen_repos:
                seen_repos[repo_full_name] = True
                # Return a dictionary carrying the source
                all_repos_to_collect.append({
                    "full_name": repo_full_name,
                    "source": collection_source
                })
                added_count += 1
            else:
                logger.debug(f"Repository {repo_full_name} already appeared in another collection, skipping")

        logger.info(f"Collection '{slug}' contributed {added_count} new repositories ({len(repos_in_collection)} retrieved in total)")

    logger.info(
        f"Collection complete, {len(collections_to_process)} collections yielded {len(all_repos_to_collect)} unique repositories")

    return all_repos_to_collect


def _discover_all_collections(base_path: Path) -> List[str]:
    """
    Discover every valid collection slug in the local collections directory.
    Each subdirectory is a collection; it is considered valid only if it contains an index.md file.
    """
    slugs = []

    try:
        # List every subdirectory of the collections directory
        all_items = sorted(base_path.iterdir())
        dirs = [item for item in all_items if item.is_dir()]

        for dir_path in dirs:
            slug = dir_path.name
            # Check whether the directory contains an index.md file
            index_file = dir_path / "index.md"
            if index_file.exists() and index_file.is_file():
                slugs.append(slug)
                logger.debug(f"Found collection: {slug}")

        logger.info(f"Parsed {len(slugs)} collections from the local directory")
        return slugs

    except Exception as e:
        logger.error(f"Failed to parse the local collections directory: {e}")
        return []


def _get_repos_from_collection_local(base_path: Path, slug: str, limit: int) -> List[str]:
    """
    Parse the repository list from the local index.md file of a given collection.
    Example of the file format:
    ---
    items:
     - tensorflow/models
     - Theano/Theano
     - BVLC/caffe
    ---
    """
    repos = []

    try:
        collection_dir = base_path / slug
        index_file = collection_dir / "index.md"

        if not index_file.exists():
            logger.warning(f"The index.md file of collection '{slug}' does not exist: {index_file}")
            return []

        logger.debug(f"Reading file: {index_file}")

        # Parse the Markdown frontmatter with python-frontmatter
        try:
            with open(index_file, 'r', encoding='utf-8') as f:
                post = frontmatter.load(f)

            # Read items from the frontmatter
            items = post.get('items', [])

            if not items:
                logger.warning(f"The items of collection '{slug}' are empty or missing")
                return []

            # Extract the repository full names, respecting the limit
            for item in items[:limit]:
                if isinstance(item, str) and '/' in item:
                    # Already in owner/repo format
                    repos.append(item.strip())
                elif isinstance(item, dict):
                    # For the dictionary format, try to read full_name
                    repo_name = item.get('full_name') or item.get('name')
                    if repo_name and '/' in repo_name:
                        repos.append(repo_name.strip())
                else:
                    logger.debug(f"Skipping invalid repository entry: {item}")

            logger.debug(f"Collection '{slug}' yielded {len(repos)} repositories")
            return repos[:limit]

        except ImportError:
            logger.error("The python-frontmatter module is not installed, run: pip install python-frontmatter")
            # Fallback: parse the frontmatter manually
            return _parse_frontmatter_manual(index_file, limit)

    except Exception as e:
        logger.error(f"Failed to parse the index.md file of collection '{slug}': {e}")
        return []


def _parse_frontmatter_manual(file_path: Path, limit: int) -> List[str]:
    """
    Fallback that parses the frontmatter manually (used when python-frontmatter is not installed)
    """
    repos = []

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Extract the frontmatter section (the content between the --- markers)
        if content.startswith('---'):
            parts = content.split('---', 2)
            if len(parts) >= 3:
                frontmatter_text = parts[1]

                # Naive parsing of the items list
                in_items = False
                for line in frontmatter_text.split('\n'):
                    line = line.strip()

                    if line == 'items:':
                        in_items = True
                        continue

                    if in_items:
                        if line.startswith('- '):
                            # Extract the repository name
                            repo = line[2:].strip()
                            if '/' in repo and len(repos) < limit:
                                repos.append(repo)
                        elif not line.startswith(' -') and line and not line.startswith('#'):
                            # End of the items list
                            in_items = False

    except Exception as e:
        logger.error(f"Manual frontmatter parsing failed: {e}")

    return repos


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