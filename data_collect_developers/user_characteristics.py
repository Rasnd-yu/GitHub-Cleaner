import json
import time
import os
import requests
from datetime import datetime
from typing import Set, Dict, List, Optional
from pathlib import Path
import logging
import sys
from collections import deque
from threading import Lock
import ctypes
import platform
import gc

# Configure UTF-8 support for the Windows console
if sys.platform == 'win32':
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')


class SafeStreamHandler(logging.StreamHandler):
    """Safe stream handler that avoids encoding errors"""

    def emit(self, record):
        try:
            msg = self.format(record)
            stream = self.stream
            try:
                stream.write(msg + self.terminator)
            except UnicodeEncodeError:
                msg = msg.encode('ascii', 'ignore').decode('ascii')
                stream.write(msg + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)


class TokenManager:
    """Token manager supporting rotation over several tokens"""

    def __init__(self, tokens: List[str], requests_per_second: float = 0.8):
        self.tokens = tokens
        self.requests_per_second = requests_per_second
        self.lock = Lock()
        self.token_limiters = {}
        self.token_states = {}

        for i, token in enumerate(tokens):
            masked_token = token[:8] + "..." + token[-4:] if len(token) > 12 else "***"
            self.token_limiters[i] = RateLimiter(requests_per_second)
            self.token_states[i] = {
                'token': token,
                'masked': masked_token,
                'rate_limit_remaining': 5000,
                'rate_limit_reset': 0,
                'requests_made': 0,
                'last_used': 0,
                'is_limited': False,
                'retry_after': 0
            }
        self.current_index = 0
        logger.info(f"Token manager initialized with {len(tokens)} tokens, each limited to {requests_per_second} requests/second")

    def wait_for_token(self, token_index: int):
        if token_index in self.token_limiters:
            self.token_limiters[token_index].wait_if_needed()

    def get_next_token(self) -> tuple:
        with self.lock:
            current_time = time.time()
            for attempt in range(len(self.tokens)):
                state = self.token_states[self.current_index]
                if state['is_limited']:
                    if state['retry_after'] > 0:
                        wait_until = state['retry_after']
                        if current_time < wait_until:
                            self.current_index = (self.current_index + 1) % len(self.tokens)
                            continue
                        else:
                            state['is_limited'] = False
                            state['retry_after'] = 0
                    else:
                        if state['rate_limit_remaining'] < 10:
                            wait_time = state['rate_limit_reset'] - current_time
                            if wait_time > 0:
                                self.current_index = (self.current_index + 1) % len(self.tokens)
                                continue
                            else:
                                state['is_limited'] = False

                token_info = (self.current_index, state['token'], state['masked'])
                self.current_index = (self.current_index + 1) % len(self.tokens)
                self.wait_for_token(token_info[0])
                return token_info

            min_wait = float('inf')
            for state in self.token_states.values():
                if state['is_limited']:
                    if state['retry_after'] > 0:
                        wait = state['retry_after'] - current_time
                    else:
                        wait = state['rate_limit_reset'] - current_time
                    if wait > 0 and wait < min_wait:
                        min_wait = wait

            if min_wait != float('inf') and min_wait > 0:
                logger.warning(f"Every token is rate limited, waiting {min_wait:.0f} seconds")
                time.sleep(min_wait + 5)
                for state in self.token_states.values():
                    if state['retry_after'] <= current_time + 5:
                        state['is_limited'] = False
                        state['retry_after'] = 0

            self.current_index = 0
            state = self.token_states[0]
            self.wait_for_token(0)
            return (0, state['token'], state['masked'])

    def update_token_state(self, token_index: int, headers: Dict, status_code: int = 200):
        with self.lock:
            if token_index not in self.token_states:
                return
            state = self.token_states[token_index]
            state['requests_made'] += 1
            state['last_used'] = time.time()

            if status_code == 403 or status_code == 429:
                if 'Retry-After' in headers:
                    retry_after = int(headers['Retry-After'])
                    state['is_limited'] = True
                    state['retry_after'] = time.time() + retry_after
                    logger.warning(f"Token {state['masked']} hit a secondary rate limit, waiting {retry_after} seconds")
                    return
                if 'X-RateLimit-Remaining' in headers:
                    remaining = int(headers['X-RateLimit-Remaining'])
                    if remaining == 0:
                        state['is_limited'] = True
                        if 'X-RateLimit-Reset' in headers:
                            state['rate_limit_reset'] = int(headers['X-RateLimit-Reset'])
                        logger.warning(f"Token {state['masked']} has exhausted its API quota")
                        return

            if 'X-RateLimit-Remaining' in headers:
                state['rate_limit_remaining'] = int(headers['X-RateLimit-Remaining'])

    def get_stats(self) -> Dict:
        with self.lock:
            stats = {}
            for idx, state in self.token_states.items():
                stats[f"Token_{idx}_{state['masked']}"] = {
                    'requests_made': state['requests_made'],
                    'rate_limit_remaining': state['rate_limit_remaining'],
                    'is_limited': state['is_limited']
                }
            return stats


class RateLimiter:
    """Standalone rate limiter"""

    def __init__(self, requests_per_second: float = 0.8):
        self.requests_per_second = requests_per_second
        self.min_interval = 1.0 / requests_per_second
        self.last_request_time = 0
        self.lock = Lock()

    def wait_if_needed(self):
        with self.lock:
            current_time = time.time()
            time_since_last = current_time - self.last_request_time
            if time_since_last < self.min_interval:
                wait_time = self.min_interval - time_since_last
                if wait_time > 0:
                    time.sleep(wait_time)
            self.last_request_time = time.time()


class BufferedWriter:
    """Buffered writer - deduplicates and cleans duplicates automatically"""

    def __init__(self, output_file: Path, buffer_size: int = 20, clean_duplicates: bool = True):
        self.output_file = output_file
        self.buffer_size = buffer_size
        self.buffer: List[Dict] = []
        self.lock = Lock()
        self.write_count = 0
        self.existing_users: Set[str] = set()

        # Load the existing users and clean them
        self._load_and_clean_existing_users(clean_duplicates)

    def _load_and_clean_existing_users(self, clean_duplicates: bool):
        """Load the existing users, optionally cleaning duplicates"""
        if not self.output_file.exists():
            logger.info("The output file does not exist, collection will start from scratch")
            return

        try:
            with open(self.output_file, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if not content:
                    return

                data = json.loads(content)
                if not isinstance(data, list):
                    logger.warning("The output file is not an array, it will be recreated")
                    return

                # Check for duplicates
                seen = {}
                duplicates = []
                unique_data = []

                for idx, item in enumerate(data):
                    if isinstance(item, dict) and 'username' in item:
                        username = item['username']
                        if username in seen:
                            duplicates.append((idx, username))
                        else:
                            seen[username] = len(unique_data)
                            unique_data.append(item)
                    else:
                        unique_data.append(item)

                # Clean and rewrite the file when duplicates are present
                if duplicates and clean_duplicates:
                    logger.warning(f"⚠️ Found {len(duplicates)} duplicate users, cleaning...")
                    for idx, username in duplicates[:10]:  # Show the first ten
                        logger.warning(f"  - Duplicate user: {username} (index {idx})")

                    if len(duplicates) > 10:
                        logger.warning(f"  ... {len(duplicates)} duplicates in total")

                    # Back up the original file
                    backup_file = self.output_file.with_suffix('.json.backup')
                    import shutil
                    shutil.copy2(self.output_file, backup_file)
                    logger.info(f"Original file backed up to: {backup_file}")

                    # Write the cleaned data
                    with open(self.output_file, 'w', encoding='utf-8') as f:
                        json.dump(unique_data, f, indent=2, ensure_ascii=False)

                    logger.info(f"✅ Cleaning complete: {len(data)} -> {len(unique_data)} records")
                    data = unique_data

                # Load the user names into the deduplication set
                for item in data:
                    if isinstance(item, dict) and 'username' in item:
                        self.existing_users.add(item['username'])

                logger.info(f"✅ Loaded {len(self.existing_users)} unique users from the output file")

                # Statistics
                if len(data) != len(self.existing_users):
                    logger.warning(f"⚠️ The original file has {len(data)} records but only {len(self.existing_users)} unique users")

        except json.JSONDecodeError as e:
            logger.error(f"Failed to load the output file: {e}, a new one will be created")
        except Exception as e:
            logger.error(f"Failed to load the output file: {e}")

    def add(self, data: Dict):
        """Add a record to the buffer (deduplicated automatically)"""
        with self.lock:
            username = data.get('username')
            if not username:
                return

            # Duplicate check
            if username in self.existing_users:
                logger.debug(f"Skipping duplicate user: {username}")
                return

            # Add to the buffer
            self.buffer.append(data)
            self.existing_users.add(username)

            if len(self.buffer) >= self.buffer_size:
                self.flush()

    def flush(self):
        """Flush the buffered records to the file"""
        with self.lock:
            if not self.buffer:
                return

            try:
                added_count = len(self.buffer)
                file_exists = self.output_file.exists() and self.output_file.stat().st_size > 0

                if not file_exists:
                    # First write, create the JSON array
                    with open(self.output_file, 'w', encoding='utf-8') as f:
                        json.dump(self.buffer, f, indent=2, ensure_ascii=False)
                else:
                    # Append to the existing file
                    with open(self.output_file, 'r', encoding='utf-8') as f:
                        existing_data = json.load(f)

                    if not isinstance(existing_data, list):
                        existing_data = []

                    existing_data.extend(self.buffer)

                    with open(self.output_file, 'w', encoding='utf-8') as f:
                        json.dump(existing_data, f, indent=2, ensure_ascii=False)

                self.write_count += added_count
                logger.info(
                    f"💾 Write complete, {added_count} records written this time, {self.write_count} in total, {len(self.existing_users)} unique users in the file")
                self.buffer = []

            except Exception as e:
                logger.error(f"Buffer write failed: {e}")

    def finalize(self):
        """Finish writing and verify deduplication one last time"""
        with self.lock:
            if self.buffer:
                self.flush()

            # Final check for duplicates in the file
            try:
                with open(self.output_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                usernames = [item.get('username') for item in data if isinstance(item, dict) and 'username' in item]
                unique_count = len(set(usernames))

                if len(usernames) != unique_count:
                    logger.warning(f"⚠️ Final check: the file has {len(usernames)} records but only {unique_count} unique users")
                else:
                    logger.info(f"✅ Final verification passed: {unique_count} unique users, no duplicates")

                logger.info(f"✅ Final statistics: {len(self.existing_users)} unique users collected")

            except Exception as e:
                logger.error(f"Final verification failed: {e}")


class KeepAwake:
    """Keep the system awake"""

    @staticmethod
    def prevent_sleep():
        if platform.system() == 'Windows':
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000002 | 0x00000001 | 0x00000002)
                logger.info("System wake state set, screensaver and sleep are blocked")
                return True
            except Exception:
                return False
        return False

    @staticmethod
    def allow_sleep():
        if platform.system() == 'Windows':
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
                logger.info("System sleep settings restored")
                return True
            except Exception:
                return False
        return False


class GitHubUserCollector:
    def __init__(self, tokens: List[str], output_dir: str = "user_characteristics",
                 output_file: str = "user_characteristics.json",
                 failed_file: str = "failed_users.json",
                 checkpoint_file: str = "checkpoint.json",
                 requests_per_second_per_token: float = 0.6,
                 buffer_size: int = 15,
                 clean_duplicates: bool = True):
        """
        Initialize the GitHub user information collector

        Args:
            clean_duplicates: Whether to clean duplicate users from the output file
        """
        self.tokens = tokens

        # Create the output directory
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Set up the file paths
        self.output_file = self.output_dir / output_file
        self.failed_file = self.output_dir / failed_file
        self.checkpoint_file = self.output_dir / checkpoint_file
        self.log_file = self.output_dir / 'github_collector.log'

        # Create the token manager
        self.token_manager = TokenManager(tokens, requests_per_second_per_token)
        self.rate_limiter = RateLimiter(requests_per_second_per_token)

        # Create the buffered writer (duplicates cleaned automatically)
        self.buffered_writer = BufferedWriter(self.output_file, buffer_size, clean_duplicates)

        # Configure logging
        self._setup_logging()

        # GitHub API configuration
        self.api_base = "https://api.github.com"

        # List of failed users
        self.failed_users: List[Dict] = []
        self.MAX_FAILED_USERS = 1000

        # Statistics
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'api_errors': 0,
        }

        # Retry policy
        self.max_retries = 3
        self.retry_delay = 5

        # Memory management
        self.last_gc_time = time.time()
        self.GC_INTERVAL = 60

        # Load the failed users and the checkpoint
        self.load_failed_users()
        self.load_checkpoint()

        # Show the statistics
        self.log_token_stats()
        self.show_current_status()

    def _setup_logging(self):
        """Configure the logging system"""
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)

        file_handler = logging.FileHandler(self.log_file, encoding='utf-8')
        file_handler.setLevel(logging.INFO)
        console_handler = SafeStreamHandler()
        console_handler.setLevel(logging.INFO)

        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)

        global logger
        logger = logging.getLogger(__name__)

    def show_current_status(self):
        """Show the current state"""
        existing_count = len(self.buffered_writer.existing_users)
        logger.info("=" * 60)
        logger.info(f"Current state:")
        logger.info(f"Unique users collected: {existing_count}")
        logger.info(f"Failed users: {len(self.failed_users)}")
        if self.output_file.exists():
            file_size = self.output_file.stat().st_size / 1024 / 1024
            logger.info(f"Output file size: {file_size:.2f} MB")
        logger.info("=" * 60)

    def log_token_stats(self):
        """Report the token statistics"""
        logger.info("=" * 60)
        logger.info("Token configuration statistics:")
        stats = self.token_manager.get_stats()
        for token_name, stat in stats.items():
            logger.info(f"  {token_name}: {stat['requests_made']} requests made, "
                        f"quota remaining {stat['rate_limit_remaining']}, "
                        f"rate limited: {'yes' if stat['is_limited'] else 'no'}")
        logger.info("=" * 60)

    def load_checkpoint(self):
        """Load the checkpoint"""
        if self.checkpoint_file.exists():
            try:
                with open(self.checkpoint_file, 'r', encoding='utf-8') as f:
                    checkpoint = json.load(f)
                    self.stats.update(checkpoint.get('stats', {}))
                logger.info(f"Statistics loaded from the checkpoint")
            except Exception as e:
                logger.error(f"Failed to load the checkpoint: {e}")

    def load_failed_users(self):
        """Load the list of failed users"""
        if self.failed_file.exists():
            try:
                with open(self.failed_file, 'r', encoding='utf-8') as f:
                    failed = json.load(f)
                    if isinstance(failed, list):
                        self.failed_users = failed[-self.MAX_FAILED_USERS:]
                logger.info(f"Loaded {len(self.failed_users)} failed users")
            except Exception as e:
                logger.error(f"Failed to load the list of failed users: {e}")

    def save_checkpoint(self):
        """Save the checkpoint"""
        try:
            checkpoint = {
                'stats': self.stats,
                'last_update': datetime.now().isoformat(),
                'total_processed': len(self.buffered_writer.existing_users),
                'token_stats': self.token_manager.get_stats()
            }
            with open(self.checkpoint_file, 'w', encoding='utf-8') as f:
                json.dump(checkpoint, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save the checkpoint: {e}")

    def save_failed_users(self):
        """Save the list of failed users"""
        try:
            if len(self.failed_users) > self.MAX_FAILED_USERS:
                self.failed_users = self.failed_users[-self.MAX_FAILED_USERS:]
            with open(self.failed_file, 'w', encoding='utf-8') as f:
                json.dump(self.failed_users, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save the list of failed users: {e}")

    def make_request(self, url: str, params: Dict = None, retry_count: int = 0) -> Optional[Dict]:
        """Send a GitHub API request"""
        self.stats['total_requests'] += 1
        token_index, token, masked_token = self.token_manager.get_next_token()

        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }

        try:
            response = requests.get(url, headers=headers, params=params, timeout=60)
            self.token_manager.update_token_state(token_index, response.headers, response.status_code)

            if response.status_code == 200:
                self.stats['successful_requests'] += 1
                return response.json()
            elif response.status_code == 404:
                logger.warning(f"User does not exist: {url}")
                self.stats['failed_requests'] += 1
                return None
            elif response.status_code == 403:
                if 'X-RateLimit-Remaining' in response.headers and int(response.headers['X-RateLimit-Remaining']) == 0:
                    reset_time = int(response.headers.get('X-RateLimit-Reset', 0))
                    wait_time = reset_time - time.time()
                    if wait_time > 0:
                        logger.warning(f"Token {masked_token} hit the rate limit, retrying in {wait_time:.0f} seconds")
                        time.sleep(wait_time + 5)
                        return self.make_request(url, params, retry_count + 1)
                else:
                    logger.error(f"API request failed (403): {url}")
                    self.stats['api_errors'] += 1
                    self.stats['failed_requests'] += 1
                    return None
            elif response.status_code in [502, 504]:
                logger.warning(f"Gateway error ({response.status_code}), retrying...")
                if retry_count < self.max_retries:
                    time.sleep(self.retry_delay * (retry_count + 1))
                    return self.make_request(url, params, retry_count + 1)
            else:
                logger.error(f"API request failed: {response.status_code} - {url}")
                self.stats['failed_requests'] += 1
                return None

        except requests.Timeout:
            logger.error(f"Request timed out: {url}")
            self.stats['api_errors'] += 1
            if retry_count < self.max_retries:
                time.sleep(self.retry_delay * (retry_count + 1))
                return self.make_request(url, params, retry_count + 1)
            return None
        except requests.RequestException as e:
            logger.error(f"Network request error: {e}")
            self.stats['api_errors'] += 1
            if retry_count < self.max_retries:
                time.sleep(self.retry_delay * (retry_count + 1))
                return self.make_request(url, params, retry_count + 1)
            return None

    def get_user_age(self, created_at: str) -> int:
        """Compute the account age (in days)"""
        try:
            created_date = datetime.strptime(created_at, '%Y-%m-%dT%H:%M:%SZ')
            days = (datetime.now() - created_date).days
            return days
        except:
            return 0

    def get_user_commits_in_range(self, username: str, start_date: str, end_date: str) -> int:
        """Get the total number of commits in the given time range"""
        query = f"author:{username} committer-date:{start_date}..{end_date}"
        url = f"{self.api_base}/search/commits"
        params = {'q': query, 'per_page': 1}

        try:
            token_index, token, masked_token = self.token_manager.get_next_token()
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json"
            }
            response = requests.get(url, headers=headers, params=params, timeout=60)
            self.token_manager.update_token_state(token_index, response.headers)

            if response.status_code == 200:
                data = response.json()
                return data.get('total_count', 0)
            return 0
        except:
            return 0

    def get_user_issues_in_range(self, username: str, start_date: str, end_date: str) -> int:
        """Get the total number of issues created in the given time range"""
        # Changed: add the is:issue qualifier
        query = f"author:{username} created:{start_date}..{end_date} is:issue"
        url = f"{self.api_base}/search/issues"
        params = {'q': query, 'per_page': 1}

        try:
            token_index, token, masked_token = self.token_manager.get_next_token()
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json"
            }
            response = requests.get(url, headers=headers, params=params, timeout=60)
            self.token_manager.update_token_state(token_index, response.headers)

            if response.status_code == 200:
                data = response.json()
                return data.get('total_count', 0)
            elif response.status_code == 422:
                logger.warning(f"Malformed issue query for {username}: {response.json().get('message', '')[:100]}")
                return 0
            else:
                logger.debug(f"Issue query failed for {username}: HTTP {response.status_code}")
                return 0
        except Exception as e:
            logger.error(f"Issue query raised for {username}: {e}")
            return 0

    def get_user_prs_in_range(self, username: str, start_date: str, end_date: str) -> int:
        """Get the total number of pull requests created in the given time range"""
        # Query the pull requests created by the user (using is:pull-request)
        query = f"author:{username} created:{start_date}..{end_date} is:pull-request"
        url = f"{self.api_base}/search/issues"
        params = {'q': query, 'per_page': 1}

        try:
            token_index, token, masked_token = self.token_manager.get_next_token()
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json"
            }
            response = requests.get(url, headers=headers, params=params, timeout=60)
            self.token_manager.update_token_state(token_index, response.headers)

            if response.status_code == 200:
                data = response.json()
                return data.get('total_count', 0)
            elif response.status_code == 422:
                logger.warning(f"Malformed PR query for {username}: {response.json().get('message', '')[:100]}")
                return 0
            else:
                logger.debug(f"PR query failed for {username}: HTTP {response.status_code}")
                return 0
        except Exception as e:
            logger.error(f"PR query raised for {username}: {e}")
            return 0

    def collect_user_info(self, username: str) -> Optional[Dict]:
        """Collect the complete information of a single user"""
        logger.debug(f"Collecting the information of user: {username}")

        user_url = f"{self.api_base}/users/{username}"
        user_data = self.make_request(user_url)

        if not user_data:
            return None

        repos_url = f"{self.api_base}/users/{username}/repos"
        params = {'per_page': 100, 'page': 1, 'type': 'owner'}
        total_stars = 0
        total_forks = 0
        public_repos_count = user_data.get('public_repos', 0)

        page = 1
        while True:
            params['page'] = page
            repos = self.make_request(repos_url, params)
            if not repos:
                break

            for repo in repos:
                if not repo.get('fork', False):
                    total_stars += repo.get('stargazers_count', 0)
                    total_forks += repo.get('forks_count', 0)

            if len(repos) < params['per_page']:
                break
            page += 1
            time.sleep(0.1)

        followers = user_data.get('followers', 0)
        following = user_data.get('following', 0)
        account_age = self.get_user_age(user_data.get('created_at', ''))

        start_date = "2024-01-01"
        end_date = "2026-01-01"

        commits = self.get_user_commits_in_range(username, start_date, end_date)
        issues = self.get_user_issues_in_range(username, start_date, end_date)
        prs = self.get_user_prs_in_range(username, start_date, end_date)  # Added: fetch the PR count

        user_info = {
            'username': username,
            'account_age_days': account_age,
            'public_repos_count': public_repos_count,
            'total_stars': total_stars,
            'total_forks': total_forks,
            'followers': followers,
            'following': following,
            'commits_2024_2026': commits,
            'issues_2024_2026': issues,
            'prs_2024_2026': prs
        }

        return user_info

    def add_user_info(self, user_info: Dict):
        """Add the user information"""
        self.buffered_writer.add(user_info)

    def _perform_garbage_collection(self):
        """Run the garbage collector"""
        current_time = time.time()
        if current_time - self.last_gc_time >= self.GC_INTERVAL:
            gc.collect()
            self.last_gc_time = current_time
            return True
        return False

    def _get_memory_usage(self) -> float:
        """Get the current memory usage (MB)"""
        try:
            import psutil
            process = psutil.Process()
            return process.memory_info().rss / 1024 / 1024
        except:
            return 0.0

    def extract_users_from_file(self, input_file: str) -> Set[str]:
        """Extract every user name to collect from the input JSON file"""
        users = set()
        input_path = Path(input_file)

        if not input_path.exists():
            logger.error(f"The input file does not exist: {input_file}")
            return users

        try:
            with open(input_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()

                try:
                    data = json.loads(content)
                    if isinstance(data, list):
                        logger.info(f"Standard JSON array format detected")
                        for repo_data in data:
                            self._extract_users_from_repo(repo_data, users)
                        logger.info(f"File {input_file} yielded {len(users)} unique users")
                        return users
                except json.JSONDecodeError:
                    pass

                # Try the JSON Lines format
                f.seek(0)
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            repo_data = json.loads(line)
                            self._extract_users_from_repo(repo_data, users)
                        except json.JSONDecodeError:
                            continue

                if users:
                    logger.info(f"JSON Lines format detected")
                    logger.info(f"File {input_file} yielded {len(users)} unique users")

        except Exception as e:
            logger.error(f"Failed to read file {input_file}: {e}")

        return users

    def _extract_users_from_repo(self, repo_data: Dict, users_set: Set[str]):
        """Extract the users from a single repository object"""
        try:
            if 'owner' in repo_data and repo_data['owner']:
                users_set.add(str(repo_data['owner']))

            if 'core_developers' in repo_data and repo_data['core_developers']:
                developers = str(repo_data['core_developers']).split(',')
                for dev in developers:
                    dev = dev.strip()
                    if dev:
                        users_set.add(dev)

            if 'all_abuse_users' in repo_data and repo_data['all_abuse_users']:
                if isinstance(repo_data['all_abuse_users'], list):
                    for abuse_user in repo_data['all_abuse_users']:
                        if abuse_user:
                            users_set.add(str(abuse_user))
        except Exception as e:
            logger.error(f"Failed to extract the users: {e}")

    def process_users(self, users: Set[str]):
        """Run the user collection task"""
        KeepAwake.prevent_sleep()

        # Get the users already collected (straight from the output file)
        collected_users = self.buffered_writer.existing_users
        already_collected = len(collected_users)

        # Compute the new users that must be collected
        new_users_list = [u for u in users if u not in collected_users]
        total_unique = len(users)
        total_new = len(new_users_list)

        logger.info(f"=" * 60)
        logger.info(f"Collection task statistics:")
        logger.info(f"Unique users in the input files: {total_unique}")
        logger.info(f"Unique users already collected: {already_collected}")
        logger.info(f"New users to collect in this run: {total_new}")
        logger.info(f"Unique users once finished: {total_unique}")
        logger.info(f"=" * 60)

        if total_new == 0:
            logger.info("There is no new user to collect")
            return

        # Estimate the duration
        estimated_requests = total_new * 7
        effective_requests_per_second = len(self.tokens) * self.rate_limiter.requests_per_second
        estimated_hours = estimated_requests / (effective_requests_per_second * 3600)
        logger.info(f"Estimated time remaining: {estimated_hours:.1f} hours ({estimated_hours / 24:.1f} days)")

        processed = 0
        failed_count = 0
        start_time = time.time()
        last_stats_time = start_time
        last_token_stats_time = start_time
        TOKEN_STATS_INTERVAL = 300

        for username in new_users_list:
            processed += 1
            current_total = already_collected + processed

            # Compute the progress
            overall_progress = (current_total / total_unique) * 100

            # Compute the ETA
            if processed > 1:
                elapsed = time.time() - start_time
                avg_time = elapsed / (processed - 1)
                remaining = (total_new - processed + 1) * avg_time
                eta = datetime.fromtimestamp(time.time() + remaining).strftime('%H:%M:%S')
            else:
                eta = "computing..."

            logger.info(f"[{current_total}/{total_unique}] ({overall_progress:.1f}%) "
                        f"this batch: {processed}/{total_new} ETA:{eta} - processing: {username}")

            # Print a heartbeat periodically
            if processed % 10 == 0:
                memory_usage = self._get_memory_usage()
                logger.info(f"[heartbeat] progress: {current_total}/{total_unique} ({overall_progress:.1f}%), "
                            f"this batch: {processed}/{total_new}, memory: {memory_usage:.1f}MB, "
                            f"failed: {failed_count}")

            # Refresh the screensaver suppression periodically
            if processed % 180 == 0:
                KeepAwake.prevent_sleep()
                logger.info("[system] refreshing the screensaver suppression")

            # Report the token statistics periodically
            if time.time() - last_token_stats_time >= TOKEN_STATS_INTERVAL:
                self.log_token_stats()
                last_token_stats_time = time.time()

            # Collect the user information
            try:
                user_info = self.collect_user_info(username)
            except Exception as e:
                logger.error(f"Exception while collecting {username}: {e}")
                user_info = None

            if user_info:
                self.add_user_info(user_info)
                logger.info(f"[OK] succeeded: {username} (total: {current_total}/{total_unique})")
            else:
                failed_count += 1
                failed_record = {
                    'username': username,
                    'failed_at': datetime.now().isoformat(),
                    'reason': 'API request failed or user not found'
                }
                self.failed_users.append(failed_record)
                if len(self.failed_users) > self.MAX_FAILED_USERS:
                    self.failed_users = self.failed_users[-self.MAX_FAILED_USERS:]
                self.save_failed_users()
                logger.warning(f"[FAIL] failed: {username} (failures so far: {failed_count})")

            # Save a checkpoint periodically
            if processed % 5 == 0:
                self.save_checkpoint()
                self.buffered_writer.flush()
                success_rate = ((processed - failed_count) / processed * 100) if processed > 0 else 0
                logger.info(f"📊 statistics - success rate of the current batch: {success_rate:.1f}%")

            # Garbage collection
            if processed % 20 == 0:
                self._perform_garbage_collection()

            # Rate control
            if user_info:
                time.sleep(0.2)
            else:
                time.sleep(1.0)

            # Statistics for long runs
            if time.time() - last_stats_time > 1800:
                elapsed_hours = (time.time() - start_time) / 3600
                rate = processed / elapsed_hours if elapsed_hours > 0 else 0
                current_success = processed - failed_count
                logger.info(f"\n{'=' * 60}")
                logger.info(f"Interim statistics (running for {elapsed_hours:.1f} hours):")
                logger.info(f"Overall progress: {current_total}/{total_unique} ({overall_progress:.1f}%)")
                logger.info(f"Processed in this batch: {processed}/{total_new} ({processed / total_new * 100:.1f}%)")
                logger.info(f"Success rate of this batch: {current_success / processed * 100:.1f}%")
                logger.info(f"Throughput: {rate:.1f} users/hour")
                self.log_token_stats()
                logger.info(f"{'=' * 60}")
                last_stats_time = time.time()

        # Final save
        self.save_checkpoint()
        self.buffered_writer.flush()
        self.buffered_writer.finalize()
        self.save_failed_users()

        KeepAwake.allow_sleep()

        # Final statistics
        total_time = time.time() - start_time
        final_success = processed - failed_count
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Batch collection complete!")
        logger.info(f"Collected successfully in this batch: {final_success} users")
        logger.info(f"Failed in this batch: {failed_count} users")
        logger.info(f"Unique users in total: {len(self.buffered_writer.existing_users)}")
        logger.info(f"Total elapsed time: {total_time / 60:.1f} minutes")
        logger.info(f"Average throughput: {processed / (total_time / 60):.1f} users/minute")
        logger.info(f"API statistics: {self.stats['total_requests']} requests in total, "
                    f"{self.stats['successful_requests']} succeeded, "
                    f"{self.stats['failed_requests']} failed")
        self.log_token_stats()
        logger.info(f"{'=' * 60}")

    def collect_from_files(self, input_files: List[str]):
        """Collect the user information from a list of input files"""
        logger.info(f"Processing {len(input_files)} input files")
        logger.info(f"Output directory: {self.output_dir.absolute()}")
        logger.info(f"Output file: {self.output_file.absolute()}")
        logger.info(f"Number of tokens: {len(self.tokens)}")
        logger.info(f"Rate limit per token: {self.rate_limiter.requests_per_second} requests/second")
        logger.info(f"Buffer size: {self.buffered_writer.buffer_size} records")

        # Extract the user names from every input file
        all_users = set()
        for input_file in input_files:
            input_path = Path(input_file)
            if input_path.exists():
                logger.info(f"Reading file: {input_file}")
                users = self.extract_users_from_file(input_file)
                all_users.update(users)
                logger.info(f"File {input_file} yielded {len(users)} users")
            else:
                logger.warning(f"The input file does not exist: {input_file}")

        if not all_users:
            logger.warning("No user to collect was found")
            return

        # Run the user collection
        self.process_users(all_users)

        # Report the final statistics
        logger.info(f"\n{'=' * 60}")
        logger.info("Final statistics:")
        logger.info(f"Unique users collected successfully: {len(self.buffered_writer.existing_users)}")
        logger.info(f"Failed users: {len(self.failed_users)}")
        logger.info(f"Output file: {self.output_file}")
        logger.info(f"File recording the failed users: {self.failed_file}")
        logger.info(f"Log file: {self.log_file}")

        if self.failed_users:
            logger.info(f"\nFailed users (first 20):")
            for failed in self.failed_users[:20]:
                logger.info(f"  - {failed['username']}")


def main():
    """Main entry point"""

    # Configure several GitHub tokens
    GITHUB_TOKENS = [
        "xxx",
        "xxx",
        "xxx",
    ]

    # Drop the tokens that are not configured
    valid_tokens = [token for token in GITHUB_TOKENS if token and "YOUR_" not in token]

    if not valid_tokens:
        print("No valid GitHub token is configured!")
        return

    input_files = [
        "data_row/trial_small_4_output_postprocess_analysis_users.json",
    ]

    for input_file in input_files:
        if not os.path.exists(input_file):
            print(f"Warning: the input file does not exist: {input_file}")

    collector = GitHubUserCollector(
        tokens=valid_tokens,
        output_dir="user_characteristics",
        output_file="user_characteristics.json",
        failed_file="failed_users.json",
        checkpoint_file="checkpoint.json",
        requests_per_second_per_token=0.8,
        buffer_size=15,
        clean_duplicates=True  # Enable duplicate cleaning
    )

    try:
        collector.collect_from_files(input_files)
    except KeyboardInterrupt:
        logger.info("\nCollection interrupted by the user, saving the progress...")
        collector.save_checkpoint()
        collector.buffered_writer.flush()
        collector.buffered_writer.finalize()
        collector.save_failed_users()
        KeepAwake.allow_sleep()
        logger.info("Progress saved")
    except Exception as e:
        logger.error(f"Error during collection: {e}")
        collector.save_checkpoint()
        collector.buffered_writer.flush()
        collector.buffered_writer.finalize()
        collector.save_failed_users()
        KeepAwake.allow_sleep()
        raise


if __name__ == "__main__":
    # Initialize the logger for module-level use
    logger = logging.getLogger(__name__)
    main()