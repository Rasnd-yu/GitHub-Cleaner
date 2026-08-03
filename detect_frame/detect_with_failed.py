"""
detect_with_failed.py
Compare input and output files, identify missing repositories, and rescan them.
Insert rescan results into the original output file.

Usage:
1. Modify the configuration section below to specify input/output file names
2. Then run: python detect_with_failed.py
"""

import json
import os
import sys
import time
import logging
from pathlib import Path
from typing import Dict, List, Set, Optional
from datetime import datetime

# ==================== Configuration Section - Edit file names here ====================
# Input file (pipeline input file)
INPUT_FILE = "trial_small_1.json"
# Output file (pipeline output file)
OUTPUT_FILE = "trial_small_1_output_clean.json"
# Configuration file path
CONFIG_FILE = "config.json"
# Output file suffix
OUTPUT_SUFFIX = "_supply"

# Scan request delay in seconds (avoid API rate limits, default 0.5)
DELAY = 0.5
# Force rescan (ignore possible cache)
FORCE_RESCAN = False
# ==================== End of Configuration Section ====================

# Import detectors from the pipeline
from github_abuse_detector_pipeline import AbuseDetectionPipeline

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DetectWithFailed:
    """Compare repositories and supplement detection results for missing ones"""

    def __init__(self, config_file: str = "config.json"):
        """Initialize detector.

        Args:
            config_file: Configuration file path
        """
        self.pipeline = AbuseDetectionPipeline(config_file)
        logger.info("Detector initialized successfully")

    def get_repo_key(self, repo_data: Dict) -> str:
        """Get the unique identifier for a repository.

        Args:
            repo_data: Repository data

        Returns:
            Unique repository identifier
        """
        # Prefer using id
        repo_id = repo_data.get("id")
        if repo_id:
            return str(repo_id)

        # Next, try full_name
        full_name = repo_data.get("full_name")
        if full_name:
            return full_name

        # Then, try html_url
        html_url = repo_data.get("html_url")
        if html_url:
            return html_url

        # Finally, use name
        return repo_data.get("name", "unknown")

    def get_expected_keys(self, input_data: List[Dict]) -> Set[str]:
        """Get all repository identifiers from input file.

        Args:
            input_data: Input data list

        Returns:
            Set of repository identifiers
        """
        keys = set()
        for repo in input_data:
            key = self.get_repo_key(repo)
            if key != "unknown":
                keys.add(key)
            else:
                logger.warning(f"Unable to get repository identifier: {repo.get('name', 'unknown')}")
        logger.info(f"Input file contains {len(keys)} valid repository identifiers")
        return keys

    def get_existing_keys(self, output_data: List[Dict]) -> Set[str]:
        """Get already-processed repository identifiers from output file.

        Args:
            output_data: Output data list

        Returns:
            Set of processed repository identifiers
        """
        keys = set()
        for repo in output_data:
            key = self.get_repo_key(repo)
            if key != "unknown":
                keys.add(key)
        logger.info(f"Output file contains {len(keys)} processed repository identifiers")
        return keys

    def find_missing_repos(self, input_data: List[Dict], output_data: List[Dict]) -> List[Dict]:
        """
        Find the repositories that are missing from the output

        Args:
            input_data: List of input data
            output_data: List of output data

        Returns:
            List of missing repository data
        """
        expected_keys = self.get_expected_keys(input_data)
        existing_keys = self.get_existing_keys(output_data)

        missing_keys = expected_keys - existing_keys

        if not missing_keys:
            logger.info("No missing repositories, all have been processed")
            return []

        logger.info(f"Found {len(missing_keys)} missing repositories")

        # Find the missing repository data
        missing_repos = []
        for repo in input_data:
            key = self.get_repo_key(repo)
            if key in missing_keys:
                missing_repos.append(repo)
                logger.info(f"Missing repository: {repo.get('full_name', repo.get('name', 'unknown'))} (key: {key})")

        return missing_repos

    def scan_missing_repos(self, missing_repos: List[Dict], delay: float = 0.5) -> List[Dict]:
        """
        Rescan the missing repositories

        Args:
            missing_repos: List of missing repository data
            delay: Delay between requests (seconds)

        Returns:
            List of scanned repository results
        """
        results = []
        total = len(missing_repos)

        logger.info(f"Starting scan of {total} missing repositories...")

        for idx, repo in enumerate(missing_repos, 1):
            repo_name = repo.get('full_name', repo.get('name', 'unknown'))
            logger.info(f"[{idx}/{total}] Scanning: {repo_name}")

            try:
                # Use the pipeline's process_repository method to run detection
                result = self.pipeline.process_repository(repo)

                # Remove detection_timestamp field (keep consistent with output file format)
                if "detection_timestamp" in result:
                    del result["detection_timestamp"]

                results.append(result)
                logger.info(f"[{idx}/{total}] Scan successful: {repo_name}, abuse_count={result.get('abuse_count', 0)}")

            except Exception as e:
                logger.error(f"[{idx}/{total}] Scan failed: {repo_name}, error: {e}")
                # Create error record
                error_result = repo.copy()
                error_result["abuse_count"] = -1
                error_result["abuse_categories"] = "ERROR"
                error_result["error"] = str(e)
                error_result["abuse_details"] = {}
                results.append(error_result)

            # Avoid API rate limits
            if delay > 0:
                time.sleep(delay)

        success_count = len([r for r in results if r.get('abuse_count') != -1])
        fail_count = len([r for r in results if r.get('abuse_count') == -1])
        logger.info(f"Scan complete: {success_count} succeeded, {fail_count} failed")

        return results

    def insert_results(self, original_output: List[Dict], new_results: List[Dict],
                       output_file: str, input_data: List[Dict]) -> List[Dict]:
        """
        Insert the newly scanned results into the original output data in the order of the input file

        Args:
            original_output: Original output data
            new_results: Newly scanned results
            output_file: Path to the output file
            input_data: Input data (used to obtain the original order)

        Returns:
            Merged complete data (ordered as in the input file)
        """
        # 1. Build an index of the existing results (keyed by repository identifier)
        existing_by_key = {}
        for repo in original_output:
            key = self.get_repo_key(repo)
            existing_by_key[key] = repo

        # 2. Build an index of the new results
        new_by_key = {}
        for repo in new_results:
            key = self.get_repo_key(repo)
            new_by_key[key] = repo

        # 3. Rebuild the final data in the order of the input file
        merged_data = []
        added_count = 0
        reused_count = 0

        for input_repo in input_data:
            key = self.get_repo_key(input_repo)

            if key in existing_by_key:
                # Use the data from the original output file
                merged_data.append(existing_by_key[key])
                reused_count += 1
            elif key in new_by_key:
                # Use the newly scanned data
                merged_data.append(new_by_key[key])
                added_count += 1
                logger.debug(f"Inserted missing repository in order: {key}")
            else:
                # Should not happen in theory, because every repository in input_data is either in original_output or in missing_repos
                logger.warning(f"Repository {key} is neither in the original output nor in the new scan results, using the input data as a placeholder")
                # Create a placeholder record
                placeholder = input_repo.copy()
                placeholder["abuse_count"] = -1
                placeholder["abuse_categories"] = "MISSING"
                placeholder["abuse_details"] = {}
                merged_data.append(placeholder)
                added_count += 1

        logger.info(f"Merge complete (in input order): reused {reused_count} existing results, added {added_count} new ones")

        # Save the merged results
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(merged_data, f, ensure_ascii=False, indent=2)

        logger.info(f"Merged results saved to: {output_file}")

        return merged_data

    def process(self, input_file: str, output_file: str,
                output_suffix: str = "_supply",
                delay: float = 0.5,
                force_rescan: bool = False) -> str:
        """
        Main processing flow

        Args:
            input_file: Path to the input JSON file
            output_file: Path to the output JSON file (the pipeline's output)
            output_suffix: Output file suffix (defaults to _supply)
            delay: Scan delay (seconds)
            force_rescan: Whether to force a rescan of all missing repositories

        Returns:
            Path to the output file
        """
        # Generate the output file path
        output_path = Path(output_file)

        # Build the supplemented output file name
        # If the original file name contains '_output_clean', replace it with the suffix
        stem = output_path.stem
        base_name = stem

        supply_file = str(output_path.parent / f"{base_name}{output_suffix}.json")

        logger.info("=" * 60)
        logger.info("Starting detection of missing repositories and supplementation")
        logger.info("=" * 60)
        logger.info(f"Input file: {input_file}")
        logger.info(f"Output file (original): {output_file}")
        logger.info(f"Output file (new): {supply_file}")

        # 1. Load the input file
        logger.info("Loading input file...")
        try:
            with open(input_file, 'r', encoding='utf-8') as f:
                input_data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load input file: {e}")
            raise

        # Make sure the input data is a list
        if isinstance(input_data, dict):
            if "repos" in input_data:
                input_data = input_data["repos"]
            else:
                input_data = [input_data]

        logger.info(f"Input file contains {len(input_data)} repositories")

        # 2. Load the output file
        logger.info("Loading output file...")
        try:
            with open(output_file, 'r', encoding='utf-8') as f:
                output_data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load output file: {e}")
            raise

        if isinstance(output_data, dict):
            if "repos" in output_data:
                output_data = output_data["repos"]
            else:
                output_data = [output_data]

        logger.info(f"Output file contains {len(output_data)} repositories")

        # 3. Find the missing repositories
        missing_repos = self.find_missing_repos(input_data, output_data)

        if not missing_repos:
            logger.info("No missing repositories, copying original file directly")
            # Copy the original file directly
            import shutil
            shutil.copy2(output_file, supply_file)
            logger.info(f"Copied to: {supply_file}")
            return supply_file

        # 4. Scan the missing repositories
        logger.info(f"Need to supplement {len(missing_repos)} repositories")
        new_results = self.scan_missing_repos(missing_repos, delay)

        # 5. Insert the results and save
        merged_data = self.insert_results(output_data, new_results, supply_file, input_data)

        # 6. Print statistics
        self.print_statistics(merged_data, len(input_data))

        logger.info("=" * 60)
        logger.info(f"Processing complete! Supplemented file saved to: {supply_file}")
        logger.info("=" * 60)

        return supply_file

    def print_statistics(self, data: List[Dict], expected_count: int):
        """Print statistics after supplementation.

        Args:
            data: Merged data
            expected_count: Expected number of repositories
        """
        total = len(data)
        success_count = len([r for r in data if r.get('abuse_count', 0) >= 0])
        error_count = len([r for r in data if r.get('abuse_count', 0) == -1])
        abuse_count = len([r for r in data if r.get('abuse_count', 0) > 0])

        logger.info("\n" + "=" * 50)
        logger.info("Post-supplementation statistics report")
        logger.info("=" * 50)
        logger.info(f"Expected repositories: {expected_count}")
        logger.info(f"Actual repositories: {total}")
        logger.info(f"Successful scans: {success_count}")
        logger.info(f"Failed scans: {error_count}")
        logger.info(f"Repositories with abuse: {abuse_count}")

        if success_count > 0:
            logger.info(f"Abuse rate: {abuse_count / success_count * 100:.2f}%")

        if total < expected_count:
            logger.warning(f"Warning: Actual repository count ({total}) is less than expected ({expected_count})")
        logger.info("=" * 50)


def main():
    """Main entry point"""
    print("\n" + "=" * 60)
    print("GitHub repository supplementation tool")
    print("=" * 60)
    print(f"Input file: {INPUT_FILE}")
    print(f"Output file: {OUTPUT_FILE}")
    print(f"Config file: {CONFIG_FILE}")
    print(f"Output suffix: {OUTPUT_SUFFIX}")
    print(f"Scan delay: {DELAY} seconds")
    print(f"Force rescan: {'Yes' if FORCE_RESCAN else 'No'}")
    print("=" * 60 + "\n")

    # Check whether the input file exists
    if not os.path.exists(INPUT_FILE):
        logger.error(f"Input file does not exist: {INPUT_FILE}")
        print(f"\n❌ Error: Input file '{INPUT_FILE}' does not exist!")
        print("Please update the INPUT_FILE variable at the top of the script to a valid path.")
        sys.exit(1)

    if not os.path.exists(OUTPUT_FILE):
        logger.error(f"Output file does not exist: {OUTPUT_FILE}")
        print(f"\n❌ Error: Output file '{OUTPUT_FILE}' does not exist!")
        print("Please update the OUTPUT_FILE variable at the top of the script to a valid path.")
        sys.exit(1)

    try:
        # Create the processor
        processor = DetectWithFailed(CONFIG_FILE)

        # Run the processing
        result_file = processor.process(
            input_file=INPUT_FILE,
            output_file=OUTPUT_FILE,
            output_suffix=OUTPUT_SUFFIX,
            delay=DELAY,
            force_rescan=FORCE_RESCAN
        )

        print(f"\n✅ Processing complete! Result file: {result_file}")

    except KeyboardInterrupt:
        logger.info("User interrupted the operation")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error during processing: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()