"""
Core keyword stuffing detector
Uses the BM25 algorithm to measure how well repository keywords match the README content
"""

import re
import json
import base64
import time
import logging
import requests
import threading
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime
from collections import OrderedDict
from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)


@dataclass
class KeywordStuffingEvidence:
    """Evidence of keyword stuffing detection"""
    repo_full_name: str
    total_keywords: int
    low_score_keywords_count: int
    low_score_keywords: List[str]
    avg_score: float
    min_score: float
    max_score: float
    is_abuse: bool
    details: Dict[str, Any]


class LRUCache:
    """LRU cache with a bounded memory size"""

    def __init__(self, max_size: int = 1000):
        """
        Initialize the LRU cache

        Args:
            max_size: Maximum number of cache entries
        """
        self.cache = OrderedDict()
        self.max_size = max_size
        self.lock = threading.Lock()

    def get(self, key: str) -> Optional[str]:
        """Get a cache entry, moving it to the end on a hit"""
        with self.lock:
            if key not in self.cache:
                return None
            # Move to the end (most recently used)
            self.cache.move_to_end(key)
            return self.cache[key]

    def set(self, key: str, value: str):
        """Set a cache entry"""
        with self.lock:
            if key in self.cache:
                # Update and move to the end
                self.cache[key] = value
                self.cache.move_to_end(key)
            else:
                # New entry
                self.cache[key] = value

                # If the maximum capacity is exceeded, remove the oldest entry
                if len(self.cache) > self.max_size:
                    oldest_key = next(iter(self.cache))
                    del self.cache[oldest_key]

    def size(self) -> int:
        """Return the cache size"""
        with self.lock:
            return len(self.cache)

    def clear(self):
        """Clear the cache"""
        with self.lock:
            self.cache.clear()


class PersistentCache:
    """Persistent cache with periodic saving and restoring"""

    def __init__(self, cache_file: str = "cache_core_keyword_stuffing.json",
                 auto_save_interval: int = 50,
                 max_memory_size: int = 1000):
        """
        Initialize the persistent cache

        Args:
            cache_file: Path to the cache file
            auto_save_interval: Auto-save interval (number of entries)
            max_memory_size: Maximum number of in-memory cache entries
        """
        self.cache_file = cache_file
        self.auto_save_interval = auto_save_interval
        self.max_memory_size = max_memory_size

        # In-memory LRU cache
        self.memory_cache = LRUCache(max_size=max_memory_size)

        # Disk cache (full data)
        self.disk_cache = {}

        # Statistics
        self.pending_saves = 0
        self.save_lock = threading.Lock()

        # Load the disk cache
        self._load_disk_cache()

        logger.info(f"Persistent cache initialized: disk cache={len(self.disk_cache)} entries, "
                    f"memory cache limit={max_memory_size} entries, auto-save interval={auto_save_interval} entries")

    def _load_disk_cache(self):
        """Load the cache from disk"""
        if Path(self.cache_file).exists():
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    self.disk_cache = json.load(f)
                logger.info(f"Loaded cache from disk: {len(self.disk_cache)} records")
            except Exception as e:
                logger.warning(f"Failed to load the cache: {e}")
                self.disk_cache = {}

    def _save_disk_cache(self):
        """Save to disk"""
        try:
            # Write to a temporary file to avoid corruption during writing
            temp_file = f"{self.cache_file}.tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self.disk_cache, f, ensure_ascii=False, indent=2)

            # Rename the temporary file
            Path(temp_file).rename(self.cache_file)

            logger.debug(f"Cache saved successfully: {len(self.disk_cache)} records")

        except Exception as e:
            logger.error(f"Failed to save the cache: {e}")

    def get(self, repo_full_name: str) -> Optional[str]:
        """
        Get a cache entry

        Args:
            repo_full_name: Full repository name

        Returns:
            README content, or None if it does not exist
        """
        # Check the in-memory cache first
        cached = self.memory_cache.get(repo_full_name)
        if cached is not None:
            return cached

        # Then check the disk cache
        if repo_full_name in self.disk_cache:
            content = self.disk_cache[repo_full_name]
            # Load into the in-memory cache
            self.memory_cache.set(repo_full_name, content)
            return content

        return None

    def set(self, repo_full_name: str, content: str):
        """
        Set a cache entry

        Args:
            repo_full_name: Full repository name
            content: README content
        """
        # Save to the in-memory cache
        self.memory_cache.set(repo_full_name, content)

        # Save to the disk cache
        with self.save_lock:
            self.disk_cache[repo_full_name] = content
            self.pending_saves += 1

            # The auto-save threshold has been reached, save to disk
            if self.pending_saves >= self.auto_save_interval:
                self._save_disk_cache()
                self.pending_saves = 0

    def force_save(self):
        """Force a save to disk"""
        with self.save_lock:
            if self.pending_saves > 0:
                self._save_disk_cache()
                self.pending_saves = 0

    def get_stats(self) -> Dict:
        """Get cache statistics"""
        return {
            "disk_cache_size": len(self.disk_cache),
            "memory_cache_size": self.memory_cache.size(),
            "pending_saves": self.pending_saves,
            "max_memory_size": self.max_memory_size,
            "auto_save_interval": self.auto_save_interval
        }

    def cleanup_old_entries(self, days_threshold: int = 30):
        """
        Clean up old cache entries (optional feature)

        Args:
            days_threshold: Age threshold in days; entries older than this are cleaned up
        """
        # Note: the current implementation does not store timestamps, so this feature needs to be extended
        # If needed, a timestamp can be added to the cached values
        pass


class KeywordStuffingCoreDetector:
    """
    Core keyword stuffing detector
    Uses a pre-built BM25 corpus to detect whether a repository stuffs keywords
    """

    def __init__(self, github_token: str = None, corpus_path: str = None, config: Dict = None):
        """
        Initialize the detector

        Args:
            github_token: GitHub API Token
            corpus_path: Path to the pre-built corpus file
            config: Detection configuration
        """
        # Get the default config
        default_config = self._default_config()

        # Merge in the user config
        if config:
            default_config.update(config)

        self.config = default_config
        self.github_token = github_token

        # Initialize the GitHub session
        self.session = requests.Session()
        self.session.headers.update({
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'KeywordStuffingDetector/1.0'
        })
        if self.github_token:
            self.session.headers['Authorization'] = f'token {self.github_token}'
            logger.info("GitHub Token loaded")
        else:
            logger.warning("No GitHub Token provided, README fetching will be limited")

        # BM25 model related
        self.bm25_model = None
        self.corpus_docs = []  # Stores the tokenized documents
        self.corpus_repos = []  # Stores the repository name of each document

        # Initialize the persistent cache
        cache_file = self.config.get('cache_file', 'cache_core_keyword_stuffing.json')
        auto_save_interval = self.config.get('cache_auto_save_interval', 50)
        max_memory_cache = self.config.get('max_memory_cache_size', 1000)

        self.readme_cache = PersistentCache(
            cache_file=cache_file,
            auto_save_interval=auto_save_interval,
            max_memory_size=max_memory_cache
        )

        # Load the pre-built corpus
        if corpus_path and Path(corpus_path).exists():
            self._load_corpus(corpus_path)
        else:
            logger.warning(f"Corpus file does not exist: {corpus_path}, falling back to simple mode")

    def _default_config(self) -> Dict:
        """Default configuration"""
        return {
            'score_threshold': 2.0,  # A score below this value counts as a low-score keyword
            'min_low_score_count': 5,  # Threshold for the number of low-score keywords
            'min_readme_length': 100,  # Minimum README length
            'min_corpus_size': 10,  # Minimum corpus size
            'rate_limit_delay': 1.0,  # API request delay
            'max_retries': 3,  # Maximum number of retries
            'cache_file': 'cache_core_keyword_stuffing.json',  # Path to the cache file
            'cache_auto_save_interval': 50,  # Auto-save interval (number of entries)
            'max_memory_cache_size': 500,  # Maximum number of in-memory cache entries
            'stop_words': {  # Default stop words
                'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of',
                'with', 'by', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
                'a', 'an', 'this', 'that', 'these', 'those', 'it', 'as', 'from',
                'not', 'has', 'have', 'github', 'git', 'code', 'project'
            }
        }

    def _load_corpus(self, corpus_path: str) -> None:
        """
        Load the pre-built corpus

        Args:
            corpus_path: Path to the corpus file
        """
        try:
            with open(corpus_path, 'r', encoding='utf-8') as f:
                corpus_data = json.load(f)

            self.corpus_docs = corpus_data.get('documents', [])
            self.corpus_repos = corpus_data.get('repositories', [])

            # Rebuild the BM25 model
            if self.corpus_docs and len(self.corpus_docs) >= self.config['min_corpus_size']:
                self.bm25_model = BM25Okapi(self.corpus_docs)
                logger.info(f"Corpus loaded successfully: {len(self.corpus_docs)} documents")
            else:
                logger.warning(f"Not enough corpus documents: {len(self.corpus_docs)} < {self.config['min_corpus_size']}")

        except Exception as e:
            logger.error(f"Failed to load the corpus: {e}")

    def _fetch_readme_content(self, owner: str, repo_name: str) -> Optional[str]:
        """
        Get the README content of a repository

        Args:
            owner: Repository owner
            repo_name: Repository name

        Returns:
            README content, or None if fetching failed
        """
        if not self.github_token:
            logger.warning("No GitHub Token configured, cannot fetch the README")
            return None

        for attempt in range(self.config['max_retries']):
            try:
                readme_url = f"https://api.github.com/repos/{owner}/{repo_name}/readme"
                response = self.session.get(readme_url, timeout=30)

                if response.status_code == 200:
                    data = response.json()
                    content = base64.b64decode(data['content']).decode('utf-8', errors='ignore')
                    return content
                elif response.status_code == 404:
                    logger.debug(f"Repository {owner}/{repo_name} has no README")
                    return None
                elif response.status_code == 403:
                    # Rate limiting
                    reset_time = response.headers.get('X-RateLimit-Reset')
                    if reset_time:
                        wait_time = max(int(reset_time) - time.time(), 0) + 2
                        logger.warning(f"API rate limited, waiting {wait_time:.0f} seconds...")
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.warning(f"API rate limit: {response.status_code}")
                        time.sleep(self.config['rate_limit_delay'] * (attempt + 1))
                else:
                    logger.warning(f"Failed to fetch the README: {response.status_code}")
                    if attempt < self.config['max_retries'] - 1:
                        time.sleep(self.config['rate_limit_delay'])

            except Exception as e:
                logger.error(f"Exception while fetching the README content: {e}")
                if attempt < self.config['max_retries'] - 1:
                    time.sleep(self.config['rate_limit_delay'])

        return None

    def _fetch_readme_files(self, owner: str, repo_name: str) -> List[str]:
        """
        Get all README related files in a repository

        Args:
            owner: Repository owner
            repo_name: Repository name

        Returns:
            List of README file contents
        """
        readme_contents = []
        readme_patterns = ['README.md', 'README.rst', 'README.txt', 'README', 'Readme.md']

        for pattern in readme_patterns:
            try:
                url = f"https://api.github.com/repos/{owner}/{repo_name}/contents/{pattern}"
                response = self.session.get(url, timeout=30)

                if response.status_code == 200:
                    data = response.json()
                    if 'content' in data:
                        content = base64.b64decode(data['content']).decode('utf-8', errors='ignore')
                        readme_contents.append(content)
                        logger.debug(f"Found file: {pattern}")

                time.sleep(0.2)  # Avoid API rate limits

            except Exception as e:
                continue

        return readme_contents

    def _get_readme_from_repo_data(self, repo_data: Dict) -> str:
        """
        Get the README content from the repository data
        Prefers the cache; calls the API on a cache miss

        Args:
            repo_data: Repository JSON data

        Returns:
            README content
        """
        # Get the full repository name
        full_name = repo_data.get('full_name', '')
        if not full_name:
            owner = repo_data.get('owner', {}).get('login', '')
            name = repo_data.get('name', '')
            full_name = f"{owner}/{name}"

        if not full_name or '/' not in full_name:
            logger.warning(f"Cannot determine the repository name: {full_name}")
            return ''

        # Check the cache
        cached_readme = self.readme_cache.get(full_name)
        if cached_readme:
            logger.debug(f"Using the cached README: {full_name}")
            return cached_readme

        # Cache miss, fetch the README
        owner, repo_name = full_name.split('/', 1)
        logger.info(f"Fetching the README content of repository {full_name}...")

        # Get the main README
        main_readme = self._fetch_readme_content(owner, repo_name)

        # Get the other README files (if the main README does not exist)
        all_readmes = []
        if not main_readme:
            all_readmes = self._fetch_readme_files(owner, repo_name)

        # Merge all README content
        combined_readme = main_readme or ''
        if all_readmes:
            combined_readme = combined_readme + '\n\n' + '\n\n'.join(all_readmes) if combined_readme else '\n\n'.join(
                all_readmes)

        # Save to the cache (memory and disk are handled automatically)
        if combined_readme:
            self.readme_cache.set(full_name, combined_readme)
            logger.debug(f"README cache saved: {full_name}, length={len(combined_readme)}")

        logger.debug(f"README fetch complete: length={len(combined_readme)}")

        return combined_readme

    def _clean_text(self, text: str) -> str:
        """
        Clean the text, removing Markdown, HTML tags and so on

        Args:
            text: Original text

        Returns:
            Cleaned text
        """
        if not text:
            return ""

        # Remove HTML tags
        text = re.sub(r'<.*?>', '', text)
        # Remove Markdown markers
        text = re.sub(r'[#*`~_\[\]()]', '', text)
        # Remove code block markers
        text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
        # Remove redundant whitespace
        text = re.sub(r'\s+', ' ', text)

        return text.strip()

    def _get_stop_words(self) -> set:
        """
        Safely get the stop word set

        Returns:
            Set of stop words
        """
        stop_words = self.config.get('stop_words')
        if stop_words is None:
            # Return the default stop words
            return {
                'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of',
                'with', 'by', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
                'a', 'an', 'this', 'that', 'these', 'those', 'it', 'as', 'from',
                'not', 'has', 'have', 'github', 'git', 'code', 'project'
            }
        return stop_words

    def _tokenize(self, text: str) -> List[str]:
        """
        Tokenization function

        Args:
            text: Input text

        Returns:
            List of tokens
        """
        if not text:
            return []

        # Convert to lowercase
        text = text.lower()

        # Remove punctuation but keep hyphens and underscores
        text = re.sub(r'[^\w\s-]', ' ', text)

        # Tokenize
        words = text.split()

        # Filter out stop words and short words
        filtered_words = []
        stop_words = self._get_stop_words()  # Use the safe accessor for stop words

        for word in words:
            if len(word) <= 2:
                continue

            # Handle hyphen-joined words
            if '-' in word:
                subwords = [sw for sw in word.split('-') if len(sw) > 2]
                for sw in subwords:
                    if sw not in stop_words:
                        filtered_words.append(sw)
            elif '_' in word:
                subwords = [sw for sw in word.split('_') if len(sw) > 2]
                for sw in subwords:
                    if sw not in stop_words:
                        filtered_words.append(sw)
            elif word not in stop_words:
                filtered_words.append(word)

        return filtered_words

    def extract_readme_content(self, repo_data: Dict) -> str:
        """
        Extract the README content from the repository data
        Supports fetching from the API and extracting from the data

        Args:
            repo_data: Repository JSON data

        Returns:
            Merged README content
        """
        all_content = []

        # 1. Try to get the README content from the data
        readme_content = repo_data.get('readme_content', '')
        if readme_content:
            all_content.append(readme_content)

        # 2. Try to get other README files (via the readme_files field)
        readme_files = repo_data.get('readme_files', [])
        if isinstance(readme_files, list):
            for readme_file in readme_files:
                if readme_file.get('content'):
                    all_content.append(readme_file['content'])

        # 3. If it is not in the data, fetch it from the API
        if not all_content:
            api_readme = self._get_readme_from_repo_data(repo_data)
            if api_readme:
                all_content.append(api_readme)

        # 4. Try to use the repository description as a supplement
        description = repo_data.get('description', '')
        if description:
            all_content.append(description)

        # Merge all content
        combined_content = '\n\n'.join(all_content)
        cleaned_content = self._clean_text(combined_content)

        return cleaned_content

    def extract_topics(self, repo_data: Dict) -> List[str]:
        """
        Extract keywords/topics from the repository data

        Note: only the repository's original topics are returned; nothing is extracted from other fields automatically.
        If the repository has no topics set, an empty list is returned and it will not be judged as abuse.

        Args:
            repo_data: Repository JSON data

        Returns:
            List of keywords, or an empty list if there are no topics
        """
        topics = repo_data.get('topics', [])

        # Only return the original topics, do not extract from other fields automatically
        # If topics is None, return an empty list
        if topics is None:
            return []

        return topics

    def get_repo_full_name(self, repo_data: Dict) -> str:
        """
        Get the full repository name

        Args:
            repo_data: Repository JSON data

        Returns:
            Full repository name (owner/repo)
        """
        full_name = repo_data.get('full_name', '')
        if not full_name:
            owner = repo_data.get('owner', {}).get('login', 'unknown')
            name = repo_data.get('name', 'unknown')
            full_name = f"{owner}/{name}"

        return full_name

    def get_cache_stats(self) -> Dict:
        """Get cache statistics"""
        return self.readme_cache.get_stats()

    def detect(self, repo_data: Dict) -> Tuple[bool, Optional[KeywordStuffingEvidence]]:
        """
        Detect whether the repository stuffs keywords

        Args:
            repo_data: Repository JSON data

        Returns:
            (is abuse, evidence object)
        """
        try:
            repo_full_name = self.get_repo_full_name(repo_data)

            # Extract the README content (fetched from the API automatically)
            readme_content = self.extract_readme_content(repo_data)

            # Check whether the README content exists and is sufficient
            # If there is no README content, return non-abuse directly
            if not readme_content:
                return False, KeywordStuffingEvidence(
                    repo_full_name=repo_full_name,
                    total_keywords=0,
                    low_score_keywords_count=0,
                    low_score_keywords=[],
                    avg_score=0.0,
                    min_score=0.0,
                    max_score=0.0,
                    is_abuse=False,
                    details={"error": "The repository has no README content, detection is skipped"}
                )

            # Check whether the README content is long enough
            if len(readme_content) < self.config['min_readme_length']:
                return False, KeywordStuffingEvidence(
                    repo_full_name=repo_full_name,
                    total_keywords=0,
                    low_score_keywords_count=0,
                    low_score_keywords=[],
                    avg_score=0.0,
                    min_score=0.0,
                    max_score=0.0,
                    is_abuse=False,
                    details={
                        "error": f"Not enough README content: {len(readme_content)} < {self.config['min_readme_length']}",
                        "has_readme": bool(readme_content),
                        "readme_length": len(readme_content)
                    }
                )

            # Extract keywords (only the repository's original topics are used)
            keywords = self.extract_topics(repo_data)

            # If there are no keywords/topics, return non-abuse directly
            if not keywords:
                return False, KeywordStuffingEvidence(
                    repo_full_name=repo_full_name,
                    total_keywords=0,
                    low_score_keywords_count=0,
                    low_score_keywords=[],
                    avg_score=0.0,
                    min_score=0.0,
                    max_score=0.0,
                    is_abuse=False,
                    details={"error": "The repository has no topics set, detection is skipped"}
                )

            # If no corpus has been loaded, use simple mode
            if not self.bm25_model:
                return self._detect_simple(repo_full_name, readme_content, keywords)

            # Tokenize the current document
            doc_tokens = self._tokenize(readme_content)

            # Temporarily add the current document to the corpus for the computation
            temp_docs = self.corpus_docs + [doc_tokens]
            temp_model = BM25Okapi(temp_docs)
            current_index = len(self.corpus_docs)

            # Compute the score of each keyword
            keyword_scores = []
            for keyword in keywords:
                keyword_tokens = self._tokenize(keyword.replace('-', ' ').replace('_', ' '))
                if keyword_tokens:
                    try:
                        doc_scores = temp_model.get_scores(keyword_tokens)
                        score = doc_scores[current_index]
                        keyword_scores.append({
                            "keyword": keyword,
                            "score": max(0.0, score)
                        })
                    except Exception as e:
                        logger.warning(f"Failed to compute the score of keyword '{keyword}': {e}")
                        keyword_scores.append({
                            "keyword": keyword,
                            "score": 0.0
                        })
                else:
                    keyword_scores.append({
                        "keyword": keyword,
                        "score": 0.0
                    })

            # Statistical analysis
            scores = [item["score"] for item in keyword_scores]
            threshold = self.config['score_threshold']
            min_low_score = self.config['min_low_score_count']

            low_score_keywords = [
                item["keyword"] for item in keyword_scores
                if item["score"] < threshold
            ]
            low_score_count = len(low_score_keywords)

            avg_score = sum(scores) / len(scores) if scores else 0
            min_score = min(scores) if scores else 0
            max_score = max(scores) if scores else 0

            # Decide whether this is abuse
            is_abuse = low_score_count > min_low_score

            evidence = KeywordStuffingEvidence(
                repo_full_name=repo_full_name,
                total_keywords=len(keywords),
                low_score_keywords_count=low_score_count,
                low_score_keywords=low_score_keywords[:10],
                avg_score=round(avg_score, 4),
                min_score=round(min_score, 4),
                max_score=round(max_score, 4),
                is_abuse=is_abuse,
                details={
                    "score_threshold": threshold,
                    "min_low_score_count": min_low_score,
                    "keyword_scores": sorted(keyword_scores, key=lambda x: x["score"])[:20],
                    "corpus_size": len(self.corpus_docs),
                    "readme_length": len(readme_content)
                }
            )

            return is_abuse, evidence

        except Exception as e:
            logger.error(f"Keyword stuffing detection failed: {e}")
            return False, None

    def _detect_simple(self, repo_full_name: str, readme_content: str, keywords: List[str]) -> Tuple[
        bool, Optional[KeywordStuffingEvidence]]:
        """
        Simple mode detection (used when there is no corpus)
        Based on how frequently the keywords appear in the README

        Args:
            repo_full_name: Full repository name
            readme_content: README content
            keywords: List of keywords

        Returns:
            (is abuse, evidence object)
        """
        readme_lower = readme_content.lower()

        keyword_scores = []
        for keyword in keywords:
            # Count the keyword occurrences
            keyword_lower = keyword.lower()
            count = readme_lower.count(keyword_lower)

            # Simple score: occurrences / content length * 1000
            score = (count * 1000) / max(len(readme_content), 1)
            keyword_scores.append({
                "keyword": keyword,
                "score": score,
                "count": count
            })

        scores = [item["score"] for item in keyword_scores]
        threshold = self.config['score_threshold']
        min_low_score = self.config['min_low_score_count']

        # In simple mode the low-score threshold has to be adjusted
        adjusted_threshold = 0.1  # Use a lower threshold in simple mode

        low_score_keywords = [
            item["keyword"] for item in keyword_scores
            if item["score"] < adjusted_threshold
        ]
        low_score_count = len(low_score_keywords)

        avg_score = sum(scores) / len(scores) if scores else 0
        min_score = min(scores) if scores else 0
        max_score = max(scores) if scores else 0

        is_abuse = low_score_count > min_low_score

        evidence = KeywordStuffingEvidence(
            repo_full_name=repo_full_name,
            total_keywords=len(keywords),
            low_score_keywords_count=low_score_count,
            low_score_keywords=low_score_keywords[:10],
            avg_score=round(avg_score, 4),
            min_score=round(min_score, 4),
            max_score=round(max_score, 4),
            is_abuse=is_abuse,
            details={
                "mode": "simple (no corpus)",
                "score_threshold": adjusted_threshold,
                "min_low_score_count": min_low_score,
                "keyword_scores": sorted(keyword_scores, key=lambda x: x["score"])[:20],
                "readme_length": len(readme_content)
            }
        )

        return is_abuse, evidence

    def save_cache(self):
        """Manually save the cache"""
        self.readme_cache.force_save()