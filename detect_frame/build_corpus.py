"""
Corpus building script.
Extracts README content from repository datasets and builds a BM25 corpus.
Supports automatic README retrieval from the GitHub API with cache management.
Supports JSON and CSV input formats.
"""

import json
import re
import base64
import time
import argparse
import logging
import requests
import csv
from pathlib import Path
from typing import List, Dict, Optional, Union
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class ReadmeCache:
    """README content cache manager."""

    def __init__(self, cache_file: str = "cache_readme_corpus.json"):
        self.cache_file = cache_file
        self.cache = {}
        self._load_cache()

    def _load_cache(self):
        """Load the cache."""
        if Path(self.cache_file).exists():
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    self.cache = json.load(f)
                logger.info(f"Loaded README cache: {len(self.cache)} entries")
            except Exception as e:
                logger.warning(f"Failed to load cache: {e}")

    def _save_cache(self):
        """Save the cache."""
        try:
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
            logger.debug(f"Saved README cache: {len(self.cache)} entries")
        except Exception as e:
            logger.warning(f"Failed to save cache: {e}")

    def get(self, repo_full_name: str) -> Optional[str]:
        """Get cached README content."""
        return self.cache.get(repo_full_name)

    def set(self, repo_full_name: str, content: str):
        """Set cache content."""
        self.cache[repo_full_name] = content
        # Save after every 50 entries
        if len(self.cache) % 50 == 0:
            self._save_cache()

    def save(self):
        """Manually save the cache."""
        self._save_cache()


class CorpusBuilder:
    """Corpus builder."""

    def __init__(self, github_token: str = None, config: Dict = None):
        """
        Initialize the corpus builder.

        Args:
            github_token: GitHub API token
            config: Configuration parameters
        """
        # Get default configuration
        default_config = self._default_config()

        # Merge user configuration (user settings override defaults)
        if config:
            default_config.update(config)

        self.config = default_config
        self.github_token = github_token

        # Initialize the GitHub session
        self.session = requests.Session()
        self.session.headers.update({
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'CorpusBuilder/1.0'
        })
        if self.github_token:
            self.session.headers['Authorization'] = f'token {self.github_token}'
            logger.info("GitHub token loaded")
        else:
            logger.warning("No GitHub token was provided; README content cannot be fetched")

        # README cache
        self.readme_cache = ReadmeCache()

        # Stop words - fix None issue
        stop_words_config = self.config.get('stop_words')
        if stop_words_config is None:
            # Use default stop words
            self.stop_words = {
                'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of',
                'with', 'by', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
                'a', 'an', 'this', 'that', 'these', 'those', 'it', 'as', 'from',
                'not', 'has', 'have', 'github', 'git', 'code', 'project'
            }
        else:
            self.stop_words = stop_words_config

    def _default_config(self) -> Dict:
        return {
            'min_doc_length': 100,  # Minimum document length
            'max_workers': 4,  # Parallel worker count
            'rate_limit_delay': 1.0,  # API request delay
            'max_retries': 3,  # Maximum retry count
            'stop_words': None  # Stop words
        }

    def _fetch_readme_content(self, owner: str, repo_name: str) -> Optional[str]:
        """
        Retrieve a repository's README content.

        Args:
            owner: Repository owner
            repo_name: Repository name

        Returns:
            README content, or None if retrieval fails
        """
        if not self.github_token:
            logger.warning("No GitHub token is configured; the README cannot be fetched")
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
                        logger.warning(f"API rate limit reached; waiting {wait_time:.0f} seconds...")
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.warning(f"API limit reached: {response.status_code}")
                        time.sleep(self.config['rate_limit_delay'] * (attempt + 1))
                else:
                    logger.warning(f"README retrieval failed: {response.status_code}")
                    if attempt < self.config['max_retries'] - 1:
                        time.sleep(self.config['rate_limit_delay'])

            except Exception as e:
                logger.error(f"An exception occurred while fetching README content: {e}")
                if attempt < self.config['max_retries'] - 1:
                    time.sleep(self.config['rate_limit_delay'])

        return None

    def _fetch_readme_files(self, owner: str, repo_name: str) -> List[str]:
        """
        Retrieve all README-related files from a repository.

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

                time.sleep(0.2)  # Avoid hitting API limits

            except Exception as e:
                continue

        return readme_contents

    def _get_readme_from_api(self, repo_data: Dict) -> str:
        """
        Fetch a repository's README content from the API.

        Args:
            repo_data: Repository data

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
            logger.warning(f"Unable to parse repository name: {full_name}")
            return ''

        # Check the cache
        cached_readme = self.readme_cache.get(full_name)
        if cached_readme:
            logger.debug(f"Using cached README: {full_name}")
            return cached_readme

        # Cache miss; fetch the README
        owner, repo_name = full_name.split('/', 1)
        logger.info(f"Fetching README content for repository {full_name}...")

        # Retrieve the main README
        main_readme = self._fetch_readme_content(owner, repo_name)

        # Retrieve other README files if the main README is missing
        all_readmes = []
        if not main_readme:
            all_readmes = self._fetch_readme_files(owner, repo_name)

        # Combine all README content
        combined_readme = main_readme or ''
        if all_readmes:
            combined_readme = combined_readme + '\n\n' + '\n\n'.join(all_readmes) if combined_readme else '\n\n'.join(
                all_readmes)

        # Save to cache
        if combined_readme:
            self.readme_cache.set(full_name, combined_readme)

        logger.debug(f"README retrieval complete: length={len(combined_readme)}")

        # Avoid API limits
        time.sleep(self.config['rate_limit_delay'])

        return combined_readme

    def _clean_text(self, text: str) -> str:
        """Clean text."""
        if not text:
            return ""

        # Remove HTML tags
        text = re.sub(r'<.*?>', '', text)

        # Remove Markdown markers
        text = re.sub(r'[#*`~_\[\]()]', '', text)

        # Remove code blocks
        text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)

        # Remove links
        text = re.sub(r'\[.*?\]\(.*?\)', '', text)

        # Remove excessive whitespace
        text = re.sub(r'\s+', ' ', text)

        return text.strip()

    def _tokenize(self, text: str) -> List[str]:
        """Tokenize text."""
        if not text:
            return []

        text = text.lower()
        text = re.sub(r'[^\w\s-]', ' ', text)
        words = text.split()

        filtered = []
        for word in words:
            if len(word) <= 2:
                continue

            if '-' in word:
                subwords = [sw for sw in word.split('-') if len(sw) > 2]
                for sw in subwords:
                    if sw not in self.stop_words:
                        filtered.append(sw)
            elif '_' in word:
                subwords = [sw for sw in word.split('_') if len(sw) > 2]
                for sw in subwords:
                    if sw not in self.stop_words:
                        filtered.append(sw)
            elif word not in self.stop_words:
                filtered.append(word)

        return filtered

    def _load_json_dataset(self, dataset_path: str) -> List[Dict]:
        """
        Load a JSON-format dataset.

        Args:
            dataset_path: JSON file path

        Returns:
            List of repository data
        """
        with open(dataset_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Handle the data format
        if isinstance(data, dict):
            if "repos" in data:
                repositories = data["repos"]
            else:
                repositories = [data]
        else:
            repositories = data

        return repositories

    def _load_csv_dataset(self, dataset_path: str) -> List[Dict]:
        """
        Load a CSV-format dataset.

        CSV format requirements:
        - Must include a repo/account_name column (or repo_name/full_name columns)
        - An optional URL column may be present

        Args:
            dataset_path: CSV file path

        Returns:
            List of repository data
        """
        repositories = []

        with open(dataset_path, 'r', encoding='utf-8') as f:
            # Auto-detect the CSV delimiter
            sample = f.read(1024)
            f.seek(0)

            # Try to auto-detect the delimiter
            if '\t' in sample:
                delimiter = '\t'
            elif ';' in sample:
                delimiter = ';'
            else:
                delimiter = ','

            reader = csv.DictReader(f, delimiter=delimiter)

            # Read column names
            fieldnames = reader.fieldnames or []

            # Determine the repository-name column
            repo_col = None
            for col in ['repo/account_name', 'repo_name', 'full_name', 'repository', 'name']:
                if col in fieldnames:
                    repo_col = col
                    break

            if not repo_col:
                raise ValueError(f"CSV file is missing a repository-name column; available columns: {fieldnames}")

            # Determine the URL column (optional)
            url_col = None
            for col in ['URL', 'url', 'repo_url', 'github_url']:
                if col in fieldnames:
                    url_col = col
                    break

            for row in reader:
                repo_name = row.get(repo_col, '').strip()
                if not repo_name:
                    continue

                # Parse the full repository name
                if '/' in repo_name:
                    owner, name = repo_name.split('/', 1)
                else:
                    # If only the repository name is present, extract it from the URL
                    if url_col and row.get(url_col):
                        url = row[url_col].strip()
                        # Extract owner/repo from the URL
                        match = re.search(r'github\.com/([^/]+)/([^/]+)', url)
                        if match:
                            owner, name = match.group(1), match.group(2)
                            repo_name = f"{owner}/{name}"
                        else:
                            logger.warning(f"Unable to parse repository name: {repo_name}")
                            continue
                    else:
                        logger.warning(f"Cannot parse repository name: {repo_name}")
                        continue

                # Build the repository data object
                repo_data = {
                    'full_name': repo_name,
                    'name': name,
                    'owner': {'login': owner}
                }

                # If a URL exists, add it to the data
                if url_col and row.get(url_col):
                    repo_data['html_url'] = row[url_col].strip()

                repositories.append(repo_data)

        logger.info(f"Loaded {len(repositories)} repositories from CSV")
        return repositories

    def load_dataset(self, dataset_path: str) -> List[Dict]:
        """
        Automatically detect and load a dataset.

        Args:
            dataset_path: Dataset file path

        Returns:
            List of repository data
        """
        dataset_path = Path(dataset_path)

        if dataset_path.suffix.lower() == '.csv':
            logger.info(f"Detected CSV format: {dataset_path}")
            return self._load_csv_dataset(str(dataset_path))
        else:
            logger.info(f"Detected JSON format: {dataset_path}")
            return self._load_json_dataset(str(dataset_path))

    def extract_all_readme_content(self, repo_data: Dict) -> Optional[str]:
        """
        Extract only the README content (description is no longer used).
        Repositories without a README are dropped directly.
        """
        all_content = []

        # 1. Retrieve README content from the data itself
        readme_content = repo_data.get('readme_content', '')
        if readme_content:
            all_content.append(readme_content)

        # 2. Read readme_files
        readme_files = repo_data.get('readme_files', [])
        if isinstance(readme_files, list):
            for readme_file in readme_files:
                content = readme_file.get('content', '')
                if content:
                    all_content.append(content)

        # 3. If none is present, fetch from the API
        if not all_content:
            api_readme = self._get_readme_from_api(repo_data)
            if api_readme:
                all_content.append(api_readme)

        # Core rule: repositories without README are filtered out
        if not all_content:
            return None

        combined = '\n\n'.join(all_content)
        cleaned = self._clean_text(combined)

        # Length filter
        if len(cleaned) < self.config['min_doc_length']:
            return None

        return cleaned

    def process_repository(self, repo_data: Dict) -> Optional[Dict]:
        """
        Process a single repository and return document data.

        Args:
            repo_data: Repository data

        Returns:
            A dictionary containing the repository name and tokenized result, or None if invalid
        """
        try:
            # Get the repository's full name
            full_name = repo_data.get('full_name', '')
            if not full_name:
                owner = repo_data.get('owner', {}).get('login', 'unknown')
                name = repo_data.get('name', 'unknown')
                full_name = f"{owner}/{name}"

            # Extract README content
            readme_content = self.extract_all_readme_content(repo_data)

            if not readme_content:
                logger.debug(f"Repository {full_name} does not have valid README content")
                return None

            # Tokenize
            tokens = self._tokenize(readme_content)

            if len(tokens) < 10:
                logger.debug(f"Repository {full_name} has fewer than 10 tokens")
                return None

            return {
                'repository': full_name,
                'tokens': tokens,
                'content_length': len(readme_content),
                'token_count': len(tokens)
            }

        except Exception as e:
            logger.error(f"Failed to process repository {repo_data.get('full_name', 'unknown')}: {e}")
            return None

    def build_from_dataset(self, dataset_path: str, output_path: str, max_items: int = None) -> Dict:
        """
        Build a corpus from a dataset.

        Args:
            dataset_path: Dataset file path (supports JSON and CSV)
            output_path: Output file path
            max_items: Maximum number of items to process

        Returns:
            Corpus statistics
        """
        logger.info(f"Loading dataset: {dataset_path}")

        # Auto-load the dataset
        repositories = self.load_dataset(dataset_path)

        logger.info(f"The dataset contains {len(repositories)} repositories")

        if max_items:
            repositories = repositories[:max_items]
            logger.info(f"Limiting processing to the first {max_items} repositories")

        # Process all repositories
        valid_docs = []
        stats = {
            'total_repos': len(repositories),
            'valid_repos': 0,
            'invalid_repos': 0,
            'total_tokens': 0,
            'avg_token_count': 0,
            'min_token_count': float('inf'),
            'max_token_count': 0,
            'api_fetched': 0,  # number of repos fetched from API
            'cached_hits': 0  # number of cache hits
        }

        logger.info("Starting repository processing...")

        for idx, repo in enumerate(repositories, 1):
            if idx % 10 == 0:
                logger.info(f"Processing progress: {idx}/{len(repositories)}")

            # Track whether content was served from cache
            full_name = repo.get('full_name', '')
            if full_name and self.readme_cache.get(full_name):
                stats['cached_hits'] += 1

            result = self.process_repository(repo)

            if result:
                # Check whether the README was newly fetched from the API (not from cache)
                if full_name and self.readme_cache.get(full_name) and not stats.get(f'cached_{full_name}'):
                    stats['api_fetched'] += 1

                valid_docs.append(result)
                stats['valid_repos'] += 1
                stats['total_tokens'] += result['token_count']
                stats['min_token_count'] = min(stats['min_token_count'], result['token_count'])
                stats['max_token_count'] = max(stats['max_token_count'], result['token_count'])
            else:
                stats['invalid_repos'] += 1

        # Compute average token statistics
        if stats['valid_repos'] > 0:
            stats['avg_token_count'] = stats['total_tokens'] / stats['valid_repos']
        else:
            stats['min_token_count'] = 0

        # Build the corpus output
        corpus = {
            'metadata': {
                'created_at': datetime.now().isoformat(),
                'source_dataset': dataset_path,
                'total_repos_processed': stats['total_repos'],
                'valid_docs_count': stats['valid_repos'],
                'statistics': stats
            },
            'repositories': [doc['repository'] for doc in valid_docs],
            'documents': [doc['tokens'] for doc in valid_docs]
        }

        # Save the corpus to disk
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(corpus, f, ensure_ascii=False, indent=2)

        # Save the README cache
        self.readme_cache.save()

        logger.info(f"Corpus saved to: {output_path}")
        logger.info(f"Stats: valid_docs={stats['valid_repos']}, total_tokens={stats['total_tokens']}")
        logger.info(f"API fetched: {stats.get('api_fetched', 0)} repos, cache hits: {stats.get('cached_hits', 0)} repos")

        return stats

    def build_from_multiple_datasets(self, dataset_paths: List[str], output_path: str,
                                     max_items_per_dataset: int = None) -> Dict:
        """Build a corpus from multiple datasets.

        Args:
            dataset_paths: List of dataset file paths
            output_path: Output file path
            max_items_per_dataset: Maximum items to process per dataset

        Returns:
            Corpus statistics
        """
        all_valid_docs = []
        total_stats = {
            'total_repos': 0,
            'valid_repos': 0,
            'datasets_processed': len(dataset_paths),
            'api_fetched': 0,
            'cached_hits': 0
        }

        for dataset_path in dataset_paths:
            logger.info(f"Processing dataset: {dataset_path}")

            stats = self.build_from_dataset(dataset_path, output_path + '.tmp', max_items_per_dataset)

            # Load temporary results
            with open(output_path + '.tmp', 'r', encoding='utf-8') as f:
                temp_corpus = json.load(f)

            for i in range(len(temp_corpus['repositories'])):
                all_valid_docs.append({
                    'repository': temp_corpus['repositories'][i],
                    'tokens': temp_corpus['documents'][i]
                })

            total_stats['total_repos'] += stats['total_repos']
            total_stats['valid_repos'] += stats['valid_repos']
            total_stats['api_fetched'] += stats.get('api_fetched', 0)
            total_stats['cached_hits'] += stats.get('cached_hits', 0)

        # Merge all documents
        final_corpus = {
            'metadata': {
                'created_at': datetime.now().isoformat(),
                'source_datasets': dataset_paths,
                'total_repos_processed': total_stats['total_repos'],
                'valid_docs_count': total_stats['valid_repos'],
                'statistics': total_stats
            },
            'repositories': [doc['repository'] for doc in all_valid_docs],
            'documents': [doc['tokens'] for doc in all_valid_docs]
        }

        # Save the final corpus
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(final_corpus, f, ensure_ascii=False, indent=2)

        # Save the README cache
        self.readme_cache.save()

        logger.info(f"Merged corpus saved to: {output_path}")
        logger.info(f"Total valid documents: {total_stats['valid_repos']}")
        logger.info(f"Total API fetched: {total_stats['api_fetched']}, total cache hits: {total_stats['cached_hits']}")

        return total_stats


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='Build the keyword stuffing detection corpus (supports JSON and CSV input)')
    parser.add_argument('--input', '-i', default='corpus_keyword_stuffing_input.json', help='Path to the input dataset file (JSON or CSV format)')
    parser.add_argument('--output', '-o', default='corpus_keyword_stuffing.json', help='Path to the output corpus file')
    parser.add_argument('--max', '-m', type=int, default=None, help='Maximum number of repositories to process')
    parser.add_argument('--min-doc-length', type=int, default=100, help='Minimum document length')
    parser.add_argument('--token', '-t', help='GitHub API Token')
    parser.add_argument('--config', '-c', default='config.json', help='Path to the config file')
    parser.add_argument('--rate-limit-delay', type=float, default=1.0, help='API request delay (seconds)')

    args = parser.parse_args()

    # Get the GitHub Token
    github_token = args.token
    # If it was not provided via command line arguments, it can be set directly here
    if not github_token:
        github_token = "xxx"
        logger.info("Using the token hardcoded in the source")

    if not github_token and Path(args.config).exists():
        try:
            with open(args.config, 'r', encoding='utf-8') as f:
                config = json.load(f)
                keyword_config = config.get('detection_configs', {}).get('keyword_stuffing', {})
                github_token = keyword_config.get('github_token')
                if github_token:
                    logger.info("Loaded token from the config file")
        except Exception as e:
            logger.warning(f"Failed to load config file: {e}")

    if not github_token:
        logger.warning("No GitHub Token provided, README content cannot be fetched")

    config = {
        'min_doc_length': args.min_doc_length,
        'rate_limit_delay': args.rate_limit_delay,
        'max_retries': 3,  # Default value added
        'max_workers': 4,  # Default value added
    }

    builder = CorpusBuilder(github_token=github_token, config=config)
    stats = builder.build_from_dataset(args.input, args.output, args.max)

    print("\n" + "=" * 50)
    print("Corpus build complete!")
    print("=" * 50)
    print(f"  Total repositories processed: {stats['total_repos']}")
    print(f"  Valid documents: {stats['valid_repos']}")
    print(f"  Invalid documents: {stats['invalid_repos']}")
    print(f"  Total tokens: {stats['total_tokens']}")
    print(f"  Average token count: {stats['avg_token_count']:.2f}")
    print(f"  Minimum token count: {stats['min_token_count']}")
    print(f"  Maximum token count: {stats['max_token_count']}")
    print(f"  API fetched: {stats.get('api_fetched', 0)}")
    print(f"  Cache hits: {stats.get('cached_hits', 0)}")
    print("=" * 50)


if __name__ == "__main__":
    main()
