# Frozen surfaces — do not affect without explicit approval

- dashboard/refresh.py, snapshot_exists_for(), and the data/snapshots/ naming
  convention it depends on
- dashboard/build_dashboard.py's input files: data/normalized/models.json,
  data/normalized/model_benchmarks.json, data/analysis/coverage_report.json,
  data/analysis/data_quality_report.json
- Any new pipeline must write to its own paths, never share a directory name
  or existence-check pattern with the chat pipeline's snapshot/output files.

Before finishing any change, check whether a new file, folder, or existence
check could be mistaken by any of the above for something it's already
watching for.