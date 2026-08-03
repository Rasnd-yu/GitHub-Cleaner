# core_spoofed_contributor.py
"""
Spoofed contributor core detection logic - detect whether a repository contains spoofed top contributors.
"""

import requests
import logging
import time
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Set, Optional, Tuple
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class AbuseEvidence:
    """Abuse evidence"""
    suspicious_contributor: str
    contributions: int
    reason: str
    contributor_info: Dict
    repo_info: Dict


class SpoofedContributorCoreDetector:
    """Core detector for spoofed contributors"""

    def __init__(self, github_token: str, config: Dict, corpus_path: str = "corpus_developers_famous.json"):
        self.github_token = github_token
        self.config = config
        self.corpus_path = corpus_path

        # Bot patterns to exclude
        self.bot_patterns = [
            r'bot$', r'^bot-', r'-bot$', r'\[bot\]$', r'\(bot\)$',
            r'github-actions', r'actions-user', r'dependabot', r'snyk-bot',
            r'renovate', r'greenkeeper', r'codecov', r'coveralls',
            r'auto', r'ci', r'test', r'build'
        ]

        # Load hot/core developers dataset
        self.hot_developers = self._load_hot_developers()

        # Cache
        self.user_cache = {}

        # Set up the session
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'token {self.github_token}',
            'Accept': 'application/vnd.github.v3+json'
        })

    def _load_hot_developers(self) -> Set[str]:
        """Load the set of well-known developer logins.

        Returns:
            Set of developer logins
        """
        hot_developers = set()

        if not os.path.exists(self.corpus_path):
            logger.warning(f"Hot developers dataset file not found: {self.corpus_path}")
            return hot_developers

        try:
            with open(self.corpus_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Two data formats are supported:
            # 1. Dict format: { "username": {...}, ... }
            # 2. List format: [ {...}, {...} ]
            if isinstance(data, dict):
                for login, info in data.items():
                    if isinstance(info, dict):
                        # Get login from the dict, or use the key as login
                        actual_login = info.get('login', login)
                        hot_developers.add(actual_login.lower())
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and 'login' in item:
                        hot_developers.add(item['login'].lower())

            logger.info(f"Loaded hot developers dataset: {len(hot_developers)} developers")

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse hot developers dataset: {e}")
        except Exception as e:
            logger.error(f"Error loading hot developers dataset: {e}")

        return hot_developers

    def is_bot_account(self, username: str) -> bool:
        """Check whether a username is a bot account"""
        if not username:
            return False

        username_lower = username.lower()

        # Check against bot patterns
        for pattern in self.bot_patterns:
            if re.search(pattern, username_lower, re.IGNORECASE):
                return True

        # Additional bot indicators
        if any(x in username_lower for x in ['[bot]', '(bot)', '_bot', 'bot_']):
            return True

        return False

    def is_top_contributor(self, username: str) -> bool:
        """Determine whether a user is a top contributor using the hot-developers dataset."""
        if not username:
            return False

        username_lower = username.lower()

        # Skip bot accounts
        if self.is_bot_account(username):
            return False

        # Check whether the user is in the popular developers dataset
        return username_lower in self.hot_developers

    def make_api_call(self, url: str, params: Dict = None) -> Optional[Dict]:
        """Safe API call with retry and rate-limit handling"""
        max_retries = self.config.get('max_retries', 3)
        request_timeout = self.config.get('request_timeout', 30)
        rate_limit_delay = self.config.get('rate_limit_delay', 2.0)

        for attempt in range(max_retries):
            try:
                response = self.session.get(url, params=params, timeout=request_timeout)

                # Handle rate limiting
                if response.status_code == 403 and 'rate limit' in response.text.lower():
                    reset_time = response.headers.get('X-RateLimit-Reset', 0)
                    if reset_time:
                        wait_time = max(int(reset_time) - time.time(), 0) + 2
                        logger.warning(f"API rate limit hit, waiting {wait_time:.0f} seconds...")
                        time.sleep(wait_time)
                        continue

                if response.status_code == 200:
                    time.sleep(rate_limit_delay)
                    return response.json()
                elif response.status_code == 404:
                    return None
                else:
                    logger.error(f"API call failed: {response.status_code} - {response.text[:100]}")

            except requests.exceptions.RequestException as e:
                logger.error(f"Request exception: {e}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)

        return None

    def get_user_info(self, username: str) -> Optional[Dict]:
        """Get user information."""
        if username in self.user_cache:
            return self.user_cache[username]

        user_data = self.make_api_call(f"https://api.github.com/users/{username}")

        if user_data:
            self.user_cache[username] = user_data

        return user_data

    def get_repository_contributors(self, owner: str, repo_name: str) -> List[Dict]:
        """Get repository contributors list."""
        try:
            max_contributors = self.config.get('max_contributors_per_repo', 30)
            params = {'per_page': max_contributors, 'anon': 'false'}

            url = f"https://api.github.com/repos/{owner}/{repo_name}/contributors"
            data = self.make_api_call(url, params)
            return data if data else []

        except Exception as e:
                logger.error(f"Failed to get contributors list: {e}")
            return []

    def detect(self, repo_data: Dict) -> Tuple[bool, List[AbuseEvidence]]:
        """Detect spoofed contributor abuse in a repository using dataset heuristics.

        Args:
            repo_data: Repository JSON data containing repository information

        Returns:
            Tuple[is_abuse, evidences]
        """
        # Extract repository information from repo_data
        full_name = repo_data.get('full_name', '')
        if not full_name:
            logger.warning("Repository data missing full_name field")
            return False, []

        # Split owner and repository name
        parts = full_name.split('/')
        if len(parts) != 2:
            logger.warning(f"Invalid full_name format: {full_name}")
            return False, []

        owner, repo_name = parts[0], parts[1]

        logger.info(f"Analyzing repository: {full_name}")

        # Get repository info (fields in repo_data take priority)
        forks = repo_data.get('forks_count', 0)
        stars = repo_data.get('stargazers_count', 0)
        created_at = repo_data.get('created_at', '')

        # Compute repository age
        repo_age_days = 0
        if created_at:
            try:
                created_date = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                repo_age_days = (datetime.now(timezone.utc) - created_date).days
            except:
                pass

        # Determine whether this is a small or new repository
        max_repo_forks = self.config.get('max_repo_forks', 1000)
        max_repo_stars = self.config.get('max_repo_stars', 1000)
        min_repo_age_days = self.config.get('min_repo_age_days', 3000)

        is_small_repo = (forks <= max_repo_forks and stars <= max_repo_stars)
        is_recent_repo = repo_age_days < min_repo_age_days

        # Skip detection if this is not a small or recent repository
        if not (is_small_repo and is_recent_repo):
            logger.info(
                f"Repository {full_name} is not a small/recent repository (forks={forks}, stars={stars}, age={repo_age_days} days), skipping detection")
            return False, []

        # Get the list of contributors
        contributors = self.get_repository_contributors(owner, repo_name)
        if not contributors:
            logger.info(f"Repository {full_name} has no contributor data")
            return False, []

        # Identify contributors who are in the hot-developers dataset
        top_contributors_in_repo = []
        for contributor in contributors:
            login = contributor.get('login', '')
            if not login:
                continue

            # Skip bot accounts
            if self.is_bot_account(login):
                continue

            # Check if user is in the hot-developers dataset
            if self.is_top_contributor(login):
                top_contributors_in_repo.append(contributor)

        # Skip detection if no top contributors are found
        if not top_contributors_in_repo:
            logger.info(f"Repository {full_name} has no top developers as contributors, skipping detection")
            return False, []

        logger.info(f"Repository {full_name} has {len(top_contributors_in_repo)} top developers")

        # Detect spoofed contributors
        evidences = []
        min_contributions = self.config.get('min_contributor_commits', 2)

        for contributor in top_contributors_in_repo:
            login = contributor.get('login', '')
            contributions = contributor.get('contributions', 0)

            # If a top contributor has suspiciously few contributions
            if contributions <= min_contributions:
                # Get user details for evidence
                user_info = self.get_user_info(login) or {}

                evidence = AbuseEvidence(
                    suspicious_contributor=login,
                    contributions=contributions,
                    reason=f"Top contributor has only {contributions} commits in small/new repository",
                    contributor_info={
                        'followers': user_info.get('followers', 0),
                        'public_repos': user_info.get('public_repos', 0)
                    },
                    repo_info={
                        'forks': forks,
                        'stars': stars,
                        'age_days': repo_age_days
                    }
                )
                evidences.append(evidence)

        # Determine if abuse detected
        is_abuse = len(evidences) > 0

        if is_abuse:
            logger.info(f"Repository {full_name}: {len(evidences)} suspected spoofed contributors detected")
        else:
            logger.info(f"Repository {full_name}: no spoofed contributors detected")

        return is_abuse, evidences

    def close(self):
        """Close session and release resources"""
        if self.session:
            self.session.close()