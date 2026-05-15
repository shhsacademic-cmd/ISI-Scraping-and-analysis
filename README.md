diff --git a/README.md b/README.md
new file mode 100644
index 0000000000000000000000000000000000000000..14b7d909483aa041b01d91f06035574b9b8a8bee
--- /dev/null
+++ b/README.md
@@ -0,0 +1,29 @@
+## ISI report analyzer
+
+### Required dependencies
+
+Install Python 3.10+ and these packages:
+
+```bash
+pip install requests beautifulsoup4 pypdf
+```
+
+### How to run
+
+From the repository root:
+
+```bash
+python scripts/isi_report_analyzer.py
+```
+
+Optional flags:
+
+- `--delay` (seconds between requests; default `0.5`)
+- `--timeout` (per-request timeout in seconds; default `25`)
+- `--max-retries` (default `4`)
+- `--log-level` (`DEBUG`, `INFO`, `WARNING`, `ERROR`)
+
+### Output locations
+
+- Downloaded report PDFs: `data/reports/<school_slug>/`
+- Phrase analysis CSV: `output/significant_strength_results.csv`
