"""
Core module for detecting automated update abuse.
Detects whether a repository exhibits automated update abuse behavior.
"""

import re
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)


@dataclass
class AutomaticUpdateEvidence:
    """Evidence for automated update detection."""
    repo_full_name: str
    total_commits: int
    avg_changes: float
    is_abuse: bool
    details: Dict[str, Any]


class AutomaticUpdatesCoreDetector:
    """
    Core detector for automated update abuse.

    Detection logic: analyze commit activity within a specified time window;
    if the number of commits exceeds the threshold and the average change size is small,
    the repository is flagged as automated update abuse.
    """

    def __init__(self, github_token: Optional[str] = None, config: Optional[Dict] = None):
        """
        Initialize the detector.

        Args:
            github_token: GitHub API token
            config: Detection configuration
        """
        self.github_token = github_token

        # Default configuration
        self.config = {
            'time_window_hours': 24,
            'min_commits': 10,
            'max_avg_changes': 5,
            'commit_delay_seconds': 0.1,
            'max_commits_to_check': 20,
            'max_retries': 3,
            'request_timeout': 30,
            'rate_limit_delay': 1.0
        }

        # Update configuration
        if config:
            self.config.update(config)

        # Create the session
        self.session = requests.Session()
        self.session.headers.update({
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'GitHub-Abuse-Detector-AutoUpdates/1.0'
        })

        if self.github_token:
            self.session.headers['Authorization'] = f'token {self.github_token}'

    def make_api_call(self, url: str, params: Dict = None) -> Optional[Dict]:
        """Perform a safe API call."""
        max_retries = self.config.get('max_retries', 3)
        request_timeout = self.config.get('request_timeout', 30)
        rate_limit_delay = self.config.get('rate_limit_delay', 1.0)

        for attempt in range(max_retries):
            try:
                response = self.session.get(url, params=params, timeout=request_timeout)

                # Handle rate limiting
                if response.status_code == 403 and 'rate limit' in response.text.lower():
                    reset_time = response.headers.get('X-RateLimit-Reset', 0)
                    if reset_time:
                        wait_time = max(int(reset_time) - time.time(), 0) + 2
                        logger.warning(f"API rate limit reached; waiting {wait_time:.0f} seconds...")
                        time.sleep(wait_time)
                        continue

                if response.status_code == 200:
                    time.sleep(rate_limit_delay)
                    return response.json()
                elif response.status_code == 404:
                    return None
                else:
                    logger.error(f"API request failed: {response.status_code} - {response.text[:100]}")

            except requests.exceptions.RequestException as e:
                logger.error(f"Request exception: {e}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)

        return None

    def get_recent_commits(self, owner: str, repo_name: str) -> List[Dict]:
        """
        Retrieve recent commits.

        Args:
            owner: Repository owner
            repo_name: Repository name

        Returns:
            List of commits
        """
        time_window_hours = self.config.get('time_window_hours', 24)
        since_time = datetime.now(timezone.utc) - timedelta(hours=time_window_hours)
        since_str = since_time.isoformat().replace('+00:00', 'Z')

        url = f"https://api.github.com/repos/{owner}/{repo_name}/commits"
        params = {"since": since_str, "per_page": 100}

        data = self.make_api_call(url, params)
        return data if data else []

    def get_commit_details(self, owner: str, repo_name: str, commit_sha: str) -> Optional[Dict]:
        """
        Retrieve commit details.

        Args:
            owner: Repository owner
            repo_name: Repository name
            commit_sha: Commit SHA

        Returns:
            Commit details
        """
        url = f"https://api.github.com/repos/{owner}/{repo_name}/commits/{commit_sha}"
        return self.make_api_call(url)

    def detect(self, repo_data: Dict) -> Tuple[bool, Optional[AutomaticUpdateEvidence]]:
        """
        Detect whether a repository exhibits automated update abuse.

        Args:
            repo_data: Repository JSON data that must include owner.login, name, or html_url

        Returns:
            (is_abuse, evidence object)
        """
        try:
            # Extract repository information from the data
            owner, repo_name = self._extract_repo_info(repo_data)

            if not owner or not repo_name:
                logger.error(f"Unable to extract repository information from the provided data: {repo_data}")
                return False, None

            repo_full_name = f"{owner}/{repo_name}"
            logger.info(f"Starting automated update detection: {repo_full_name}")

            # Retrieve recent commits
            commits = self.get_recent_commits(owner, repo_name)

            if len(commits) < self.config.get('min_commits', 10):
                logger.info(f"Insufficient number of commits: {len(commits)} < {self.config.get('min_commits')}")
                return False, AutomaticUpdateEvidence(
                    repo_full_name=repo_full_name,
                    total_commits=len(commits),
                    avg_changes=0,
                    is_abuse=False,
                    details={
                        "total_commits": len(commits),
                        "reason": "Commit count below the threshold"
                    }
                )

            # Analyze commits
            total_changes = 0
            valid_commits = 0
            max_commits = self.config.get('max_commits_to_check', 20)
            commit_delay = self.config.get('commit_delay_seconds', 0.1)

            for commit in commits[:max_commits]:
                commit_details = self.get_commit_details(owner, repo_name, commit['sha'])
                if commit_details:
                    stats = commit_details.get('stats', {})
                    total_changes += stats.get('additions', 0) + stats.get('deletions', 0)
                    valid_commits += 1

                time.sleep(commit_delay)

            avg_changes = total_changes / valid_commits if valid_commits > 0 else 0

            # Determine whether the behavior is abusive
            min_commits = self.config.get('min_commits', 10)
            max_avg_changes = self.config.get('max_avg_changes', 5)
            is_abuse = (len(commits) >= min_commits and avg_changes <= max_avg_changes)

            evidence = AutomaticUpdateEvidence(
                repo_full_name=repo_full_name,
                total_commits=len(commits),
                avg_changes=avg_changes,
                is_abuse=is_abuse,
                details={
                    "total_commits": len(commits),
                    "valid_commits_checked": valid_commits,
                    "total_changes": total_changes,
                    "avg_changes": avg_changes,
                    "time_window_hours": self.config.get('time_window_hours', 24)
                }
            )

            logger.info(f"Detection complete: is_abuse={is_abuse}, total_commits={len(commits)}, avg_changes={avg_changes:.2f}")
            return is_abuse, evidence

        except Exception as e:
            logger.error(f"Automated update detection failed: {e}")
            return False, None

    def _extract_repo_info(self, repo_data: Dict) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract owner and repository name from repository data.

        Supports multiple data formats:
        1. Direct owner.login and name fields
        2. full_name field in format: owner/repo
        3. html_url field in format: https://github.com/owner/repo

        Args:
            repo_data: Repository JSON data

        Returns:
            (owner, repo_name)
        """
        # Method 1: Extract from owner field
        if 'owner' in repo_data and isinstance(repo_data['owner'], dict):
            owner = repo_data['owner'].get('login')
            if owner and 'name' in repo_data:
                return owner, repo_data['name']
            if owner and 'repo' in repo_data:
                return owner, repo_data['repo']

        # Method 2: Extract from full_name field
        if 'full_name' in repo_data:
            full_name = repo_data['full_name']
            if '/' in full_name:
                parts = full_name.split('/', 1)
                return parts[0], parts[1]

        # Method 3: Extract from html_url field
        if 'html_url' in repo_data:
            url = repo_data['html_url']
            owner, repo_name = self._extract_from_url(url)
            if owner and repo_name:
                return owner, repo_name

        # Method 4: Direct owner and repo strings
        if 'owner' in repo_data and isinstance(repo_data['owner'], str):
            if 'repo' in repo_data:
                return repo_data['owner'], repo_data['repo']
            if 'name' in repo_data:
                return repo_data['owner'], repo_data['name']

        logger.warning(f"Unable to extract owner/repo from repository data: {repo_data.keys()}")
        return None, None

    def _extract_from_url(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """Extract repository information from URL."""
        if not url:
            return None, None

        url = url.rstrip('/').rstrip('.git')
        pattern = r"github\.com/([^/]+)/([^/?]+)"
        match = re.search(pattern, url)
        if not match:
            return None, None
        return match.group(1), match.group(2)

    def close(self):
        """Close the session."""
        if self.session:
            self.session.close()