#!/usr/bin/env python3
"""
Accompanying repository collection module (token-separation optimized version)
Internal design: two tokens are used for searching, one token is reserved for collector.py writes
"""

import logging
import json
import time
import concurrent.futures
import random
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from queue import Queue
from threading import Lock
from datetime import datetime
import hashlib

logger = logging.getLogger(__name__)

# Module configuration - the original structure is preserved
MODULE_CONFIG = {
    'enabled': False,
    'force': False,
    'source_prefix': 'Accompanying repository',
    'tokens': [
        'xxx',
        'xxx',
        'xxx'
    ],
    'max_repos_per_query': 5,
    'sample_size': 1000,
    'max_workers': 1,
    'hot_repos_file': '../github_repos_hot.json',
    'search_delay': 1.0,
    'request_timeout': 15,
    # Internal configuration: which token is used for writing (index 2, i.e. the third token)
    '_internal': {
        'write_token_index': 2,  # The third token is dedicated to writing
        'search_token_indices': [0, 1],  # The first two tokens are used for searching
        'enable_cache': True,
        'cache_file': 'search_cache.json',
        'batch_size': 10,
        'batch_delay': 30,
        'max_search_retries': 3
    }
}


class TokenSeparator:
    """
    Token separator
    Splits the three tokens into two search-only tokens and one write-only token
    """

    def __init__(self, tokens: List[str], write_index: int = 2, search_indices: List[int] = [0, 1]):
        """
        Initialize the token separator

        Args:
            tokens: List of every token
            write_index: Index of the token used for writing
            search_indices: Indices of the tokens used for searching
        """
        self.all_tokens = tokens
        self.write_token = tokens[write_index] if write_index < len(tokens) else None
        self.search_tokens = [tokens[i] for i in search_indices if i < len(tokens)]

        # State management of the search tokens
        self.search_token_queue = Queue()
        self.search_token_stats = {}
        self.lock = Lock()

        # Initialize the search token queue
        for token in self.search_tokens:
            self.search_token_stats[token] = {
                'used': 0,
                'rate_limited': False,
                'rate_limit_time': 0,
                'last_used': 0
            }
            self.search_token_queue.put(token)

        logger.info(f"Token separation initialized:")
        logger.info(f"  - Search-only tokens: {len(self.search_tokens)}")
        logger.info(f"  - Write-only token: {'configured' if self.write_token else 'not configured'}")

    def get_search_token(self) -> Optional[str]:
        """Get an available search token"""
        with self.lock:
            current_time = time.time()

            # Check and reset rate-limited search tokens
            for token in self.search_tokens:
                stats = self.search_token_stats[token]
                if stats['rate_limited']:
                    if current_time - stats['rate_limit_time'] > 60:
                        stats['rate_limited'] = False
                        self.search_token_queue.put(token)
                        logger.info(f"Search token {token[:8]}... is no longer rate limited")

            # Get a search token
            try:
                token = self.search_token_queue.get_nowait()
                stats = self.search_token_stats[token]
                stats['last_used'] = current_time
                stats['used'] += 1
                return token
            except:
                return None

    def return_search_token(self, token: str, rate_limited: bool = False):
        """Return a search token to the pool"""
        with self.lock:
            if rate_limited:
                self.search_token_stats[token]['rate_limited'] = True
                self.search_token_stats[token]['rate_limit_time'] = time.time()
                logger.warning(f"Search token {token[:8]}... is rate limited, suspended for 60 seconds")
            else:
                self.search_token_queue.put(token)

    def get_write_token(self) -> Optional[str]:
        """Get the write-only token (checking whether it is available)"""
        if not self.write_token:
            return None

        # Simply check whether the write token is available
        # No elaborate rate-limit management here; collector.py handles that
        return self.write_token

    def get_stats(self) -> Dict:
        """Get the statistics"""
        stats = {
            'search_tokens': dict(self.search_token_stats),
            'write_token': self.write_token[:8] + '...' if self.write_token else None
        }
        return stats


class SearchCache:
    """Search cache"""

    def __init__(self, cache_file: str = 'search_cache.json'):
        self.cache_file = Path(__file__).parent / cache_file
        self.cache = self._load_cache()

    def _load_cache(self) -> Dict:
        if self.cache_file.exists():
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def _save_cache(self):
        try:
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save the cache: {e}")

    def get(self, repo_name: str) -> Optional[List[str]]:
        """Get a cached search result"""
        key = hashlib.md5(repo_name.encode()).hexdigest()
        if key in self.cache:
            cache_time = datetime.fromisoformat(self.cache[key]['timestamp'])
            if (datetime.now() - cache_time).days < 7:
                logger.debug(f"Using the cache: {repo_name}")
                return self.cache[key]['results']
        return None

    def set(self, repo_name: str, results: List[str]):
        """Set a cache entry"""
        key = hashlib.md5(repo_name.encode()).hexdigest()
        self.cache[key] = {
            'timestamp': datetime.now().isoformat(),
            'results': results
        }
        # Cap the cache size
        if len(self.cache) > 1000:
            sorted_items = sorted(self.cache.items(), key=lambda x: x[1]['timestamp'])
            for k, _ in sorted_items[:100]:
                del self.cache[k]

        self._save_cache()


def create_search_session(token: str) -> requests.Session:
    """Create a search-only session"""
    session = requests.Session()

    retry_strategy = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.headers.update({
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'GitHub-Dataset-Collector',
        'Authorization': f'token {token}'
    })

    return session


def search_related_repos(
        repo_name: str,
        token: str,
        max_results: int = 5,
        cache: Optional[SearchCache] = None,
        max_retries: int = 3
) -> Tuple[List[str], Dict[str, Any]]:
    """
    Search for accompanying repositories (using a search token)
    With a retry strategy
    """
    # Check the cache
    if cache:
        cached_results = cache.get(repo_name)
        if cached_results is not None:
            return cached_results, {'from_cache': True}

    parts = repo_name.split('/')
    if len(parts) != 2:
        return [], {'error': 'invalid_format'}

    repo = parts[1]

    for attempt in range(max_retries):
        params = {
            'q': repo,
            'per_page': min(max_results + 5, 30),
            'sort': 'best match',
        }

        session = create_search_session(token)

        try:
            response = session.get(
                'https://api.github.com/search/repositories',
                params=params,
                timeout=15
            )

            # Check the API rate limit
            remaining = response.headers.get('X-RateLimit-Remaining', '0')
            if remaining == '0':
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 10
                    logger.warning(f"Search rate limited, retrying in {wait_time} seconds ({attempt + 1}/{max_retries})")
                    time.sleep(wait_time)
                    continue
                return [], {'error': 'rate_limited'}

            if response.status_code == 403:
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 10
                    time.sleep(wait_time)
                    continue
                return [], {'error': 'rate_limited'}
            elif response.status_code != 200:
                return [], {'error': f'http_{response.status_code}'}

            data = response.json()
            items = data.get('items', [])

            related_repos = []
            for item in items:
                full_name = item.get('full_name')
                if full_name and full_name != repo_name:
                    related_repos.append(full_name)
                    if len(related_repos) >= max_results:
                        break

            # Store in the cache
            if cache and related_repos:
                cache.set(repo_name, related_repos)

            return related_repos[:max_results], {}

        except Exception as e:
            logger.error(f"Search failed for {repo_name}: {e}")
            if attempt < max_retries - 1:
                time.sleep((attempt + 1) * 5)
            else:
                return [], {'error': str(e)}
        finally:
            session.close()

    return [], {'error': 'max_retries_exceeded'}


def load_and_sample_hot_repos(file_path: str, sample_size: int) -> List[Dict]:
    """Load and sample the popular repositories"""
    try:
        current_dir = Path(__file__).parent
        full_path = current_dir / file_path

        if not full_path.exists():
            logger.error(f"The popular repositories file does not exist: {full_path}")
            return []

        with open(full_path, 'r', encoding='utf-8') as f:
            all_repos = json.load(f)

        total_repos = len(all_repos)
        logger.info(f"The popular repositories file contains {total_repos} repositories")

        if total_repos <= sample_size:
            sampled = all_repos
        else:
            sampled = random.sample(all_repos, sample_size)

        logger.info(f"Randomly sampled {len(sampled)} repositories")
        return sampled

    except Exception as e:
        logger.error(f"Failed to load the popular repositories: {e}")
        return []


def collect(module_config: Optional[Dict[str, Any]] = None, **kwargs) -> List[Dict[str, Any]]:
    """
    Collect accompanying repositories from the popular repositories (token-separation version)

    Returns:jian
        List of repository dictionaries, each shaped as:
        {"full_name": "owner/repo", "source": "Accompanying repository_<user name/repository name>_top5"}
        Note: only repository names are returned here, without any token information
    """
    logger.info("=" * 60)
    logger.info("Collecting accompanying repositories from the popular repositories (token-separation mode)")
    logger.info("=" * 60)

    # Merge the configuration
    config = MODULE_CONFIG.copy()
    if module_config:
        config.update(module_config)

    # Read the internal configuration
    internal_config = config.get('_internal', {})
    write_token_index = internal_config.get('write_token_index', 2)
    search_token_indices = internal_config.get('search_token_indices', [0, 1])

    # Initialize the token separator
    tokens = config.get('tokens', [])
    if len(tokens) < 3:
        logger.error(f"At least three tokens are required, only {len(tokens)} are available")
        return []

    token_separator = TokenSeparator(tokens, write_token_index, search_token_indices)

    # Parameter setup
    max_results = config.get('max_repos_per_query', 5)
    sample_size = config.get('sample_size', 20)
    hot_repos_file = config.get('hot_repos_file')
    search_delay = config.get('search_delay', 1.0)
    batch_size = internal_config.get('batch_size', 10)
    batch_delay = internal_config.get('batch_delay', 30)
    max_retries = internal_config.get('max_search_retries', 3)

    logger.info(f"Configuration:")
    logger.info(f"  - Target accompanying repositories per repository: {max_results}")
    logger.info(f"  - Random sample size: {sample_size}")
    logger.info(f"  - Search-only tokens: {len(search_token_indices)}")
    logger.info(f"  - Write-only token index: {write_token_index}")
    logger.info(f"  - Search delay: {search_delay}s")

    # Load and randomly sample the popular repositories
    sampled_repos = load_and_sample_hot_repos(hot_repos_file, sample_size)
    if not sampled_repos:
        logger.error("No popular repository could be loaded")
        return []

    logger.info(f"About to process {len(sampled_repos)} randomly sampled popular repositories")

    # Initialize the cache
    cache = SearchCache(internal_config.get('cache_file', 'search_cache.json'))

    # Collect every accompanying repository
    all_related_repos = []
    processed_count = 0
    rate_limited_count = 0
    success_count = 0

    # Process in batches
    for batch_start in range(0, len(sampled_repos), batch_size):
        batch_end = min(batch_start + batch_size, len(sampled_repos))
        batch = sampled_repos[batch_start:batch_end]

        batch_num = batch_start // batch_size + 1
        total_batches = (len(sampled_repos) + batch_size - 1) // batch_size
        logger.info(f"Processing batch {batch_num}/{total_batches} (repositories {batch_start + 1}-{batch_end})")

        # Process the current batch
        for repo_data in batch:
            repo_full_name = repo_data.get('full_name')
            if not repo_full_name:
                continue

            # Get a search token
            token = token_separator.get_search_token()
            if not token:
                logger.warning("No search token available, waiting 30 seconds...")
                time.sleep(30)
                token = token_separator.get_search_token()
                if not token:
                    logger.error("Still no search token available, skipping the remaining work")
                    break

            # Search for accompanying repositories
            related_repos, metadata = search_related_repos(
                repo_full_name,
                token,
                max_results,
                cache,
                max_retries
            )

            # Check whether the request was rate limited
            rate_limited = metadata.get('error') == 'rate_limited'

            if rate_limited:
                rate_limited_count += 1
                token_separator.return_search_token(token, rate_limited=True)
                logger.warning(f"Rate limited while searching {repo_full_name}")
                time.sleep(10)  # Wait a little longer after being rate limited
            else:
                token_separator.return_search_token(token, rate_limited=False)

                if related_repos:
                    success_count += 1
                    source = f"Accompanying repository_{repo_full_name}_top{max_results}"
                    for related_repo in related_repos:
                        all_related_repos.append({
                            'full_name': related_repo,
                            'source': source
                        })

                    logger.debug(f"{repo_full_name} yielded {len(related_repos)} accompanying repositories")
                else:
                    logger.debug(f"{repo_full_name} has no accompanying repository")

            processed_count += 1

            # Progress report
            if processed_count % 10 == 0:
                logger.info(f"Progress: {processed_count}/{len(sampled_repos)} " +
                            f"(succeeded: {success_count}, rate limited: {rate_limited_count})")

            # Search delay
            time.sleep(search_delay)

        # Delay between batches
        if batch_end < len(sampled_repos):
            logger.info(f"Batch complete, waiting {batch_delay} seconds before continuing...")
            time.sleep(batch_delay)

    # Deduplicate
    unique_repos = []
    seen = set()
    for repo in all_related_repos:
        if repo['full_name'] not in seen:
            seen.add(repo['full_name'])
            unique_repos.append(repo)

    # Report the statistics
    logger.info("=" * 60)
    logger.info("Processing statistics:")
    logger.info(f"  - Repositories processed: {processed_count}")
    logger.info(f"  - Repositories with accompanying repositories found: {success_count}")
    logger.info(f"  - Rate-limit events: {rate_limited_count}")
    logger.info(f"  - Accompanying repositories collected: {len(all_related_repos)}")
    logger.info(f"  - Unique accompanying repositories: {len(unique_repos)}")

    # Report the token usage statistics
    stats = token_separator.get_stats()
    logger.info(f"\nToken usage statistics:")
    for token_key, token_stats in stats['search_tokens'].items():
        logger.info(f"  - Search token {token_key[:8]}...: used {token_stats['used']} times")
    logger.info(f"  - Write token: {stats['write_token']}")
    logger.info("=" * 60)

    # Important: only the search results are returned here, without any token information
    # collector.py uses its own token to fetch the detailed repository information
    return unique_repos


if __name__ == '__main__':
    # Configure the log level
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # Run the test
    test_config = MODULE_CONFIG.copy()
    test_config['sample_size'] = 5  # Only process five entries during the test
    test_config['search_delay'] = 2

    repos = collect(test_config)

    print(f"\nTest complete, {len(repos)} accompanying repositories collected")
    if repos:
        print("\nExamples:")
        for i, repo in enumerate(repos[:5]):
            print(f"  {i + 1}. {repo['full_name']} (source: {repo['source']})")