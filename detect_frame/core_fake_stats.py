import re
import base64
import logging
from typing import Dict, List, Optional, Tuple, Any
import requests
import time
from functools import lru_cache
import urllib.parse

logger = logging.getLogger(__name__)


class FakeStatsCoreDetector:
    """Core fake stats detector - detects false claims and abusive behavior in profile READMEs"""

    def __init__(self, github_token: str = None, config: Dict = None):
        self.github_token = github_token
        self.config = config or {}

        # Set up the session
        self.session = requests.Session()
        self.session.headers.update({
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': self.config.get('user_agent', 'FakeStatsDetector/1.0')
        })
        if self.github_token:
            self.session.headers['Authorization'] = f'token {self.github_token}'

        # Compile regex patterns
        self._compile_patterns()

        # Caches
        self._stats_cache = {}
        self._username_cache = {}  # Caches user ID to username mappings
        self._profile_repo_cache = {}  # Caches profile repository lookup results

        # Whitelist config - repositories skipped by the fake stats check
        self.whitelist_repos = {'jaywcjlove/linux-command', 'rougier/nano-emacs'}

    def _compile_patterns(self):
        """Compile regex patterns"""
        # Patterns for user star claims (original patterns preserved)
        self.user_star_patterns = [
            r'(?:my|total|github)\s+(?:stars?|⭐)\s*[:\-]?\s*(\d+[,.]?\d*[kKmM]?)',
            r'(\d+[,.]?\d*[kKmM]?)\s+(?:stars?|⭐)\s+(?:on|across|in)\s+github',
            r'github\s+stars?\s*[:\-]?\s*(\d+[,.]?\d*[kKmM]?)',
            r'total\s+stars?\s*[:\-]?\s*(\d+[,.]?\d*[kKmM]?)',
            r'⭐\s*(\d+[,.]?\d*[kKmM]?)\+?\s*(?:stars?)?',
            r'🌟\s*(\d+[,.]?\d*[kKmM]?)\+?\s*(?:stars?)?',
            r'stars?\s*[:\-]?\s*(\d+[,.]?\d*[kKmM]?)\+?',
            r'(?:i\s+have|i\'ve\s+got|i\s+got)\s+(\d+[,.]?\d*[kKmM]?)\s+stars?',
        ]

        # Enhanced github-readme-stats URL patterns
        self.stats_url_patterns = [
            # Full URL matches (including protocol)
            r'https?://github-readme-stats\.vercel\.app/api/?\?[^"\'\)\s>]*',
            r'https?://github-readme-stats\.vercel\.app/?\?[^"\'\)\s>]*',
            r'https?://github-readme-stats\.git\.app/api/?\?[^"\'\)\s>]*',
            r'https?://api\.github-readme-stats\.vercel\.app/?\?[^"\'\)\s>]*',
            r'https?://git-stats-readme\.vercel\.app/?\?[^"\'\)\s>]*',

            # Match Markdown image syntax
            r'!\[.*?\]\((https?://github-readme-stats\.vercel\.app[^\)]+)\)',
            r'!\[.*?\]\((https?://github-readme-stats\.git\.app[^\)]+)\)',
            r'!\[.*?\]\((https?://api\.github-readme-stats\.vercel\.app[^\)]+)\)',

            # Match HTML img tags
            r'<img[^>]+src=["\'](https?://github-readme-stats\.vercel\.app[^"\']+)["\'][^>]*>',
        ]

        # Keywords identifying stats cards
        self.stats_card_indicators = [
            'github-readme-stats',
            'stats',
            'top-langs',
            'wakatime',
            'streak-stats'
        ]

        self.compiled_user_star_patterns = [re.compile(pattern, re.IGNORECASE)
                                            for pattern in self.user_star_patterns]
        self.compiled_stats_patterns = [re.compile(pattern, re.IGNORECASE)
                                        for pattern in self.stats_url_patterns]

        # Keywords for profile repositories
        self.profile_repo_keywords = [
            'profile', 'home', 'homepage', 'personal', 'readme'
        ]

    def is_whitelisted(self, repo_full_name: str) -> bool:
        """
        Check whether the repository is whitelisted

        Args:
            repo_full_name: Full repository name in 'owner/repo' format

        Returns:
            True if whitelisted, otherwise False
        """
        if not repo_full_name:
            return False

        # Normalize the repository name (lowercase)
        repo_full_name_lower = repo_full_name.lower()

        for whitelist_repo in self.whitelist_repos:
            if whitelist_repo.lower() == repo_full_name_lower:
                logger.info(f"Repository {repo_full_name} is whitelisted, skipping fake stats check")
                return True

        return False

    def detect(self, data: Dict) -> Tuple[bool, List[Dict]]:
        """
        Data-driven detection - extracts user information from the incoming data

        Args:
            data: Repository data dict, may contain core_developers and repo_name fields

        Returns:
            (is_abuse, evidences)
        """
        # Check whether the repository is whitelisted
        repo_name = data.get('full_name')
        if repo_name and self.is_whitelisted(repo_name):
            logger.debug(f"Repository {repo_name} is whitelisted, skipping detection")
            return False, []

        # Extract the core developer list from the data
        core_developers = data.get('core_developers', '')

        if not core_developers or core_developers == "NULL":
            logger.debug("No core developer information in the data, skipping detection")
            return False, []

        # Parse the developer list
        developers = [dev.strip() for dev in core_developers.split(',')]

        # Filter out bot users and invalid users
        developers = self._filter_valid_developers(developers)

        all_evidences = []
        abuse_detected = False

        for developer in developers:
            try:
                logger.debug(f"Checking developer: {developer}")
                is_abuse, evidences = self.detect_user_abuse(developer)

                if is_abuse:
                    abuse_detected = True
                    # Tag every piece of evidence with the developer
                    for evidence in evidences:
                        evidence['developer'] = developer
                    all_evidences.extend(evidences)

            except Exception as e:
                logger.error(f"Failed to check developer {developer}: {e}")
                continue

        return abuse_detected, all_evidences

    def add_to_whitelist(self, repo_full_name: str):
        """
        Dynamically add a repository to the whitelist

        Args:
            repo_full_name: Full repository name in 'owner/repo' format
        """
        self.whitelist_repos.add(repo_full_name.lower())
        logger.info(f"Repository {repo_full_name} has been added to the whitelist")

    def remove_from_whitelist(self, repo_full_name: str):
        """
        Remove a repository from the whitelist

        Args:
            repo_full_name: Full repository name in 'owner/repo' format
        """
        repo_lower = repo_full_name.lower()
        if repo_lower in self.whitelist_repos:
            self.whitelist_repos.remove(repo_lower)
            logger.info(f"Repository {repo_full_name} has been removed from the whitelist")

    def get_whitelist(self) -> set:
        """
        Get the current whitelist

        Returns:
            Set of whitelisted repositories
        """
        return self.whitelist_repos.copy()

    def _filter_valid_developers(self, developers: List[str]) -> List[str]:
        """
        Filter out bot users and invalid users

        Args:
            developers: List of developer usernames

        Returns:
            Filtered list of valid developers
        """
        filtered_developers = []

        # bot keyword patterns
        bot_patterns = [
            r'(?i)^bot[-_]?\d*$',  # bot, bot1, bot_2
            r'(?i)^.*bot$',  # xxxbot
            r'(?i)^github-?actions?$',  # github-actions, githubactions
            r'(?i)^dependabot.*$',  # dependabot
            r'(?i)^renovate.*$',  # renovate
            r'(?i)^snyk-?bot$',  # snyk-bot
            r'(?i)^codecov.*$',  # codecov
            r'(?i)^coveralls.*$',  # coveralls
            r'(?i)^greenkeeper.*$',  # greenkeeper
            r'(?i)^travis-?ci$',  # travis-ci
            r'(?i)^circle-?ci$',  # circle-ci
            r'(?i)^semantic-release.*$',  # semantic-release
            r'(?i)^pre-commit-?ci$',  # pre-commit-ci
            r'(?i)^cla-?assistant$',  # cla-assistant
            r'(?i)^imgbot$',  # imgbot
            r'(?i)^stale$',  # stale bot
            r'(?i)^lock-?bot$',  # lock-bot
            r'(?i)^welcome-?bot$',  # welcome-bot
            r'(?i)^issue-?label-?bot$',  # issue-label-bot
            r'(?i)^mergify$',  # mergify
            r'(?i)^kodiak$',  # kodiak
            r'(?i)^bors-?.*$',  # bors
        ]

        # User patterns: user_0, user_1, user_2, etc.
        user_number_pattern = re.compile(r'^user[-_]?\d+$', re.IGNORECASE)

        # Common test/placeholder users
        test_patterns = [
            r'(?i)^test.*$',  # test, test_user
            r'(?i)^demo.*$',  # demo, demo_user
            r'(?i)^example.*$',  # example
            r'(?i)^sample.*$',  # sample
            r'^placeholder$',  # placeholder
            r'^anonymous$',  # anonymous
            r'^unknown$',  # unknown
        ]

        for developer in developers:
            skip = False
            developer_lower = developer.lower()

            # Check whether it is a bot
            for pattern in bot_patterns:
                if re.match(pattern, developer_lower):
                    logger.debug(f"Filtered out bot user: {developer}")
                    skip = True
                    break

            if skip:
                continue

            # Check whether it matches the user_0, user_1 pattern
            if user_number_pattern.match(developer_lower):
                logger.debug(f"Filtered out user_N style user: {developer}")
                continue

            # Check whether it is a test/placeholder user
            for pattern in test_patterns:
                if re.match(pattern, developer_lower):
                    logger.debug(f"Filtered out test/placeholder user: {developer}")
                    skip = True
                    break

            if skip:
                continue

            filtered_developers.append(developer)

        logger.info(f"Original developer count: {len(developers)}, after filtering: {len(filtered_developers)}")
        return filtered_developers

    def detect_user_abuse(self, username: str) -> Tuple[bool, List[Dict]]:
        """
        Detect a user's false claims and abusive behavior

        Args:
            username: GitHub username

        Returns:
            (is_abuse, evidences)
        """
        evidences = []

        try:
            # Look for the profile repository first; return immediately if there is none
            profile_repo = self._find_profile_repository(username)
            if not profile_repo:
                logger.debug(f"No profile repository found for user {username}, skipping detection")
                return False, []

            # Get the README content
            readme_content = self._get_readme_content(profile_repo['full_name'])
            if not readme_content:
                logger.debug(f"Profile repository of user {username} has no README, skipping detection")
                return False, []

            # 1. Detect fake star claims
            star_evidence = self._detect_fake_stars(username, readme_content)
            if star_evidence:
                evidences.extend(star_evidence)

            # 2. Detect use of other people's stats (enhanced)
            others_stats_evidence = self._detect_others_stats(username, readme_content)
            if others_stats_evidence:
                evidences.extend(others_stats_evidence)

            return len(evidences) > 0, evidences

        except Exception as e:
            logger.error(f"Failed to check user {username}: {e}")
            return False, []

    def _detect_fake_stars(self, username: str, readme_content: str) -> List[Dict]:
        """Detect fake star claims"""
        evidences = []

        # Extract every possible star count claim
        star_claims = self._extract_user_star_numbers(readme_content, username)

        if star_claims:
            logger.debug(f"User {username} claims a star count in the README, starting verification")

            # Get the user's actual stats
            actual_stars, _ = self._get_user_actual_stats(username)

            # Find the largest of all claimed star counts
            max_claimed = max([count for _, count in star_claims])
            discrepancy_threshold = self.config.get('star_discrepancy_threshold', 5)

            # Check for an obvious fake
            if max_claimed > actual_stars * discrepancy_threshold:
                evidence = {
                    'type': 'fake_user_stars',
                    'claimed_stars': max_claimed,
                    'actual_stars': actual_stars,
                    'discrepancy_ratio': round(max_claimed / max(actual_stars, 1), 2),
                    'details': f"Claims {max_claimed:,} stars, but actually has only {actual_stars:,}",
                    'all_claims': [{'source': source, 'count': count}
                                   for source, count in star_claims[:5]]
                }
                evidences.append(evidence)
                logger.info(f"Fake star claim detected for user {username}: claimed {max_claimed:,} vs actual {actual_stars:,}")
            else:
                logger.debug(f"Star claim of user {username} is reasonable: claimed {max_claimed:,} vs actual {actual_stars:,}")

        return evidences

    def _detect_others_stats(self, username: str, readme_content: str) -> List[Dict]:
        """Detect whether other people's stats are being used (enhanced)"""
        evidences = []

        # Detect github-readme-stats usage
        stats_urls, username_mappings = self._extract_stats_urls_and_users(readme_content)

        if not username_mappings:
            return evidences

        # Detect whether someone else's stats are used
        others_stats_detected = False
        other_users = []
        same_person_users = []
        legitimate_references = []
        empty_profile_references = []

        # Used for deduplication
        seen_users = set()
        seen_same_person = set()
        seen_legitimate = set()
        seen_empty = set()

        # Cache profile evaluations of referenced users to avoid repeated calls
        profile_eval_cache = {}

        for url, stats_username in username_mappings:
            if stats_username.lower() != username.lower():
                # Verify that this is a stats card URL
                if self._is_stats_card_url(url):
                    # Check whether it is the same person - use a cache key
                    cache_key_same = f"same_person_{username}_{stats_username}"

                    # Check the cache first
                    if cache_key_same in self._username_cache:
                        is_same_person, same_person_evidence = self._username_cache[cache_key_same]
                    else:
                        is_same_person, same_person_evidence = self._check_same_person(
                            username, stats_username, readme_content
                        )
                        self._username_cache[cache_key_same] = (is_same_person, same_person_evidence)

                    if is_same_person:
                        if stats_username.lower() not in seen_same_person:
                            same_person_users.append({
                                'username': stats_username,
                                'evidence': same_person_evidence
                            })
                            seen_same_person.add(stats_username.lower())
                            logger.info(f"Detected an account that may belong to the same person: {stats_username}")
                    else:
                        # If not the same person, evaluate the referenced user's profile value - using the cache
                        cache_key_value = f"profile_value_{stats_username}_{username}"

                        if cache_key_value in profile_eval_cache:
                            profile_value, profile_details = profile_eval_cache[cache_key_value]
                        else:
                            profile_value, profile_details = self._evaluate_user_profile_value(
                                stats_username, username, readme_content
                            )
                            profile_eval_cache[cache_key_value] = (profile_value, profile_details)

                        if profile_value == 'legitimate':
                            if stats_username.lower() not in seen_legitimate:
                                legitimate_references.append({
                                    'username': stats_username,
                                    'details': profile_details
                                })
                                seen_legitimate.add(stats_username.lower())
                                logger.debug(f"Legitimate reference detected: {stats_username}")

                        elif profile_value == 'empty_or_no_profile':
                            if stats_username.lower() not in seen_empty:
                                empty_profile_references.append({
                                    'username': stats_username,
                                    'details': profile_details
                                })
                                seen_empty.add(stats_username.lower())
                                logger.debug(f"Possible reference mistake detected: {stats_username}")
                        else:  # 'abuse'
                            if stats_username.lower() not in seen_users:
                                others_stats_detected = True
                                other_users.append({
                                    'username': stats_username,
                                    'details': profile_details
                                })
                                seen_users.add(stats_username.lower())
                                logger.info(f"Possible abuse of another person's stats card detected: {stats_username}")

        # Add evidence for accounts that may belong to the same person
        if same_person_users:
            evidence = {
                'type': 'possible_same_person_accounts',
                'username': username,
                'related_accounts': same_person_users,
                'details': f"Found other accounts that may belong to the same person as the current user: {', '.join([u['username'] for u in same_person_users])}"
            }
            evidences.append(evidence)

        # If abuse really does exist
        if others_stats_detected:
            evidence = {
                'type': 'others_stats_abuse',
                'username': username,
                'stats_users_used': other_users,
                'details': f"Abused another person's github-readme-stats data: {', '.join([u['username'] for u in other_users])}"
            }
            evidences.append(evidence)

        return evidences

    def _evaluate_user_profile_value(self, target_username: str, source_username: str, source_readme: str) -> Tuple[
        str, str]:
        """
        Evaluate the profile value of the target user

        Args:
            target_username: The referenced username
            source_username: Source username (the user currently being checked)
            source_readme: README content of the source user

        Returns:
            (evaluation result, details)
            - 'legitimate': legitimate reference (mutual mention exists)
            - 'empty_or_no_profile': profile is empty or does not exist (possibly a reference mistake)
            - 'abuse': abuse (the other user has a valuable profile but there is no mutual mention)
        """
        try:
            # 1. Find the target user's profile repository
            target_profile_repo = self._find_profile_repository(target_username)

            if not target_profile_repo:
                return 'empty_or_no_profile', f"User {target_username} has no profile repository"

            # 2. Get the target user's README content
            target_readme = self._get_readme_content(target_profile_repo['full_name'])

            if not target_readme:
                return 'empty_or_no_profile', f"Profile repository of user {target_username} has no README"

            # 3. Check for mutual mentions
            has_mutual_mention = self._check_mutual_mention(
                source_username, target_username, source_readme, target_readme
            )

            if has_mutual_mention:
                return 'legitimate', f"User {target_username} mentions {source_username} in the README"

            # 4. Get the target user's total star count
            target_stars, _ = self._get_user_actual_stats(target_username)
            if target_stars == 0:
                return 'empty_or_no_profile', f"Total star count of user {target_username} is 0"

            # 5. Assess whether the target user's profile is valuable (non-empty with substantial content)
            profile_value_score = self._assess_profile_content_value(target_username, target_readme)

            if profile_value_score < 0.3:
                return 'empty_or_no_profile', f"Profile of user {target_username} has little content (score: {profile_value_score:.2f})"
            else:
                return 'abuse', f"User {target_username} has a valuable profile (score: {profile_value_score:.2f}) but there is no mutual mention"

        except Exception as e:
            logger.error(f"Failed to evaluate profile value of user {target_username}: {e}")
            return 'empty_or_no_profile', f"Error during evaluation: {str(e)}"

    def _check_mutual_mention(self, user_a: str, user_b: str, readme_a: str, readme_b: str) -> bool:
        """Check whether two users mention each other in their READMEs"""
        try:
            b_in_a = self._is_user_mentioned_in_readme(user_b, readme_a)
            a_in_b = self._is_user_mentioned_in_readme(user_a, readme_b)
            return b_in_a and a_in_b
        except Exception as e:
            logger.error(f"Failed to check mutual mention: {e}")
            return False

    def _is_user_mentioned_in_readme(self, username: str, readme_content: str) -> bool:
        """Check whether the username is mentioned in the README content"""
        if not readme_content:
            return False

        content_lower = readme_content.lower()
        username_lower = username.lower()

        mention_patterns = [
            rf'@{username_lower}\b',
            rf'github\.com/{username_lower}\b',
            rf'\b{username_lower}\b',
            rf'contributors?.*{username_lower}',
            rf'thanks?.*{username_lower}',
            rf'credit.*{username_lower}',
            rf'collaborator.*{username_lower}',
        ]

        for pattern in mention_patterns:
            if re.search(pattern, content_lower, re.IGNORECASE):
                return True

        return False

    def _assess_profile_content_value(self, username: str, readme_content: str) -> float:
        """Assess the content value of a profile README (score 0-1)"""
        if not readme_content or len(readme_content.strip()) < 50:
            return 0.0

        score = 0.0
        content_lower = readme_content.lower()

        # 1. Get the user's actual total star count
        actual_stars, _ = self._get_user_actual_stats(username)

        if actual_stars == 0:
            star_score = 0.0
        else:
            star_score = min(actual_stars / 1000, 0.2)
        score += star_score

        # 2. Content length assessment
        length_score = min(len(readme_content) / 2000, 0.25)
        score += length_score

        # 3. Project/work related keywords
        project_indicators = [
            'project', 'work', 'job', 'developer', 'engineer',
            'code', 'programming', 'build', 'create', 'developed',
            'portfolio', 'resume', 'experience', 'skills'
        ]
        project_count = sum(1 for word in project_indicators if word in content_lower)
        project_score = min(project_count / 10, 0.15)
        score += project_score

        # 4. Tech stack/tooling related
        tech_indicators = [
            'python', 'javascript', 'java', 'react', 'node',
            'docker', 'kubernetes', 'aws', 'azure', 'gcp',
            'frontend', 'backend', 'fullstack', 'devops',
            'machine learning', 'ai', 'data science', 'database',
            'golang', 'rust', 'c++', 'typescript', 'vue', 'angular'
        ]
        tech_count = sum(1 for word in tech_indicators if word in content_lower)
        tech_score = min(tech_count / 15, 0.15)
        score += tech_score

        # 5. Links/resources
        link_indicators = [
            'http://', 'https://', 'linkedin', 'twitter',
            'blog', 'website', 'portfolio', 'email',
            'medium', 'dev.to', 'stackoverflow'
        ]
        link_count = sum(1 for word in link_indicators if word in content_lower)
        link_score = min(link_count / 5, 0.15)
        score += link_score

        # 6. Structured content
        structure_indicators = [
            '|', '- [', '* [', '1. ', '- ', '###', '##',
            '---', '***'
        ]
        structure_count = sum(1 for char in structure_indicators if char in readme_content)
        structure_score = min(structure_count / 10, 0.1)
        score += structure_score

        logger.debug(f"Profile score of user {username}: {score:.2f} (stars: {actual_stars})")
        return min(score, 1.0)

    def _check_same_person(self, username: str, extracted_username: str, readme_content: str) -> Tuple[bool, str]:
        """Check whether the extracted username belongs to the same person as the current user"""
        if username.lower() == extracted_username.lower():
            return True, "Usernames match exactly"

        # 1. Display name check
        display_name_match, display_evidence = self._check_display_name_match(username, extracted_username,
                                                                              readme_content)
        if display_name_match:
            return True, f"Display name match: {display_evidence}"

        # 2. Email association check
        email_match, email_evidence = self._check_email_association(username, extracted_username)
        if email_match:
            return True, f"Email association: {email_evidence}"

        return False, "No association found"

    def _check_display_name_match(self, username: str, extracted_username: str, readme_content: str) -> Tuple[
        bool, str]:
        """Check whether the extracted username is the display name of the current user"""
        try:
            user_url = f"https://api.github.com/users/{username}"
            user_data = self._make_api_call(user_url)

            if not user_data:
                return False, ""

            display_name = user_data.get('name', '')
            if not display_name:
                return False, ""

            display_name_normalized = re.sub(r'[^a-zA-Z0-9]', '', display_name.lower())
            extracted_normalized = re.sub(r'[^a-zA-Z0-9]', '', extracted_username.lower())

            if display_name_normalized == extracted_normalized:
                return True, f"{extracted_username} is the display name of {username}"
            elif extracted_username.lower() in display_name.lower():
                return True, f"{extracted_username} is contained in the display name {display_name}"
            elif display_name.lower() in extracted_username.lower():
                return True, f"Display name {display_name} is contained in {extracted_username}"

            # Check whether the README declares a display name
            name_patterns = [
                r'(?:my|i am|i\'m)\s+name\s+is\s+([^\.!\n]+)',
                r'(?:name|display name|full name)[:\s]+([^\.!\n]+)',
                r'👋\s*[Hh]i,\s*(?:I\'m|I am)\s+([^\.!\n]+)',
            ]

            for pattern in name_patterns:
                match = re.search(pattern, readme_content, re.IGNORECASE)
                if match:
                    readme_name = match.group(1).strip()
                    readme_name_normalized = re.sub(r'[^a-zA-Z0-9]', '', readme_name.lower())
                    if readme_name_normalized == extracted_normalized:
                        return True, f"Name {readme_name} declared in the README matches {extracted_username}"

            return False, ""

        except Exception as e:
            logger.error(f"Failed to check display name match: {e}")
            return False, ""

    def _check_email_association(self, username: str, extracted_username: str) -> Tuple[bool, str]:
        """Check whether the two accounts use the same email"""
        try:
            def get_user_emails(username):
                emails = set()

                user_url = f"https://api.github.com/users/{username}"
                user_data = self._make_api_call(user_url)
                if user_data and user_data.get('email'):
                    emails.add(user_data['email'])

                events_url = f"https://api.github.com/users/{username}/events/public"
                events_data = self._make_api_call(events_url, {'per_page': 30})

                if events_data:
                    for event in events_data:
                        if event.get('type') == 'PushEvent':
                            payload = event.get('payload', {})
                            commits = payload.get('commits', [])
                            for commit in commits:
                                author = commit.get('author', {})
                                if author.get('email'):
                                    emails.add(author['email'])

                return list(emails)

            user_emails = get_user_emails(username)
            extracted_user_emails = get_user_emails(extracted_username)

            common_emails = set(user_emails) & set(extracted_user_emails)

            if common_emails:
                common_email = list(common_emails)[0]
                logger.info(f"Found a shared email: {common_email}")
                return True, f"Both accounts use the same email {common_email}"

            return False, ""

        except Exception as e:
            logger.error(f"Failed to check email association: {e}")
            return False, ""

    def _extract_user_star_numbers(self, text: str, username: str = None) -> List[Tuple[str, int]]:
        """Extract user star count claims from text"""
        found_stars = []

        text_stars = self._extract_stars_from_text(text)
        found_stars.extend(text_stars)

        if username:
            badge_stars = self._extract_personal_stars_from_badges(text, username)
            found_stars.extend(badge_stars)

        stats_stars = self._extract_stars_from_stats_cards(text)
        found_stars.extend(stats_stars)

        project_stars = self._extract_stars_from_project_list(text)
        found_stars.extend(project_stars)

        unique_stars = []
        seen_counts = set()
        seen_texts = set()

        for source_text, count in found_stars:
            if count not in seen_counts and source_text not in seen_texts:
                unique_stars.append((source_text, count))
                seen_counts.add(count)
                seen_texts.add(source_text)

        unique_stars.sort(key=lambda x: x[1], reverse=True)

        if unique_stars:
            logger.debug(f"Extracted star claims: {[(text, count) for text, count in unique_stars]}")

        return unique_stars

    def _extract_stars_from_text(self, text: str) -> List[Tuple[str, int]]:
        """Extract star count claims from text"""
        found_stars = []

        star_context_patterns = [
            r'(?:^|\s)(?:my|total|github)\s+(?:stars?|⭐)\s*[:\-]?\s*(\d+[,.]?\d*[kKmM]?)(?:\s|$|\.)',
            r'(?:^|\s)(\d+[,.]?\d*[kKmM]?)\s+(?:stars?|⭐)\s+(?:on|across|in)\s+github(?:\s|$|\.)',
            r'(?:^|\s)github\s+stars?\s*[:\-]?\s*(\d+[,.]?\d*[kKmM]?)(?:\s|$|\.)',
            r'(?:^|\s)total\s+stars?\s*[:\-]?\s*(\d+[,.]?\d*[kKmM]?)(?:\s|$|\.)',
            r'(?:^|\s)⭐\s*(\d+[,.]?\d*[kKmM]?)\+?\s*(?:stars?)?(?:\s|$|\.)',
            r'(?:^|\s)🌟\s*(\d+[,.]?\d*[kKmM]?)\+?\s*(?:stars?)?(?:\s|$|\.)',
            r'(?:^|\s)i\s+have\s+(\d+[,.]?\d*[kKmM]?)\s+stars?(?:\s|$|\.)',
            r'i\'ve\s+got\s+(\d+[,.]?\d*[kKmM]?)\s+stars?',
            r'i\s+got\s+(\d+[,.]?\d*[kKmM]?)\s+stars?',
            r'with\s+(\d+[,.]?\d*[kKmM]?)\s+stars?',
            r'received\s+(\d+[,.]?\d*[kKmM]?)\s+stars?',
            r'accumulated\s+(\d+[,.]?\d*[kKmM]?)\s+stars?',
        ]

        for pattern in star_context_patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE)
            for match in matches:
                star_text = match.group(1)
                if self._is_likely_not_star_count(star_text, match.group(0)):
                    continue
                star_count = self._parse_star_count(star_text)
                if star_count >= self.config.get('min_fake_stars', 100) and star_count < 10000000:
                    found_stars.append((f"text:{star_text}", star_count))

        return found_stars

    def _extract_personal_stars_from_badges(self, text: str, username: str) -> List[Tuple[str, int]]:
        """Extract the author's total star count from badges on the profile page"""
        found_stars = []

        badge_patterns = [
            r'!\[[^\]]*\]\(https?://img\.shields\.io/github/stars/([^/?]+)(?:\?[^\)]*)?\)',
            r'https?://img\.shields\.io/github/stars/([^/?]+)(?:\?[^\)\s]*)',
            r'github/stars/([^/?\s"\')]+)',
            r'\[!\[[^\]]*\]\([^\)]+\)\]\(https?://img\.shields\.io/github/stars/([^/?]+)',
            r'<img[^>]+src=["\']https?://img\.shields\.io/github/stars/([^/?]+)',
            r'https?://badgen\.net/github/stars/([^/?]+)',
            r'https?://github-readme-stats\.vercel\.app/api/?\?username=([^&]+)',
        ]

        for pattern in badge_patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for match in matches:
                badge_username = match.group(1)
                if badge_username.lower() == username.lower():
                    stars = self._get_user_total_stars_from_api(username)
                    if stars and stars > 0:
                        found_stars.append((f"badge:{badge_username}", stars))
                        logger.debug(f"Extracted total star count of user {username} from badge: {stars}")

        return found_stars

    def _extract_stars_from_stats_cards(self, text: str) -> List[Tuple[str, int]]:
        """Extract star counts from stats cards"""
        found_stars = []

        stats_patterns = [
            r'(?:total|all-time|累计)\s+stars?[:\s]*(\d+[,.]?\d*[kKmM]?)',
            r'stars?[:\s]+(\d+[,.]?\d*[kKmM]?)(?:\s|$)',
            r'\|[^|]*stars?[^|]*\|[^|]*(\d+[,.]?\d*[kKmM]?)[^|]*\|',
            r'(\d+[,.]?\d*[kKmM]?)\s+(?:total\s+)?stars?',
        ]

        for pattern in stats_patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE)
            for match in matches:
                star_text = match.group(1)
                if self._is_in_code_block(text, match.start()):
                    continue
                star_count = self._parse_star_count(star_text)
                if star_count >= self.config.get('min_fake_stars', 100):
                    context = text[max(0, match.start() - 30):min(len(text), match.end() + 30)]
                    if 'total' in context.lower() or 'stars' in context.lower():
                        found_stars.append((f"stats:{star_text}", star_count))
                        logger.debug(f"Extracted star count from stats card: {star_text} -> {star_count}")

        return found_stars

    def _extract_stars_from_project_list(self, text: str) -> List[Tuple[str, int]]:
        """Extract star counts from a project list"""
        found_stars = []

        project_patterns = [
            r'([A-Za-z\-]+(?:\s+[A-Za-z\-]+)*)\s+[🟢🔴🟣🟡]\s*(\d+\.?\d*[kKmM]?)\s+[⬇️⬆️]\s*\d+\.?\d*[kKmM]?',
            r'([A-Za-z\-]+(?:\s+[A-Za-z\-]+)*)\s+[🟢🔴🟣🟡]\s*(\d+\.?\d*[kKmM]?)(?:\s|$)',
            r'([A-Za-z\-]+(?:\s+[A-Za-z\-]+)*)\s+⭐\s*(\d+\.?\d*[kKmM]?)',
        ]

        for pattern in project_patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE)
            for match in matches:
                star_text = match.group(2)
                if self._is_in_code_block(text, match.start()):
                    continue
                star_count = self._parse_star_count(star_text)
                if star_count >= self.config.get('min_fake_stars', 100):
                    found_stars.append((f"project:{star_text}", star_count))
                    logger.debug(f"Extracted star count from project list: {star_text} -> {star_count}")

        return found_stars

    def _get_user_total_stars_from_api(self, username: str) -> Optional[int]:
        """Get the user's total star count through the API (cached version)"""
        cache_key = f"stats_{username}"
        if cache_key in self._stats_cache:
            cached_time, cached_data = self._stats_cache[cache_key]
            if time.time() - cached_time < 300:
                return cached_data[0]

        try:
            total_stars = self._calculate_user_total_stars(username)
            return total_stars
        except Exception as e:
            logger.error(f"Failed to get total star count of user {username}: {e}")
            return None

    def _is_in_code_block(self, text: str, position: int) -> bool:
        """Determine whether the given position is inside a code block"""
        before_text = text[:position]
        backticks = before_text.count('```')
        return backticks % 2 == 1

    def _is_likely_not_star_count(self, number_text: str, full_match: str) -> bool:
        """Determine whether the extracted number is likely not a star count"""
        number_text = number_text.lower()

        if number_text.isdigit() and len(number_text) == 4:
            year = int(number_text)
            if 1900 <= year <= 2099:
                year_context = ['copyright', '©', 'update', 'updated', 'since', 'as of', '©']
                if any(word in full_match.lower() for word in year_context):
                    return True
                if len(full_match.strip()) <= 10:
                    return True

        if '.' in number_text and number_text.replace('.', '').isdigit():
            version_parts = number_text.split('.')
            if len(version_parts) <= 3 and all(len(part) <= 3 for part in version_parts):
                return True

        common_non_star = ['2020', '2021', '2022', '2023', '2024', '2025', '2026']
        if number_text in common_non_star:
            return True

        try:
            num = float(number_text.replace('k', '').replace('m', ''))
            if num < 10 and 'star' not in full_match.lower():
                return True
        except:
            pass

        return False

    def _find_profile_repository(self, username: str) -> Optional[Dict]:
        """Find the user's profile repository - with cache"""
        if username in self._profile_repo_cache:
            return self._profile_repo_cache[username]

        try:
            username_repo_url = f"https://api.github.com/repos/{username}/{username}"
            username_repo = self._make_api_call(username_repo_url)

            if username_repo and username_repo.get('id'):
                logger.debug(f"Found repository named after the user: {username}/{username}")
                result = {
                    "full_name": username_repo['full_name'],
                    "name": username_repo['name'],
                    "html_url": username_repo['html_url']
                }
                self._profile_repo_cache[username] = result
                return result

            logger.debug(f"User {username} has no repository with the same name, searching for repositories matching keywords")

            repos_url = f"https://api.github.com/users/{username}/repos"
            repos_data = self._make_api_call(repos_url, {
                'per_page': self.config.get('max_profile_repos_to_check', 5),
                'sort': 'updated',
                'direction': 'desc'
            })

            if not repos_data:
                self._profile_repo_cache[username] = None
                return None

            for repo in repos_data:
                repo_name = repo['name'].lower()
                for keyword in self.profile_repo_keywords:
                    if keyword in repo_name:
                        logger.debug(f"Found repository matching a keyword: {repo['full_name']}")
                        result = {
                            "full_name": repo['full_name'],
                            "name": repo['name'],
                            "html_url": repo['html_url']
                        }
                        self._profile_repo_cache[username] = result
                        return result

            logger.debug(f"No profile repository found for user {username}")
            self._profile_repo_cache[username] = None
            return None

        except Exception as e:
            logger.error(f"Failed to find profile repository: {e}")
            self._profile_repo_cache[username] = None
            return None

    def _remove_comments(self, content: str) -> str:
        """Remove HTML/XML style comments <!-- -->"""
        return re.sub(r'<!--.*?-->', '', content, flags=re.DOTALL)

    def _get_readme_content(self, repo_full_name: str) -> Optional[str]:
        """Get the README content of a repository (enhanced - comments removed)"""
        try:
            readme_url = f"https://api.github.com/repos/{repo_full_name}/readme"
            readme_data = self._make_api_call(readme_url)

            if not readme_data or 'content' not in readme_data:
                return None

            content = base64.b64decode(readme_data['content']).decode('utf-8', errors='ignore')
            content = self._remove_comments(content)
            return content

        except Exception as e:
            logger.error(f"Failed to get README content: {e}")
            return None

    def _get_user_actual_stats(self, username: str) -> Tuple[int, int]:
        """Get the user's actual stats"""
        cache_key = f"stats_{username}"
        if cache_key in self._stats_cache:
            cached_time, cached_data = self._stats_cache[cache_key]
            if time.time() - cached_time < 300:
                logger.debug(f"Using cached data for {username}")
                return cached_data

        try:
            user_url = f"https://api.github.com/users/{username}"
            user_data = self._make_api_call(user_url)

            if not user_data:
                return 0, 0

            public_repos = user_data.get('public_repos', 0)
            total_stars = self._calculate_user_total_stars(username)

            result = (total_stars, public_repos)
            self._stats_cache[cache_key] = (time.time(), result)
            logger.debug(f"Actual stats of user {username}: {total_stars} stars, {public_repos} repos")

            return result

        except Exception as e:
            logger.error(f"Failed to get user stats: {e}")
            return 0, 0

    def _calculate_user_total_stars(self, username: str) -> int:
        """Calculate the total star count across all of a user's repositories"""
        total_stars = 0
        page = 1
        per_page = 100
        max_pages = self.config.get('max_repo_pages', 10)

        logger.debug(f"Start calculating the total repository star count of user {username}")

        while page <= max_pages:
            repos_url = f"https://api.github.com/users/{username}/repos"
            repos_data = self._make_api_call(repos_url, {
                'per_page': per_page,
                'page': page,
                'sort': 'pushed',
                'direction': 'desc'
            })

            if not repos_data:
                break

            page_stars = sum(repo.get('stargazers_count', 0) for repo in repos_data)
            total_stars += page_stars

            logger.debug(f"Page {page}: got {len(repos_data)} repositories, stars on this page: {page_stars}, cumulative: {total_stars}")

            if len(repos_data) < per_page:
                break

            page += 1
            time.sleep(self.config.get('rate_limit_delay', 0.5))

        logger.debug(f"Total star count of user {username} computed: {total_stars}")
        return total_stars

    def _parse_star_count(self, star_text: str) -> int:
        """Parse star text into a number"""
        star_text = star_text.lower().replace(',', '').strip()

        if len(star_text) > 15:
            return 0

        multiplier = 1
        if 'k' in star_text:
            multiplier = 1000
            star_text = star_text.replace('k', '')
        elif 'm' in star_text:
            multiplier = 1000000
            star_text = star_text.replace('m', '')

        try:
            number = float(star_text)
            if number * multiplier > 10000000:
                return 0
            return int(number * multiplier)
        except ValueError:
            return 0

    def _extract_stats_urls_and_users(self, text: str) -> Tuple[List[str], List[Tuple[str, str]]]:
        """Extract github-readme-stats URLs and the usernames they contain"""
        stats_urls = []
        username_mappings = []

        enhanced_patterns = [
            r'!\[.*?\]\((https?://github-readme-stats\.vercel\.app[^\)]+)\)',
            r'!\[.*?\]\((https?://github-readme-stats\.git\.app[^\)]+)\)',
            r'!\[.*?\]\((https?://api\.github-readme-stats\.vercel\.app[^\)]+)\)',
            r'(https?://github-readme-stats\.vercel\.app[^\s"\'<>]+)',
            r'(https?://github-readme-stats\.git\.app[^\s"\'<>]+)',
            r'(https?://api\.github-readme-stats\.vercel\.app[^\s"\'<>]+)',
            r'<img[^>]+src=["\'](https?://github-readme-stats\.vercel\.app[^"\']+)["\'][^>]*>',
            r'github-readme-stats\.vercel\.app/api\?[^\s"\'<>]+',
            r'git-stats-readme\.vercel\.app\?[^\s"\'<>]+',
        ]

        for pattern in enhanced_patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for match in matches:
                url = match.group(1) if len(match.groups()) > 0 else match.group(0)

                if not url.startswith(('http://', 'https://')):
                    url = 'https://' + url

                stats_urls.append(url)

                username = self._extract_username_from_stats_url(url)
                if username:
                    username_mappings.append((url, username))

        stats_urls = list(set(stats_urls))
        username_mappings = list(set(username_mappings))

        return stats_urls, username_mappings

    def _extract_username_from_stats_url(self, url: str) -> Optional[str]:
        """Extract the username from a github-readme-stats URL"""
        try:
            clean_url = url.split(')')[0] if ')' in url else url
            clean_url = clean_url.split(']')[0] if ']' in clean_url else clean_url

            parsed_url = urllib.parse.urlparse(clean_url if '://' in clean_url else f'https://{clean_url}')
            query_params = urllib.parse.parse_qs(parsed_url.query)

            for param in ['username', 'user', 'login']:
                if param in query_params:
                    username = query_params[param][0]
                    username = username.split(')')[0] if ')' in username else username
                    username = username.split(']')[0] if ']' in username else username
                    return username

            return None

        except Exception as e:
            logger.error(f"Failed to parse URL {url}: {e}")
            return None

    def _is_stats_card_url(self, url: str) -> bool:
        """Determine whether the URL is a stats card"""
        url_lower = url.lower()
        for indicator in self.stats_card_indicators:
            if indicator in url_lower:
                return True
        return False

    def _make_api_call(self, url: str, params: Dict = None) -> Optional[Dict]:
        """Safe API call"""
        max_retries = self.config.get('max_retries', 3)
        request_timeout = self.config.get('request_timeout', 30)
        rate_limit_delay = self.config.get('rate_limit_delay', 1.0)

        for attempt in range(max_retries):
            try:
                response = self.session.get(url, params=params, timeout=request_timeout)

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
                    logger.error(f"API call failed: {response.status_code} - {url}")

            except requests.exceptions.RequestException as e:
                logger.error(f"Request exception: {e}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)

        return None

    def clear_cache(self):
        """Clear all caches"""
        self._stats_cache.clear()
        self._username_cache.clear()
        self._profile_repo_cache.clear()
        logger.info("All caches cleared")