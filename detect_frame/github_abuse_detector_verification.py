"""
Verification script
Reads a CSV file and repository JSON data, and runs detection verification for the abuse type specified for each repository
Outputs a CSV file containing detect_label
"""

import json
import csv
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
import sys

# Add the parent directory to the path so the detectors can be imported
sys.path.insert(0, str(Path(__file__).parent.parent))

from github_abuse_detector import (
    AbuseDetectorFactory,
    FakeStarsDetector,
    AutomaticUpdatesDetector,
    TypoSquattingDetector,
    ReputationFarmingDetector,
    FakeStatsDetector,
    SpoofedContributorDetector,
    IssueSpamDetector,
    KeywordStuffingDetector
)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Mapping from category to detector class
CATEGORY_TO_DETECTOR = {
    "automatic_updates": AutomaticUpdatesDetector,
    "fake_stars": FakeStarsDetector,
    "fake_stats": FakeStatsDetector,
    "issue_spam": IssueSpamDetector,
    "keyword_stuffing": KeywordStuffingDetector,
    "reputation_farming": ReputationFarmingDetector,
    "spoofed_contributor": SpoofedContributorDetector,
    "typo_squatting": TypoSquattingDetector,
}


class VerificationPipeline:
    """Verification pipeline: runs detection verification for a specific abuse type"""

    def __init__(self, config_file: str = "config.json"):
        """
        Initialize the verification pipeline

        Args:
            config_file: Path to the config file
        """
        self.detector_factory = AbuseDetectorFactory(config_file)
        self.detectors = {}  # Cache of detector instances

        # Try to resolve the config file path
        script_dir = Path(__file__).parent
        config_path = script_dir / config_file
        if not config_path.exists():
            # Try the parent directory
            config_path = script_dir.parent / config_file
            if not config_path.exists():
                logger.warning(f"Config file {config_file} does not exist, the default config will be used")

        logger.info("Verification pipeline initialized")

    def _get_detector(self, category: str):
        """Get or create a detector"""
        if category not in self.detectors:
            self.detectors[category] = self.detector_factory.get_detector(category)
        return self.detectors[category]

    def _load_repos_json(self, json_path: str) -> Dict[str, Dict]:
        """
        Load repository JSON data and build a mapping from URL to repository data

        Args:
            json_path: Path to the JSON file

        Returns:
            Dict mapping URL to repository data
        """
        logger.info(f"Loading repository JSON data: {json_path}")

        with open(json_path, 'r', encoding='utf-8') as f:
            repos = json.load(f)

        url_to_repo = {}
        for repo in repos:
            html_url = repo.get("html_url", "")
            if html_url:
                # Normalize the URL (remove the trailing slash)
                normalized_url = html_url.rstrip('/')
                url_to_repo[normalized_url] = repo

                # Also store full_name as an alternative key
                full_name = repo.get("full_name", "")
                if full_name:
                    url_to_repo[full_name] = repo

        logger.info(f"Loaded {len(url_to_repo)} repository records")
        return url_to_repo

    def _load_csv(self, csv_path: str) -> List[Dict]:
        """
        Load the CSV file

        Args:
            csv_path: Path to the CSV file

        Returns:
            List of CSV row data
        """
        rows = []
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)

        logger.info(f"Loaded {len(rows)} CSV records")
        return rows

    def _normalize_url(self, url: str) -> str:
        """Normalize a URL for matching"""
        if not url:
            return ""
        return url.rstrip('/')

    def _find_repo_data(self, url_to_repo: Dict, repo_name: str, url: str) -> Optional[Dict]:
        """
        Look up repository data by repository name or URL

        Args:
            url_to_repo: Mapping from URL to repository data
            repo_name: Repository name (format: owner/repo)
            url: Repository URL

        Returns:
            Repository data, or None
        """
        # Try matching by URL first
        normalized_url = self._normalize_url(url)
        if normalized_url in url_to_repo:
            return url_to_repo[normalized_url]

        # Try matching by repository name
        if repo_name in url_to_repo:
            return url_to_repo[repo_name]

        # Try extracting the full name from the URL and matching that
        if "github.com/" in url:
            parts = url.split("github.com/")
            if len(parts) > 1:
                full_name = parts[1].rstrip('/')
                if full_name in url_to_repo:
                    return url_to_repo[full_name]

        return None

    def verify_repository(self, repo_data: Dict, category: str) -> bool:
        """
        Run the detection of a specific category on a single repository

        Args:
            repo_data: Repository JSON data
            category: Detection category

        Returns:
            Whether the detection result is abuse (True means abuse was detected)
        """
        # Get the detector
        detector = self._get_detector(category)
        if not detector:
            logger.warning(f"No detector found for category {category}")
            return False

        try:
            # Run detection
            detection_result = detector.detect(repo_data)

            # Log the details
            if detection_result.is_abuse:
                logger.debug(f"Abuse detected: {category}, details: {detection_result.details}")

            return detection_result.is_abuse

        except Exception as e:
            logger.error(f"Error while detecting category {category}: {e}")
            return False

    def run(self, csv_path: str, json_path: str, output_path: str = None):
        """
        Run the verification pipeline

        Args:
            csv_path: Path to the input CSV file
            json_path: Path to the repository JSON data file
            output_path: Path to the output CSV file (generated in the same directory by default)
        """
        # Determine the output path
        if output_path is None:
            csv_file = Path(csv_path)
            output_path = csv_file.parent / f"{csv_file.stem}_output{csv_file.suffix}"

        # Load data
        url_to_repo = self._load_repos_json(json_path)
        csv_rows = self._load_csv(csv_path)

        # Process each row
        results = []
        success_count = 0
        fail_count = 0
        not_found_count = 0

        for idx, row in enumerate(csv_rows, 1):
            sub_category = row.get("sub_category", "")
            repo_name = row.get("repo/account_name", "")
            url = row.get("URL", "")
            m_label = row.get("m_label", "")

            logger.info(f"Processing [{idx}/{len(csv_rows)}]: {repo_name} - {sub_category}")

            # Look up the repository data
            repo_data = self._find_repo_data(url_to_repo, repo_name, url)

            if repo_data is None:
                logger.warning(f"Repository data not found: {repo_name} ({url})")
                detect_label = "Not_Found"
                not_found_count += 1
            else:
                # Run detection (use True/False directly, no uppercase conversion)
                is_abuse = self.verify_repository(repo_data, sub_category)
                detect_label = str(is_abuse)  # Yields "True" or "False"

                if is_abuse:
                    success_count += 1
                else:
                    fail_count += 1

                # Avoid API rate limits (in case the detector makes network requests)
                time.sleep(0.3)

            # Build the output row
            output_row = {
                "sub_category": sub_category,
                "repo/account_name": repo_name,
                "URL": url,
                "m_label": m_label,
                "detect_label": detect_label
            }
            results.append(output_row)

            # Print progress every 10 records
            if idx % 10 == 0:
                logger.info(
                    f"Progress: {idx}/{len(csv_rows)} (True: {success_count}, False: {fail_count}, Not_Found: {not_found_count})")

        # Write the output CSV
        fieldnames = ["sub_category", "repo/account_name", "URL", "m_label", "detect_label"]
        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)

        # Print statistics
        logger.info("\n" + "=" * 50)
        logger.info("Verification summary")
        logger.info("=" * 50)
        logger.info(f"Total records: {len(csv_rows)}")
        logger.info(f"Abuse detected (detect_label=True): {success_count}")
        logger.info(f"No abuse detected (detect_label=False): {fail_count}")
        logger.info(f"Repository not found (Not_Found): {not_found_count}")

        # Compute accuracy
        valid_count = success_count + fail_count
        if valid_count > 0:
            # Compute match accuracy against m_label
            correct = 0
            for row in results:
                m = row.get("m_label", "").upper()
                d = row.get("detect_label", "")
                if d != "Not_Found" and m == d:
                    correct += 1
            if valid_count > 0:
                logger.info(f"Match accuracy: {correct}/{valid_count} = {correct / valid_count * 100:.2f}%")

        logger.info(f"Output file saved to: {output_path}")


def main():
    """Main entry point"""
    # Specify the parameters directly here, no command line input needed
    csv_file = "verification/Single/v_fake_stats.csv"
    json_file = "verification/Single/v_fake_stats.json"
    output_file = "verification/Single/v_fake_stats_output.csv"
    config_file = "config.json"

    # Create the verification pipeline
    pipeline = VerificationPipeline(config_file)

    # Run verification
    pipeline.run(csv_file, json_file, output_file)


if __name__ == "__main__":
    main()