"""
Post-processing script - filters and re-judges abuse detection results
Categories handled:
1. reputation_farming: counts as abuse only when abuse_activity_count >= 5
2. issue_spam: counts as abuse only when spam_count >= 5
3. spoofed_contributor: keeps only contributors present in the famous developer dataset; no valid contributor means no abuse

Keeps the original output structure, no new fields added
"""

import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Set

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class AbuseResultPostProcessor:
    """Post-processor for abuse detection results"""

    # Categories that need post-processing
    CATEGORIES_TO_PROCESS = ["reputation_farming", "issue_spam", "spoofed_contributor"]

    # Threshold configuration
    THRESHOLDS = {
        "reputation_farming": {"min_abuse_activity_count": 5},
        "issue_spam": {"min_spam_count": 5}
    }

    def __init__(self, corpus_path: str = "corpus_developers_famous.json"):
        """
        Initialize the post-processor

        Args:
            corpus_path: Path to the famous developer dataset
        """
        self.corpus_path = corpus_path
        self.famous_developers: Set[str] = set()
        self._load_famous_developers()

    def _load_famous_developers(self):
        """Load the famous developer dataset"""
        try:
            if Path(self.corpus_path).exists():
                with open(self.corpus_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.famous_developers = set(data.keys())
                logger.info(f"Loaded famous developer dataset: {len(self.famous_developers)} developers")
            else:
                logger.warning(f"Famous developer dataset does not exist: {self.corpus_path}")
        except Exception as e:
            logger.error(f"Failed to load famous developer dataset: {e}")

    def is_famous_developer(self, login: str) -> bool:
        """Determine whether this is a famous developer"""
        return login in self.famous_developers

    def process_reputation_farming(self, detail: Dict) -> Dict:
        """
        Handle the reputation_farming category
        Condition: counts as abuse only when abuse_activity_count >= 5
        """
        if not detail.get("is_abuse", False):
            return detail

        abuse_activity_count = detail.get("details", {}).get("abuse_activity_count", 0)
        is_abuse = abuse_activity_count >= self.THRESHOLDS["reputation_farming"]["min_abuse_activity_count"]

        detail["is_abuse"] = is_abuse
        return detail

    def process_issue_spam(self, detail: Dict) -> Dict:
        """
        Handle the issue_spam category
        Condition: counts as abuse only when spam_count >= 5
        """
        if not detail.get("is_abuse", False):
            return detail

        spam_count = detail.get("details", {}).get("spam_count", 0)
        is_abuse = spam_count >= self.THRESHOLDS["issue_spam"]["min_spam_count"]

        detail["is_abuse"] = is_abuse
        return detail

    def process_spoofed_contributor(self, detail: Dict) -> Dict:
        """
        Handle the spoofed_contributor category
        Condition: keeps only contributors present in the famous developer dataset; no valid contributor means no abuse
        """
        if not detail.get("is_abuse", False):
            return detail

        details = detail.get("details", {})
        suspicious_contributors = details.get("suspicious_contributors", [])

        # Filter: keep only famous developers
        valid_contributors = []
        for contributor in suspicious_contributors:
            login = contributor.get("login", "")
            if self.is_famous_developer(login):
                valid_contributors.append(contributor)

        # Update suspicious_contributors in details
        details["suspicious_contributors"] = valid_contributors

        # Update the summary information
        if valid_contributors:
            details["summary"] = f"Found {len(valid_contributors)} popular developers with insufficient contributions in small/new repositories"
            detail["is_abuse"] = True
        else:
            detail["is_abuse"] = False

        return detail

    def remove_detection_timestamp(self, repo_data: Dict) -> Dict:
        """
        Delete the detection_timestamp field
        """
        if "detection_timestamp" in repo_data:
            del repo_data["detection_timestamp"]
        return repo_data

    def process_repository(self, repo_data: Dict) -> Dict:
        """
        Process the detection result of a single repository
        """
        processed_repo = repo_data.copy()

        # Delete the detection_timestamp field
        processed_repo = self.remove_detection_timestamp(processed_repo)

        abuse_details = processed_repo.get("abuse_details", {})

        # Handle each category
        for category in self.CATEGORIES_TO_PROCESS:
            if category in abuse_details:
                if category == "reputation_farming":
                    abuse_details[category] = self.process_reputation_farming(abuse_details[category])
                elif category == "issue_spam":
                    abuse_details[category] = self.process_issue_spam(abuse_details[category])
                elif category == "spoofed_contributor":
                    abuse_details[category] = self.process_spoofed_contributor(abuse_details[category])

        # Recompute abuse_count and abuse_categories
        new_abuse_categories = []
        for category, detail in abuse_details.items():
            if detail.get("is_abuse", False):
                new_abuse_categories.append(category)

        processed_repo["abuse_details"] = abuse_details
        processed_repo["abuse_count"] = len(new_abuse_categories)
        processed_repo["abuse_categories"] = ",".join(new_abuse_categories) if new_abuse_categories else "NULL"

        # Log the processing (not written to file)
        if processed_repo.get("abuse_count", 0) != repo_data.get("abuse_count", 0):
            logger.info(
                f"Repository {processed_repo.get('full_name', 'unknown')}: "
                f"original abuse category count={repo_data.get('abuse_count', 0)}, "
                f"new abuse category count={processed_repo['abuse_count']}"
            )

        return processed_repo

    def process_dataset(self, input_file: str, output_file: str = None) -> List[Dict]:
        """
        Process the whole dataset

        Args:
            input_file: Path to the input JSON file
            output_file: Path to the output JSON file (generated automatically if None)

        Returns:
            List of processed repositories
        """
        input_path = Path(input_file)

        if output_file is None:
            output_file = str(input_path.parent / f"{input_path.stem}_postprocess{input_path.suffix}")

        logger.info(f"Input file: {input_file}")
        logger.info(f"Output file: {output_file}")

        # Load data
        with open(input_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Make sure the data is a list
        if isinstance(data, dict):
            repositories = data.get("repos", [data])
        else:
            repositories = data

        logger.info(f"Loaded detection results for {len(repositories)} repositories in total")

        # Process each repository
        processed_repos = []
        for idx, repo in enumerate(repositories, 1):
            logger.info(f"Processing repository [{idx}/{len(repositories)}]: {repo.get('full_name', 'unknown')}")
            processed_repo = self.process_repository(repo)
            processed_repos.append(processed_repo)

        # Save the results
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(processed_repos, f, ensure_ascii=False, indent=2)

        logger.info(f"Processing complete! Results saved to: {output_file}")

        return processed_repos

    def generate_statistics(self, processed_repos: List[Dict], output_file: str = None,
                            original_repos: List[Dict] = None) -> Dict:
        """
        Generate the post-processing statistics report

        Args:
            processed_repos: List of processed repositories
            output_file: Output path of the statistics report
            original_repos: List of original repositories (used for comparison)

        Returns:
            Statistics dict
        """
        stats = {
            "total_repos": len(processed_repos),
            "repos_with_abuse": 0,
            "repos_without_abuse": 0,
            "repos_with_error": 0,
            "abuse_category_counts": {},
            "abuse_counts_distribution": {},
            "post_process_changes": {
                "reputation_farming": {"changed": 0, "changed_repos": []},
                "issue_spam": {"changed": 0, "changed_repos": []},
                "spoofed_contributor": {"changed": 0, "changed_repos": []}
            },
            "generated_at": datetime.now().isoformat()
        }

        # Build a mapping of the original data (used to compare changes)
        original_map = {}
        if original_repos:
            for repo in original_repos:
                key = repo.get("full_name", repo.get("html_url", ""))
                original_map[key] = repo

        for repo in processed_repos:
            abuse_count = repo.get("abuse_count", 0)
            abuse_categories = repo.get("abuse_categories", "NULL")

            if abuse_count == -1:
                stats["repos_with_error"] += 1
                continue

            if abuse_count > 0:
                stats["repos_with_abuse"] += 1
                if abuse_categories != "NULL":
                    for cat in abuse_categories.split(','):
                        stats["abuse_category_counts"][cat] = stats["abuse_category_counts"].get(cat, 0) + 1
            else:
                stats["repos_without_abuse"] += 1

            stats["abuse_counts_distribution"][abuse_count] = stats["abuse_counts_distribution"].get(abuse_count, 0) + 1

            # Compare changes
            repo_key = repo.get("full_name", repo.get("html_url", ""))
            original_repo = original_map.get(repo_key)
            if original_repo:
                for category in self.CATEGORIES_TO_PROCESS:
                    original_detail = original_repo.get("abuse_details", {}).get(category, {})
                    new_detail = repo.get("abuse_details", {}).get(category, {})

                    if original_detail.get("is_abuse", False) != new_detail.get("is_abuse", False):
                        stats["post_process_changes"][category]["changed"] += 1
                        stats["post_process_changes"][category]["changed_repos"].append({
                            "full_name": repo.get("full_name", "unknown"),
                            "original_is_abuse": original_detail.get("is_abuse", False),
                            "new_is_abuse": new_detail.get("is_abuse", False)
                        })
                        # Limit the list length to keep it from growing too large
                        if len(stats["post_process_changes"][category]["changed_repos"]) > 10:
                            stats["post_process_changes"][category]["changed_repos"] = \
                                stats["post_process_changes"][category]["changed_repos"][:10]

        # Compute percentages
        total_valid = stats["repos_with_abuse"] + stats["repos_without_abuse"]
        if total_valid > 0:
            stats["abuse_percentage"] = round(stats["repos_with_abuse"] / total_valid * 100, 2)

        # Print statistics
        self._print_statistics(stats)

        # Save the statistics report
        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(stats, f, ensure_ascii=False, indent=2)
            logger.info(f"Statistics report saved to: {output_file}")

        return stats

    def _print_statistics(self, stats: Dict):
        """Print statistics"""
        logger.info("\n" + "=" * 60)
        logger.info("Post-processing statistics report")
        logger.info("=" * 60)

        logger.info(f"Total repositories: {stats['total_repos']}")
        if stats['repos_with_error'] > 0:
            logger.info(f"Repositories that failed processing: {stats['repos_with_error']}")
        logger.info(f"Repositories with abuse: {stats['repos_with_abuse']} ({stats.get('abuse_percentage', 0)}%)")
        logger.info(f"Repositories without abuse: {stats['repos_without_abuse']}")

        logger.info("\n" + "-" * 40)
        logger.info("Post-processing category change statistics:")
        logger.info("-" * 40)

        for category, changes in stats["post_process_changes"].items():
            logger.info(f"  {category}: verdict changed for {changes['changed']} repositories")
            if changes['changed'] > 0 and changes['changed_repos']:
                logger.info(f"    Examples (first 3):")
                for repo in changes['changed_repos'][:3]:
                    status = "abuse -> non-abuse" if repo['original_is_abuse'] and not repo['new_is_abuse'] else "non-abuse -> abuse"
                    logger.info(f"      - {repo['full_name']}: {status}")

        logger.info("\n" + "-" * 40)
        logger.info("Abuse category distribution:")
        logger.info("-" * 40)
        for category, count in sorted(stats["abuse_category_counts"].items(), key=lambda x: x[1], reverse=True):
            logger.info(f"  {category}: {count}")

        logger.info("\n" + "-" * 40)
        logger.info("Abuse count distribution:")
        logger.info("-" * 40)
        for count, repos_count in sorted(stats["abuse_counts_distribution"].items()):
            if count >= 0:
                logger.info(f"  {count} abuse types: {repos_count} repositories")

        logger.info("=" * 60)


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description='Post-processing of GitHub abuse detection results')
    parser.add_argument('--input', '-i', default='trial_small_3_output.json', help='Path to the input JSON file (detection results)')
    parser.add_argument('--output', '-o', default=None, help='Path to the output JSON file (auto-generated by default)')
    parser.add_argument('--stats', '-s', default=None, help='Output path of the statistics report (defaults to the detect_pipeline_log directory)')
    parser.add_argument('--corpus', '-c', default='corpus_developers_famous.json', help='Path to the famous developer dataset')

    args = parser.parse_args()

    input_path = Path(args.input)

    # Determine the output file path
    if args.output is None:
        output_file = str(input_path.parent / f"{input_path.stem}_postprocess{input_path.suffix}")
    else:
        output_file = args.output

    # Determine the statistics report path
    if args.stats is None:
        log_dir = input_path.parent / "detect_pipeline_log"
        log_dir.mkdir(exist_ok=True)
        stats_file = str(log_dir / f"{input_path.stem}_postprocess_statistics.json")
    else:
        stats_file = args.stats

    # Load the original data (used for comparison statistics)
    with open(args.input, 'r', encoding='utf-8') as f:
        original_data = json.load(f)
    if isinstance(original_data, dict):
        original_repos = original_data.get("repos", [original_data])
    else:
        original_repos = original_data

    # Create the post-processor and run it
    processor = AbuseResultPostProcessor(corpus_path=args.corpus)

    # Process the dataset
    processed_repos = processor.process_dataset(args.input, output_file)

    # Generate the statistics report (passing the original data for comparison)
    processor.generate_statistics(processed_repos, stats_file, original_repos)

    logger.info(f"\nProcessing complete!")
    logger.info(f"Result file: {output_file}")
    logger.info(f"Statistics report: {stats_file}")


if __name__ == "__main__":
    main()