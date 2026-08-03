"""
GitHub abuse detection pipeline
Applies eight abuse detectors sequentially to repository JSON datasets.
Supports resumable execution and live writes.
"""

import json
import csv
import time
import logging
import os
import shutil
from typing import Dict, List, Optional, Any, Set
from pathlib import Path
from datetime import datetime

# Import existing detectors
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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class AbuseDetectionPipeline:
    """Abuse detection pipeline (supports resumable execution)"""

    # Detection categories and their corresponding detector classes
    DETECTION_CATEGORIES = [
        ("fake_stars", FakeStarsDetector),
        ("automatic_updates", AutomaticUpdatesDetector),
        ("typo_squatting", TypoSquattingDetector),
        ("reputation_farming", ReputationFarmingDetector),
        ("fake_stats", FakeStatsDetector),
        ("spoofed_contributor", SpoofedContributorDetector),
        ("issue_spam", IssueSpamDetector),
        ("keyword_stuffing", KeywordStuffingDetector)
    ]

    def __init__(self, config_file: str = "config.json"):
        """
        Initialize the detection pipeline.
        """
        # Create the detector factory and pass in the configuration file
        self.detector_factory = AbuseDetectorFactory(config_file)

        # Cache detector instances to avoid repeated creation
        self.detectors = {}

        # Log directory name
        self.LOG_DIR = "detect_pipeline_log"

        # File suffixes
        self.PROGRESS_SUFFIX = "_progress.json"
        self.TEMP_SUFFIX = "_temp.json"
        self.FAILED_SUFFIX = "_failed.json"
        self.STATS_SUFFIX = "_statistics.json"

        logger.info("Abuse detection pipeline initialized")

    def _ensure_log_dir(self, base_output_path: str):
        """Ensure the log directory exists"""
        # Determine the output file's directory (script directory)
        output_dir = Path(base_output_path).parent
        log_dir = output_dir / self.LOG_DIR
        log_dir.mkdir(exist_ok=True)
        return str(log_dir)

    def _get_log_file_path(self, base_output_path: str, suffix: str) -> str:
        """Get the full path for a log file"""
        log_dir = self._ensure_log_dir(base_output_path)
        base_name = Path(base_output_path).stem
        return str(Path(log_dir) / f"{base_name}{suffix}")

    def _get_detector(self, category: str):
        """Get or create a detector instance"""
        if category not in self.detectors:
            self.detectors[category] = self.detector_factory.get_detector(category)
        return self.detectors[category]

    def _get_repo_key(self, repo_data: Dict) -> str:
        """Get a unique identifier for a repository"""
        # Prefer id, then full_name, then html_url
        repo_id = repo_data.get("id")
        if repo_id:
            return str(repo_id)

        full_name = repo_data.get("full_name")
        if full_name:
            return full_name

        html_url = repo_data.get("html_url")
        if html_url:
            return html_url

        # Last resort: use name
        return repo_data.get("name", "unknown")

    def _load_progress(self, progress_file: str) -> Set[str]:
        """Load identifiers of completed repositories from a progress file"""
        if os.path.exists(progress_file):
            try:
                with open(progress_file, 'r', encoding='utf-8') as f:
                    progress_data = json.load(f)
                    completed = set(progress_data.get("completed_repos", []))
                    logger.info(f"Loaded progress file: {len(completed)} completed repositories")
                    return completed
            except Exception as e:
                logger.warning(f"Failed to load progress file: {e}")
                return set()
        return set()

    def _save_progress(self, progress_file: str, completed_repos: Set[str],
                       last_index: int, total: int):
        """Save progress information to disk"""
        try:
            progress_data = {
                "completed_repos": list(completed_repos),
                "last_index": last_index,
                "total_repos": total,
                "last_update": datetime.now().isoformat()
            }
            with open(progress_file, 'w', encoding='utf-8') as f:
                json.dump(progress_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save progress file: {e}")

    def _append_repo_result(self, temp_file: str, repo_result: Dict,
                            failed_file: str, is_first: bool = False):
        """Append a single repository result to the temporary output file"""
        # Remove detection_timestamp field
        repo_result_to_save = {k: v for k, v in repo_result.items() if k != "detection_timestamp"}

        try:
            if is_first:
                # First write: create a new file
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump([repo_result_to_save], f, ensure_ascii=False, indent=2)
            else:
                # Append mode: read existing data, append new result, write back
                with open(temp_file, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)

                existing_data.append(repo_result_to_save)

                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(existing_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to write result: {e}")
            # Fallback: save this result to the failed file separately
            try:
                with open(failed_file, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(repo_result_to_save, ensure_ascii=False) + '\n')
                logger.info(f"Result saved to fallback file: {failed_file}")
            except Exception as e2:
                logger.error(f"Failed to save to fallback file as well: {e2}")

    def process_repository(self, repo_data: Dict) -> Dict:
        """
        Process a single repository record and execute all detectors.

        Args:
            repo_data: Repository JSON data

        Returns:
            Repository data augmented with detection results
        """
        result = repo_data.copy()

        repo_url = repo_data.get("html_url", "")
        if not repo_url:
            logger.warning(f"Repository {repo_data.get('full_name', 'unknown')} is missing html_url field")
            result["abuse_count"] = 0
            result["abuse_categories"] = "NULL"
            return result

        abuse_categories = []
        abuse_details = {}

        logger.info(f"Starting detection for repository: {repo_url}")

        # Use a data-driven model: pass repository JSON to all detectors
        for category, detector_class in self.DETECTION_CATEGORIES:
            logger.debug(f"  Running detector: {category}")

            try:
                detector = self._get_detector(category)
                if not detector:
                    logger.warning(f"Detector not found for category {category}")
                    abuse_details[category] = {"is_abuse": False, "error": "Detector not available"}
                    continue
                    continue

                # Use a data-driven model: pass the repository JSON data directly
                detection_result = detector.detect(repo_data)

                if detection_result.is_abuse:
                    abuse_categories.append(category)
                    abuse_details[category] = {
                        "is_abuse": True,
                        "details": detection_result.details
                    }

                    # For fake_stars, optionally extract low-activity users list
                    if category == "fake_stars":
                        abuse_details[category]["low_activity_users"] = detection_result.details.get(
                            "low_activity_users", [])
                else:
                    abuse_details[category] = {"is_abuse": False}

                # Avoid API throttling
                time.sleep(0.5)

            except Exception as e:
                logger.error(f"Error processing category {category}: {e}")
                abuse_details[category] = {"is_abuse": False, "error": str(e)}

        result["abuse_count"] = len(abuse_categories)
        result["abuse_categories"] = ",".join(abuse_categories) if abuse_categories else "NULL"
        result["abuse_details"] = abuse_details

        logger.info(f"Repository detection complete: abuse_count={result['abuse_count']}, categories={result['abuse_categories']}")

        return result

    def process_dataset(self, input_file: str, output_file: str = None,
                        max_items: int = None, force_restart: bool = False):
        """
        Process the entire dataset (supports resumable execution).

        Args:
            input_file: Path to the input JSON file
            output_file: Path to the output JSON file (auto-generated if None)
            max_items: Maximum number of items to process (for testing)
            force_restart: Whether to force restart and ignore existing progress
        """
        # Determine the output file path (placed in the directory of this py file)
        if output_file is None:
            input_path = Path(input_file)
            # If the input file is in the current directory, place output there.
            # If the input file is in a different directory, place output in the script directory.
            script_dir = Path(__file__).parent
            output_file = str(script_dir / f"{input_path.stem}_abuse_detected{input_path.suffix}")

        # Get paths for log files (all auxiliary files are placed in detect_pipeline_log)
        progress_file = self._get_log_file_path(output_file, self.PROGRESS_SUFFIX)
        temp_file = self._get_log_file_path(output_file, self.TEMP_SUFFIX)
        failed_file = self._get_log_file_path(output_file, self.FAILED_SUFFIX)
        stats_file = self._get_log_file_path(output_file, self.STATS_SUFFIX)

        logger.info(f"Output file: {output_file}")
        logger.info(f"Log directory: {Path(output_file).parent / self.LOG_DIR}")
        logger.info(f"Temporary file: {temp_file}")
        logger.info(f"Progress file: {progress_file}")
        logger.info(f"Fallback failed file: {failed_file}")

        # Load the dataset
        logger.info(f"Loading dataset: {input_file}")
        with open(input_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Ensure the data is a list
        if isinstance(data, dict):
            if "repos" in data:
                repositories = data["repos"]
            else:
                repositories = [data]
        else:
            repositories = data

        logger.info(f"Dataset contains {len(repositories)} repositories")

        # Limit the number of repositories to process (if specified)
        if max_items:
            repositories = repositories[:max_items]
            logger.info(f"Limiting processing to first {max_items} repositories")

        total = len(repositories)

        # Load completed repositories from progress
        completed_repos = set()
        start_index = 0

        if not force_restart and os.path.exists(progress_file):
            completed_repos = self._load_progress(progress_file)
            start_index = len(completed_repos)

            if start_index > 0:
                logger.info(f"Detected {start_index} completed repositories, resuming from repository {start_index + 1}")

                # Load the existing result file
                if os.path.exists(temp_file):
                    try:
                        with open(temp_file, 'r', encoding='utf-8') as f:
                            existing_results = json.load(f)
                            logger.info(f"Loaded {len(existing_results)} existing results")
                    except Exception as e:
                        logger.warning(f"Failed to load existing results file: {e}")
                        existing_results = []
                else:
                    existing_results = []
            else:
                existing_results = []
        else:
            if force_restart:
                logger.info("Force restart mode: ignoring existing progress")
                # Clean up old progress and temporary files
                if os.path.exists(progress_file):
                    os.remove(progress_file)
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                if os.path.exists(failed_file):
                    os.remove(failed_file)

            completed_repos = set()
            start_index = 0
            existing_results = []

        # Process repositories that remain
        processed_repos = list(existing_results)  # preserve already processed results
        failed_repos = []  # record failed repositories

        for idx, repo in enumerate(repositories, 1):
            # Skip repositories that are already completed
            repo_key = self._get_repo_key(repo)
            if repo_key in completed_repos:
                logger.info(f"Skipping already completed repository [{idx}/{total}]: {repo.get('full_name', 'unknown')}")
                continue

            logger.info(f"Processing repository [{idx}/{total}]: {repo.get('full_name', 'unknown')}")

            try:
                processed_repo = self.process_repository(repo)
                processed_repos.append(processed_repo)

                # Write results live (append mode)
                is_first = (len(processed_repos) == 1)
                self._append_repo_result(temp_file, processed_repo, failed_file, is_first)

                # Update progress
                completed_repos.add(repo_key)
                self._save_progress(progress_file, completed_repos, idx, total)

                logger.info(f"Repository processed and saved [{len(completed_repos)}/{total}]")

            except Exception as e:
                logger.error(f"Error processing repository {repo.get('full_name', 'unknown')}: {e}")
                # Mark repository result as error
                repo_with_error = repo.copy()
                repo_with_error["abuse_count"] = -1
                repo_with_error["abuse_categories"] = "ERROR"
                repo_with_error["error"] = str(e)

                processed_repos.append(repo_with_error)
                failed_repos.append(repo_with_error)

                # Record failed repository to fallback file
                try:
                    with open(failed_file, 'a', encoding='utf-8') as f:
                        f.write(json.dumps(repo_with_error, ensure_ascii=False) + '\n')
                except:
                    pass

        # Final result: copy the temporary file to the output file
        if os.path.exists(temp_file):
            shutil.copy2(temp_file, output_file)
            logger.info(f"Final results saved to: {output_file}")
        else:
            # If no results were generated, save an empty list
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump([], f, ensure_ascii=False, indent=2)

        # Generate statistics report (placed in the log directory)
        self._generate_statistics(processed_repos, stats_file)

        logger.info(f"Processing complete! Results saved to: {output_file}")
        if failed_repos:
            logger.warning(f"There were {len(failed_repos)} failed repositories. See: {failed_file}")

        return processed_repos

    def _generate_statistics(self, processed_repos: List[Dict], stats_file: str):
        """Generate a statistics report"""
        stats = {
            "total_repos": len(processed_repos),
            "repos_with_abuse": 0,
            "repos_without_abuse": 0,
            "repos_with_error": 0,
            "abuse_category_counts": {},
            "abuse_counts_distribution": {},
            "abuse_details": {},
            "generated_at": datetime.now().isoformat()
        }

        for repo in processed_repos:
            abuse_count = repo.get("abuse_count", 0)

            if abuse_count == -1:
                stats["repos_with_error"] += 1
                continue

            if abuse_count > 0:
                stats["repos_with_abuse"] += 1

                # Count abuse category distribution
                categories = repo.get("abuse_categories", "")
                if categories != "NULL":
                    for category in categories.split(','):
                        stats["abuse_category_counts"][category] = stats["abuse_category_counts"].get(category, 0) + 1
            else:
                stats["repos_without_abuse"] += 1

            # Count abuse count distribution
            stats["abuse_counts_distribution"][abuse_count] = stats["abuse_counts_distribution"].get(abuse_count, 0) + 1

        # Compute percentages
        total_valid = stats["repos_with_abuse"] + stats["repos_without_abuse"]
        if total_valid > 0:
            stats["abuse_percentage"] = round(stats["repos_with_abuse"] / total_valid * 100, 2)

        # Save the statistics report
        with open(stats_file, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

        # Log statistics summary
        logger.info("\n" + "=" * 50)
        logger.info("Abuse detection statistics report")
        logger.info("=" * 50)
        logger.info(f"Total repositories: {stats['total_repos']}")
        logger.info(f"Valid repositories: {total_valid}")
        logger.info(f"Repositories with abuse: {stats['repos_with_abuse']} ({stats.get('abuse_percentage', 0)}%)")
        logger.info(f"Repositories without abuse: {stats['repos_without_abuse']}")
        logger.info(f"Repositories with errors: {stats['repos_with_error']}")
        logger.info("\nAbuse category distribution:")
        for category, count in sorted(stats["abuse_category_counts"].items(), key=lambda x: x[1], reverse=True):
            logger.info(f"  {category}: {count}")
        logger.info("\nAbuse count distribution:")
        for count, repos_count in sorted(stats["abuse_counts_distribution"].items()):
            if count >= 0:
                logger.info(f"  {count} abuse types: {repos_count} repositories")
        logger.info("=" * 50)
        logger.info(f"Statistics report saved to: {stats_file}")


def convert_to_csv(json_file: str, csv_file: str = None):
    """
    Convert JSON results to CSV format.

    Args:
        json_file: JSON results file
        csv_file: Output CSV file path (auto-generated if None)
    """
    if csv_file is None:
        csv_file = json_file.replace('.json', '.csv')

    # Load data
    with open(json_file, 'r', encoding='utf-8') as f:
        repos = json.load(f)

    # Prepare CSV fields
    fieldnames = [
        'id', 'name', 'full_name', 'html_url', 'stargazers_count', 'forks_count',
        'abuse_count', 'abuse_categories', 'fake_stars', 'automatic_updates',
        'typo_squatting', 'reputation_farming', 'fake_stats', 'spoofed_contributor',
        'issue_spam', 'keyword_stuffing'
    ]

    with open(csv_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for repo in repos:
            # Extract detection results for each category
            abuse_details = repo.get('abuse_details', {})

            row = {
                'id': repo.get('id', ''),
                'name': repo.get('name', ''),
                'full_name': repo.get('full_name', ''),
                'html_url': repo.get('html_url', ''),
                'stargazers_count': repo.get('stargazers_count', 0),
                'forks_count': repo.get('forks_count', 0),
                'abuse_count': repo.get('abuse_count', 0),
                'abuse_categories': repo.get('abuse_categories', 'NULL'),
            }

            # Add result values for each category
            for category in ['fake_stars', 'automatic_updates', 'typo_squatting',
                             'reputation_farming', 'fake_stats', 'spoofed_contributor',
                             'issue_spam', 'keyword_stuffing']:
                category_result = abuse_details.get(category, {})
                row[category] = category_result.get('is_abuse', False)

            writer.writerow(row)

    logger.info(f"CSV file saved to: {csv_file}")


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description='GitHub abuse detection pipeline (supports resumable execution)')
    parser.add_argument('--input', '-i', default='trial_small_2_small.json', help='Input JSON dataset file path')
    parser.add_argument('--output', '-o', default='trial_small_3_output.json',
                        help='Output JSON file path (default placed in script directory)')
    parser.add_argument('--config', '-c', default='config.json', help='Configuration file path')
    parser.add_argument('--max', '-m', type=int, default=None, help='Maximum number of repositories to process')
    parser.add_argument('--csv', '-csv', action='store_true', default=False, help='Also generate CSV output')
    parser.add_argument('--force', '-f', action='store_true', default=False,
                        help='Force restart and ignore existing progress')
    parser.add_argument('--single', '-s', help='Process a single repository from full JSON or URL')

    args = parser.parse_args()

    # Create the detection pipeline
    pipeline = AbuseDetectionPipeline(args.config)

    if args.single:
        # Process a single repository
        try:
            # Try to parse as JSON
            repo_data = json.loads(args.single)
        except:
            # If not JSON, treat as a URL
            repo_data = {
                "html_url": args.single,
                "full_name": args.single.split('/')[-2] + '/' + args.single.split('/')[
                    -1] if '/' in args.single else args.single
            }

        result = pipeline.process_repository(repo_data)
        # Remove detection_timestamp from the result
        if "detection_timestamp" in result:
            del result["detection_timestamp"]
        print(json.dumps(result, ensure_ascii=False, indent=2))

    else:
        # Process the full dataset
        output_file = args.output
        if output_file is None:
            input_path = Path(args.input)
            # Output file is placed in the script directory
            script_dir = Path(__file__).parent
            output_file = str(script_dir / f"{input_path.stem}_abuse_detected{input_path.suffix}")

        processed = pipeline.process_dataset(
            args.input,
            output_file,
            args.max,
            force_restart=args.force
        )

        # Generate CSV if requested
        if args.csv:
            convert_to_csv(output_file)


if __name__ == "__main__":
    main()