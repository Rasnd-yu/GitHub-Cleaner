"""
balance_repo_categories.py
Balances the number of repositories across the three categories by randomly sampling the specified number of repositories of each category from the dataset
Keeps the repositories in the input and output files in correspondence (removes the repositories at the same positions)
"""

import json
import random
import os
import sys
import logging
from pathlib import Path
from typing import Dict, List, Tuple
from collections import defaultdict

# ==================== Configuration ====================
# Input file (the pipeline's input file)
INPUT_FILE = "trial_small_3.json"

# Output file (the pipeline's output file, corresponding to the input file)
OUTPUT_FILE = "trial_small_3_output.json"

# Output file prefix (generated file names = prefix + .json and prefix + _output.json)
OUTPUT_PREFIX = "trial_small_4"

# Expected repository count for each category
CATEGORY_LIMITS = {
    "category1": 4000,  # Trending Repositories
    "category2": 4000,  # Companion Repositories
    "category3": 4000,  # General Repositories
}

# Whether to sample randomly (True: shuffle then sample, False: sample in the original order)
RANDOM_SELECTION = True

# Random seed (for reproducible results, None means use the system time)
RANDOM_SEED = 42

# Category definitions and matching rules
CATEGORY_PATTERNS = {
    "category1": {
        "patterns": [
            "2025 GitHub Trending",
            "GitHub Topics_",
            "GitHub Collections_"
        ],
        "name": "Trending Repositories"
    },
    "category2": {
        "patterns": [
            "Accompanying repository_"
        ],
        "name": "Companion Repositories"
    },
    "category3": {
        "patterns": [
            "ossf-scorecard-2026.03.16",
            "Blog & Report & Gray Literature"
        ],
        "name": "General Repositories"
    }
}

# Whether to print detailed classification statistics
VERBOSE = True

# ==================== End of configuration ====================

# Set up logging
logging.basicConfig(
    level=logging.INFO if VERBOSE else logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class RepoCategoryBalancer:
    """Repository category balancer"""

    def __init__(self, patterns: Dict, random_seed: int = None):
        """
        Initialize the balancer

        Args:
            patterns: Category matching rules
            random_seed: Random seed
        """
        self.patterns = patterns
        if random_seed is not None:
            random.seed(random_seed)
            logger.info(f"Random seed set: {random_seed}")

    def classify_repo(self, repo_data: Dict) -> str:
        """
        Classify a repository by its source field

        Args:
            repo_data: Repository data

        Returns:
            Category name (category1, category2, category3)
        """
        source = repo_data.get("source", "")

        if not source:
            logger.warning(f"Repository {repo_data.get('full_name', 'unknown')} has no source field")
            return "unknown"

        for category, info in self.patterns.items():
            for pattern in info["patterns"]:
                # Check whether the pattern matches (prefix match and exact match are supported)
                if pattern.endswith("_"):
                    # Prefix match (starts with pattern)
                    if source.startswith(pattern):
                        return category
                else:
                    # Exact match
                    if source == pattern:
                        return category

        logger.warning(f"Source '{source}' of repository {repo_data.get('full_name', 'unknown')} did not match any category")
        return "unknown"

    def verify_correspondence(self, input_data: List[Dict], output_data: List[Dict]) -> bool:
        """
        Verify the correspondence between the input and output files (by id)

        Args:
            input_data: Input data
            output_data: Output data

        Returns:
            Whether they correspond
        """
        len_in = len(input_data)
        len_out = len(output_data)

        logger.info(f"Input file length: {len_in}, output file length: {len_out}")

        if len_in != len_out:
            logger.error(f"❌ File lengths differ! input={len_in}, output={len_out}")
            logger.error("Please run detect_with_failed.py first to fix the missing repositories")
            return False

        mismatch_count = 0
        mismatch_examples = []

        # Loop over indices to avoid the problems of zip
        for i in range(len_in):
            input_id = input_data[i].get('id')
            output_id = output_data[i].get('id')

            # Safe conversion to avoid None comparison problems
            input_id_str = str(input_id) if input_id is not None else "None"
            output_id_str = str(output_id) if output_id is not None else "None"

            if input_id_str != output_id_str:
                mismatch_count += 1
                if len(mismatch_examples) < 5:  # Record only the first 5 mismatches
                    input_name = input_data[i].get('full_name', 'unknown')
                    output_name = output_data[i].get('full_name', 'unknown')
                    mismatch_examples.append({
                        'index': i,
                        'input_id': input_id_str,
                        'output_id': output_id_str,
                        'input_name': input_name,
                        'output_name': output_name
                    })

        if mismatch_count > 0:
            logger.error(f"❌ Found {mismatch_count} repositories with mismatched ids!")
            logger.error("Mismatch examples (first 5):")
            for ex in mismatch_examples:
                logger.error(
                    f"   index {ex['index']}: input id={ex['input_id']}({ex['input_name']}) vs output id={ex['output_id']}({ex['output_name']})")
            return False
        else:
            logger.info(f"✅ Verification passed: the input and output files correspond exactly ({len_in} repositories)")
            return True

    def select_repos_to_keep(self, input_data: List[Dict]) -> Tuple[List[int], Dict]:
        """
        Select the repository indices to keep (based only on the input file classification)

        Args:
            input_data: List of input data

        Returns:
            (list of kept indices, statistics)
        """
        # Group by category, recording the index of every repository
        category_indices = defaultdict(list)
        unknown_indices = []

        for idx, repo in enumerate(input_data):
            category = self.classify_repo(repo)
            if category == "unknown":
                unknown_indices.append(idx)
            else:
                category_indices[category].append(idx)

        # Statistics of the original distribution
        logger.info("Original classification statistics:")
        for category, indices in category_indices.items():
            name = self.patterns.get(category, {}).get("name", category)
            logger.info(f"  {name}: {len(indices)} repositories")
        logger.info(f"  Unknown category: {len(unknown_indices)} repositories")

        # Select the indices to keep
        keep_indices = []
        removal_stats = {}

        # Handle each category
        for category, indices in category_indices.items():
            expected_count = CATEGORY_LIMITS.get(category)
            current_count = len(indices)

            if expected_count is None:
                # Unlimited, keep everything
                keep_indices.extend(indices)
                removal_stats[category] = {
                    "name": self.patterns.get(category, {}).get("name", category),
                    "original": current_count,
                    "kept": current_count,
                    "removed": 0
                }
                logger.info(f"Category {category}: unlimited, keeping all {current_count}")

            elif current_count <= expected_count:
                # Not enough repositories, keep everything
                keep_indices.extend(indices)
                removal_stats[category] = {
                    "name": self.patterns.get(category, {}).get("name", category),
                    "original": current_count,
                    "kept": current_count,
                    "removed": 0
                }
                logger.info(f"Category {category}: current {current_count} <= expected {expected_count}, keeping all")

            else:
                # Too many repositories, randomly choose which ones to keep
                remove_count = current_count - expected_count
                logger.info(f"Category {category}: current {current_count} > expected {expected_count}, {remove_count} need to be removed")

                if RANDOM_SELECTION:
                    # Randomly select the indices to keep
                    selected_indices = random.sample(indices, expected_count)
                else:
                    # Take the first expected_count entries in the original order
                    selected_indices = indices[:expected_count]

                keep_indices.extend(selected_indices)

                removal_stats[category] = {
                    "name": self.patterns.get(category, {}).get("name", category),
                    "original": current_count,
                    "kept": expected_count,
                    "removed": remove_count
                }

                # Record the removed repositories (for debugging)
                removed_indices = [idx for idx in indices if idx not in set(selected_indices)]
                if VERBOSE and removed_indices:
                    logger.debug(f"Examples of repositories removed from category {category}:")
                    for idx in removed_indices[:5]:
                        repo_name = input_data[idx].get('full_name', 'unknown')
                        repo_id = input_data[idx].get('id', 'unknown')
                        logger.debug(f"  - index {idx}: {repo_name} (id: {repo_id})")
                    if len(removed_indices) > 5:
                        logger.debug(f"  ... and {len(removed_indices) - 5} more")

        # Handle the unknown category
        if unknown_indices:
            # Keep all unknown-category repositories (they could be removed instead, but here everything is kept)
            keep_indices.extend(unknown_indices)
            removal_stats["unknown"] = {
                "name": "Unknown Category",
                "original": len(unknown_indices),
                "kept": len(unknown_indices),
                "removed": 0
            }
            logger.info(f"Unknown category: keeping all {len(unknown_indices)}")

        # Sort the indices to preserve the original order
        keep_indices.sort()

        logger.info(f"\nKeeping {len(keep_indices)} repositories in total")

        return keep_indices, removal_stats

    def extract_repos_by_indices(self, data: List[Dict], indices: List[int]) -> List[Dict]:
        """
        Extract repository data by index

        Args:
            data: Original data list
            indices: List of indices to keep

        Returns:
            Extracted data list
        """
        return [data[i] for i in indices]

    def save_balanced_data(self, balanced_input: List[Dict], balanced_output: List[Dict],
                           input_file: str, output_file: str):
        """
        Save the balanced data

        Args:
            balanced_input: Balanced input data
            balanced_output: Balanced output data
            input_file: Save path of the input file
            output_file: Save path of the output file
        """
        # Save the input file
        with open(input_file, 'w', encoding='utf-8') as f:
            json.dump(balanced_input, f, ensure_ascii=False, indent=2)
        logger.info(f"Input file saved: {input_file} ({len(balanced_input)} repositories)")

        # Save the output file
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(balanced_output, f, ensure_ascii=False, indent=2)
        logger.info(f"Output file saved: {output_file} ({len(balanced_output)} repositories)")

    def print_statistics(self, input_data: List[Dict], output_data: List[Dict],
                         keep_indices: List[int], removal_stats: Dict,
                         new_input: List[Dict], new_output: List[Dict]):
        """
        Print statistics

        Args:
            input_data: Original input data
            output_data: Original output data
            keep_indices: List of kept indices
            removal_stats: Removal statistics
            new_input: New input data
            new_output: New output data
        """
        print("\n" + "=" * 80)
        print("Repository category balance report")
        print("=" * 80)

        # Original distribution
        print("\n[Original data]")
        print("-" * 40)
        print(f"  Repositories in the input file: {len(input_data)}")
        print(f"  Repositories in the output file: {len(output_data)}")

        # Category distribution
        print("\n[Original category distribution]")
        print("-" * 40)
        category_counts = defaultdict(int)
        for repo in input_data:
            category = self.classify_repo(repo)
            category_counts[category] += 1

        for category, count in sorted(category_counts.items()):
            if category in self.patterns:
                name = self.patterns[category]["name"]
            else:
                name = "Unknown Category"
            print(f"  {name:35s}: {count:5d} repositories")

        # Expected distribution
        print("\n[Expected distribution]")
        print("-" * 40)
        total_expected = 0
        for category, limit in CATEGORY_LIMITS.items():
            name = self.patterns.get(category, {}).get("name", category)
            total_expected += limit
            print(f"  {name:35s}: {limit:5d} repositories")
        print(f"  {'Total':35s}: {total_expected:5d} repositories")

        # Removal statistics
        print("\n[Removal statistics]")
        print("-" * 40)
        total_removed = 0
        for category, stats in sorted(removal_stats.items()):
            if stats["removed"] > 0:
                name = stats["name"]
                print(f"  {name:35s}: removed {stats['removed']:3d} (original {stats['original']} -> kept {stats['kept']})")
                total_removed += stats["removed"]

        if total_removed > 0:
            print(f"  {'Total':35s}: removed {total_removed:3d} repositories")
        else:
            print("  No repositories were removed")

        # Distribution after balancing
        print("\n[Balanced data]")
        print("-" * 40)
        print(f"  Repositories in the input file: {len(new_input)}")
        print(f"  Repositories in the output file: {len(new_output)}")

        # Category distribution after balancing
        print("\n[Balanced category distribution]")
        print("-" * 40)
        new_category_counts = defaultdict(int)
        for repo in new_input:
            category = self.classify_repo(repo)
            new_category_counts[category] += 1

        for category, count in sorted(new_category_counts.items()):
            if category in self.patterns:
                name = self.patterns[category]["name"]
            else:
                name = "Unknown Category"
            limit = CATEGORY_LIMITS.get(category, "unlimited")
            limit_str = f"(target: {limit})" if limit != "unlimited" else ""
            print(f"  {name:35s}: {count:5d} repositories {limit_str}")

        # Data consistency check
        print("\n[Data consistency check]")
        print("-" * 40)
        if len(new_input) == len(new_output):
            print("  ✅ The input and output files contain the same number of repositories")
            print(f"     Input file: {len(new_input)} repositories")
            print(f"     Output file: {len(new_output)} repositories")

            # Verify the index correspondence (by id)
            print("\n  [Correspondence check (by repository ID)]")
            match_count = 0
            mismatch_count = 0
            mismatch_examples = []

            # Loop over indices to avoid the problems of zip
            for i in range(len(new_input)):
                input_id = new_input[i].get('id')
                output_id = new_output[i].get('id')

                # Safe conversion
                input_id_str = str(input_id) if input_id is not None else "None"
                output_id_str = str(output_id) if output_id is not None else "None"

                if input_id_str == output_id_str:
                    match_count += 1
                else:
                    mismatch_count += 1
                    if len(mismatch_examples) < 3:
                        mismatch_examples.append({
                            'index': i,
                            'input_id': input_id_str,
                            'output_id': output_id_str
                        })

            print(f"    Matching repositories: {match_count}")
            print(f"    Mismatching repositories: {mismatch_count}")

            if mismatch_examples:
                for ex in mismatch_examples:
                    print(f"    ⚠️ index {ex['index']}: input id={ex['input_id']}, output id={ex['output_id']} do not match")

            if match_count == len(new_input):
                print("    ✅ Perfect correspondence: the repositories in the input and output files match exactly")
            elif mismatch_count > 0:
                print("    ⚠️ Warning: there are mismatching repositories, please check the data")
        else:
            print("  ❌ Warning: the input and output files contain different numbers of repositories!")
            print(f"     Input file: {len(new_input)} repositories")
            print(f"     Output file: {len(new_output)} repositories")

        print("=" * 80 + "\n")

    def process(self, input_file: str, output_file: str,
                output_prefix: str) -> Tuple[str, str]:
        """
        Main processing flow

        Args:
            input_file: Path to the input file
            output_file: Path to the output file
            output_prefix: Output file prefix

        Returns:
            (new input file path, new output file path)
        """
        logger.info("=" * 60)
        logger.info("Starting to balance repository categories")
        logger.info("=" * 60)
        logger.info(f"Input file: {input_file}")
        logger.info(f"Output file: {output_file}")
        logger.info(f"Output prefix: {output_prefix}")
        logger.info(f"Random selection: {'yes' if RANDOM_SELECTION else 'no'}")
        logger.info(f"Random seed: {RANDOM_SEED if RANDOM_SELECTION else 'not used'}")

        # 1. Load the data
        logger.info("\nLoading data files...")
        try:
            with open(input_file, 'r', encoding='utf-8') as f:
                input_data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load the input file: {e}")
            raise

        try:
            with open(output_file, 'r', encoding='utf-8') as f:
                output_data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load the output file: {e}")
            raise

        # Make sure the data is a list
        if isinstance(input_data, dict):
            if "repos" in input_data:
                input_data = input_data["repos"]
            else:
                input_data = [input_data]

        if isinstance(output_data, dict):
            if "repos" in output_data:
                output_data = output_data["repos"]
            else:
                output_data = [output_data]

        logger.info(f"The original input file contains {len(input_data)} repositories")
        logger.info(f"The original output file contains {len(output_data)} repositories")

        # 2. Verify the correspondence (by id)
        logger.info("\nVerifying the correspondence between the input and output files...")
        if not self.verify_correspondence(input_data, output_data):
            logger.error("The input and output files do not correspond, please check the files!")
            logger.error("Hint: run detect_with_failed.py first to fix the missing repositories")
            sys.exit(1)

        # 3. Select the repository indices to keep (based only on the input file classification)
        logger.info("\nAnalyzing the category distribution and selecting the repositories to keep...")
        keep_indices, removal_stats = self.select_repos_to_keep(input_data)

        # 4. Extract the data by index (preserving the correspondence)
        logger.info(f"\nKeeping {len(keep_indices)} repositories...")
        balanced_input = self.extract_repos_by_indices(input_data, keep_indices)
        balanced_output = self.extract_repos_by_indices(output_data, keep_indices)

        # 5. Generate the output file names
        input_path = Path(input_file)
        output_path = Path(output_file)

        new_input_file = str(input_path.parent / f"{output_prefix}.json")
        new_output_file = str(output_path.parent / f"{output_prefix}_output.json")

        # 6. Save the balanced data
        logger.info("\nSaving the balanced data...")
        self.save_balanced_data(balanced_input, balanced_output, new_input_file, new_output_file)

        # 7. Print statistics
        self.print_statistics(input_data, output_data, keep_indices, removal_stats,
                              balanced_input, balanced_output)

        logger.info("=" * 60)
        logger.info(f"✅ Processing complete!")
        logger.info(f"   New input file: {new_input_file}")
        logger.info(f"   New output file: {new_output_file}")
        logger.info("=" * 60)

        return new_input_file, new_output_file


def main():
    """Main entry point"""
    print("\n" + "=" * 80)
    print("GitHub repository category balancing tool")
    print("=" * 80)
    print(f"Input file: {INPUT_FILE}")
    print(f"Output file: {OUTPUT_FILE}")
    print(f"Output prefix: {OUTPUT_PREFIX}")
    print(f"Random selection: {'yes' if RANDOM_SELECTION else 'no'}")
    if RANDOM_SELECTION and RANDOM_SEED:
        print(f"Random seed: {RANDOM_SEED}")
    print("\nCategory limits:")
    for category, limit in CATEGORY_LIMITS.items():
        name = CATEGORY_PATTERNS.get(category, {}).get("name", category)
        print(f"  {name:35s}: {limit:5d} repositories")
    print("=" * 80 + "\n")

    # Check whether the files exist
    if not os.path.exists(INPUT_FILE):
        logger.error(f"Input file does not exist: {INPUT_FILE}")
        print(f"\n❌ Error: input file '{INPUT_FILE}' does not exist!")
        print("Please edit the INPUT_FILE variable at the top of the script and set the correct input file path.")
        sys.exit(1)

    if not os.path.exists(OUTPUT_FILE):
        logger.error(f"Output file does not exist: {OUTPUT_FILE}")
        print(f"\n❌ Error: output file '{OUTPUT_FILE}' does not exist!")
        print("Please edit the OUTPUT_FILE variable at the top of the script and set the correct output file path.")
        sys.exit(1)

    try:
        # Create the balancer
        balancer = RepoCategoryBalancer(CATEGORY_PATTERNS, RANDOM_SEED if RANDOM_SELECTION else None)

        # Run the balancing
        new_input, new_output = balancer.process(
            input_file=INPUT_FILE,
            output_file=OUTPUT_FILE,
            output_prefix=OUTPUT_PREFIX
        )

        # Simple statistics output
        with open(new_input, 'r', encoding='utf-8') as f:
            input_count = len(json.load(f))
        with open(new_output, 'r', encoding='utf-8') as f:
            output_count = len(json.load(f))

        print(f"\n✅ Processing complete!")
        print(f"   New input file: {new_input} ({input_count} repositories)")
        print(f"   New output file: {new_output} ({output_count} repositories)")

        if input_count == output_count:
            print(f"   ✅ Data consistency: the input and output repository counts match")
        else:
            print(f"   ⚠️ Data consistency: the input and output repository counts do not match!")

    except KeyboardInterrupt:
        logger.info("Operation interrupted by the user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"An error occurred during processing: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()