# Collection inputs

The datasets consumed by the collectors in the parent directory. Together they define
the sampling frame of the study: the collectors read them from the layout recreated by
the archives below, so the repository selection can be reproduced rather than merely
described.

The inputs are stored compressed because their expanded form amounts to 596 MB across
roughly 51,000 files, and one of them exceeds GitHub's 100 MB per-file limit for regular
Git objects.

| Archive | Expands to | Compressed | Expanded | Consumed by |
| --- | --- | ---: | ---: | --- |
| `trending-archive.tar.gz` | `../archive/` | 10.0 MB | 63 MB, 49,449 files | `trending_2022.py`, `trending_2024.py`, `trending_2025.py` |
| `github-collections.tar.gz` | `../collections/` | 2.0 MB | 2.4 MB, 163 files | `collections.py` |
| `github-topics.tar.gz` | `../topics/` | 14.6 MB | 18 MB, 1,883 files | `topics.py` |
| `ossf-scorecard-20260316.tar.gz` | `../OpenSSF-scorecard/` | 12.8 MB | 399 MB, 10 files | `ossf_scorecard.py` |
| `ossf-criticality-score-2025.07.25.txt.gz` | `../2025.07.25_010355_all.txt` | 30.7 MB | 114 MB | `ossf_criticality.py` |

## Extraction

Run from the `data_collect_repo/` directory; every archive recreates the path the
collectors expect:

```bash
cd data_collect_repo
for a in datasets/*.tar.gz; do tar -xzf "$a"; done
gunzip -c datasets/ossf-criticality-score-2025.07.25.txt.gz > 2025.07.25_010355_all.txt
```

The expanded copies are deliberately excluded from version control; see `../.gitignore`.

## Integrity

`SHA256SUMS` covers every archive in this directory:

```bash
cd data_collect_repo/datasets
sha256sum -c SHA256SUMS
```

## Provenance

`ossf-scorecard-20260316.tar.gz` is an export of the public OpenSSF Scorecard BigQuery
table taken on 2026-03-16 and can be regenerated with `../get_scorecard.py`.
`ossf-criticality-score-2025.07.25.txt.gz` is the public OSSF Criticality Score release
of 2025-07-25. The trending, topics and collections snapshots were captured from the
corresponding GitHub pages; they are point-in-time observations that a later crawl
cannot reconstruct, which is why they are archived here rather than re-fetched.
