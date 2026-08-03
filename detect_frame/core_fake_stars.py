"""
Core module for detecting low-activity user starring based on GitHub Archive.
Smart version: three-stage pipeline (collect -> pre-process -> deep check).
Uses 4 tokens by default: token 0 fetches stargazers, tokens 1-3 handle activity checks
with adaptive rate limiting and parallel processing.
Added: supports local dataset pre-query to avoid duplicate scans.
"""

import json
import os
import time
import re
import queue
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
import requests
from requests.adapters import HTTPAdapter
from threading import Lock, Event


@dataclass
class AbuseEvidence:
    """Evidence of abuse"""
    repo_full_name: str
    total_stars: int
    low_activity_stars: int
    low_activity_percentage: float
    detection_reason: str
    meets_threshold: bool
    source: str = "api"  # data source: "dataset" or "api"
    low_activity_users: List[str] = field(default_factory=list)  # list of low-activity usernames


@dataclass
class UserBasicInfo:
    """Basic user information"""
    username: str
    star_date: str
    followers: int = 0
    public_repos: int = 0
    is_organization: bool = False
    fetched: bool = False


class LocalDataset:
    """Local dataset query helper class"""

    def __init__(self, dataset_path: str = None):
        """
        Initialize the local dataset helper.

        Args:
            dataset_path: Path to the dataset file. Defaults to
                the bundled fake_star_scan_data/result25.02.01-26.02.01.json
        """
        if dataset_path is None:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            self.dataset_path = os.path.join(current_dir, "fake_star_scan_data", "result25.02.01-26.02.01.json")
        else:
            self.dataset_path = dataset_path

        self.data = {}  # repo_name -> record
        self.loaded = False
        self.load_error = None
        self.lock = Lock()

    def load(self) -> bool:
        """Load the dataset"""
        with self.lock:
            if self.loaded:
                return True

            try:
                if not os.path.exists(self.dataset_path):
                    self.load_error = f"Dataset file does not exist: {self.dataset_path}"
                    return False

                print(f"Loading local dataset: {self.dataset_path}")
                with open(self.dataset_path, 'r', encoding='utf-8') as f:
                    dataset = json.load(f)

                if 'data' in dataset:
                    for record in dataset['data']:
                        repo_name = record.get('repo_name')
                        if repo_name:
                            repo_name = repo_name.replace('\\/', '/')
                            self.data[repo_name] = record

                self.loaded = True
                print(f"Dataset loaded successfully, {len(self.data)} repository records")
                return True

            except Exception as e:
                self.load_error = str(e)
                print(f"Failed to load dataset: {e}")
                return False

    def query(self, repo_full_name: str) -> Optional[Dict[str, Any]]:
        """
        Query whether a repository exists in the local dataset.

        Args:
            repo_full_name: Repository full name, e.g. "owner/repo".

        Returns:
            The record if found, otherwise None.
        """
        if not self.loaded:
            if not self.load():
                return None

        repo_full_name = repo_full_name.replace('\\/', '/')
        record = self.data.get(repo_full_name)

        if record:
            return record

        escaped_name = repo_full_name.replace('/', '\\/')
        record = self.data.get(escaped_name)
        if record:
            return record

        return None

    def get_stats(self) -> Dict:
        """Get dataset statistics"""
        with self.lock:
            return {
                "loaded": self.loaded,
                "records_count": len(self.data),
                "load_error": self.load_error
            }


class RateLimiter:
    """Adaptive rate limiter"""

    def __init__(self, token_name: str, initial_rate: float = 1.0):
        """
        Args:
            token_name: Human-friendly name for the token/session
            initial_rate: Initial interval between requests (seconds)
        """
        self.token_name = token_name
        self.current_interval = initial_rate
        self.min_interval = 0.5
        self.max_interval = 5.0
        self.last_request_time = 0
        self.lock = Lock()
        self.consecutive_limits = 0

    def wait_if_needed(self):
        """Wait for the appropriate interval before returning"""
        with self.lock:
            current_time = time.time()
            time_since_last = current_time - self.last_request_time

            if time_since_last < self.current_interval:
                sleep_time = self.current_interval - time_since_last
                time.sleep(sleep_time)

            self.last_request_time = time.time()

    def report_success(self):
        """Report a successful request, allowing the rate to speed up"""
        with self.lock:
            self.consecutive_limits = 0
            if self.current_interval > self.min_interval:
                self.current_interval = max(
                    self.min_interval,
                    self.current_interval * 0.75
                )

    def report_limit(self):
        """Report hitting a rate limit, requiring the rate to slow down"""
        with self.lock:
            self.consecutive_limits += 1
            self.current_interval = min(
                self.max_interval,
                self.current_interval * 2.0
            )
            print(f"[{self.token_name}] API rate limit triggered, adjusted request interval to {self.current_interval:.1f}s")


class SimpleGitHubSession:
    """Simple GitHub API session with adaptive rate limiting"""

    def __init__(self, token: str, session_name: str, config: Dict):
        self.token = token
        self.session_name = session_name
        self.config = config

        self.api_settings = config.get('api_settings', {})
        self.request_timeout = self.api_settings.get('request_timeout', 30)
        self.request_count = 0

        self.rate_limiter = RateLimiter(session_name, initial_rate=0.2)

        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=30, pool_maxsize=50, max_retries=1)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

        self.session.headers.update({
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': f'GitHub-Abuse-Detector/{session_name}',
            'Authorization': f'token {token}',
            'Connection': 'keep-alive'
        })

    def get(self, url: str, params: Dict = None, headers: Dict = None) -> Optional[Dict]:
        """Send a GET request with adaptive rate limiting"""
        self.rate_limiter.wait_if_needed()

        try:
            request_headers = self.session.headers.copy()
            if headers:
                request_headers.update(headers)

            self.request_count += 1

            response = self.session.get(
                url,
                params=params,
                headers=request_headers,
                timeout=self.request_timeout
            )

            if response.status_code == 200:
                self.rate_limiter.report_success()
                return response.json()

            elif response.status_code in [404, 422]:
                self.rate_limiter.report_success()
                return None

            elif response.status_code in [403, 429]:
                self.rate_limiter.report_limit()
                reset_time = response.headers.get('X-RateLimit-Reset', 0)
                if reset_time:
                    wait_time = max(int(reset_time) - time.time(), 0) + 1
                    if wait_time > 0:
                        time.sleep(wait_time)
                return None
            else:
                self.rate_limiter.report_success()
                return None

        except Exception as e:
            return None

    def close(self):
        if self.session:
            self.session.close()


class PreFilterProcessor:
    """Pre-filter processor: quickly filters obviously active users"""

    def __init__(self, activity_sessions: List[SimpleGitHubSession], config: Dict):
        self.activity_sessions = activity_sessions
        self.config = config

        self.input_queue = queue.Queue(maxsize=500)
        self.output_queue = queue.Queue()

        self.processed_count = 0
        self.filtered_out_count = 0
        self.lock = Lock()

        self.producer_done = Event()
        self.started = Event()

        self.max_followers = config.get('prefilter_max_followers', 50)
        self.max_public_repos = config.get('prefilter_max_public_repos', 10)
        self.skip_organizations = config.get('prefilter_skip_organizations', True)

    def _worker(self, session: SimpleGitHubSession, worker_id: int):
        """Pre-filter worker thread: fetch basic user info and filter"""
        self.started.wait()

        while True:
            try:
                try:
                    user_info = self.input_queue.get(timeout=1.0)
                except queue.Empty:
                    if self.producer_done.is_set() and self.input_queue.empty():
                        break
                    continue

                username = user_info.username

                try:
                    user_url = f"https://api.github.com/users/{username}"
                    user_data = session.get(user_url)

                    if not user_data:
                        self.output_queue.put(user_info)
                        self.input_queue.task_done()
                        continue

                    followers = user_data.get('followers', 0)
                    public_repos = user_data.get('public_repos', 0)
                    is_org = user_data.get('type') == 'Organization'

                    user_info.followers = followers
                    user_info.public_repos = public_repos
                    user_info.is_organization = is_org
                    user_info.fetched = True

                    should_filter = False

                    if is_org and self.skip_organizations:
                        should_filter = True
                    elif followers > self.max_followers:
                        should_filter = True
                    elif public_repos > self.max_public_repos:
                        should_filter = True

                    if should_filter:
                        with self.lock:
                            self.filtered_out_count += 1
                    else:
                        self.output_queue.put(user_info)

                    with self.lock:
                        self.processed_count += 1

                    self.input_queue.task_done()

                except Exception as e:
                    self.output_queue.put(user_info)
                    self.input_queue.task_done()

            except Exception:
                time.sleep(0.5)

    def start_workers(self):
        """Start pre-filter worker threads"""
        workers = []
        for i, session in enumerate(self.activity_sessions, 1):
            worker = threading.Thread(
                target=self._worker,
                args=(session, i),
                daemon=True
            )
            worker.start()
            workers.append(worker)
        return workers

    def add_user(self, user_info: UserBasicInfo):
        """Add a user to the pre-filter input queue"""
        self.input_queue.put(user_info)

    def set_producer_done(self):
        """Mark producer as finished"""
        self.producer_done.set()

    def get_stats(self) -> Dict:
        with self.lock:
            return {
                "processed": self.processed_count,
                "filtered_out": self.filtered_out_count
            }


class DeepChecker:
    """Deep checker: analyze user activity in detail"""

    def __init__(self, activity_sessions: List[SimpleGitHubSession], config: Dict):
        self.activity_sessions = activity_sessions
        self.config = config

        self.input_queue = queue.Queue(maxsize=500)
        self.output_queue = queue.Queue()

        self.processed_count = 0
        self.low_activity_count = 0
        self.low_activity_users = []  # store low-activity usernames
        self.lock = Lock()

        self.producer_done = Event()
        self.started = Event()

    def _worker(self, session: SimpleGitHubSession, worker_id: int):
        """Deep-check worker thread: analyze user activity in detail"""
        self.started.wait()

        while True:
            try:
                try:
                    user_info = self.input_queue.get(timeout=1.0)
                except queue.Empty:
                    if self.producer_done.is_set() and self.input_queue.empty():
                        break
                    continue

                username = user_info.username
                star_date = user_info.star_date

                try:
                    star_dt = datetime.fromisoformat(star_date.replace('Z', '+00:00'))
                    days = self.config.get('activity_days_around_star', 30)
                    start_date = star_dt - timedelta(days=days)
                    end_date = star_dt + timedelta(days=days)

                    events = []
                    page = 1
                    per_page = 100
                    max_pages = 3

                    while page <= max_pages:
                        events_url = f"https://api.github.com/users/{username}/events"
                        params = {'per_page': per_page, 'page': page}

                        page_events = session.get(events_url, params)

                        if not page_events:
                            break

                        for event in page_events:
                            event_date_str = event.get('created_at')
                            if event_date_str:
                                event_date = datetime.fromisoformat(event_date_str.replace('Z', '+00:00'))
                                if start_date <= event_date <= end_date:
                                    events.append(event)

                        if len(page_events) < per_page:
                            break

                        page += 1

                    repo_set = set()
                    org_set = set()
                    event_dates = []

                    for event in events:
                        event_date = event.get('created_at')
                        if event_date:
                            event_dates.append(event_date)

                        if 'repo' in event and event['repo']:
                            repo_name = event['repo'].get('name', '')
                            if repo_name:
                                repo_set.add(repo_name)

                        org = event.get('org')
                        if org and org.get('login'):
                            org_set.add(org['login'])

                    n_actions = len(events)
                    n_repos = len(repo_set)
                    n_orgs = len(org_set)

                    if event_dates:
                        first_active = min(event_dates)
                        last_active = max(event_dates)
                        same_day = first_active[:10] == last_active[:10]
                    else:
                        same_day = False

                    is_low_activity = (
                            same_day and
                            n_actions <= self.config.get('max_actions', 2) and
                            n_repos <= self.config.get('max_repos', 1) and
                            n_orgs <= self.config.get('max_orgs', 1)
                    )

                    with self.lock:
                        self.processed_count += 1
                        if is_low_activity:
                            self.low_activity_count += 1
                            self.low_activity_users.append(username)

                    self.output_queue.put({
                        "username": username,
                        "is_low_activity": is_low_activity,
                        "star_date": star_date
                    })

                    self.input_queue.task_done()

                except Exception:
                    self.input_queue.task_done()

            except Exception:
                time.sleep(0.5)

    def start_workers(self):
        """Start deep-check worker threads"""
        workers = []
        for i, session in enumerate(self.activity_sessions, 1):
            worker = threading.Thread(
                target=self._worker,
                args=(session, i),
                daemon=True
            )
            worker.start()
            workers.append(worker)
        return workers

    def add_user(self, user_info: UserBasicInfo):
        """Add a user to the deep-check input queue"""
        self.input_queue.put(user_info)

    def set_producer_done(self):
        """Mark producer as finished"""
        self.producer_done.set()

    def get_stats(self) -> Dict:
        with self.lock:
            return {
                "processed": self.processed_count,
                "low_activity": self.low_activity_count
            }

    def get_low_activity_users(self) -> List[str]:
        """Get the list of low-activity usernames"""
        with self.lock:
            return self.low_activity_users.copy()


class FakeStarsCoreDetector:
    """Core detector for fake stars - three-stage pipeline"""

    def __init__(self, github_tokens: List[str], config: Dict, dataset_path: str = None,
                 enable_api_detection: bool = True):
        """
        Initialize the detector.

        Args:
            github_tokens: List of GitHub tokens
            config: Configuration dictionary
            dataset_path: Path to the local dataset
            enable_api_detection: Whether to enable live API checks (default True)
        """
        self.github_tokens = github_tokens
        self.config = config
        self.enable_api_detection = enable_api_detection  # whether live API detection is enabled

        self.dataset = LocalDataset(dataset_path)

        # Initialize sessions only when API detection is enabled
        if self.enable_api_detection:
            self.stargazer_session = SimpleGitHubSession(
                github_tokens[0],
                "STARGAZER",
                config
            )

            self.activity_sessions = []
            for i in range(1, min(4, len(github_tokens))):
                session = SimpleGitHubSession(
                    github_tokens[i],
                    f"ACTIVITY-{i}",
                    config
                )
                self.activity_sessions.append(session)

            self.prefilter = PreFilterProcessor(self.activity_sessions, config)
            self.deep_checker = DeepChecker(self.activity_sessions, config)
        else:
            # Initialize empty placeholders when API detection is disabled
            self.stargazer_session = None
            self.activity_sessions = []
            self.prefilter = None
            self.deep_checker = None

        self.prefilter_workers = None
        self.deep_checker_workers = None
        self.producer_thread = None

        self.total_stargazers = 0
        self.from_dataset = 0
        self.low_activity_users_from_api = []

    def extract_repo_info(self, repo_data: Dict) -> Tuple[str, str, int]:
        full_name = repo_data.get('full_name', '')
        if not full_name and 'html_url' in repo_data:
            html_url = repo_data['html_url']
            pattern = r"github\.com/([^/]+)/([^/?]+)"
            match = re.search(pattern, html_url)
            if match:
                owner, repo_name = match.group(1), match.group(2)
            else:
                raise ValueError("Unable to extract repository info from data")
        else:
            parts = full_name.split('/')
            if len(parts) == 2:
                owner, repo_name = parts[0], parts[1]
            else:
                raise ValueError("Invalid full_name format")

        total_stars = repo_data.get('stargazers_count', 0)
        return owner, repo_name, total_stars

    def check_dataset_first(self, repo_full_name: str) -> Optional[Tuple[bool, AbuseEvidence]]:
        """
        Query the dataset first.

        Args:
            repo_full_name: Full name of the repository.

        Returns:
            If found and meets conditions, return the result; otherwise, return None.
        """
        record = self.dataset.query(repo_full_name)

        if not record:
            return None

        n_stars = record.get('n_stars', 0)
        low_activity_stars = record.get('low_activity_stars', 0)
        low_activity_actors = record.get('low_activity_actors', [])

        print(f"\nLocal dataset hit: {repo_full_name}")
        print(f"Total stars={n_stars}, low-activity stars={low_activity_stars}")
        print(f"Sample low-activity users: {low_activity_actors[:5]}")

        low_activity_percentage = low_activity_stars / n_stars if n_stars > 0 else 0
        min_low_activity_percentage = self.config.get('min_low_activity_percentage', 0.1)
        is_abuse = low_activity_percentage >= min_low_activity_percentage

        evidence = AbuseEvidence(
            repo_full_name=repo_full_name,
            total_stars=n_stars,
            low_activity_stars=low_activity_stars,
            low_activity_percentage=low_activity_percentage,
            detection_reason=f"Dataset record: {low_activity_stars} low-activity users ({low_activity_percentage:.1%})",
            meets_threshold=is_abuse,
            source="dataset",
            low_activity_users=low_activity_actors  # keep all low-activity usernames
        )

        self.from_dataset += 1

        print(f"Dataset detection result: {'abuse found' if is_abuse else 'no abuse found'}")
        return is_abuse, evidence

    def _produce_stargazers(self, owner: str, repo_name: str):
        """Producer: fetch stargazers and put them into the pre-filter queue"""
        try:
            page = 1
            per_page = self.config.get('stargazers_per_page', 100)
            max_stargazers = self.config.get('max_stargazers_to_check', 200)

            print(f"Fetching stargazers: {owner}/{repo_name}")

            total_fetched = 0

            while total_fetched < max_stargazers:
                url = f"https://api.github.com/repos/{owner}/{repo_name}/stargazers"
                params = {'page': page, 'per_page': per_page}

                try:
                    response = self.stargazer_session.session.get(
                        url,
                        params=params,
                        headers={**self.stargazer_session.session.headers,
                                 "Accept": "application/vnd.github.v3.star+json"},
                        timeout=self.stargazer_session.request_timeout
                    )
                except Exception:
                    break

                if response.status_code == 200:
                    data = response.json()
                else:
                    if response.status_code in [403, 429]:
                        time.sleep(60)
                        continue
                    break

                if not data:
                    break

                for star_data in data:
                    username = star_data.get('login') or star_data.get('user', {}).get('login')
                    star_date = star_data.get('starred_at')

                    if username and star_date:
                        user_info = UserBasicInfo(username=username, star_date=star_date)
                        self.prefilter.add_user(user_info)
                        total_fetched += 1

                    if total_fetched >= max_stargazers:
                        break

                if len(data) < per_page:
                    break

                page += 1
                time.sleep(0.2)

            self.total_stargazers = total_fetched
            print(f"Fetch complete: {total_fetched} stargazers")

        finally:
            self.prefilter.set_producer_done()

    def _bridge_prefilter_to_deepcheck(self):
        """Bridge thread: forward pre-filter output into deep-check input"""
        self.prefilter.started.wait()

        while True:
            try:
                try:
                    user_info = self.prefilter.output_queue.get(timeout=1.0)
                except queue.Empty:
                    if self.prefilter.producer_done.is_set() and \
                            self.prefilter.input_queue.empty() and \
                            self.prefilter.output_queue.empty():
                        break
                    continue

                self.deep_checker.add_user(user_info)
                self.prefilter.output_queue.task_done()

            except Exception:
                time.sleep(0.5)

        self.deep_checker.set_producer_done()

    def detect(self, repo_data: Dict) -> Tuple[bool, Optional[AbuseEvidence]]:
        try:
            owner, repo_name, total_stars = self.extract_repo_info(repo_data)
            repo_full_name = f"{owner}/{repo_name}"

            # Clear low-activity users found during API scan
            self.low_activity_users_from_api = []

            # 1. Check the local dataset first
            dataset_result = self.check_dataset_first(repo_full_name)

            # 2. If dataset hit, return that result
            if dataset_result is not None:
                return dataset_result

            # 3. If API detection is disabled, skip live checking
            if not self.enable_api_detection:
                print(f"\n{'=' * 60}")
                print(f"API detection is disabled, skipping live check: {repo_full_name}")
                print(f" Repository stars: {total_stars}")
                print(f"{'=' * 60}\n")
                return False, None

            # Mark the start of API detection
            print("\n" + "=" * 60)
            print(f"Dataset not hit, starting real-time API detection: {repo_full_name}")
            print(f" Repository stars: {total_stars}")
            print("=" * 60 + "\n")

            if total_stars < self.config.get('min_stars_for_detection', 20):
                print("Star count below threshold, skipping API detection")
                return False, None

            # 4. Start the API detection thread pipeline

            self.prefilter_workers = self.prefilter.start_workers()
            self.deep_checker_workers = self.deep_checker.start_workers()

            bridge_thread = threading.Thread(
                target=self._bridge_prefilter_to_deepcheck,
                daemon=True
            )
            bridge_thread.start()

            self.producer_thread = threading.Thread(
                target=self._produce_stargazers,
                args=(owner, repo_name),
                daemon=True
            )
            self.producer_thread.start()

            time.sleep(2)
            self.prefilter.started.set()
            self.deep_checker.started.set()

            print("Waiting for API detection to finish...")

            if self.producer_thread and self.producer_thread.is_alive():
                self.producer_thread.join()

            self.prefilter.input_queue.join()
            self.prefilter.set_producer_done()

            for worker in self.prefilter_workers:
                worker.join(timeout=5)

            bridge_thread.join(timeout=5)
            self.deep_checker.input_queue.join()
            self.deep_checker.set_producer_done()

            for worker in self.deep_checker_workers:
                worker.join(timeout=5)

            results = []
            while not self.deep_checker.output_queue.empty():
                try:
                    results.append(self.deep_checker.output_queue.get_nowait())
                except queue.Empty:
                    break

            if not results:
                print("API detection returned no results")
                return False, None

            # get all low-activity usernames
            self.low_activity_users_from_api = self.deep_checker.get_low_activity_users()
            low_activity_count = len(self.low_activity_users_from_api)
            low_activity_percentage = low_activity_count / len(results) if results else 0

            print(f"\nAPI detection results summary:")
            print(f"   - Total stargazers: {self.total_stargazers}")
            print(f"   - Passed pre-filter: {len(results)}")
            print(f"   - Low-activity users: {low_activity_count}/{len(results)} ({low_activity_percentage:.2%})")
            if low_activity_count > 0:
                print(f"   - Sample low-activity users: {self.low_activity_users_from_api[:5]}")

            min_low_activity_percentage = self.config.get('min_low_activity_percentage', 0.1)
            is_abuse = low_activity_percentage >= min_low_activity_percentage

            evidence = AbuseEvidence(
                repo_full_name=repo_full_name,
                total_stars=total_stars,
                low_activity_stars=low_activity_count,
                low_activity_percentage=low_activity_percentage,
                detection_reason=f"{low_activity_count} low-activity users ({low_activity_percentage:.1%})",
                meets_threshold=is_abuse,
                source="api",
                low_activity_users=self.low_activity_users_from_api
            )

            print(f"\n{'=' * 60}")
            print(f"API detection completed: {'abuse found' if is_abuse else 'no abuse found'}")
            print(f"{'=' * 60}\n")

            return is_abuse, evidence

        except Exception as e:
            print(f"API detection failed: {e}")
            return False, None
        finally:
            self.close()

    def close(self):
        """Close all sessions"""
        if not self.enable_api_detection:
            return

        try:
            if self.prefilter:
                self.prefilter.set_producer_done()
            if self.deep_checker:
                self.deep_checker.set_producer_done()
        except:
            pass

        if self.stargazer_session:
            self.stargazer_session.close()
        for session in self.activity_sessions:
            session.close()

    def __del__(self):
        self.close()

    def get_stats(self) -> Dict:
        """Return detector statistics"""
        return {
            "from_dataset": self.from_dataset,
            "from_api": self.total_stargazers > 0,
            "dataset_stats": self.dataset.get_stats()
        }