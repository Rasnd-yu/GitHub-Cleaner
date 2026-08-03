import os
import time
import logging
import re
from typing import Dict, List, Any, Optional, Set, Tuple
from datetime import datetime

import requests

from utils import file_utils
from config import Config

logger = logging.getLogger(__name__)


class GitHubRepoContributorsCollector:
    """GitHub repository contributor collection module"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.enabled = config.get('enabled', True)
        self.force = config.get('force', False)
        self.source_file = config.get('source_file', 'github_repos_hot_wd.json')
        self.source_file_path = config.get('source_file_path')

        # User name filtering configuration
        self.username_filters = config.get('username_filters', {
            'enabled': True,
            'patterns': [
                r'^user_\d+$',
                r'^bot_\d+$',
                r'^test_\d+$',
                r'^\d+$',
                r'^unknown$',
                r'^none$',
                r'^example',
                r'^demo',
            ],
            'min_username_length': 2,
            'max_username_length': 39,
            'valid_username_pattern': r'^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$'
        })

        # GitHub API configuration
        self.github_token = Config.GITHUB_TOKEN
        self.api_base = Config.GITHUB_API_BASE
        self.request_delay = Config.REQUEST_DELAY

        # Session object, reused across requests
        self.session = self._create_session()

        # User cache (used for live deduplication)
        self.user_cache = file_utils.UserCache(Config.USERS_FILE)

        # Run state
        self.collected_count = 0
        self.skipped_count = 0
        self.filtered_count = 0
        self.failed_count = 0
        self.failed_users = []
        self.filtered_users = []

        # Records the users already handled in this batch (avoids duplicates within a batch)
        self.batch_processed_users = set()

        # User data file
        self.users_file = Config.USERS_FILE

    def _create_session(self) -> requests.Session:
        """Create an authenticated session"""
        session = requests.Session()
        if self.github_token and self.github_token != 'your_github_token_here':
            session.headers.update({
                'Authorization': f'Bearer {self.github_token}',
                'Accept': 'application/vnd.github.v3+json'
            })
        else:
            logger.warning("GitHub token not configured, API rate limit will be restricted")
        return session

    def _get_source_file_path(self) -> str:
        """Get the path of the source file"""
        if self.source_file_path:
            return self.source_file_path
        return os.path.join(Config.DATA_DIR, self.source_file)

    def _is_valid_username(self, username: str) -> bool:
        """
        Validate that the user name is well formed and worth collecting
        Returns True when the user should be collected, False when it should be filtered out
        """
        if not username or not isinstance(username, str):
            return False

        username_lower = username.lower().strip()

        # Check the length
        min_len = self.username_filters.get('min_username_length', 2)
        max_len = self.username_filters.get('max_username_length', 39)
        if len(username_lower) < min_len or len(username_lower) > max_len:
            logger.debug(f"Username {username} filtered: invalid length ({len(username_lower)})")
            return False

        # Check the GitHub user name format
        valid_pattern = self.username_filters.get('valid_username_pattern')
        if valid_pattern and not re.match(valid_pattern, username_lower):
            logger.debug(f"Username {username} filtered: invalid format")
            return False

        # Check the blacklist patterns
        if self.username_filters.get('enabled', True):
            patterns = self.username_filters.get('patterns', [])
            for pattern in patterns:
                if re.match(pattern, username_lower, re.IGNORECASE):
                    logger.debug(f"Username {username} filtered: matched pattern {pattern}")
                    return False

        # Exclude the common test/example user names
        excluded_keywords = ['test', 'example', 'demo', 'sample', 'dummy', 'temp', 'bot', 'user']
        for keyword in excluded_keywords:
            if username_lower.startswith(keyword) or username_lower == keyword:
                logger.debug(f"Username {username} filtered: contains excluded keyword {keyword}")
                return False

        return True

    def _check_duplicate(self, username: str) -> Tuple[bool, str]:
        """
        Check whether the user name is a duplicate
        Returns (is duplicate, reason)
        """
        # Check whether it was already handled in this batch (avoids duplicates within a batch)
        if username in self.batch_processed_users:
            return True, 'already_processed_in_batch'

        # Check whether it is present in the cache
        if self.user_cache.exists(username):
            return True, 'already_exists_in_db'

        # In forced mode it may still be collected
        if self.force:
            return False, 'force_mode_enabled'

        return False, 'new_user'

    def _should_collect(self, username: str) -> Tuple[bool, str]:
        """
        Decide whether the user should be collected
        Returns (collect, reason)
        """
        # Validate the user name first
        if not self._is_valid_username(username):
            return False, 'filtered_invalid_username'

        # Check for duplicates
        is_duplicate, reason = self._check_duplicate(username)
        if is_duplicate:
            return False, reason

        return True, 'ok'

    def _call_github_api(self, endpoint: str, retry_count: int = 0) -> Optional[Dict[str, Any]]:
        """Call the GitHub API, with enhanced rate-limit handling"""
        url = f"{self.api_base}/{endpoint.lstrip('/')}"
        max_retries = 5

        try:
            response = self.session.get(url)

            if response.status_code == 200:
                return response.json()

            elif response.status_code == 403:
                # Check the rate-limit information
                remaining = response.headers.get('X-RateLimit-Remaining')
                reset_time = response.headers.get('X-RateLimit-Reset')

                if remaining == '0' and reset_time:
                    wait_time = int(reset_time) - time.time()
                    wait_time = max(wait_time, 60)  # Wait at least 60 seconds
                    logger.warning(f"Rate limit exhausted. Waiting {wait_time:.0f} seconds...")
                    time.sleep(wait_time)

                    if retry_count < max_retries:
                        return self._call_github_api(endpoint, retry_count + 1)
                    else:
                        logger.error(f"Max retries ({max_retries}) exceeded for {endpoint}")
                        return None
                else:
                    logger.error(f"API request forbidden: {response.text}")
                    return None

            elif response.status_code == 404:
                logger.debug(f"User not found: {endpoint}")
                return None
            else:
                logger.error(f"API call failed: {url}, status: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"API call error: {e}")
            return None

    def _get_user_info(self, username: str) -> Optional[Dict[str, Any]]:
        """Get the user information"""
        return self._call_github_api(f"/users/{username}")

    def _parse_core_developers(self, core_developers_str: str) -> List[str]:
        """
        Parse the core_developers field and return the list of user names
        Several separators are supported: comma, semicolon, space and so on
        """
        if not core_developers_str:
            return []

        # Replace the common separators with a comma first
        cleaned = core_developers_str
        for sep in [';', '|', '\t', '\n']:
            cleaned = cleaned.replace(sep, ',')

        # Split on commas
        developers = [d.strip() for d in cleaned.split(',') if d.strip()]

        # Deduplicate, preserving the order
        seen = set()
        unique_developers = []
        for dev in developers:
            if dev not in seen:
                seen.add(dev)
                unique_developers.append(dev)

        return unique_developers

    def _process_repo(self, repo: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Process the contributors of a single repository and return the collected users"""
        collected_users = {}

        # Read the source field
        source = repo.get('source', 'Unknown')

        # Read the core_developers field
        core_developers = repo.get('core_developers', '')
        if not core_developers:
            logger.debug(f"No core_developers in repo: {repo.get('full_name', 'Unknown')}")
            return collected_users

        # Parse the contributor list
        developers = self._parse_core_developers(core_developers)

        if not developers:
            logger.debug(f"No valid developers after parsing: {repo.get('full_name', 'Unknown')}")
            return collected_users

        logger.debug(f"Found {len(developers)} developers in {repo.get('full_name', 'Unknown')}")

        for username in developers:
            # Check on the fly whether the user should be collected (including the duplicate check)
            should_collect, reason = self._should_collect(username)

            if not should_collect:
                if reason == 'filtered_invalid_username':
                    self.filtered_count += 1
                    self.filtered_users.append(username)
                    logger.debug(f"Filtered user: {username} (invalid username)")
                elif reason in ['already_exists_in_db', 'already_processed_in_batch']:
                    self.skipped_count += 1
                    logger.debug(f"Skipping duplicate user: {username} ({reason})")
                continue

            logger.info(f"Collecting new user: {username}")

            # Mark it as handled in this batch, to avoid duplicates
            self.batch_processed_users.add(username)

            user_info = self._get_user_info(username)

            if user_info:
                # Add the source field
                user_info['source'] = source

                # Filter the fields, keeping only the standard ones
                filtered_user = file_utils.filter_user_data(user_info)
                collected_users[username] = filtered_user
                self.collected_count += 1
                logger.info(f"Successfully collected: {username} (type: {user_info.get('type')})")
            else:
                self.failed_count += 1
                self.failed_users.append(username)
                logger.warning(f"Failed to collect: {username}")
                # On failure, remove it from the batch set so that it can be retried
                self.batch_processed_users.remove(username)

            # Delay between API requests
            time.sleep(self.request_delay)

        return collected_users

    def _save_collected_users(self, users_dict: Dict[str, Dict[str, Any]]) -> bool:
        """Save the collected users to the unified JSON file in bulk (using the cache)"""
        if not users_dict:
            return True

        try:
            # Use the bulk insert method of the cache
            if self.user_cache.add_users_batch(users_dict):
                logger.info(f"Successfully saved {len(users_dict)} users to {self.users_file}")
                return True
            else:
                logger.error(f"Failed to save users to {self.users_file}")
                return False
        except Exception as e:
            logger.error(f"Error saving users: {e}")
            return False

    def run(self) -> Dict[str, Any]:
        """Run the collection module"""
        logger.info("=" * 50)
        logger.info(f"Starting GitHubRepoContributorsCollector")
        logger.info(f"Enabled: {self.enabled}, Force: {self.force}")
        logger.info(f"Username filtering: {'enabled' if self.username_filters.get('enabled') else 'disabled'}")
        logger.info(f"Users file: {self.users_file}")
        logger.info("=" * 50)

        if not self.enabled:
            logger.info("Module is disabled, skipping...")
            return {'status': 'skipped', 'reason': 'disabled'}

        # Refresh the user cache
        self.user_cache.refresh()
        cache_stats = self.user_cache.get_stats()
        logger.info(f"Loaded {cache_stats['total_users']} existing users from database")

        # Get the path of the source file
        source_path = self._get_source_file_path()
        if not os.path.exists(source_path):
            logger.error(f"Source file not found: {source_path}")
            return {'status': 'failed', 'reason': 'source_file_not_found'}

        # Load the source data
        repos = file_utils.load_json_file(source_path)
        if not repos:
            logger.error(f"Failed to load source file or empty: {source_path}")
            return {'status': 'failed', 'reason': 'invalid_source_file'}

        # Process every repository in an array; wrap a single object into an array
        if isinstance(repos, dict):
            repos = [repos]
        elif not isinstance(repos, list):
            logger.error(f"Invalid source file format: expected list or dict, got {type(repos)}")
            return {'status': 'failed', 'reason': 'invalid_format'}

        logger.info(f"Processing {len(repos)} repositories")

        # Process every repository, saving as we go
        for i, repo in enumerate(repos, 1):
            repo_name = repo.get('full_name', repo.get('name', 'Unknown'))
            logger.info(f"Processing repo [{i}/{len(repos)}]: {repo_name}")

            # Reset the batch set
            self.batch_processed_users = set()

            collected_users = self._process_repo(repo)

            # Save the collected users
            if collected_users:
                if self._save_collected_users(collected_users):
                    logger.info(f"Saved {len(collected_users)} new users from {repo_name}")
                else:
                    logger.error(f"Failed to save users from {repo_name}")

        # Final statistics
        final_user_count = file_utils.get_user_count(self.users_file)

        result = {
            'status': 'completed',
            'collected': self.collected_count,
            'filtered': self.filtered_count,
            'skipped': self.skipped_count,
            'failed': self.failed_count,
            'filtered_users': self.filtered_users[:10],
            'failed_users': self.failed_users[:10],
            'total_processed': len(repos),
            'total_users_after': final_user_count
        }

        logger.info("=" * 50)
        logger.info(f"Collection completed!")
        logger.info(f"New users collected: {self.collected_count}")
        logger.info(f"Users filtered (invalid): {self.filtered_count}")
        logger.info(f"Duplicate users skipped: {self.skipped_count}")
        logger.info(f"Failed to collect: {self.failed_count}")
        logger.info(f"Total users in database: {final_user_count}")

        if self.filtered_users:
            logger.info(f"Filtered users sample: {self.filtered_users[:10]}")
        if self.failed_users:
            logger.info(f"Failed users sample: {self.failed_users[:10]}")
        logger.info("=" * 50)

        return result

    def get_stats(self) -> Dict[str, Any]:
        """Get the statistics"""
        total_users = file_utils.get_user_count(self.users_file)
        users_by_source = file_utils.get_users_by_source(self.users_file, '2025 GitHub Trending')

        return {
            'total_users': total_users,
            'users_from_this_source': len(users_by_source),
            'last_run': {
                'collected': self.collected_count,
                'filtered': self.filtered_count,
                'skipped': self.skipped_count,
                'failed': self.failed_count
            }
        }