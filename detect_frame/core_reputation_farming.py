"""
GitHub Reputation Farming Detector - core detection module
Provides the core detection functionality for github_abuse_detector.py
Adapted to detect PR abuse in repositories
Optimized version: merged API calls for speed + multithreading + token rotation
"""

import requests
import time
import logging
import re
import concurrent.futures
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, field
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import threading

logger = logging.getLogger(__name__)


@dataclass
class AbuseEvidence:
    """Evidence of abusive behavior"""
    user_name: str
    repo_name: str
    repo_url: str
    target_url: str
    target_type: str  # 'pr' or 'issue'
    action_type: str  # 'approve', 'comment', 'review', 'merge'
    action_date: str
    content: Optional[str] = None
    suspicious_reason: str = ""
    pr_state: Optional[str] = None  # PR state: open, closed, merged
    pr_merged: Optional[bool] = None  # Whether the PR was merged
    days_after_resolution: int = 0  # Days between the PR being resolved (merged/closed) and the activity


@dataclass
class UserAbuseEvidence:
    """Summary of a user's abuse evidence"""
    user_name: str
    user_url: str
    abuse_count: int
    evidences: List[AbuseEvidence] = field(default_factory=list)
    first_activity: str = ""
    last_activity: str = ""


@dataclass
class RepositoryAbuseReport:
    """Repository abuse report"""
    repository: str
    repo_url: str
    has_abuse_behavior: bool
    abuse_user_count: int  # Number of abusive users
    abuse_activity_count: int  # Total number of abusive activities
    suspicious_users_evidence: List[UserAbuseEvidence]  # List of user abuse evidence (grouped by user)
    timestamp: str = ""


class TokenManager:
    """Token manager - supports proactive rotation across multiple tokens"""

    def __init__(self, tokens: List[str], thresholds: List[int] = None):
        """
        Initialize the token manager

        Args:
            tokens: List of tokens
            thresholds: Proactive switch threshold of each token (requests per minute), 70 for all by default
        """
        if not tokens:
            raise ValueError("At least one token is required")

        self.tokens = tokens
        self.token_count = len(tokens)

        # Proactive switch threshold (switch to the next token once it is reached)
        if thresholds and len(thresholds) >= self.token_count:
            self.switch_thresholds = thresholds[:self.token_count]
        else:
            self.switch_thresholds = [70] * self.token_count

        # Hard limit threshold (force a wait once it is reached)
        self.hard_limit_rest = 80  # REST API hard limit (actually 83, leaving a buffer of 3)
        self.hard_limit_search = 25  # Search API hard limit (actually 30, leaving a buffer of 5)

        # Index of the token currently in use
        self.current_index = 0
        self.current_token = tokens[0]

        self.lock = threading.Lock()

        # Per-minute request counters, one per token
        self.token_rest_requests = [0] * self.token_count  # REST API request counts
        self.token_search_requests = [0] * self.token_count  # Search API request counts

        # Total request count per token (cumulative, never reset)
        self.token_total_rest = [0] * self.token_count
        self.token_total_search = [0] * self.token_count

        # Number of times each token has been rate limited
        self.token_rate_limited = [0] * self.token_count
        self.token_exhausted = [False] * self.token_count

        # Reset time tracking
        self.last_reset_time = time.time()

        logger.info(f"Token manager initialized with {self.token_count} tokens")
        logger.info(f"  Proactive switch thresholds: {self.switch_thresholds}")
        logger.info(f"  Hard limit thresholds: REST={self.hard_limit_rest}, search={self.hard_limit_search}")

    def get_token(self) -> str:
        """Get the token that is currently available"""
        with self.lock:
            # Reset the minute counters of all tokens every minute
            current_time = time.time()
            if current_time - self.last_reset_time >= 60:
                self.token_rest_requests = [0] * self.token_count
                self.token_search_requests = [0] * self.token_count
                self.token_exhausted = [False] * self.token_count
                self.last_reset_time = current_time
                logger.debug("Minute counters reset, all tokens available")

            return self.current_token

    def _switch_to_next_token(self):
        """Switch to the next available token"""
        original_index = self.current_index

        # Mark the current token as exhausted (proactive switch threshold reached)
        self.token_exhausted[self.current_index] = True

        for i in range(1, self.token_count + 1):
            next_index = (self.current_index + i) % self.token_count
            if not self.token_exhausted[next_index]:
                self.current_index = next_index
                self.current_token = self.tokens[next_index]
                rest_reqs = self.token_rest_requests[next_index]
                search_reqs = self.token_search_requests[next_index]
                logger.info(
                    f"Switched to token #{next_index + 1} (REST: {rest_reqs}/{self.switch_thresholds[next_index]}, search: {search_reqs})")
                return True

        # All tokens have reached the proactive switch threshold, wait for the reset
        logger.warning(f"All tokens reached the proactive switch threshold, waiting for the next minute reset")
        return False

    def record_request(self, is_search: bool = False):
        """Record a request and check whether the token should be switched proactively"""
        with self.lock:
            # Update the request counters of the current token
            if is_search:
                self.token_search_requests[self.current_index] += 1
                self.token_total_search[self.current_index] += 1
                current_requests = self.token_search_requests[self.current_index]
            else:
                self.token_rest_requests[self.current_index] += 1
                self.token_total_rest[self.current_index] += 1
                current_requests = self.token_rest_requests[self.current_index]

            current_threshold = self.switch_thresholds[self.current_index]

            # Check whether the current token reached the proactive switch threshold
            if not self.token_exhausted[self.current_index]:
                if current_requests >= current_threshold:
                    logger.info(
                        f"Token #{self.current_index + 1} reached the proactive switch threshold ({current_requests}/{current_threshold}), preparing to switch")
                    self._switch_to_next_token()

    def report_rate_limit(self, token_index: int = None):
        """Passively report a rate limit (emergency handling when the hard limit is hit)"""
        with self.lock:
            if token_index is None:
                token_index = self.current_index

            self.token_rate_limited[token_index] += 1
            logger.warning(f"Token #{token_index + 1} hit the hard limit! Waiting 60 seconds...")
            time.sleep(60)

            # Reset the counters of this token
            self.token_rest_requests[token_index] = 0
            self.token_search_requests[token_index] = 0
            self.token_exhausted[token_index] = False
            logger.info(f"Token #{token_index + 1} has recovered")

    def can_make_request(self, is_search: bool = False) -> bool:
        """Check whether the current token can issue a request (based on the hard limits)"""
        with self.lock:
            if is_search:
                # Search API: wait once the current token reaches 25 requests/minute
                if self.token_search_requests[self.current_index] >= self.hard_limit_search:
                    logger.warning(
                        f"Token #{self.current_index + 1} search API is close to the hard limit ({self.token_search_requests[self.current_index]}/{self.hard_limit_search})")
                    return False
            else:
                # REST API: wait once the current token reaches 80 requests/minute
                if self.token_rest_requests[self.current_index] >= self.hard_limit_rest:
                    logger.warning(
                        f"Token #{self.current_index + 1} REST API is close to the hard limit ({self.token_rest_requests[self.current_index]}/{self.hard_limit_rest})")
                    return False
            return True

    def get_stats(self) -> Dict:
        """Get token usage statistics"""
        return {
            "token_count": self.token_count,
            "current_token_index": self.current_index + 1,
            "switch_thresholds": self.switch_thresholds.copy(),
            "token_rest_requests": self.token_rest_requests.copy(),
            "token_search_requests": self.token_search_requests.copy(),
            "token_total_rest": self.token_total_rest.copy(),
            "token_total_search": self.token_total_search.copy(),
            "token_rate_limited": self.token_rate_limited.copy(),
            "token_exhausted": self.token_exhausted.copy(),
        }


class ReputationFarmingCoreDetector:
    """Core reputation farming detector - can be called externally to detect PR abuse in a repository"""

    def __init__(self, github_token: str = None, config: Dict = None,
                 backup_token: str = None, tokens: List[str] = None):
        """
        Initialize the detector

        Args:
            github_token: Primary GitHub token (kept for the old interface)
            config: Configuration dict
            backup_token: Backup GitHub token (kept for the old interface)
            tokens: List of tokens (new interface, takes priority)
        """
        self.config = config or {}

        # Get the token list (priority: tokens > the github_tokens config > github_token+backup_token)
        if tokens:
            self.tokens = tokens
        elif 'github_tokens' in self.config:
            self.tokens = self.config['github_tokens']
        else:
            self.tokens = [github_token] if github_token else []
            if backup_token:
                self.tokens.append(backup_token)

        if not self.tokens:
            raise ValueError("At least one GitHub token is required")

        # Get the switch threshold of each token (optional)
        token_thresholds = self.config.get('token_thresholds', None)

        # Initialize the token manager
        self.token_manager = TokenManager(self.tokens, token_thresholds)

        # Default configuration - balances speed and stability
        self.min_pr_age_days = self.config.get('min_pr_age_days', 400)
        self.max_prs_per_repo = self.config.get('max_prs_per_repo', 300)
        self.post_resolution_delay_days = self.config.get('post_resolution_delay_days', 400)

        # Multithreading configuration - moderate concurrency
        self.max_workers = self.config.get('max_workers', 5)
        self.api_delay = self.config.get('api_delay', 0.5)
        self.search_api_delay = self.config.get('search_api_delay', 1.0)

        # Add a semaphore to limit concurrent API calls
        self.api_semaphore = threading.Semaphore(5)

        # Detection configuration
        self.suspicious_keywords = self.config.get('suspicious_keywords', [
            "+1", "LGTM", "looks good", "approved", "nice", "good job", "thanks",
            "great", "awesome", "excellent", "good work", "well done"
        ])
        self.min_comment_length = self.config.get('min_comment_length', 10)
        self.generic_patterns = self.config.get('generic_patterns', [
            r"^[\s\W]*$",
            r"^(good|nice|great|awesome|excellent)[\s\.,!]*$",
            r"^\+1[\s\W]*$",
            r"^LGTM[\s\W]*$",
            r"^thanks?[\s\.,!]*$",
            r"^approved[\s\.,!]*$"
        ])

        # List of excluded users
        self.excluded_users = [
            'github-action[bot]', 'github-actions[bot]', 'dependabot[bot]',
            'codecov[bot]', 'pre-commit-ci[bot]', 'sonarcloud[bot]',
            'snyk-bot', 'renovate[bot]', 'mergify[bot]'
        ]

        # Request counter (used for rate control)
        self.request_lock = threading.Lock()

        # Create the session
        self.session = self._create_session()

    def _create_session(self) -> requests.Session:
        """Create a configured session with a larger connection pool and SSL settings"""
        session = requests.Session()

        # Configure the adapter with a larger connection pool
        adapter = HTTPAdapter(
            pool_connections=50,
            pool_maxsize=100,
            max_retries=Retry(
                total=3,
                backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                raise_on_status=False
            )
        )
        session.mount('http://', adapter)
        session.mount('https://', adapter)

        # Add headers
        session.headers.update({
            'Accept': 'application/vnd.github.v3+json',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'User-Agent': 'GitHub-Reputation-Core-Detector/3.0'
        })

        self._update_session_token(session)

        return session

    def _update_session_token(self, session: requests.Session = None):
        """Update the token of the session"""
        if session is None:
            session = self.session

        token = self.token_manager.get_token()
        if token:
            session.headers['Authorization'] = f'token {token}'

    def _get_session(self):
        """Get a session (for multithreaded use)"""
        session = requests.Session()
        session.headers.update({
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'GitHub-Reputation-Core-Detector/3.0'
        })

        token = self.token_manager.get_token()
        if token:
            session.headers['Authorization'] = f'token {token}'

        return session

    def _check_and_wait_if_needed(self, is_search: bool = False):
        """Check whether we need to wait to avoid rate limits"""
        if not self.token_manager.can_make_request(is_search):
            # Wait until the next minute
            current_time = time.time()
            wait_time = 60 - (current_time - self.token_manager.last_reset_time) + 1
            if wait_time > 0:
                logger.info(f"Rate limit control, waiting {wait_time:.1f} seconds")
                time.sleep(wait_time)

    def make_api_call(self, url: str, params: Dict = None, session: requests.Session = None,
                      is_search: bool = False, timeout: int = 30) -> Optional[Dict]:
        """Safe API call (with special handling for the search API) - with timeout control"""

        # Check the rate limit
        self._check_and_wait_if_needed(is_search)

        if session is None:
            session = self.session

        # The search API uses a longer delay
        delay = self.search_api_delay if is_search else self.api_delay

        # Retry at most 3 times
        max_attempts = 3

        for attempt in range(max_attempts):
            try:
                # Use the semaphore to limit concurrency, with a timeout
                acquired = self.api_semaphore.acquire(timeout=timeout)
                if not acquired:
                    logger.error(f"Timed out acquiring the semaphore ({timeout}s): {url[:100]}")
                    return None

                try:
                    # Record the request
                    self.token_manager.record_request(is_search)

                    # Add timeout control
                    response = session.get(url, params=params, timeout=timeout)

                    # Handle rate limiting
                    if response.status_code == 403:
                        try:
                            error_msg = response.json().get('message', '') if response.text else ''
                        except:
                            error_msg = response.text[:100] if response.text else ''

                        if 'rate limit' in error_msg.lower() or 'api rate limit' in error_msg.lower():
                            logger.warning(f"Token hit the rate limit: {error_msg[:100]}")
                            self.token_manager.report_rate_limit()
                            # Update the token of the session
                            self._update_session_token(session)
                            # Wait and retry
                            time.sleep(3)
                            continue

                        # A 403 error that is not a rate limit
                        logger.error(f"API returned 403 (attempt {attempt + 1}/{max_attempts}): {url[:100]}")
                        if attempt < max_attempts - 1:
                            time.sleep(2)
                            continue
                        else:
                            return None

                    if response.status_code == 429:
                        logger.warning("Received 429 Too Many Requests")
                        reset_time = response.headers.get('X-RateLimit-Reset', 0)
                        if reset_time:
                            try:
                                wait_time = max(int(reset_time) - time.time(), 0) + 2
                                if wait_time < 60:  # Wait at most 60 seconds
                                    logger.warning(f"Waiting {wait_time:.0f} seconds...")
                                    time.sleep(wait_time)
                                else:
                                    logger.warning(f"Wait time too long ({wait_time:.0f}s), skipping")
                                    return None
                            except:
                                time.sleep(5)
                        else:
                            time.sleep(5)
                        continue

                    # Successful response
                    if response.status_code == 200:
                        time.sleep(delay)
                        return response.json()
                    elif response.status_code == 404:
                        return None
                    elif response.status_code == 500 or response.status_code == 502 or response.status_code == 503:
                        # Server error, retry
                        logger.warning(
                            f"Server error {response.status_code} (attempt {attempt + 1}/{max_attempts}): {url[:100]}")
                        if attempt < max_attempts - 1:
                            time.sleep(2 ** attempt)  # Exponential backoff
                            continue
                        else:
                            return None
                    else:
                        logger.error(f"API call failed {response.status_code}: {url[:100]}")
                        if attempt < max_attempts - 1:
                            time.sleep(1)
                        else:
                            return None

                except requests.exceptions.Timeout as e:
                    logger.error(f"Request timed out ({timeout}s) attempt {attempt + 1}/{max_attempts}: {url[:100]}")
                    if attempt < max_attempts - 1:
                        time.sleep(2)
                    else:
                        return None

                except requests.exceptions.ConnectionError as e:
                    logger.error(f"Connection error attempt {attempt + 1}/{max_attempts}: {url[:100]} - {e}")
                    if attempt < max_attempts - 1:
                        time.sleep(3)
                    else:
                        return None

                except requests.exceptions.RequestException as e:
                    logger.error(f"Request exception attempt {attempt + 1}/{max_attempts}: {url[:100]} - {e}")
                    if attempt < max_attempts - 1:
                        time.sleep(1)
                    else:
                        return None

                finally:
                    self.api_semaphore.release()

            except Exception as e:
                logger.error(f"Unknown exception attempt {attempt + 1}/{max_attempts}: {url[:100]} - {e}")
                if attempt < max_attempts - 1:
                    time.sleep(1)
                else:
                    return None

        return None

    def _parse_github_datetime(self, dt_str: str) -> datetime:
        """Parse a GitHub datetime string"""
        if not dt_str:
            return None

        if dt_str.endswith('Z'):
            dt_str = dt_str.replace('Z', '+00:00')

        if '+' not in dt_str and '-' not in dt_str[-6:]:
            dt = datetime.fromisoformat(dt_str)
            return dt.replace(tzinfo=timezone.utc)
        else:
            return datetime.fromisoformat(dt_str)

    def get_old_prs_parallel(self, owner: str, repo: str) -> List[Dict]:
        """Fetch old PRs in parallel (balances speed and stability)"""
        cutoff_date = (datetime.now() - timedelta(days=self.min_pr_age_days)).strftime('%Y-%m-%d')

        # Get the total count first - using the search API
        count_url = "https://api.github.com/search/issues"
        count_params = {
            'q': f'repo:{owner}/{repo} is:pr created:<{cutoff_date}',
            'per_page': 1
        }

        # Search API delay
        time.sleep(self.search_api_delay)
        count_data = self.make_api_call(count_url, count_params, is_search=True)
        total_count = count_data.get('total_count', 0) if count_data else 0

        logger.info(f"Repository {owner}/{repo} has {total_count} old PRs in total")

        # Determine how many to fetch
        max_count = min(self.max_prs_per_repo, total_count) if self.max_prs_per_repo else total_count

        if max_count == 0:
            return []

        # Compute the number of pages needed
        per_page = 100  # Increased to 100 to reduce the number of pages
        total_pages = (max_count + per_page - 1) // per_page
        pages_to_fetch = min(total_pages, 8)  # At most 8 pages

        def fetch_page(page_num: int) -> List[Dict]:
            """Fetch a single page of PR data"""
            # Delay between pages
            time.sleep(self.search_api_delay)

            session = self._get_session()
            url = "https://api.github.com/search/issues"
            params = {
                'q': f'repo:{owner}/{repo} is:pr created:<{cutoff_date}',
                'sort': 'created',
                'order': 'asc',
                'per_page': per_page,
                'page': page_num
            }

            try:
                data = self.make_api_call(url, params, session, is_search=True)
                if data and 'items' in data:
                    return data['items']
            except Exception as e:
                logger.error(f"Failed to fetch page {page_num}: {e}")
            finally:
                session.close()

            return []

        # Use low concurrency for the search pages (the search API has stricter limits)
        search_workers = min(2, pages_to_fetch)
        all_prs = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=search_workers) as executor:
            future_to_page = {executor.submit(fetch_page, page): page for page in range(1, pages_to_fetch + 1)}

            for future in concurrent.futures.as_completed(future_to_page):
                page = future_to_page[future]
                try:
                    items = future.result()
                    all_prs.extend(items)
                    logger.debug(f"Page {page} returned {len(items)} PRs")
                except Exception as e:
                    logger.error(f"Error while processing the results of page {page}: {e}")

        # Sort by creation time and cap the count
        all_prs.sort(key=lambda x: x.get('created_at', ''))
        all_prs = all_prs[:max_count]

        logger.info(f"Fetching complete, {len(all_prs)} old PRs in total")
        return all_prs

    def get_pr_comprehensive_details(self, owner: str, repo: str, pr_number: int, timeout: int = 45) -> Optional[Dict]:
        """Get the complete information of a PR (details, reviews, comments) - with timeout control"""
        pr_data = {
            'pr_details': None,
            'reviews': [],
            'comments': [],
            'combined_data': None
        }

        try:
            # If a GitHub token was provided, try the GraphQL API first
            current_token = self.token_manager.get_token()
            if current_token:
                try:
                    # Use the semaphore to limit GraphQL calls
                    acquired = self.api_semaphore.acquire(timeout=timeout)
                    if not acquired:
                        logger.debug(f"Timed out acquiring the semaphore, falling back to the REST API for PR #{pr_number}")
                        return self._get_pr_details_rest(owner, repo, pr_number)

                    try:
                        # Check the rate limit
                        self._check_and_wait_if_needed(is_search=False)
                        self.token_manager.record_request(is_search=False)

                        query = """
                        query GetPRDetails($owner: String!, $repo: String!, $prNumber: Int!) {
                          repository(owner: $owner, name: $repo) {
                            pullRequest(number: $prNumber) {
                              title
                              state
                              createdAt
                              closedAt
                              mergedAt
                              merged
                              url
                              author {
                                login
                                url
                              }
                              reviews(first: 50) {
                                nodes {
                                  state
                                  body
                                  submittedAt
                                  author {
                                    login
                                    url
                                  }
                                }
                              }
                              comments(first: 50) {
                                nodes {
                                  body
                                  createdAt
                                  author {
                                    login
                                    url
                                  }
                                }
                              }
                            }
                          }
                        }
                        """

                        graphql_url = "https://api.github.com/graphql"
                        headers = {
                            'Authorization': f'Bearer {current_token}',
                            'Content-Type': 'application/json',
                        }

                        variables = {
                            "owner": owner,
                            "repo": repo,
                            "prNumber": pr_number
                        }

                        response = requests.post(
                            graphql_url,
                            headers=headers,
                            json={'query': query, 'variables': variables},
                            timeout=timeout
                        )

                        time.sleep(self.api_delay)

                        # Handle GraphQL rate limiting
                        if response.status_code in [403, 429]:
                            logger.warning(f"GraphQL API rate limited, switching to the REST API")
                            self.token_manager.report_rate_limit()
                            return self._get_pr_details_rest(owner, repo, pr_number)

                        if response.status_code == 200:
                            data = response.json()
                            if 'errors' in data:
                                logger.debug(f"GraphQL error: {data['errors']}")
                                return self._get_pr_details_rest(owner, repo, pr_number)

                            pr_node = data.get('data', {}).get('repository', {}).get('pullRequest', {})

                            if pr_node:
                                # Build a data structure compatible with the REST API
                                pr_data['pr_details'] = {
                                    'title': pr_node.get('title'),
                                    'state': pr_node.get('state'),
                                    'created_at': pr_node.get('createdAt'),
                                    'closed_at': pr_node.get('closedAt'),
                                    'merged_at': pr_node.get('mergedAt'),
                                    'merged': pr_node.get('merged'),
                                    'html_url': pr_node.get('url'),
                                    'user': pr_node.get('author')
                                }

                                pr_data['reviews'] = pr_node.get('reviews', {}).get('nodes', [])
                                pr_data['comments'] = pr_node.get('comments', {}).get('nodes', [])

                                logger.debug(f"Successfully fetched the data of PR #{pr_number} via GraphQL")
                                return pr_data
                    finally:
                        self.api_semaphore.release()

                except requests.exceptions.Timeout:
                    logger.debug(f"GraphQL API timed out, falling back to the REST API for PR #{pr_number}")
                    return self._get_pr_details_rest(owner, repo, pr_number)
                except Exception as e:
                    logger.debug(f"GraphQL API failed, falling back to the REST API: {e}")

            # Use the REST API
            return self._get_pr_details_rest(owner, repo, pr_number)

        except Exception as e:
            logger.error(f"Failed to get the complete information of PR #{pr_number}: {e}")
            return None

    def _get_pr_details_rest(self, owner: str, repo: str, pr_number: int) -> Optional[Dict]:
        """Get PR details via the REST API (fetched in parallel)"""
        pr_data = {
            'pr_details': None,
            'reviews': [],
            'comments': []
        }

        try:
            # Fetch the three REST API endpoints in parallel
            def fetch_details():
                url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
                return self.make_api_call(url)

            def fetch_reviews():
                url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
                data = self.make_api_call(url, {'per_page': 50})
                return data if data else []

            def fetch_comments():
                comments = []
                # Get the review comments
                url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/comments"
                data = self.make_api_call(url, {'per_page': 50})
                if data:
                    comments.extend(data)

                time.sleep(0.3)  # Small delay

                # Get the issue comments
                url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
                data = self.make_api_call(url, {'per_page': 50})
                if data:
                    comments.extend(data)
                return comments

            # Run in parallel with a thread pool (3 in parallel)
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                future_details = executor.submit(fetch_details)
                future_reviews = executor.submit(fetch_reviews)
                future_comments = executor.submit(fetch_comments)

                pr_data['pr_details'] = future_details.result()
                pr_data['reviews'] = future_reviews.result()
                pr_data['comments'] = future_comments.result()

            if not pr_data['pr_details']:
                return None

            return pr_data

        except Exception as e:
            logger.error(f"REST API failed to get PR #{pr_number}: {e}")
            return None

    def _is_excluded_user(self, username: str) -> bool:
        """Check whether the user is excluded"""
        if not username:
            return True

        if username in self.excluded_users:
            return True

        if '[bot]' in username:
            return True

        return False

    def _is_generic_content(self, text: str) -> Tuple[bool, str]:
        """Check whether the content is generic/templated"""
        if not text:
            return True, "Empty comment"

        text_lower = text.strip().lower()

        if len(text_lower) < self.min_comment_length:
            return True, f"Comment too short (fewer than {self.min_comment_length} characters)"

        # Check the keywords
        for keyword in self.suspicious_keywords:
            if keyword.lower() in text_lower:
                return True, f"Contains a suspicious keyword: {keyword}"

        # Check the regex patterns
        for pattern in self.generic_patterns:
            try:
                if re.match(pattern, text_lower, re.IGNORECASE):
                    return True, "Matches a generic comment pattern"
            except re.error:
                continue

        return False, ""

    def analyze_pr_activity(self, owner: str, repo: str, pr: Dict) -> List[AbuseEvidence]:
        """Analyze all activity on a PR (using the optimized method)"""
        evidences = []
        pr_number = pr['number']
        pr_title = pr.get('title', 'No title')[:50]
        pr_created = pr['created_at']
        pr_state = pr.get('state', 'open')
        pr_url = pr['html_url']
        pr_closed_at = pr.get('closed_at')

        logger.debug(f"Analyzing PR #{pr_number}: {pr_title}")

        # Get the complete PR information
        pr_comprehensive = self.get_pr_comprehensive_details(owner, repo, pr_number)
        if not pr_comprehensive:
            return evidences

        # Extract the data
        pr_details = pr_comprehensive['pr_details']
        reviews = pr_comprehensive['reviews']
        comments = pr_comprehensive['comments']

        # Get the merge state
        if pr_details:
            pr_merged = pr_details.get('merged', False)
            merge_date = pr_details.get('merged_at')
        else:
            pr_merged = False
            merge_date = None

        # Parse the timestamps
        pr_date = self._parse_github_datetime(pr_created)
        if not pr_date:
            return evidences

        # Determine the resolution time
        resolution_date = pr_date
        resolution_type = "created"

        if pr_merged and merge_date:
            resolution_date = self._parse_github_datetime(merge_date)
            if resolution_date:
                resolution_type = "merged"
        elif pr_closed_at:
            resolution_date = self._parse_github_datetime(pr_closed_at)
            if resolution_date:
                resolution_type = "closed"

        # Analyze the reviews
        for review in reviews:
            # Handle the different data structures of GraphQL and the REST API
            if isinstance(review, dict):
                if 'author' in review:  # GraphQL format
                    user = review.get('author', {})
                    review_state = review.get('state', '')
                    review_body = review.get('body', '')
                    submitted_at = review.get('submittedAt', '')
                else:  # REST API format
                    user = review.get('user', {})
                    review_state = review.get('state', '')
                    review_body = review.get('body', '')
                    submitted_at = review.get('submitted_at', '')
            else:
                continue

            username = user.get('login', '') if user else ''

            # Check whether the user is excluded
            if self._is_excluded_user(username):
                continue

            if not submitted_at:
                continue

            review_date = self._parse_github_datetime(submitted_at)
            if not review_date:
                continue

            # Compute the number of days after resolution
            days_after_resolution = (review_date - resolution_date).days if resolution_date else 0

            # Only care about activity after resolution
            if days_after_resolution <= 0:
                continue

            # Detection: activity long after the PR was resolved
            if days_after_resolution > self.post_resolution_delay_days:
                is_generic, generic_reason = self._is_generic_content(review_body)

                if is_generic or review_state == 'APPROVED':
                    suspicious_reason = f"Activity {days_after_resolution} days after the PR was {resolution_type}"
                    if is_generic:
                        suspicious_reason += f", generic comment content: {generic_reason}"

                    evidence = AbuseEvidence(
                        user_name=username,
                        repo_name=f"{owner}/{repo}",
                        repo_url=f"https://github.com/{owner}/{repo}",
                        target_url=pr_url,
                        target_type='pr',
                        action_type='approve' if review_state == 'APPROVED' else 'review',
                        action_date=submitted_at,
                        content=review_body[:200] if review_body else '',
                        suspicious_reason=suspicious_reason,
                        pr_state='merged' if pr_merged else pr_state,
                        pr_merged=pr_merged,
                        days_after_resolution=days_after_resolution
                    )
                    evidences.append(evidence)

        # Analyze the PR comments
        for comment in comments:
            # Handle the different data structures of GraphQL and the REST API
            if isinstance(comment, dict):
                if 'author' in comment:  # GraphQL format
                    user = comment.get('author', {})
                    comment_body = comment.get('body', '')
                    created_at = comment.get('createdAt', '')
                    html_url = None
                else:  # REST API format
                    user = comment.get('user', {})
                    comment_body = comment.get('body', '')
                    created_at = comment.get('created_at', '')
                    html_url = comment.get('html_url')
            else:
                continue

            username = user.get('login', '') if user else ''

            # Check whether the user is excluded
            if self._is_excluded_user(username):
                continue

            if not created_at:
                continue

            comment_date = self._parse_github_datetime(created_at)
            if not comment_date:
                continue

            # Compute the number of days after resolution
            days_after_resolution = (comment_date - resolution_date).days if resolution_date else 0

            # Only care about activity after resolution
            if days_after_resolution <= 0:
                continue

            # Check for new comments long after the PR was resolved
            if days_after_resolution > self.post_resolution_delay_days:
                is_generic, reason = self._is_generic_content(comment_body)

                # Use the comment URL or the PR URL
                target_url = html_url or pr_url

                evidence = AbuseEvidence(
                    user_name=username,
                    repo_name=f"{owner}/{repo}",
                    repo_url=f"https://github.com/{owner}/{repo}",
                    target_url=target_url,
                    target_type='pr',
                    action_type='comment',
                    action_date=created_at,
                    content=comment_body[:200] if comment_body else '',
                    suspicious_reason=f"Comment {days_after_resolution} days after the PR was {resolution_type}: {reason or 'possibly meaningless'}",
                    pr_state='merged' if pr_merged else pr_state,
                    pr_merged=pr_merged,
                    days_after_resolution=days_after_resolution
                )
                evidences.append(evidence)

        return evidences

    def analyze_pr_parallel(self, owner: str, repo: str, pr: Dict) -> List[AbuseEvidence]:
        """Analyze a single PR (for multithreaded use)"""
        try:
            return self.analyze_pr_activity(owner, repo, pr)
        except Exception as e:
            logger.error(f"Error while analyzing PR #{pr.get('number', 'unknown')}: {e}")
            return []

    def detect_repository_abuse(self, repo_data: Dict) -> Tuple[bool, RepositoryAbuseReport]:
        """
        Detect whether the repository has PR abuse (data-driven mode + multithreading optimization)

        Args:
            repo_data: Repository JSON data (contains the basic repository information)

        Returns:
            tuple: (whether abuse exists, report object)
        """
        try:
            # Extract the repository information from the repository JSON data
            repo_url = repo_data.get('html_url', '')
            if not repo_url:
                logger.error(f"The repository data has no html_url field")
                return False, None

            # Extract the owner and repository name
            pattern = r"github\.com/([^/]+)/([^/?]+)"
            match = re.search(pattern, repo_url)
            if not match:
                logger.error(f"Invalid GitHub repository URL: {repo_url}")
                return False, None

            owner, repo = match.group(1), match.group(2)
            repo_full_name = repo_data.get('full_name', f"{owner}/{repo}")

            logger.info(f"Starting PR abuse detection for repository {repo_full_name}")

            # Get the old PRs
            old_prs = self.get_old_prs_parallel(owner, repo)
            logger.info(f"Got {len(old_prs)} old PRs")

            if not old_prs:
                logger.info(f"Repository {repo_full_name} has no old PRs")
                return False, RepositoryAbuseReport(
                    repository=repo_full_name,
                    repo_url=f"https://github.com/{owner}/{repo}",
                    has_abuse_behavior=False,
                    abuse_user_count=0,
                    abuse_activity_count=0,
                    suspicious_users_evidence=[],
                    timestamp=datetime.now().isoformat()
                )

            # Analyze all PRs in parallel
            all_evidences = []
            completed = 0
            total = len(old_prs)

            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                # Submit all analysis tasks
                future_to_pr = {
                    executor.submit(self.analyze_pr_parallel, owner, repo, pr): pr
                    for pr in old_prs
                }

                # Collect the results
                for future in concurrent.futures.as_completed(future_to_pr):
                    pr = future_to_pr[future]
                    try:
                        evidences = future.result()
                        all_evidences.extend(evidences)
                        completed += 1

                        # Print the progress every 50 completed PRs
                        if completed % 50 == 0:
                            logger.info(f"  PR analysis progress: {completed}/{total}, {len(all_evidences)} suspicious activities found so far")
                    except Exception as e:
                        logger.error(f"Error while analyzing PR #{pr.get('number', 'unknown')}: {e}")

            logger.info(f"PR analysis complete, {len(all_evidences)} suspicious activities found in total")

            # Group the evidence by user
            user_evidences_map = {}
            for evidence in all_evidences:
                username = evidence.user_name
                if username not in user_evidences_map:
                    # Build the user URL
                    user_url = f"https://github.com/{username}"
                    user_evidences_map[username] = UserAbuseEvidence(
                        user_name=username,
                        user_url=user_url,
                        abuse_count=0,
                        evidences=[],
                        first_activity=evidence.action_date,
                        last_activity=evidence.action_date
                    )

                user_evidences_map[username].evidences.append(evidence)
                user_evidences_map[username].abuse_count += 1

                # Update the first/last activity timestamps
                if evidence.action_date < user_evidences_map[username].first_activity:
                    user_evidences_map[username].first_activity = evidence.action_date
                if evidence.action_date > user_evidences_map[username].last_activity:
                    user_evidences_map[username].last_activity = evidence.action_date

            # Build the suspicious user evidence list sorted by abuse count
            suspicious_users_evidence = sorted(
                user_evidences_map.values(),
                key=lambda u: u.abuse_count,
                reverse=True
            )

            # Statistics
            abuse_user_count = len(user_evidences_map)
            abuse_activity_count = len(all_evidences)

            # Decide whether abuse exists
            has_abuse = abuse_activity_count > 0

            # Print token usage statistics - fixed version
            token_stats = self.token_manager.get_stats()
            logger.info(f"Total tokens: {token_stats['token_count']}")
            logger.info(f"Currently in use: token #{token_stats['current_token_index']}")

            # Generate the report
            report = RepositoryAbuseReport(
                repository=repo_full_name,
                repo_url=f"https://github.com/{owner}/{repo}",
                has_abuse_behavior=has_abuse,
                abuse_user_count=abuse_user_count,
                abuse_activity_count=abuse_activity_count,
                suspicious_users_evidence=suspicious_users_evidence,
                timestamp=datetime.now().isoformat()
            )

            logger.info(
                f"Repository {repo_full_name} detection complete: "
                f"abuse={has_abuse}, users={abuse_user_count}, activities={abuse_activity_count}"
            )

            return has_abuse, report

        except Exception as e:
            logger.error(f"Error while detecting the repository: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False, None

    def get_token_stats(self) -> Dict:
        """Get token usage statistics"""
        return self.token_manager.get_stats()