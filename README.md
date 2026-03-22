# Autonomous Document Scraping Agent (v2.0)

An intelligent, autonomous web-crawling agent built explicitly for discovering and downloading documents (PDFs, DOCXs, CSVs, XLSXs) from complex, deeply nested websites without rigid manual rules. Powered by the Gemini Generative AI model, it acts as a smart navigator capable of solving web hierarchies, bypassing dead-ends, and pushing links to a polite, highly-concurrent downloading engine.

## 🏗️ Architecture: The Two-Tiered System

The core innovation of v2.0 is the complete division of labor between deterministic Python script logic and probabilistic AI decision-making. 

### 1. The Python Script: "Always-On File Triage" (Downloading)
We **do not** waste expensive LLM-context asking the AI if it sees `.pdf` links. Instead, milliseconds after the browser visits a new page, the Python script instantly scans the raw DOM for any known document extensions. If files are found, they are executed immediately via a concurrent downloading pipeline. 

### 2. The LLM Engine: "The Navigation Engine" (Crawling)
Because the "Always-On Triage" passively guarantees every document on a page is collected securely, the AI is freed up to act purely as a navigator. The agent converts the raw HTML `<a>` tags into readable Markdown loops and sends it to Gemini. Gemini analyzes the context and returns a JSON payload containing the most promising exact URLs to explore next—dedicating 100% of its intelligence to interpreting pagination ("Next Page" buttons) and identifying branching report hierarchies.

### 3. The Offline Downloader (The Safety Net)
At the very end of every single crawl session, an offline HTML parser sweeps through all locally saved `.html` snapshots taken during the run. This acts as a final safety net to comb the raw HTML one last time, catching any obscure file links that might have been dropped due to live connection issues.

## 🚀 Key Features

- **Fault-Tolerant Resumability:** The StateManager frequently checkpoints `sm.visited`, the active `sm.queue`, and current metrics into JSON. If the crawler is terminated, running `python main_experiment.py --resume` instantly recovers the exact state without losing a single URL in the queue or attempting to re-download an existing file.
- **Hardcoded Domain Confinement:** The crawler's queue safely filters out links that don't match the seed URL's base `netloc`, preventing the AI from wandering into external sites.
- **SPA & Fragment Loop Prevention:** Automatically parses and strips `#` DOM anchors to prevent infinite crawling loops on Single Page Applications.
- **Robust Concurrent Politeness (`downloader.py`):** Utilizes rotated `USER_AGENTS`, limits simultaneous downloads via `asyncio.Semaphore(10)`, and includes an exponential backoff retry loop for stable downloading without tripping IP bans.
- **LLM Safety Filter Overrides:** Gemini safety settings are completely minimized to `BLOCK_NONE` to prevent the crawler from instantly halting when summarizing or reading sensitive research papers context.

## 📈 Real-World Verification

The architecture has been thoroughly tested, easily completing maxed-out 100-page crawl iterations on dense research repositories like `arxiv.org`.

**Live Terminal Verification:**
```bash
2026-03-23 00:24:06,643 - INFO - State saved successfully. Pages: 100
2026-03-23 00:24:08,856 - INFO - --- Starting Offline HTML Parser Safety Net ---
2026-03-23 00:24:09,934 - INFO - Crawl session complete.

# Testing Resumability from the terminal
(scraper_agent) PS C:\Users\atulc\Desktop\Scraper agent research paper> python main_experiment.py --resume
2026-03-23 00:26:08,783 - INFO - Resumed state: 100 pages visited, 2069 in queue.
2026-03-23 00:26:08,784 - INFO - Resuming crawl from saved state...
```
*(The above successfully demonstrates that checkpoints hold state perfectly, retaining thousands of queued navigation targets across program closures without data loss.)*
