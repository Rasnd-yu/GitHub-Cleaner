import json
import re
import requests
import time
import csv
import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from abc import ABC, abstractmethod
import warnings

from core_reputation_farming import ReputationFarmingCoreDetector
from core_spoofed_contributor import SpoofedContributorCoreDetector, AbuseEvidence as AbuseEvidence_SC
from core_typo_squatting import TypoSquattingCoreDetector
from core_fake_stars import FakeStarsCoreDetector, AbuseEvidence as AbuseEvidence_FSR
from core_fake_stats import FakeStatsCoreDetector

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Suppress sklearn version mismatch warnings
warnings.filterwarnings('ignore', category=UserWarning,
                        message='Trying to unpickle estimator')


@dataclass
class DetectionResult:
    """Detection result"""
    sub_category: str
    url: str
    is_abuse: bool
    details: Dict[str, Any]


class BaseDetector(ABC):
    """Abstract base class for detectors"""

    def __init__(self, config: Dict):
        self.config = config

        # Get the configuration specific to this detector
        detector_config = self._get_detector_config()

        # Support both the new and the old configuration styles
        if 'github_tokens' in detector_config:
            # New config: uses a token array
            self.github_tokens = detector_config['github_tokens']
            self.github_token = self.github_tokens[0] if self.github_tokens else None
        else:
            # Old config: uses a single token
            self.github_token = detector_config.get('github_token')
            self.github_tokens = [self.github_token] if self.github_token else []

        self.detection_params = detector_config['detection_params']
        self.api_settings = detector_config.get('api_settings', {})

        # Set up the session
        self.session = requests.Session()
        user_agent = self.api_settings.get('user_agent',
                                           self.config['global_settings']['default_user_agent'])
        self.session.headers.update({
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': user_agent
        })
        if self.github_token:
            self.session.headers['Authorization'] = f'token {self.github_token}'

    def _get_detector_config(self) -> Dict:
        """Get the detector configuration"""
        sub_category = self.get_sub_category()
        return self.config['detection_configs'][sub_category]

    @abstractmethod
    def get_sub_category(self) -> str:
        """Return the detector subcategory"""
        pass

    def make_api_call(self, url: str, params: Dict = None) -> Optional[Dict]:
        """Safe API call"""
        # Use the API settings specific to this detector
        max_retries = self.api_settings.get('max_retries',
                                            self.config['global_settings']['default_max_retries'])
        request_timeout = self.api_settings.get('request_timeout',
                                                self.config['global_settings']['default_request_timeout'])
        rate_limit_delay = self.api_settings.get('rate_limit_delay',
                                                 self.config['global_settings']['default_rate_limit_delay'])

        for attempt in range(max_retries):
            try:
                response = self.session.get(url, params=params, timeout=request_timeout)

                # Handle rate limiting
                if response.status_code == 403 and 'rate limit' in response.text.lower():
                    reset_time = response.headers.get('X-RateLimit-Reset', 0)
                    if reset_time:
                        wait_time = max(int(reset_time) - time.time(), 0) + 2
                        logger.warning(f"API rate limited, waiting {wait_time:.0f} seconds...")
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

    def extract_repo_info(self, repo_url: str) -> Tuple[str, str]:
        """Extract the repository information from a URL"""
        repo_url = repo_url.rstrip('/').rstrip('.git')
        pattern = r"github\.com/([^/]+)/([^/?]+)"
        match = re.search(pattern, repo_url)
        if not match:
            raise ValueError(f"Invalid GitHub repository URL: {repo_url}")
        return match.group(1), match.group(2)

    def extract_user_info(self, user_url: str) -> str:
        """Extract the user information from a URL"""
        user_url = user_url.rstrip('/')
        pattern = r"github\.com/([^/?]+)"
        match = re.search(pattern, user_url)
        if not match:
            raise ValueError(f"Invalid GitHub user URL: {user_url}")
        return match.group(1)

    @abstractmethod
    def detect(self, url: str) -> DetectionResult:
        """Run the detection"""
        pass


class FakeStarsDetector(BaseDetector):
    def get_sub_category(self) -> str:
        return "fake_stars"

    def __init__(self, config: Dict):
        super().__init__(config)
        # Do not create core_detector here
        self.core_detector = None
        self.core_config = self._prepare_config()

        # Read from the config whether API detection is enabled, True (enabled) by default
        detection_params = self.detection_params
        self.enable_api_detection = detection_params.get('enable_api_detection', True)

    def _prepare_config(self) -> Dict:
        """Prepare the core detector configuration"""
        detection_params = self.detection_params
        return {
            'max_actions': detection_params.get('max_actions', 2),
            'max_repos': detection_params.get('max_repos', 1),
            'max_orgs': detection_params.get('max_orgs', 1),
            'stargazers_per_page': detection_params.get('stargazers_per_page', 100),
            'max_stargazers_to_check': detection_params.get('max_stargazers_to_check', 200),
            'min_stars_for_detection': detection_params.get('min_stars_for_detection', 20),
            'prefilter_max_followers': detection_params.get('prefilter_max_followers', 50),
            'prefilter_max_public_repos': detection_params.get('prefilter_max_public_repos', 10),
            'min_low_activity_stars': detection_params.get('min_low_activity_stars', 5),
            'min_low_activity_percentage': detection_params.get('min_low_activity_percentage', 0.1),
            'activity_days_around_star': detection_params.get('activity_days_around_star', 30),
            'max_workers': detection_params.get('max_workers', 15),
            'api_settings': self.api_settings
        }

    def detect(self, repo_data: Dict) -> DetectionResult:
        """Create a new core_detector instance for every detection"""
        core_detector = None
        try:
            # Create a new instance every time and destroy it when done
            github_tokens = getattr(self, 'github_tokens', [self.github_token] if self.github_token else [])

            core_detector = FakeStarsCoreDetector(
                github_tokens=github_tokens,
                config=self.core_config,
                enable_api_detection=self.enable_api_detection  # Pass the switch through
            )

            is_abuse, evidence = core_detector.detect(repo_data)
            # Pass core_config instead of core_detector
            details = self._generate_details(is_abuse, evidence, self.core_config)

            repo_url = repo_data.get('html_url', '')
            return DetectionResult(
                sub_category="fake_stars",
                url=repo_url,
                is_abuse=is_abuse,
                details=details
            )

        except Exception as e:
            logger.error(f"Fake star detection failed: {e}")
            return DetectionResult(
                sub_category="fake_stars",
                url=repo_data.get('html_url', 'unknown'),
                is_abuse=False,
                details={"error": str(e)}
            )
        finally:
            # Make sure it is closed
            if core_detector:
                core_detector.close()

    def _generate_details(self, is_abuse: bool, evidence: Optional[AbuseEvidence_FSR], core_config: Dict) -> Dict:
        """Generate a simplified detail report"""
        if not evidence:
            return {
                "abuse_detected": False,
                "message": "No fake star behavior found"
            }

        return {
            "abuse_detected": is_abuse,
            "repository": evidence.repo_full_name,
            "total_stars": evidence.total_stars,
            "low_activity_stars": evidence.low_activity_stars,
            "low_activity_percentage": round(evidence.low_activity_percentage, 4),
            "detection_reason": evidence.detection_reason,
            "thresholds": {
                "min_low_activity_percentage": core_config.get('min_low_activity_percentage', 0.1)
            },
            "low_activity_users": evidence.low_activity_users if hasattr(evidence, 'low_activity_users') else []
        }


class AutomaticUpdatesDetector(BaseDetector):
    """Automatic updates detector - uses the core detection module"""

    def get_sub_category(self) -> str:
        return "automatic_updates"

    def __init__(self, config: Dict):
        super().__init__(config)

        # Get the detection parameters from the config
        detection_params = self.detection_params

        # Core detector configuration
        core_config = {
            'time_window_hours': detection_params.get('time_window_hours', 48),
            'min_commits': detection_params.get('min_commits', 20),
            'max_avg_changes': detection_params.get('max_avg_changes', 5),
            'commit_delay_seconds': detection_params.get('commit_delay_seconds', 0.1),
            'max_commits_to_check': detection_params.get('max_commits_to_check', 30),
            'max_retries': self.api_settings.get('max_retries', 3),
            'request_timeout': self.api_settings.get('request_timeout', 30),
            'rate_limit_delay': self.api_settings.get('rate_limit_delay', 1.0)
        }

        # Create the core detector
        self.core_detector = None
        self.core_config = core_config

    def _get_core_detector(self):
        """Lazily load the core detector"""
        if self.core_detector is None:
            from core_automatic_updates import AutomaticUpdatesCoreDetector

            github_token = getattr(self, 'github_token', None)
            self.core_detector = AutomaticUpdatesCoreDetector(
                github_token=github_token,
                config=self.core_config
            )
        return self.core_detector

    def detect(self, repo_data: Dict) -> DetectionResult:
        """
        Detect whether the repository abuses automatic updates

        Args:
            repo_data: Repository JSON data

        Returns:
            Detection result
        """
        try:
            core_detector = self._get_core_detector()

            # Run the detection with the core detector
            is_abuse, evidence = core_detector.detect(repo_data)

            # Get the repository URL
            repo_url = repo_data.get('html_url', '')
            if not repo_url and evidence:
                repo_url = f"https://github.com/{evidence.repo_full_name}"

            # Generate the details
            details = self._generate_details(evidence, is_abuse)

            return DetectionResult(
                sub_category="automatic_updates",
                url=repo_url,
                is_abuse=is_abuse,
                details=details
            )

        except Exception as e:
            logger.error(f"Automatic updates detection failed: {e}")
            return DetectionResult(
                sub_category="automatic_updates",
                url=repo_data.get('html_url', 'unknown'),
                is_abuse=False,
                details={"error": str(e)}
            )

    def _generate_details(self, evidence: Optional[Any], is_abuse: bool) -> Dict:
        """Generate the detail report"""
        if not evidence:
            return {
                "abuse_detected": False,
                "message": "An error occurred during detection"
            }

        return {
            "repository": evidence.repo_full_name,
            "abuse_detected": evidence.is_abuse,
            "total_commits": evidence.total_commits,
            "avg_changes": evidence.avg_changes,
            "summary": f"Detected {evidence.total_commits} commits, average change size {evidence.avg_changes:.2f}"
        }

    def close(self):
        """Close the core detector"""
        if self.core_detector:
            self.core_detector.close()


class TypoSquattingDetector(BaseDetector):
    """Typo squatting detector - based on the core detection module"""

    def get_sub_category(self) -> str:
        return "typo_squatting"

    def __init__(self, config: Dict):
        super().__init__(config)

        # Get the detection parameters from the config
        detection_params = self.detection_params

        core_config = {
            'min_stars_high': detection_params.get('min_stars_high', 1000),
            'similarity_threshold': detection_params.get('similarity_threshold', 0.7),
            'name_similarity_threshold': detection_params.get('name_similarity_threshold', 0.7),
            'similar_repo_check_count': detection_params.get('similar_repo_check_count', 5),
            'exclude_topics': detection_params.get('exclude_topics', ["template", "boilerplate"]),
            'max_retries': self.api_settings.get('max_retries', 3),
            'request_timeout': self.api_settings.get('request_timeout', 20),
            'rate_limit_delay': self.api_settings.get('rate_limit_delay', 2),
            'min_star_ratio': detection_params.get('min_star_ratio', 2.0),
            'min_fork_ratio': detection_params.get('min_fork_ratio', 2.0),
            'famous_orgs': detection_params.get('famous_orgs', [
                "microsoft", "google", "facebook", "amazon", "apple", "netflix",
                "uber", "airbnb", "twitter", "linkedin", "github", "docker",
                "kubernetes", "tensorflow", "pytorch", "reactjs", "angular",
                "vuejs", "nodejs", "python", "golang", "rust-lang"
            ])
        }

        # Get the path of the popular repositories dataset
        corpus_path = detection_params.get('corpus_path', 'corpus_repos_hot.json')

        # Create the core detector, passing the dataset path
        self.core_detector = TypoSquattingCoreDetector(
            github_token=self.github_token,
            config=core_config,
            corpus_path=corpus_path
        )

    def detect(self, repo_data: Dict) -> DetectionResult:
        """Detect whether the repository is typo squatting (data-driven mode)"""
        try:
            # Run the detection with the core detector (passing the full repository JSON data)
            is_abuse, evidences = self.core_detector.detect_repository_abuse(repo_data)

            # Get the repository URL
            repo_url = repo_data.get('html_url', '')

            # Generate the details
            details = self._generate_details(repo_data, is_abuse, evidences)

            return DetectionResult(
                sub_category="typo_squatting",
                url=repo_url,
                is_abuse=is_abuse,
                details=details
            )

        except Exception as e:
            logger.error(f"Typo squatting detection failed: {e}")
            return DetectionResult(
                sub_category="typo_squatting",
                url=repo_data.get('html_url', 'unknown'),
                is_abuse=False,
                details={"error": str(e)}
            )

    def _generate_details(self, repo_data: Dict, is_abuse: bool,
                          evidences: List[Dict]) -> Dict:
        """Generate the detail report"""
        repo_full_name = repo_data.get('full_name', 'unknown')
        repo_stars = repo_data.get('stargazers_count', 0)

        if not evidences:
            return {
                "repository": repo_full_name,
                "abuse_detected": False,
                "total_evidences": 0,
                "message": "No typo squatting behavior found"
            }

        # Prepare the evidence details
        evidence_details = []
        for evidence in evidences:
            evidence_details.append({
                "similar_repo": evidence.get("similar_repo", ""),
                "content_similarity": evidence.get("content_similarity", 0.0),
                "name_similarity": evidence.get("name_similarity", 0.0),
                "current_stars": evidence.get("current_stars", 0),
                "similar_stars": evidence.get("similar_stars", 0),
                "star_ratio": evidence.get("similar_stars", 0) / max(evidence.get("current_stars", 1), 1),
                "abuse_reason": evidence.get("abuse_reason", "")
            })

        # Sort by content similarity
        evidence_details_sorted = sorted(evidence_details, key=lambda x: x['content_similarity'], reverse=True)

        return {
            "repository": repo_full_name,
            "repository_stars": repo_stars,
            "abuse_detected": True,
            "total_evidences": len(evidences),
            "detection_strategy": "similar-name-high-star-comparison",
            "detection_logic": "Check whether a low-popularity repository has content similar to a high-popularity repository with the same or a similar name",
            "evidences": evidence_details_sorted[:3],
            "highest_content_similarity": max(e.get("content_similarity", 0) for e in evidences),
            "avg_content_similarity": sum(e.get("content_similarity", 0) for e in evidences) / len(
                evidences) if evidences else 0,
            "name_similarity_threshold": self.core_detector.config.get("name_similarity_threshold", 0.9),
            "content_similarity_threshold": self.core_detector.config.get("similarity_threshold", 0.8),
            "summary": f"Found content similar to {len(evidences)} high-popularity repositories with similar names"
        }

class ReputationFarmingDetector(BaseDetector):
    """Reputation farming detector - based on repository PR abuse analysis (data-driven mode)"""

    def get_sub_category(self) -> str:
        return "reputation_farming"

    def __init__(self, config: Dict):
        super().__init__(config)

        # Get the detection parameters from the config
        detection_params = self.detection_params

        # Core detector configuration
        core_config = {
            'min_pr_age_days': detection_params.get('min_pr_age_days', 400),
            'max_prs_per_repo': detection_params.get('max_prs_per_repo', 300),
            'post_resolution_delay_days': detection_params.get('post_resolution_delay_days', 400),
            'max_workers': detection_params.get('max_workers', 5),
            'api_delay': detection_params.get('api_delay', 0.5),
            'search_api_delay': detection_params.get('search_api_delay', 1.0),
            'token_thresholds': detection_params.get('token_thresholds', None),  # Threshold of each token
            'suspicious_keywords': detection_params.get('suspicious_keywords', [
                "+1", "LGTM", "looks good", "approved", "nice", "good job", "thanks",
                "great", "awesome", "excellent", "good work", "well done"
            ]),
            'min_comment_length': detection_params.get('min_comment_length', 10),
            'generic_patterns': detection_params.get('generic_patterns', [
                r"^[\s\W]*$",
                r"^(good|nice|great|awesome|excellent)[\s\.,!]*$",
                r"^\+1[\s\W]*$",
                r"^LGTM[\s\W]*$",
                r"^thanks?[\s\.,!]*$",
                r"^approved[\s\.,!]*$"
            ])
        }

        # Get the token list
        github_tokens = getattr(self, 'github_tokens', [self.github_token] if self.github_token else [])

        # Create the core detector, passing the token list
        self.core_detector = ReputationFarmingCoreDetector(
            tokens=github_tokens,  # Use the tokens parameter
            config=core_config
        )

    def detect(self, repo_data: Dict) -> DetectionResult:
        """
        Detect whether the repository has PR abuse (data-driven mode)

        Args:
            repo_data: Repository JSON data

        Returns:
            Detection result
        """
        try:
            # Run the detection with the core detector (passing the JSON data)
            is_abuse, report = self.core_detector.detect_repository_abuse(repo_data)

            # Generate the details
            details = self._generate_details(report, is_abuse)

            return DetectionResult(
                sub_category="reputation_farming",
                url=repo_data.get('html_url', ''),
                is_abuse=is_abuse,
                details=details
            )

        except Exception as e:
            logger.error(f"Reputation farming detection failed: {e}")
            return DetectionResult(
                sub_category="reputation_farming",
                url=repo_data.get('html_url', 'unknown'),
                is_abuse=False,
                details={"error": str(e)}
            )

    def _generate_details(self, report, is_abuse: bool) -> Dict:
        """Generate the detail report"""
        if not report:
            return {
                "abuse_detected": False,
                "message": "An error occurred during detection"
            }

        # Convert the UserAbuseEvidence objects into serializable dicts
        suspicious_users_evidence = []
        for user_evidence in report.suspicious_users_evidence:
            user_dict = {
                "user_name": user_evidence.user_name,
                "abuse_count": user_evidence.abuse_count,
                "first_activity": user_evidence.first_activity,
                "last_activity": user_evidence.last_activity,
                "evidences": [
                    {
                        "target_url": e.target_url,
                        "target_type": e.target_type,
                        "action_type": e.action_type,
                        "action_date": e.action_date,
                        "content": e.content,
                        "suspicious_reason": e.suspicious_reason,
                        "pr_state": e.pr_state,
                        "days_after_resolution": e.days_after_resolution
                    }
                    for e in user_evidence.evidences
                ]
            }
            suspicious_users_evidence.append(user_dict)

        return {
            "repository": report.repository,
            "abuse_detected": is_abuse,
            "abuse_user_count": report.abuse_user_count,
            "abuse_activity_count": report.abuse_activity_count,
            "detection_strategy": "repository-pr-abuse-analysis",
            "detection_logic": "Detect generic comment/approval activity happening more than 400 days after an old PR was resolved (closed/merged)",
            "suspicious_users_evidence": suspicious_users_evidence,
            "summary": f"Found {report.abuse_user_count} suspicious users with {report.abuse_activity_count} abusive activities in total"
        }


class FakeStatsDetector(BaseDetector):
    """Fake stats detector - based on the core detection module (data-driven mode only)"""

    def get_sub_category(self) -> str:
        return "fake_stats"

    def __init__(self, config: Dict):
        super().__init__(config)

        # Get the detection parameters from the config
        detection_params = self.detection_params

        # Core detector configuration
        core_config = {
            'star_discrepancy_threshold': detection_params.get('star_discrepancy_threshold', 5),
            'min_fake_stars': detection_params.get('min_fake_stars', 100),
            'max_profile_repos_to_check': detection_params.get('max_profile_repos_to_check', 5),
            'max_user_repos_to_check': detection_params.get('max_user_repos_to_check', 50),
            'max_retries': self.api_settings.get('max_retries', 3),
            'request_timeout': self.api_settings.get('request_timeout', 30),
            'rate_limit_delay': self.api_settings.get('rate_limit_delay', 1.0)
        }

        # Create the core detector
        self.core_detector = FakeStatsCoreDetector(
            github_token=self.github_token,
            config=core_config
        )

    def detect(self, repo_data: Dict) -> DetectionResult:
        """
        Detect fake stats among the core developers of the repository (data-driven mode)

        Args:
            repo_data: Repository data dict, contains the core_developers field

        Returns:
            Detection result
        """
        try:
            # Run the detection with the core detector (data-driven mode)
            is_abuse, evidences = self.core_detector.detect(repo_data)

            # Get the repository URL
            repo_url = repo_data.get('html_url', '')

            # Generate the details
            details = self._generate_details(is_abuse, evidences, repo_url)

            return DetectionResult(
                sub_category="fake_stats",
                url=repo_url,
                is_abuse=is_abuse,
                details=details
            )

        except Exception as e:
            logger.error(f"Fake stats detection failed: {e}")
            return DetectionResult(
                sub_category="fake_stats",
                url=repo_data.get('html_url', 'unknown'),
                is_abuse=False,
                details={"error": str(e)}
            )

    def _generate_details(self, is_abuse: bool, evidences: List[Dict], repo_url: str) -> Dict:
        """Generate the detail report"""
        if not evidences:
            return {
                "repository": repo_url,
                "abuse_detected": False,
                "total_evidences": 0,
                "message": "No fake stats behavior found"
            }

        # Group the evidence by developer
        developer_abuses = {}
        for evidence in evidences:
            developer = evidence.get('developer', 'unknown')
            if developer not in developer_abuses:
                developer_abuses[developer] = []
            developer_abuses[developer].append(evidence)

        # Handle the different evidence types separately
        fake_star_developers = []
        others_stats_developers = []
        # Removed same_person_developers - this is not evidence of abuse

        for developer, dev_evidences in developer_abuses.items():
            has_fake_stars = any(e.get('type') == 'fake_user_stars' for e in dev_evidences)
            has_others_stats = any(e.get('type') == 'others_stats_abuse' for e in dev_evidences)
            # Removed the has_same_person check

            if has_fake_stars:
                fake_star_developers.append(developer)
            if has_others_stats:
                others_stats_developers.append(developer)
            # Removed the same_person_developers logic

        # Collect the detailed evidence of every developer
        developers_details = []
        for developer, dev_evidences in developer_abuses.items():
            dev_detail = {
                "developer": developer,
                "has_fake_stars": any(e.get('type') == 'fake_user_stars' for e in dev_evidences),
                "has_others_stats": any(e.get('type') == 'others_stats_abuse' for e in dev_evidences),
                # Removed the has_same_person field
                "evidences": []
            }

            for ev in dev_evidences:
                if ev.get('type') == 'fake_user_stars':
                    dev_detail["evidences"].append({
                        "type": "fake_star_declaration",
                        "claimed_stars": ev.get('claimed_stars'),
                        "actual_stars": ev.get('actual_stars'),
                        "details": ev.get('details')
                    })
                elif ev.get('type') == 'others_stats_abuse':
                    dev_detail["evidences"].append({
                        "type": "others_stats_abuse",
                        "stats_users_used": [u['username'] for u in ev.get('stats_users_used', [])],
                        "details": ev.get('details')
                    })
                # Removed the handling of possible_same_person_accounts - it is not evidence of abuse

            developers_details.append(dev_detail)

        details = {
            "repository": repo_url,
            "abuse_detected": is_abuse,
            "total_evidences": len(evidences),
            "affected_developers": len(developer_abuses),
            "developers_with_fake_stars": fake_star_developers,
            "developers_with_others_stats": others_stats_developers,
            "developers_details": developers_details,
            "detection_strategy": "data-driven-developer-analysis",
            "detection_logic": "Detect fake star claims in core developer profiles and the abuse of other people's stats",
            "thresholds": {
                "star_discrepancy_threshold": self.detection_params.get('star_discrepancy_threshold', 5),
                "min_fake_stars": self.detection_params.get('min_fake_stars', 100)
            }
        }

        # Generate the overall summary - same_person parts removed
        summary_parts = []
        if fake_star_developers:
            summary_parts.append(f"{len(fake_star_developers)} developers have fake star claims")
        if others_stats_developers:
            summary_parts.append(f"{len(others_stats_developers)} developers use other people's stats")
        # Removed the same_person part of the summary

        details["summary"] = "; ".join(summary_parts) if summary_parts else "Fake stats behavior found"

        return details


class SpoofedContributorDetector(BaseDetector):
    """Spoofed contributor detector - based on the core detection module"""

    def get_sub_category(self) -> str:
        return "spoofed_contributor"

    def __init__(self, config: Dict):
        super().__init__(config)

        # Get the detection parameters from the config
        detection_params = self.detection_params

        # Core detector configuration
        core_config = {
            'min_contributor_commits': detection_params.get('min_contributor_commits', 2),
            'max_repo_forks': detection_params.get('max_repo_forks', 1000),
            'max_repo_stars': detection_params.get('max_repo_stars', 1000),
            'min_repo_age_days': detection_params.get('min_repo_age_days', 3000),
            'max_contributors_per_repo': detection_params.get('max_contributors_per_repo', 30),
            'max_retries': self.api_settings.get('max_retries', 3),
            'request_timeout': self.api_settings.get('request_timeout', 35),
            'rate_limit_delay': self.api_settings.get('rate_limit_delay', 2.0)
        }

        # Get the path of the popular developers dataset
        corpus_path = detection_params.get('corpus_path', 'corpus_developers_famous.json')

        # Create the core detector
        self.core_detector = SpoofedContributorCoreDetector(
            github_token=self.github_token,
            config=core_config,
            corpus_path=corpus_path
        )

    def detect(self, repo_data: Dict) -> DetectionResult:
        """Detect spoofed contributors in the repository (data-driven mode)"""
        try:
            # Run the detection with the core detector (passing the full repository JSON data)
            is_abuse, evidences = self.core_detector.detect(repo_data)

            # Get the repository URL
            repo_url = repo_data.get('html_url', '')

            # Generate the condensed details
            details = self._generate_details(repo_data, is_abuse, evidences)

            return DetectionResult(
                sub_category="spoofed_contributor",
                url=repo_url,
                is_abuse=is_abuse,
                details=details
            )

        except Exception as e:
            logger.error(f"Spoofed contributor detection failed: {e}")
            return DetectionResult(
                sub_category="spoofed_contributor",
                url=repo_data.get('html_url', 'unknown'),
                is_abuse=False,
                details={"error": str(e)}
            )
        finally:
            # Close the session
            self.core_detector.close()

    def _generate_details(self, repo_data: Dict, is_abuse: bool,
                          evidences: List[AbuseEvidence_SC]) -> Dict:
        """Generate the condensed detail report"""
        repo_full_name = repo_data.get('full_name', 'unknown')

        if not is_abuse or not evidences:
            return {
                "abuse_detected": False,
                "repository": repo_full_name,
                "message": "No spoofed contributor behavior found"
            }

        # Group by contributor and extract the key information
        suspicious_contributors = []
        for evidence in evidences:
            suspicious_contributors.append({
                "login": evidence.suspicious_contributor,
                "contributions": evidence.contributions,
                "followers": evidence.contributor_info.get('followers', 0),
                "public_repos": evidence.contributor_info.get('public_repos', 0)
            })

        # Basic repository information
        repo_info = {
            "forks": repo_data.get('forks_count', 0),
            "stars": repo_data.get('stargazers_count', 0),
            "created_at": repo_data.get('created_at', '')
        }

        # Compute the repository age
        repo_age_days = 0
        if repo_info['created_at']:
            try:
                created_date = datetime.fromisoformat(repo_info['created_at'].replace('Z', '+00:00'))
                repo_age_days = (datetime.now(timezone.utc) - created_date).days
            except:
                pass
        repo_info['age_days'] = repo_age_days

        return {
            "abuse_detected": True,
            "repository": repo_full_name,
            "repo_info": repo_info,
            "suspicious_contributors": suspicious_contributors,
            "summary": f"Found {len(suspicious_contributors)} popular developers with insufficient contributions in small/new repositories"
        }


class IssueSpamDetector(BaseDetector):
    """Issue spam detector - based on the core detection module"""

    def get_sub_category(self) -> str:
        return "issue_spam"

    def __init__(self, config: Dict):
        super().__init__(config)

        # Get the detection parameters from the config
        detection_params = self.detection_params

        # Core detector configuration (multithreading parameters added)
        core_config = {
            'per_page': detection_params.get('per_page', 100),
            'max_issues_to_check': detection_params.get('max_issues_to_check', 500),
            'fetch_delay_seconds': detection_params.get('fetch_delay_seconds', 0.8),
            'model_path': detection_params.get('model_path',
                                               'mlartifacts/2/0579ea92a6c7494e9bfdf42813fe3867/artifacts/nn/model.pkl'),
            'max_retries': self.api_settings.get('max_retries', 3),
            'request_timeout': self.api_settings.get('request_timeout', 30),
            'rate_limit_delay': self.api_settings.get('rate_limit_delay', 1.0),
            'predict_workers': detection_params.get('predict_workers', 10),  # Prediction concurrency
            'fetch_workers': detection_params.get('fetch_workers', 3)  # Fetch concurrency
        }

        # Create the core detector
        self.core_detector = None
        self.core_config = core_config

    def _get_core_detector(self):
        """Lazily load the core detector"""
        if self.core_detector is None:
            from core_issue_spam import IssueSpamCoreDetector
            self.core_detector = IssueSpamCoreDetector(
                github_token=self.github_token,
                config=self.core_config
            )
        return self.core_detector

    def detect(self, repo_data: Dict) -> DetectionResult:
        """
        Detect issue spam in the repository (data-driven mode)

        Args:
            repo_data: Repository JSON data

        Returns:
            Detection result
        """
        try:
            core_detector = self._get_core_detector()

            # Run the detection with the core detector
            is_abuse, evidence = core_detector.detect(repo_data)

            # Get the repository URL
            repo_url = repo_data.get('html_url', '')
            if not repo_url and evidence:
                repo_url = f"https://github.com/{evidence.repo_full_name}"

            # Generate the details
            details = self._generate_details(evidence, is_abuse)

            return DetectionResult(
                sub_category="issue_spam",
                url=repo_url,
                is_abuse=is_abuse,
                details=details
            )

        except Exception as e:
            logger.error(f"Issue spam detection failed: {e}")
            return DetectionResult(
                sub_category="issue_spam",
                url=repo_data.get('html_url', 'unknown'),
                is_abuse=False,
                details={"error": str(e)}
            )

    def _generate_details(self, evidence, is_abuse: bool) -> Dict:
        """Generate the condensed detail report"""
        if not evidence:
            return {
                "abuse_detected": False,
                "message": "An error occurred during detection"
            }

        # Build the evidence organized by user
        spam_evidence_list = []
        for user_evidence in evidence.spam_evidence:
            spam_evidence_list.append({
                "username": user_evidence.username,
                "user_url": user_evidence.user_url,
                "spam_count": user_evidence.spam_count,
                "spam_issues": user_evidence.spam_issues
            })

        return {
            "repository": evidence.repo_full_name,
            "abuse_detected": evidence.is_abuse,
            "total_issues": evidence.total_issues,
            "spam_count": evidence.spam_count,
            "spam_ratio": round(evidence.spam_ratio, 4),
            "detection_reason": evidence.detection_reason,
            "spam_evidence": spam_evidence_list
        }

    def close(self):
        """Close the core detector"""
        if self.core_detector:
            self.core_detector.close()


class KeywordStuffingDetector(BaseDetector):
    """BM25-based keyword stuffing detector - uses the core module"""

    def get_sub_category(self) -> str:
        return "keyword_stuffing"

    def __init__(self, config: Dict):
        super().__init__(config)

        # Get the GitHub token
        github_token = self.github_token

        # Get the corpus path
        corpus_path = self.detection_params.get('corpus_path', 'corpus_keyword_stuffing.json')

        # Get the detection parameters from the config
        core_config = {
            'score_threshold': self.detection_params.get('score_threshold', 2.0),
            'min_low_score_count': self.detection_params.get('min_low_score_count', 5),
            'min_readme_length': self.detection_params.get('min_readme_length', 100),
            'min_corpus_size': self.detection_params.get('min_corpus_size', 10),
            'rate_limit_delay': self.api_settings.get('rate_limit_delay', 1.0),
            'max_retries': self.api_settings.get('max_retries', 3)
        }

        # Import the core detector
        try:
            from core_keyword_stuffing import KeywordStuffingCoreDetector

            self.core_detector = KeywordStuffingCoreDetector(
                github_token=github_token,
                corpus_path=corpus_path,
                config=core_config
            )
            self.model_loaded = True
            logger.info(f"Keyword stuffing detector initialized, corpus path: {corpus_path}")

        except ImportError as e:
            logger.error(f"Failed to import core_keyword_stuffing: {e}")
            self.model_loaded = False
        except Exception as e:
            logger.error(f"Failed to initialize the keyword stuffing detector: {e}")
            self.model_loaded = False

    def detect(self, repo_data: Dict) -> DetectionResult:
        """
        Detect whether the repository stuffs keywords
        Uses the JSON data passed in directly; the README is fetched internally

        Args:
            repo_data: Repository JSON data

        Returns:
            Detection result
        """
        try:
            if not self.model_loaded:
                return DetectionResult(
                    sub_category="keyword_stuffing",
                    url=repo_data.get('html_url', 'unknown'),
                    is_abuse=False,
                    details={"error": "The detector was not initialized correctly"}
                )

            # Run the detection with the core detector (the README is fetched internally)
            is_abuse, evidence = self.core_detector.detect(repo_data)

            # Get the repository URL
            repo_url = repo_data.get('html_url', '')
            if not repo_url and evidence:
                repo_url = f"https://github.com/{evidence.repo_full_name}"

            # Generate the details
            details = self._generate_details(evidence, is_abuse)

            return DetectionResult(
                sub_category="keyword_stuffing",
                url=repo_url,
                is_abuse=is_abuse,
                details=details
            )

        except Exception as e:
            logger.error(f"Keyword stuffing detection failed: {e}")
            return DetectionResult(
                sub_category="keyword_stuffing",
                url=repo_data.get('html_url', 'unknown'),
                is_abuse=False,
                details={"error": str(e)}
            )

    def _generate_details(self, evidence, is_abuse: bool) -> Dict:
        """Generate the detail report"""
        if not evidence:
            return {
                "abuse_detected": False,
                "message": "An error occurred during detection"
            }

        return {
            "repository": evidence.repo_full_name,
            "abuse_detected": evidence.is_abuse,
            "total_keywords": evidence.total_keywords,
            "low_score_keywords_count": evidence.low_score_keywords_count,
            "low_score_keywords": evidence.low_score_keywords[:10],
            "avg_score": evidence.avg_score,
            "min_score": evidence.min_score,
            "max_score": evidence.max_score,
            "mode": evidence.details.get('mode', 'bm25'),
            "readme_length": evidence.details.get('readme_length', 0),
            "summary": f"Found {evidence.low_score_keywords_count}/{evidence.total_keywords} keywords scoring below the threshold, "
                       f"average score: {evidence.avg_score:.4f}"
        }


class AbuseDetectorFactory:
    """Detector factory"""

    def __init__(self, config_file: str = "config.json"):
        with open(config_file, 'r', encoding='utf-8') as f:
            self.config = json.load(f)

        self.detectors = {
            "fake_stars": FakeStarsDetector(self.config),
            "automatic_updates": AutomaticUpdatesDetector(self.config),
            "typo_squatting": TypoSquattingDetector(self.config),
            "reputation_farming": ReputationFarmingDetector(self.config),
            "fake_stats": FakeStatsDetector(self.config),
            "spoofed_contributor": SpoofedContributorDetector(self.config),
            "issue_spam": IssueSpamDetector(self.config),
            "keyword_stuffing": KeywordStuffingDetector(self.config)
        }

    def get_detector(self, sub_category: str) -> Optional[BaseDetector]:
        """Get a detector"""
        return self.detectors.get(sub_category)

    def detect_repository(self, repo_data: Dict) -> Dict[str, DetectionResult]:
        """
        Run all detections on a single repository

        Args:
            repo_data: Repository JSON data

        Returns:
            Dict of detection results, keyed by detection category
        """
        results = {}
        repo_url = repo_data.get('html_url', '')

        for category, detector in self.detectors.items():
            try:
                # All detectors use the data-driven mode
                result = detector.detect(repo_data)
                results[category] = result
            except Exception as e:
                logger.error(f"Error while detecting category {category}: {e}")
                results[category] = DetectionResult(
                    sub_category=category,
                    url=repo_url,
                    is_abuse=False,
                    details={"error": str(e)}
                )

        return results

    def detect_csv(self, csv_file: str, output_file: str = "detection_output.csv"):
        """
        Batch detection over a CSV file - note: sub_category in the CSV must be a repository-related detection category

        Since most detectors are data-driven now, the full repository information has to be read from the CSV
        This method may need to be redesigned or removed
        """
        logger.warning("The detect_csv method is deprecated, because most detectors now need the full repository JSON data")
        logger.warning("Use AbuseDetectionPipeline to process JSON datasets instead")
        return []


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description='GitHub abuse detection framework')
    parser.add_argument('--repo', '-r', help='Path to the repository JSON file or a JSON string')
    parser.add_argument('--category', '-c', help='Detection category (optional, all detections run if omitted)')
    parser.add_argument('--output', '-o', help='Path to the output file')

    args = parser.parse_args()

    factory = AbuseDetectorFactory()

    if args.repo:
        # Load the repository data
        try:
            # Try reading it as a JSON file
            with open(args.repo, 'r', encoding='utf-8') as f:
                repo_data = json.load(f)
        except FileNotFoundError:
            # Try parsing it as a JSON string
            try:
                repo_data = json.loads(args.repo)
            except json.JSONDecodeError:
                logger.error("Cannot parse the repository data, please provide a valid JSON file path or JSON string")
                return

        # Make sure this is a single repository record
        if isinstance(repo_data, list):
            if len(repo_data) > 0:
                repo_data = repo_data[0]
            else:
                logger.error("The repository list is empty")
                return

        # Run the detection
        if args.category:
            # Only detect the specified category
            detector = factory.get_detector(args.category)
            if detector:
                result = detector.detect(repo_data)
                print(json.dumps({
                    "category": result.sub_category,
                    "url": result.url,
                    "is_abuse": result.is_abuse,
                    "details": result.details
                }, indent=2, ensure_ascii=False))
            else:
                logger.error(f"Detection category not found: {args.category}")
        else:
            # Run all detections
            results = factory.detect_repository(repo_data)
            output = {}
            for category, result in results.items():
                output[category] = {
                    "url": result.url,
                    "is_abuse": result.is_abuse,
                    "details": result.details
                }

            if args.output:
                with open(args.output, 'w', encoding='utf-8') as f:
                    json.dump(output, f, indent=2, ensure_ascii=False)
                logger.info(f"Results saved to: {args.output}")
            else:
                print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
