"""
Core module for detecting typo-squatting abuse in GitHub repositories.
It analyzes provided repository data rather than automatically discovering new repositories.
"""

import requests
import re
import time
import base64
import json
from difflib import SequenceMatcher
from rapidfuzz.distance import Levenshtein, DamerauLevenshtein
from typing import Dict, List, Tuple, Optional, Set
from improved_similarity import ImprovedSimilarityCalculator
import logging

logger = logging.getLogger(__name__)


class TypoSquattingCoreDetector:
    def __init__(self, github_token: str = None, config: Dict = None, corpus_path: str = None):
        """
        Initialize the core detector.

        Args:
            github_token: GitHub personal access token
            config: Configuration dictionary
            corpus_path: Path to the hot-repository corpus file (corpus_repos_hot.json)
        """
        # Default configuration
        self.config = {
            "min_stars_high": 1000,  # Popularity threshold
            "similarity_threshold": 0.7,  # Content similarity threshold
            "name_similarity_threshold": 0.7,  # Repository-name similarity threshold
            "similar_repo_check_count": 5,
            "exclude_topics": ["template", "boilerplate"],
            "max_retries": 3,
            "request_timeout": 30,
            "rate_limit_delay": 1.0,
            "min_star_ratio": 2.0,  # Minimum ratio of high-popularity repository stars to current repository stars
            "min_fork_ratio": 2.0,  # Minimum ratio of high-popularity repository forks to current repository forks
            "famous_orgs": [  # Whitelist of well-known organizations
                "microsoft", "google", "facebook", "amazon", "apple", "netflix",
                "uber", "airbnb", "twitter", "linkedin", "github", "docker",
                "kubernetes", "tensorflow", "pytorch", "reactjs", "angular",
                "vuejs", "nodejs", "python", "golang", "rust-lang", "dockersamples"
            ],
            "skip_awesome_repos": True  # Whether to skip awesome-* style repositories
        }

        # Merge user configuration
        if config:
            self.config.update(config)

        self.github_token = github_token

        # Initialize session
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "GitHub-TypoSquatting-Detector/1.0"
        })

        if self.github_token:
            self.session.headers["Authorization"] = f"token {self.github_token}"

        # API rate-limit tracking
        self.rate_limit_remaining = 5000
        self.rate_limit_reset = 0
        self.request_count = 0

        # Load the hot-repository corpus
        self.hot_repos_corpus = []
        self.corpus_loaded = False
        if corpus_path:
            self._load_hot_repos_corpus(corpus_path)

        # Initialize the improved similarity calculator
        self.similarity_calculator = ImprovedSimilarityCalculator(use_tfidf=True)
        # The similarity computation method can be configured
        self.similarity_method = config.get('similarity_method', 'rapidfuzz') if config else 'rapidfuzz'

    def _load_hot_repos_corpus(self, corpus_path: str) -> None:
        """
        Load the hot-repository corpus.

        Args:
            corpus_path: Path to the hot-repository corpus file
        """
        try:
            with open(corpus_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Handle different JSON formats
            if isinstance(data, list):
                self.hot_repos_corpus = data
            elif isinstance(data, dict) and 'repos' in data:
                self.hot_repos_corpus = data['repos']
            elif isinstance(data, dict) and 'items' in data:
                self.hot_repos_corpus = data['items']
            else:
                self.hot_repos_corpus = []

            # Filter out fork repositories and keep only original repositories
            self.hot_repos_corpus = [repo for repo in self.hot_repos_corpus if not repo.get('fork', False)]

            # Sort by star count
            self.hot_repos_corpus.sort(key=lambda x: x.get('stargazers_count', 0), reverse=True)

            self.corpus_loaded = True
            logger.info(f"Successfully loaded hot-repository corpus: {len(self.hot_repos_corpus)} repositories")

        except FileNotFoundError:
            logger.warning(f"Hot-repository corpus file does not exist: {corpus_path}; only API search will be used")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse hot-repository corpus: {e}")
        except Exception as e:
            logger.error(f"Error loading hot-repository corpus: {e}")

    def _is_awesome_repo(self, repo_name: str, repo_description: str = "") -> bool:
        """
        Detect whether a repository is an awesome-* style repository (for example, awesome-python, awesome-go, awesome-ml).

        Awesome repositories are usually community-maintained resource lists with the following traits:
        1. The repository name starts with "awesome" (with or without a hyphen or underscore)
        2. Or the description contains keywords such as "awesome list"
        3. These repositories are large, popular resource collections and are usually not squatting behavior

        Args:
            repo_name: Repository name
            repo_description: Repository description (optional)

        Returns:
            Whether the repository is an awesome-style repository
        """
        if not repo_name:
            return False

        name_lower = repo_name.strip().lower()

        # Pattern 1: starts with awesome (possibly followed by a hyphen, underscore, or direct content)
        # Examples: awesome, awesome-python, awesome_go, awesomeai, awesomeML
        awesome_patterns = [
            r'^awesome[-_]?',           # awesome-, awesome_, awesome
            r'^awesome$',                # Exact match for awesome
            r'^awesome[a-z]',            # awesome followed directly by a letter (awesomeai)
        ]

        for pattern in awesome_patterns:
            if re.match(pattern, name_lower):
                logger.debug(f"Repository {repo_name} identified as an awesome-style repository (name match)")
                return True

        # Pattern 2: description contains keywords related to awesome lists
        if repo_description:
            desc_lower = repo_description.lower()
            awesome_keywords = [
                'awesome list',
                'awesome collection',
                'awesome resources',
                'curated list of awesome',
                'awesome ',
                'list of awesome'
            ]
            for keyword in awesome_keywords:
                if keyword in desc_lower:
                    logger.debug(f"Repository {repo_name} identified as an awesome-style repository (description match: {keyword})")
                    return True

        return False

    def _is_famous_org(self, owner_login: str) -> bool:
        """Check whether the owner belongs to a famous organization (whitelist)."""
        famous_orgs = self.config.get("famous_orgs", [])
        return owner_login.lower() in [org.lower() for org in famous_orgs]

    def _is_hot_repo(self, repo_data: Dict) -> bool:
        """Check whether the repository is a popular one."""
        stars = repo_data.get('stargazers_count', 0)
        forks = repo_data.get('forks_count', 0)
        min_stars_high = self.config.get("min_stars_high", 1000)

        return stars >= min_stars_high or forks >= min_stars_high

    def _search_in_corpus(self, repo_name: str, exclude_owner: str = None) -> List[Dict]:
        """
        Search the local hot-repository corpus for repositories with similar names.

        Args:
            repo_name: Current repository name
            exclude_owner: Owner to exclude

        Returns:
            List of matching repositories
        """
        if not self.corpus_loaded or not self.hot_repos_corpus:
            return []

        matched_repos = []
        name_similarity_threshold = self.config.get("name_similarity_threshold", 0.7)
        min_stars_high = self.config.get("min_stars_high", 1000)
        skip_awesome = self.config.get("skip_awesome_repos", True)

        for repo in self.hot_repos_corpus:
            # Consider only high-popularity repositories
            stars = repo.get('stargazers_count', 0)
            if stars < min_stars_high:
                continue

                # Exclude the current repository itself
            repo_owner = repo.get('owner', {}).get('login', '')
            repo_full_name = repo.get('full_name', '')
            if exclude_owner and repo_owner == exclude_owner:
                continue

            repo_candidate_name = repo.get('name', '')
            if not repo_candidate_name:
                continue

            # Skip awesome-style repositories because they are typically legitimate resource collections
            if skip_awesome and self._is_awesome_repo(repo_candidate_name, repo.get('description', '')):
                logger.debug(f"Skipping awesome-style repository in candidates: {repo_candidate_name}")
                continue

            # Calculate name similarity
            name_similarity = self._calculate_name_similarity(repo_name, repo_candidate_name)

            if name_similarity >= name_similarity_threshold:
                matched_repos.append({
                    "full_name": repo_full_name,
                    "name": repo_candidate_name,
                    "owner": repo_owner,
                    "stars": stars,
                    "forks": repo.get('forks_count', 0),
                    "url": repo.get('html_url', ''),
                    "description": repo.get('description', '') or '',
                    "created_at": repo.get('created_at', ''),
                    "name_similarity": name_similarity,
                    "source": "corpus"  # Mark the source as the local corpus
                })

        # Sort by similarity
        matched_repos.sort(key=lambda x: x['name_similarity'], reverse=True)
        # Limit the number of returned matches
        max_count = self.config.get("similar_repo_check_count", 5)
        matched_repos = matched_repos[:max_count]

        if matched_repos:
            logger.info(f"Found {len(matched_repos)} repositories with similar names in the hot-repository corpus")

        return matched_repos

    def make_api_call(self, url: str, params: Dict = None) -> Optional[Dict]:
        """Perform a safe API call while handling rate limiting."""
        self.request_count += 1
        max_retries = self.config.get("max_retries", 3)
        request_timeout = self.config.get("request_timeout", 30)
        rate_limit_delay = self.config.get("rate_limit_delay", 1.0)

        is_search_request = "search/repositories" in url

        for attempt in range(max_retries):
            try:
                response = self.session.get(url, params=params, timeout=request_timeout)

                # Check rate limiting
                if 'X-RateLimit-Remaining' in response.headers:
                    self.rate_limit_remaining = int(response.headers['X-RateLimit-Remaining'])
                    self.rate_limit_reset = int(response.headers.get('X-RateLimit-Reset', 0))

                    if self.rate_limit_remaining < 10:
                        wait_time = max(self.rate_limit_reset - time.time(), 0) + 10
                        logger.warning(f"[API limit] {self.rate_limit_remaining} requests remaining; waiting {wait_time:.0f} seconds...")
                        time.sleep(wait_time)
                        continue

                if response.status_code == 200:
                    if is_search_request:
                        time.sleep(2)  # Extra delay between search requests
                    else:
                        time.sleep(rate_limit_delay)
                    return response.json()

                elif response.status_code == 403 and 'rate limit' in response.text.lower():
                    reset_time = response.headers.get('X-RateLimit-Reset')
                    if reset_time:
                        wait_time = max(int(reset_time) - time.time(), 0) + 10
                        logger.warning(f"[API limit] reached; waiting {wait_time:.0f} seconds...")
                        time.sleep(wait_time)
                        continue

                elif response.status_code in [404, 422]:
                    return None

                else:
                    logger.error(f"API request failed: {response.status_code} - {response.text[:100]}")
                    return None

            except requests.exceptions.RequestException as e:
                logger.error(f"Request exception: {e}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)

        return None

    def _calculate_name_similarity(self, name1: str, name2: str) -> float:
        """Calculate the similarity between two repository names using an enhanced method."""
        if not name1 or not name2:
            return 0.0

        name1_clean = name1.strip().lower()
        name2_clean = name2.strip().lower()

        if name1_clean == name2_clean:
            return 1.0

        # Levenshtein similarity (normalized)
        lev_dist = Levenshtein.distance(name1_clean, name2_clean)
        max_len = max(len(name1_clean), len(name2_clean))
        lev_sim = 1 - lev_dist / max_len if max_len > 0 else 0

        # Damerau-Levenshtein (supports transpositions)
        dam_dist = DamerauLevenshtein.distance(name1_clean, name2_clean)
        dam_sim = 1 - dam_dist / max_len if max_len > 0 else 0

        # SequenceMatcher
        seq_sim = SequenceMatcher(None, name1_clean, name2_clean).ratio()

        # Weighted fusion
        final_sim = 0.4 * lev_sim + 0.4 * dam_sim + 0.2 * seq_sim

        return final_sim

    def _check_star_fork_ratio(self, current_stars: int, current_forks: int,
                               high_star_stars: int, high_star_forks: int) -> bool:
        """
        Check whether the stars and forks of a high-popularity repository satisfy the required ratio against the current repository.

        Args:
            current_stars: Current repository star count
            current_forks: Current repository fork count
            high_star_stars: High-popularity repository star count
            high_star_forks: High-popularity repository fork count

        Returns:
            Whether the ratio condition is satisfied
        """
        min_star_ratio = self.config.get("min_star_ratio", 2.0)
        min_fork_ratio = self.config.get("min_fork_ratio", 2.0)

        # If the current repository has zero stars, any high-popularity repository with stars >= min_stars_high is considered sufficient
        if current_stars == 0:
            star_condition = high_star_stars >= self.config.get("min_stars_high", 1000)
        else:
            star_condition = high_star_stars >= current_stars * min_star_ratio

        # If the current repository has zero forks, any high-popularity repository with at least one fork satisfies the condition
        if current_forks == 0:
            fork_condition = high_star_forks >= 1
        else:
            fork_condition = high_star_forks >= current_forks * min_fork_ratio

        logger.debug(
            f"Ratio check: stars {current_stars} -> {high_star_stars} (required >= {current_stars * min_star_ratio if current_stars > 0 else min_star_ratio}), "
            f"forks {current_forks} -> {high_star_forks} (required >= {current_forks * min_fork_ratio if current_forks > 0 else 1})")

        return star_condition and fork_condition

    def _get_fork_parent_info(self, owner: str, repo: str) -> Optional[Dict]:
        """
        Get the parent repository information for a fork.

        Args:
            owner: Repository owner
            repo: Repository name

        Returns:
            Parent repository information, or None if the repository is not a fork
        """
        try:
            url = f"https://api.github.com/repos/{owner}/{repo}"
            data = self.make_api_call(url)

            if data and data.get("fork", False):
                parent = data.get("parent")
                if parent:
                    return {
                        "full_name": parent.get("full_name", ""),
                        "owner": parent.get("owner", {}).get("login", ""),
                        "name": parent.get("name", ""),
                        "stars": parent.get("stargazers_count", 0),
                        "forks": parent.get("forks_count", 0)
                    }
                # If there is no parent, try the source field
                source = data.get("source")
                if source:
                    return {
                        "full_name": source.get("full_name", ""),
                        "owner": source.get("owner", {}).get("login", ""),
                        "name": source.get("name", ""),
                        "stars": source.get("stargazers_count", 0),
                        "forks": source.get("forks_count", 0)
                    }
        except Exception as e:
            logger.error(f"Failed to get fork parent repository information: {e}")

        return None

    def search_high_star_repos_api(self, repo_name: str, exclude_owner: str = None) -> List[Dict]:
        """
        Search GitHub repositories with many stars via the GitHub API as comparison targets.
        Optimization: use the 'xxx in:name' search strategy and sort by stars.

        Args:
            repo_name: Current repository name
            exclude_owner: Owner to exclude (the current repository owner)

        Returns:
            List of high-popularity repositories
        """
        url = "https://api.github.com/search/repositories"
        skip_awesome = self.config.get("skip_awesome_repos", True)

        # Search strategy: use the "xxx in:name" syntax
        # fork:false ensures the results are original repositories rather than forks
        params = {
            "q": f'{repo_name} in:name stars:>={self.config["min_stars_high"]} fork:false',
            "sort": "stars",
            "order": "desc",
            "per_page": self.config.get("similar_repo_check_count", 5)
        }

        logger.info(f"API search strategy: '{repo_name} in:name', sorted by stars, returning up to {params['per_page']} repositories")

        data = self.make_api_call(url, params)
        if not data or "items" not in data:
            return []

        high_star_repos = []
        name_similarity_threshold = self.config.get("name_similarity_threshold", 0.7)

        for item in data["items"]:
                # Exclude the current repository itself
            if exclude_owner and item["owner"]["login"] == exclude_owner:
                continue

            repo_candidate_name = item["name"]

                # Skip awesome-style repositories because they are typically legitimate resource collections
            if skip_awesome and self._is_awesome_repo(repo_candidate_name, item.get("description", "")):
                logger.debug(f"Skipping awesome-style repository in API results: {repo_candidate_name}")
                continue

            # Calculate name similarity
            name_similarity = self._calculate_name_similarity(repo_name, repo_candidate_name)

            # Keep only repositories whose name similarity meets the threshold
            if name_similarity >= name_similarity_threshold:
                repo_info = {
                    "full_name": item["full_name"],
                    "name": item["name"],
                    "owner": item["owner"]["login"],
                    "stars": item["stargazers_count"],
                    "forks": item["forks_count"],
                    "url": item["html_url"],
                    "description": item["description"] or "",
                    "created_at": item["created_at"],
                    "name_similarity": name_similarity,
                    "source": "api"  # Mark source as API
                }
                high_star_repos.append(repo_info)

        logger.info(f"API search found {len(high_star_repos)} high-popularity repositories with similar names")
        return high_star_repos

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """Calculate text similarity using an improved algorithm."""
        if not text1 or not text2:
            return 0.0

        # Use the improved similarity calculator
        return self.similarity_calculator.calculate_similarity(
            text1, text2,
            method=self.similarity_method
        )

    def get_readme_content(self, owner: str, repo: str) -> Optional[str]:
        """Fetch the repository README content."""
        # First, try to get README.md
        url = f"https://api.github.com/repos/{owner}/{repo}/readme"
        data = self.make_api_call(url)

        if data and "content" in data:
            try:
                content = base64.b64decode(data["content"]).decode('utf-8', errors='ignore')
                return content
            except:
                pass

        # If there is no README, fall back to the repository description
        url = f"https://api.github.com/repos/{owner}/{repo}"
        data = self.make_api_call(url)

        if data and "description" in data:
            return data["description"] or ""

        return ""

    def detect_repository_abuse(self, repo_data: Dict) -> Tuple[bool, List[Dict]]:
        """
        Detect whether the provided repository exhibits typo-squatting abuse.

        Args:
            repo_data: Repository JSON data from the input dataset

        Returns:
            Tuple[is_abuse, evidence list]
        """
        try:
            # Extract information from the JSON data
            repo_owner = repo_data.get("owner", {}).get("login", "")
            repo_name = repo_data.get("name", "")
            repo_full_name = repo_data.get("full_name", "")
            repo_stars = repo_data.get("stargazers_count", 0)
            repo_forks = repo_data.get("forks_count", 0)
            repo_description = repo_data.get("description", "") or ""

            # If owner information is missing, try to parse it from full_name
            if not repo_owner and "/" in repo_full_name:
                parts = repo_full_name.split("/")
                repo_owner = parts[0]
                if not repo_name:
                    repo_name = parts[1]

            if not repo_owner or not repo_name:
                logger.error(f"Unable to extract repository information: {repo_data}")
                return False, []

            logger.info(f"Checking repository: {repo_full_name} (stars={repo_stars}, forks={repo_forks})")

            # ========== Skip Condition Checks ==========

            # Whitelist check: skip repositories belonging to famous organizations
            if self._is_famous_org(repo_owner):
                logger.info(f"Repository {repo_full_name} belongs to well-known org {repo_owner}, skipping")
                return False, []

            # Awesome-style repository check: these are large resource lists and not squatting behavior
            skip_awesome = self.config.get("skip_awesome_repos", True)
            if skip_awesome and self._is_awesome_repo(repo_name, repo_description):
                logger.info(f"Repository {repo_full_name} is an awesome-style repository (resource list); skipping typo-squatting detection")
                return False, []

            # Get parent repository information for the current repository if it is a fork
            fork_parent = self._get_fork_parent_info(repo_owner, repo_name)
            if fork_parent:
                logger.info(f"Current repository {repo_full_name} is a fork; parent repository is: {fork_parent['full_name']}")

            # Merge all candidate high-popularity repositories (deduplicate)
            all_candidate_repos = []
            seen_full_names = set()

            # Step 1: search the local hot-repository corpus for repositories with similar names
            corpus_matched = self._search_in_corpus(repo_name, exclude_owner=repo_owner)
            for repo in corpus_matched:
                if repo["full_name"] not in seen_full_names:
                    seen_full_names.add(repo["full_name"])
                    all_candidate_repos.append(repo)

            if corpus_matched:
                logger.info(f"Found {len(corpus_matched)} candidate repositories from the corpus")

            # Step 2: search the API for repositories with similar names
            api_matched = self.search_high_star_repos_api(repo_name, exclude_owner=repo_owner)
            for repo in api_matched:
                if repo["full_name"] not in seen_full_names:
                    seen_full_names.add(repo["full_name"])
                    all_candidate_repos.append(repo)

            if api_matched:
                logger.info(f"Found {len(api_matched)} candidate repositories from API search")

            if not all_candidate_repos:
                logger.info(f"No high-popularity repositories similar to {repo_name} were found")
                return False, []

            logger.info(f"Found {len(all_candidate_repos)} candidate high-popularity repositories in total")

            # Fetch the current repository README
            current_readme = self.get_readme_content(repo_owner, repo_name)

            if not current_readme:
                logger.info(f"Repository {repo_full_name} has no README content; skipping detection")
                return False, []

            # Analyze similarity
            evidences = []

            for similar_repo in all_candidate_repos:
                # Check whether the current repository is a fork of the high-popularity repository
                if fork_parent and fork_parent['full_name'] == similar_repo['full_name']:
                    logger.info(f"Current repository {repo_full_name} is a fork of {similar_repo['full_name']}; skipping abuse determination")
                    continue

                # Fetch the high-popularity repository README
                similar_readme = self.get_readme_content(similar_repo["owner"], similar_repo["name"])

                if not similar_readme:
                    continue

                # Calculate content similarity
                content_similarity = self._calculate_similarity(current_readme, similar_readme)
                name_similarity = similar_repo.get("name_similarity",
                                                   self._calculate_name_similarity(repo_name, similar_repo["name"]))

                # Check the star/fork ratio condition
                star_fork_condition = self._check_star_fork_ratio(
                    repo_stars, repo_forks,
                    similar_repo["stars"], similar_repo["forks"]
                )

                # Check whether the abuse conditions are satisfied
                # Condition 1: content similarity meets the threshold
                # Condition 2: the high-popularity repository's stars and forks satisfy the ratio requirement
                is_abuse = (
                        content_similarity >= self.config["similarity_threshold"] and
                        star_fork_condition and
                        similar_repo["stars"] >= self.config["min_stars_high"]
                )

                # Calculate ratios for logging output
                star_ratio = similar_repo["stars"] / max(repo_stars, 1)
                fork_ratio = similar_repo["forks"] / max(repo_forks, 1)

                source_info = f"[Source: {similar_repo.get('source', 'unknown')}]"
                logger.info(f"Comparing {repo_full_name} with {similar_repo['full_name']} {source_info}: "
                            f"content similarity={content_similarity:.2%}, name similarity={name_similarity:.2%}, "
                            f"star ratio={star_ratio:.1f}x, fork ratio={fork_ratio:.1f}x, "
                            f"is_abuse={is_abuse}")

                if is_abuse:
                    evidences.append({
                        "current_repo": repo_full_name,
                        "similar_repo": similar_repo["full_name"],
                        "content_similarity": content_similarity,
                        "name_similarity": name_similarity,
                        "current_stars": repo_stars,
                        "current_forks": repo_forks,
                        "similar_stars": similar_repo["stars"],
                        "similar_forks": similar_repo["forks"],
                        "star_ratio": star_ratio,
                        "fork_ratio": fork_ratio,
                        "source": similar_repo.get("source", "unknown"),
                        "abuse_reason": f"The repository is highly similar to the popular repository {similar_repo['full_name']} ({content_similarity:.1%} content similarity), "
                                        f"with name similarity {name_similarity:.1%}, and the high-popularity repository has {star_ratio:.1f}x the stars and {fork_ratio:.1f}x the forks"
                    })

            is_final_abuse = len(evidences) > 0

            if is_final_abuse:
                logger.warning(f"Detected abuse behavior: {repo_full_name} -> {len(evidences)} pieces of evidence")
            else:
                logger.info(f"No abuse behavior detected: {repo_full_name}")

            return is_final_abuse, evidences

        except Exception as e:
            logger.error(f"Failed to detect repository {repo_data.get('full_name', 'unknown')}: {e}")
            return False, []

    def batch_detect_abuse(self, repos_data: List[Dict]) -> List[Dict]:
        """
        Batch-detect abuse behavior.

        Args:
            repos_data: List of repository data entries, where each item is a full repository JSON object

        Returns:
            List of detection results
        """
        results = []

        logger.info(f"Starting batch detection for {len(repos_data)} repositories...")

        # Track the number of awesome repositories skipped
        skipped_awesome_count = 0
        skip_awesome = self.config.get("skip_awesome_repos", True)

        for i, repo_data in enumerate(repos_data, 1):
            repo_full_name = repo_data.get("full_name", "unknown")
            repo_name = repo_data.get("name", "")
            repo_description = repo_data.get("description", "") or ""

            logger.info(f"Progress: {i}/{len(repos_data)} - detecting repository: {repo_full_name}")

            # Pre-count awesome repositories for logging
            if skip_awesome and self._is_awesome_repo(repo_name, repo_description):
                skipped_awesome_count += 1

            is_abuse, evidences = self.detect_repository_abuse(repo_data)

            # Build the result object
            result = {
                "repository": repo_full_name,
                "repository_url": repo_data.get("html_url", ""),
                "is_abuse": is_abuse,
                "evidences": evidences,
                "stars": repo_data.get("stargazers_count", 0),
                "forks": repo_data.get("forks_count", 0),
                "owner": repo_data.get("owner", {}).get("login", ""),
                "owner_type": repo_data.get("owner", {}).get("type", ""),
                "is_famous_org": self._is_famous_org(repo_data.get("owner", {}).get("login", "")),
                "is_hot_repo": self._is_hot_repo(repo_data),
                "is_awesome_repo": skip_awesome and self._is_awesome_repo(repo_name, repo_description)  # Mark whether the repository is awesome-style
            }

            results.append(result)

        # Summarize results
        abuse_count = sum(1 for r in results if r["is_abuse"])
        awesome_count = sum(1 for r in results if r.get("is_awesome_repo", False))
        logger.info(f"Batch detection complete: {abuse_count}/{len(results)} repositories exhibited abuse behavior")
        if skip_awesome and awesome_count > 0:
            logger.info(f"Awesome-style repositories skipped from detection: {awesome_count}")

        return results