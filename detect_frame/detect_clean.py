"""
Dataset self-deduplication tool.
Deduplicates repository data within a single JSON file.
"""

import json
import os
from pathlib import Path
from typing import Dict, List, Optional
from collections import defaultdict


def deduplicate_dataset(
        input_file: str,
        dedup_keys: List[str] = None,
        priority_strategy: str = 'best',
        output_file: Optional[str] = None
) -> List[Dict]:
    """
    Deduplicate a dataset against itself.

    Args:
        input_file: Input JSON file path
        dedup_keys: Keys used to identify duplicates; defaults to ['full_name']
        priority_strategy: Deduplication strategy
            - 'first': keep the first occurrence
            - 'latest': keep the record with the latest update time
            - 'best': keep the highest-quality record (description present, more stars, fewer abuse signals)
        output_file: Output file path; if None, a file with a _clean suffix is generated in the same directory

    Returns:
        Deduplicated repository list
    """
    if dedup_keys is None:
        dedup_keys = ['full_name']

    # Load the data
    print(f"Loading file: {input_file}")
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Ensure the data is in list form
    if isinstance(data, dict):
        if "repos" in data:
            repositories = data["repos"]
        else:
            repositories = [data]
    else:
        repositories = data

    original_count = len(repositories)
    print(f"Original record count: {original_count}")

    # Check for duplicates
    key_to_indices = defaultdict(list)
    for idx, repo in enumerate(repositories):
        unique_key = tuple(str(repo.get(key, '')) for key in dedup_keys)
        key_to_indices[unique_key].append(idx)

    duplicate_groups = {k: v for k, v in key_to_indices.items() if len(v) > 1}
    if duplicate_groups:
        print(f"Found {len(duplicate_groups)} groups of duplicate repositories")
        total_duplicates = sum(len(v) - 1 for v in duplicate_groups.values())
        print(f"Duplicate record count: {total_duplicates}")
    else:
        print("No duplicate repositories found")
        total_duplicates = 0

    # Deduplicate
    unique_repos = {}
    for repo in repositories:
        unique_key = tuple(str(repo.get(key, '')) for key in dedup_keys)

        if unique_key not in unique_repos:
            unique_repos[unique_key] = repo
        else:
            existing = unique_repos[unique_key]
            should_replace = False

            if priority_strategy == 'latest':
                existing_time = existing.get('updated_at', '')
                new_time = repo.get('updated_at', '')
                if new_time > existing_time:
                    should_replace = True
            elif priority_strategy == 'best':
                if is_better_repo(repo, existing):
                    should_replace = True

            if should_replace:
                unique_repos[unique_key] = repo

    deduplicated_repos = list(unique_repos.values())
    dedup_count = len(deduplicated_repos)

    print(f"Deduplicated record count: {dedup_count}")
    print(f"Removed duplicates: {original_count - dedup_count}")

    # Determine the output file path
    if output_file is None:
        input_path = Path(input_file)
        output_file = str(input_path.parent / f"{input_path.stem}_clean{input_path.suffix}")

    # Save the result
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(deduplicated_repos, f, ensure_ascii=False, indent=2)

    print(f"Deduplicated data saved to: {output_file}")

    # Generate a report in the detect_pipeline_log folder
    generate_clean_report(
        original_count=original_count,
        dedup_count=dedup_count,
        duplicate_groups=duplicate_groups,
        total_duplicates=total_duplicates,
        input_file=input_file,
        output_file=output_file,
        dedup_keys=dedup_keys,
        priority_strategy=priority_strategy
    )

    return deduplicated_repos


def is_better_repo(repo1: Dict, repo2: Dict) -> bool:
    """
    Determine whether repo1 is of higher quality than repo2.

    Priority order:
    1. Repositories with a description are better
    2. Repositories with more stars are better
    3. Repositories with fewer abuse_count values are better (if present)
    4. More recently updated repositories are better
    """
    # 1. Description
    has_desc1 = bool(repo1.get('description'))
    has_desc2 = bool(repo2.get('description'))
    if has_desc1 != has_desc2:
        return has_desc1

    # 2. Stars
    stars1 = repo1.get('stargazers_count', 0) or 0
    stars2 = repo2.get('stargazers_count', 0) or 0
    if stars1 != stars2:
        return stars1 > stars2

    # 3. Abuse count (if present)
    abuse1 = repo1.get('abuse_count', 0) or 0
    abuse2 = repo2.get('abuse_count', 0) or 0
    if abuse1 != abuse2:
        return abuse1 < abuse2

    # 4. Update time
    time1 = repo1.get('updated_at', '')
    time2 = repo2.get('updated_at', '')
    if time1 != time2:
        return time1 > time2

    return False


def generate_clean_report(original_count: int, dedup_count: int,
                          duplicate_groups: Dict, total_duplicates: int,
                          input_file: str, output_file: str,
                          dedup_keys: List[str], priority_strategy: str):
    """Generate a cleaning report and save it in the detect_pipeline_log folder."""

    # Determine the log directory path
    output_path = Path(output_file)
    log_dir = output_path.parent / "detect_pipeline_log"
    log_dir.mkdir(exist_ok=True)

    # Generate the report file name based on the input file name
    input_path = Path(input_file)
    report_file = log_dir / f"{input_path.stem}_dedup_report.txt"

    # Generate a detailed JSON report
    json_report_file = log_dir / f"{input_path.stem}_dedup_report.json"

    # Console output
    print("\n" + "=" * 60)
    print("Dataset deduplication report")
    print("=" * 60)
    print(f"Input file: {input_file}")
    print(f"Output file: {output_file}")
    print(f"Original record count: {original_count}")
    print(f"Deduplicated record count: {dedup_count}")
    print(f"Removed duplicates: {original_count - dedup_count}")
    print(f"Duplicate group count: {len(duplicate_groups)}")
    print(f"Deduplication keys: {', '.join(dedup_keys)}")
    print(f"Priority strategy: {priority_strategy}")
    print(f"\nReport location: {log_dir}")

    # Save the text report
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write("=" * 60 + "\n")
        f.write("Dataset deduplication detailed report\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"Generation time: {Path(output_file).stat().st_mtime}\n")
        f.write(f"Input file: {input_file}\n")
        f.write(f"Output file: {output_file}\n")
        f.write(f"Deduplication keys: {', '.join(dedup_keys)}\n")
        f.write(f"Priority strategy: {priority_strategy}\n\n")
        f.write(f"Original record count: {original_count}\n")
        f.write(f"Deduplicated record count: {dedup_count}\n")
        f.write(f"Removed duplicates: {original_count - dedup_count}\n")
        f.write(f"Duplicate group count: {len(duplicate_groups)}\n\n")

        if duplicate_groups:
            f.write("Duplicate repository details:\n")
            f.write("-" * 40 + "\n")
            for i, (key_tuple, indices) in enumerate(duplicate_groups.items(), 1):
                # Format the duplicate key for display
                if len(key_tuple) == 1:
                    key_display = key_tuple[0]
                else:
                    key_display = " | ".join(key_tuple)

                f.write(f"\n{i}. Duplicate key: {key_display}\n")
                f.write(f"   Occurrences: {len(indices)}\n")
                f.write(f"   Index positions: {indices}\n")

                # If abuse_count information is present, show the versions
                if len(indices) <= 5:  # Only show the first 5 duplicate details
                    for idx in indices[:5]:
                        repo = None
                        # Reload data to obtain detailed information (simplified here)
                        pass

    print(f"Text report saved to: {report_file}")

    # Save the JSON report (easier for programmatic processing)
    json_report = {
        "input_file": input_file,
        "output_file": output_file,
        "timestamp": Path(output_file).stat().st_mtime,
        "dedup_keys": dedup_keys,
        "priority_strategy": priority_strategy,
        "statistics": {
            "original_count": original_count,
            "dedup_count": dedup_count,
            "removed_count": original_count - dedup_count,
            "duplicate_groups": len(duplicate_groups),
            "total_duplicates": total_duplicates
        },
        "duplicate_groups": [
            {
                "key": list(key_tuple) if isinstance(key_tuple, tuple) else [key_tuple],
                "indices": indices,
                "count": len(indices)
            }
            for key_tuple, indices in duplicate_groups.items()
        ]
    }

    with open(json_report_file, 'w', encoding='utf-8') as f:
        json.dump(json_report, f, ensure_ascii=False, indent=2)

    print(f"JSON report saved to: {json_report_file}")
    print("=" * 60)


# ==================== Main program ====================

if __name__ == "__main__":
    # ===== Specify the file to process here =====
    INPUT_FILE = "trial_small_3_output.json"  # Change to your file name

    # Optional configuration
    DEDUP_KEYS = ['full_name']  # Fields used for deduplication
    PRIORITY_STRATEGY = 'best'  # 'first', 'latest', 'best'
    # ================================

    print("=" * 60)
    print("GitHub dataset self-deduplication tool")
    print("=" * 60)

    # Check whether the file exists
    if not Path(INPUT_FILE).exists():
        print(f"❌ File does not exist: {INPUT_FILE}")
        print("Please update the INPUT_FILE variable in the code to a valid file path")
        exit(1)

    # Execute deduplication
    deduplicated = deduplicate_dataset(
        input_file=INPUT_FILE,
        dedup_keys=DEDUP_KEYS,
        priority_strategy=PRIORITY_STRATEGY
    )

    print("\n✅ Deduplication complete!")