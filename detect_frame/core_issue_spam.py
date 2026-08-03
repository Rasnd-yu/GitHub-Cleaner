"""
Issue spam core detection module.
Detects spam content in GitHub repository issues using a machine learning model.
"""

import logging
import pickle
import time
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import os
os.environ['NUMEXPR_MAX_THREADS'] = '16'

logger = logging.getLogger(__name__)


@dataclass
class UserSpamEvidence:
    """Evidence collected for a single user."""
    username: str
    user_url: str
    spam_issues: List[Dict] = field(default_factory=list)
    spam_count: int = 0


@dataclass
class IssueSpamEvidence:
    """Evidence for issue spam detection."""
    repo_full_name: str
    total_issues: int
    spam_count: int
    spam_ratio: float
    is_abuse: bool
    spam_evidence: List[UserSpamEvidence]  # Evidence grouped by user
    detection_reason: str


class IssueSpamCoreDetector:
    """Issue spam core detector using a data-driven approach."""

    def __init__(self, github_token: str = None, config: Dict = None):
        """
        Initialize the core detector.

        Args:
            github_token: GitHub API token used to fetch issues
            config: Configuration parameters
        """
        self.github_token = github_token
        self.config = config or {}

        # Detection parameters
        self.per_page = self.config.get('per_page', 100)
        self.max_issues_to_check = self.config.get('max_issues_to_check', 500)
        self.fetch_delay_seconds = self.config.get('fetch_delay_seconds', 0.8)
        self.max_retries = self.config.get('max_retries', 3)
        self.request_timeout = self.config.get('request_timeout', 30)
        self.rate_limit_delay = self.config.get('rate_limit_delay', 1.0)

        # Threading configuration
        self.predict_workers = self.config.get('predict_workers', 10)  # Prediction concurrency
        self.fetch_workers = self.config.get('fetch_workers', 3)  # Issue fetch concurrency

        # Model path
        self.model_path = self.config.get('model_path',
                                          'mlartifacts/2/0579ea92a6c7494e9bfdf42813fe3867/artifacts/nn/model.pkl')

        # Load model
        self.model = self._load_model()
        self.model_loaded = self.model is not None

        # Session (lazy-loaded)
        self._session = None
        self._session_lock = Lock()

    def _load_model(self):
        """Load the spam detection model."""
        try:
            import pickle
            with open(self.model_path, 'rb') as f:
                model = pickle.load(f)
            logger.info(f"Issue spam detection model loaded successfully: {self.model_path}")
            return model
        except Exception as e:
            logger.error(f"Model loading failed: {e}")
            return None

    def _get_session(self):
        """Create or reuse a thread-safe session."""
        if self._session is None:
            with self._session_lock:
                if self._session is None:
                    import requests
                    self._session = requests.Session()
                    self._session.headers.update({
                        'Accept': 'application/vnd.github.v3+json',
                        'User-Agent': 'GitHub-Abuse-Detector/1.0'
                    })
                    if self.github_token:
                        self._session.headers['Authorization'] = f'token {self.github_token}'
        return self._session

    def _make_api_call(self, url: str, params: Dict = None) -> Optional[Dict]:
        """Perform a safe API call."""
        session = self._get_session()

        for attempt in range(self.max_retries):
            try:
                response = session.get(url, params=params, timeout=self.request_timeout)

                # Handle rate limiting
                if response.status_code == 403 and 'rate limit' in response.text.lower():
                    reset_time = response.headers.get('X-RateLimit-Reset', 0)
                    if reset_time:
                        wait_time = max(int(reset_time) - time.time(), 0) + 2
                        logger.warning(f"API rate limit reached; waiting {wait_time:.0f} seconds...")
                        time.sleep(wait_time)
                        continue

                if response.status_code == 200:
                    time.sleep(self.rate_limit_delay)
                    return response.json()
                elif response.status_code == 404:
                    return None
                else:
                    logger.error(f"API request failed: {response.status_code} - {response.text[:100]}")

            except Exception as e:
                logger.error(f"Request exception: {e}")
                if attempt < self.max_retries - 1:
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)

        return None

    def _fetch_issues_single_page(self, owner: str, repo_name: str, page: int) -> List[Dict]:
        """Fetch a single page of issues."""
        url = f"https://api.github.com/repos/{owner}/{repo_name}/issues"
        params = {
            "state": "all",
            "per_page": self.per_page,
            "page": page,
            "sort": "created",
            "direction": "desc"
        }

        try:
            data = self._make_api_call(url, params)
            if not data:
                return []

            # Filter out pull requests
            real_issues = []
            for issue in data:
                if "pull_request" not in issue:
                    real_issues.append(issue)

            return real_issues
        except Exception as e:
            logger.error(f"Failed to fetch issues on page {page}: {e}")
            return []

    def _fetch_issues_multithreaded(self, owner: str, repo_name: str) -> List[Dict]:
        """Fetch all issues for a repository using multiple threads."""
        logger.info(f"Starting multi-threaded issue fetch for repository {owner}/{repo_name}...")

        # Fetch the first page first to determine the total number of pages
        first_page_issues = self._fetch_issues_single_page(owner, repo_name, 1)
        if not first_page_issues:
            logger.info("The repository has no issues")
            return []

        all_issues = list(first_page_issues)

        # Check whether there are more pages
        if len(first_page_issues) < self.per_page:
            logger.info(f"Issue fetching complete; collected {len(all_issues)} issues")
            return all_issues[:self.max_issues_to_check]

        # Estimate the number of pages to fetch
        max_pages = min(
            (self.max_issues_to_check + self.per_page - 1) // self.per_page,
            100  # GitHub API limit is 100 pages
        )

        # Fetch the remaining pages in parallel
        pages_to_fetch = list(range(2, max_pages + 1))

        with ThreadPoolExecutor(max_workers=self.fetch_workers) as executor:
            futures = {
                executor.submit(self._fetch_issues_single_page, owner, repo_name, page): page
                for page in pages_to_fetch
            }

            for future in as_completed(futures):
                page = futures[future]
                try:
                    issues = future.result()
                    if issues:
                        all_issues.extend(issues)
                        logger.info(f"Page {page}: fetched {len(issues)} issues; total {len(all_issues)}")

                    # If a page contains fewer than per_page items, there are no more pages
                    if len(issues) < self.per_page:
                        logger.info(f"Page {page} returned insufficient data; stopping further fetches")
                        # Cancel the remaining tasks
                        for f in futures:
                            if not f.done():
                                f.cancel()
                        break

                except Exception as e:
                    logger.error(f"Failed to fetch page {page}: {e}")

                # Add a short delay to avoid rate limiting
                time.sleep(self.fetch_delay_seconds)

        # Sort by creation time in descending order
        all_issues.sort(key=lambda x: x.get('created_at', ''), reverse=True)

        logger.info(f"Finished fetching {len(all_issues)} issues")
        return all_issues[:self.max_issues_to_check]

    def _prepare_issue_text(self, issue: Dict) -> str:
        """Prepare issue text for prediction."""
        title = issue.get("title", "")
        body = issue.get("body", "") or ""
        return f"{title} {body}".strip()

    def _predict_single_spam(self, issue: Dict) -> Tuple[Dict, bool]:
        """Predict whether a single issue is spam."""
        issue_text = self._prepare_issue_text(issue)
        if not issue_text or len(issue_text.strip()) < 5:
            return issue, False

        try:
            prediction = self.model.predict([issue_text])[0]
            is_spam = prediction == 'spam'
            return issue, is_spam
        except Exception as e:
            logger.warning(f"Prediction for issue {issue.get('id')} failed: {e}")
            return issue, False

    def _predict_spam_batch(self, issues: List[Dict]) -> List[Tuple[Dict, bool]]:
        """Predict issues in batches using multiple threads."""
        results = []

        with ThreadPoolExecutor(max_workers=self.predict_workers) as executor:
            futures = [executor.submit(self._predict_single_spam, issue) for issue in issues]

            for future in as_completed(futures):
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    logger.error(f"Prediction task failed: {e}")

        return results

    def detect(self, repo_data: Dict) -> Tuple[bool, Optional[IssueSpamEvidence]]:
        """
        Detect issue spam in a repository using a data-driven approach.

        Args:
            repo_data: Repository JSON data containing fields such as full_name, owner, and html_url

        Returns:
            (is_abuse, evidence object)
        """
        try:
            # Extract repository information from the JSON payload
            full_name = repo_data.get('full_name', '')
            if not full_name:
                logger.error("Repository data is missing the full_name field")
                return False, None

            owner, repo_name = full_name.split('/') if '/' in full_name else (None, None)
            if not owner or not repo_name:
                logger.error(f"Unable to parse repository name: {full_name}")
                return False, None

            # Check whether the model loaded successfully
            if not self.model_loaded:
                return False, IssueSpamEvidence(
                    repo_full_name=full_name,
                    total_issues=0,
                    spam_count=0,
                    spam_ratio=0.0,
                    is_abuse=False,
                    spam_evidence=[],
                    detection_reason="The spam detection model was not loaded"
                )

            # Fetch all issues (multi-threaded)
            issues = self._fetch_issues_multithreaded(owner, repo_name)

            if len(issues) == 0:
                return False, IssueSpamEvidence(
                    repo_full_name=full_name,
                    total_issues=0,
                    spam_count=0,
                    spam_ratio=0.0,
                    is_abuse=False,
                    spam_evidence=[],
                    detection_reason="No issues were found in the repository"
                )

            logger.info(f"Starting detection on {len(issues)} issues...")

            # Run predictions in parallel
            prediction_results = self._predict_spam_batch(issues)

            # Collect spam issues
            spam_issues = []
            for issue, is_spam in prediction_results:
                if is_spam:
                    user_login = issue.get('user', {}).get('login', 'unknown')
                    user_url = issue.get('user', {}).get('html_url', '')

                    spam_issues.append({
                        "id": issue.get("id"),
                        "title": issue.get("title", "")[:80] + "..." if len(issue.get("title", "")) > 80 else issue.get(
                            "title", ""),
                        "url": issue.get("html_url", ""),
                        "created_at": issue.get("created_at", ""),
                        "state": issue.get("state", "unknown"),
                        "username": user_login,
                        "user_url": user_url
                    })

            # Group spam evidence by user
            user_spam_map: Dict[str, UserSpamEvidence] = {}
            for spam_issue in spam_issues:
                username = spam_issue.pop('username')
                user_url = spam_issue.pop('user_url')

                if username not in user_spam_map:
                    user_spam_map[username] = UserSpamEvidence(
                        username=username,
                        user_url=user_url
                    )

                user_spam_map[username].spam_issues.append(spam_issue)
                user_spam_map[username].spam_count += 1

            # Convert to a list
            spam_evidence = list(user_spam_map.values())

            # Sort by spam count
            spam_evidence.sort(key=lambda x: x.spam_count, reverse=True)

            # Abuse logic: any spam issue marks the repository as abusive
            is_abuse = len(spam_issues) > 0

            total_issues = len(issues)
            spam_count = len(spam_issues)
            spam_ratio = spam_count / total_issues if total_issues > 0 else 0

            if is_abuse:
                # Generate a concise detection reason
                user_summary = ", ".join([f"{u.username}({u.spam_count})" for u in spam_evidence[:5]])
                if len(spam_evidence) > 5:
                    user_summary += f" and {len(spam_evidence) - 5} more users"
                detection_reason = f"Found {spam_count} spam issues from {len(spam_evidence)} users: {user_summary}"
            else:
                detection_reason = f"No spam issues found (checked {total_issues} issues in total)"

            evidence = IssueSpamEvidence(
                repo_full_name=full_name,
                total_issues=total_issues,
                spam_count=spam_count,
                spam_ratio=spam_ratio,
                is_abuse=is_abuse,
                spam_evidence=spam_evidence,
                detection_reason=detection_reason
            )

            return is_abuse, evidence

        except Exception as e:
            logger.error(f"Issue spam detection failed: {e}")
            return False, None

    def close(self):
        """Close the session."""
        if self._session:
            self._session.close()
            self._session = None