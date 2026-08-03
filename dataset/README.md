# Released datasets

The repository and developer data on which the reported study is computed: the collected repository
pool, the scanning corpus with the verdicts SPREAD produced on it, the labeled sets used to
calibrate and evaluate the framework, and the account metrics behind the user analysis.

The four JSON datasets are stored gzip-compressed, since their expanded form amounts to 228 MB and
one of them exceeds GitHub's 100 MB per-file limit for regular Git objects.

| File | Expands to | Compressed | Expanded |
| --- | --- | ---: | ---: |
| `source_github_repos.json.gz` | `source_github_repos.json` | 15.2 MB | 133 MB |
| `experiment_input.json.gz` | `experiment_input.json` | 3.5 MB | 29 MB |
| `experiment_output.json.gz` | `experiment_output.json` | 5.9 MB | 51 MB |
| `user_characteristics.json.gz` | `user_characteristics.json` | 1.2 MB | 15 MB |
| `threshold_calibration.csv` | — | 24 KB | — |
| `baseline_detection_output.csv` | — | 42 KB | — |
| `detection_verify_abuse.csv` | — | 20 KB | — |
| `detection_verify_benign.csv` | — | 6.4 KB | — |

## Contents

- **`source_github_repos.json`** — the collected repository pool, including the 13,368 Trending,
  13,503 Companion and 12,136 General repositories of the study. Each record carries the full
  repository object, its `source` provenance and the `core_developers` list.
- **`experiment_input.json`** — the scanning corpus drawn from that pool, 4,000 repositories per
  group. Input of `detect_frame/github_abuse_detector_pipeline.py`.
- **`experiment_output.json`** — the same records extended with `abuse_count`, `abuse_categories`
  and `abuse_details`, the last holding, per detector, the verdict and the observations that
  produced it. Output of the pipeline after `detect_postprocess.py`.
- **`user_characteristics.json`** — per-account metrics, including the 56,735 accounts profiled in
  RQ4: `account_age_days`, `public_repos_count`, `total_stars`, `total_forks`, `followers`,
  `following`, and `commits_2024_2026`, `issues_2024_2026`, `prs_2024_2026` measured over
  Jan. 1, 2024 – Jan. 1, 2026.
- **`threshold_calibration.csv`** — the 300 manually labeled cases (149 abuse / 151 benign) over
  which the thresholds were calibrated. Disjoint from the benchmark below.
- **`baseline_detection_output.csv`** — the labeled benchmark of 460 instances (230 abuse / 230
  benign), with the manual label `m_label` and the framework verdict `detect_label`.
- **`detection_verify_abuse.csv`** / **`detection_verify_benign.csv`** — the 221 flagged-abuse cases
  and 221 matched benign controls inspected manually by two experts, with the adjudicated outcome in
  `verify`.

The JSON datasets are released as collected, so they cover more than the study analyzes: the paper
works on a fixed subset and excludes suspended, deleted and bot accounts.

## Extraction

Run from this directory; each archive expands next to itself:

```bash
cd dataset
gunzip -k *.json.gz
```

The expanded copies are deliberately excluded from version control; see `../.gitignore`.

## Integrity

`SHA256SUMS` covers every file in this directory:

```bash
cd dataset
sha256sum -c SHA256SUMS
```

## Provenance

All records were obtained from public GitHub endpoints through the official REST and GraphQL APIs
between Mar. 1, 2025 and May. 1, 2026. Since repositories are subsequently deleted or made private
and accounts suspended, these snapshots are the authoritative reference for the reported numbers and
cannot be reconstructed by a later crawl.
