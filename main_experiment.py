import asyncio
import argparse
import logging
import os
import re
import aiohttp
import time
from urllib.parse import urlparse, urljoin
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from collections import deque

from state_manager import StateManager
from navigator import Navigator
from llm_engine import LLMEngine
from downloader import download_files_concurrently
# --- 1. ENABLE LOGGING (This fixes the silent failure) ---
os.makedirs("./state", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(), # Print to terminal
        logging.FileHandler("./state/execution_trace.log", encoding="utf-8") # Save exact execution
    ]
)

# Configuration for specific domains [Tailored Prompting]
SITE_HINTS = {
    "rfc-editor.org": "Focus on finding the 'PDF' version of the RFC. Ignore 'Plain Text' if PDF is available.",
    "arxiv.org": "Focus on finding the 'PDF' download link in the right-hand sidebar of abstract pages.",
    "default": "Find direct download links for PDF, DOCX, or XLSX files."
    # "default": "Focus on navigating to other sub-domains of the site provided and try and investigate if there are any documents available."
}

async def offline_html_parser_and_downloader(sm, start_url):
    """Parses all saved HTML files to find and download any missed file links."""
    logging.info("--- Starting Offline HTML Parser Safety Net ---")
    
    all_found_links = set()
    download_dir = "./downloads"
    os.makedirs(download_dir, exist_ok=True)
    
    # sm.html_map contains url -> local_file_path mapping
    for source_url, filepath in sm.html_map.items():
        if not os.path.exists(filepath):
            continue
            
        with open(filepath, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f, 'html.parser')
            
            for a_tag in soup.find_all('a', href=True):
                href = a_tag.get('href')
                if not href:
                    continue
                
                full_url = urljoin(source_url, href)
                path = urlparse(full_url).path.lower()
                
                if any(path.endswith(ext) for ext in [".pdf", ".docx", ".xlsx", ".csv"]):

                    
                    # Check if already exists 
                    local_name = re.sub(r'[\\/*?:"<>|]', "_", urlparse(full_url).path.split('/')[-1])
                    if not local_name: local_name = "offline_file"
                    path = os.path.join(download_dir, local_name)
                    if not os.path.exists(path):
                        all_found_links.add(full_url)
    
    if not all_found_links:
        logging.info("Offline parser found no new file links to download.")
        return

    logging.info(f"Offline parser found a total of {len(all_found_links)} unique, missing file links. Starting download...")
    await download_files_concurrently(list(all_found_links), "offline_parser", download_dir, sm)
    logging.info("--- Offline HTML Parser Safety Net Finished ---")

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true", help="Resume from last checkpoint")
    # parser.add_argument("--api_key", type=str, default=None, help="Gemini API Key")
    args = parser.parse_args()
    
    # Load environment variables from .env file
    load_dotenv()
    
    api_key = os.getenv("API_KEY") or os.getenv("GEMINI_API_KEY")
    # 1. Initialize Components
    sm = StateManager()
    nav = Navigator(headless=True)
    # llm = LLMEngine(api_key=args.api_key, mock_mode=(args.api_key is None))
    llm = LLMEngine(api_key=api_key, mock_mode=False)

    # 2. Initialize Action History (Short-term memory to prevent loops)
    # Using maxlen=5 ensures we only keep the most recent context
    action_history = deque(maxlen=5) 

    # 3. State Loading Logic
    seed_url = "https://dspace.mit.edu"
    base_domain = urlparse(seed_url).netloc
    
    if args.resume and sm.load_state():
        logging.info("Resuming crawl from saved state...")
    else:
        logging.info("Starting fresh crawl...")
        # Add seed URL only if starting fresh
        if not sm.queue:
            sm.queue.append(seed_url)

    # 4. Main Autonomous Loop
    async with aiohttp.ClientSession() as session:
        while sm.queue and sm.metrics["pages_crawled"] < 100:
            url = sm.queue.popleft()
            
            if url in sm.visited:
                continue
            
            logging.info(f"--- Processing: {url} ---")
            
            # Step A: Observation (Visit & Snapshot)
            html, file_path = await nav.visit_url(url)
            if not html:
                logging.warning(f"Failed to load {url}. Skipping.")
                continue 
                
            sm.html_map[url] = file_path
            start_time = time.time()
            
            # --- ALWAYS-ON TRIAGE ---
            logging.info("Running Always-On File Triage...")
            all_links = nav.get_links(url)
            docs = [l for l in all_links if any(urlparse(l).path.lower().endswith(ext) for ext in [".pdf", ".docx", ".xlsx", ".csv"])]
            if docs:
                logging.info(f"Triage found {len(docs)} documents on this page.")
                await download_files_concurrently(docs, url, "./downloads", sm)
                
            # --- Secondary Check for Potential Endpoints ---
            from downloader import verify_pdf_endpoint
            potential_endpoints = [
                l for l in all_links 
                if not any(urlparse(l).path.lower().endswith(ext) for ext in [".pdf", ".docx", ".xlsx", ".csv"]) 
                and any(kw in l.lower() for kw in ['/pdf/', '/download/', '/fetch/', '/bitstream/', '/item/'])
            ]
            
            sniffed_docs = []
            for ep in potential_endpoints:
                if verify_pdf_endpoint(ep):
                    sniffed_docs.append(ep)
                    
            if sniffed_docs:
                logging.info(f"MIME Sniffing found {len(sniffed_docs)} PDF endpoints.")
                await download_files_concurrently(
                    sniffed_docs, url, "./downloads", sm, force_extension=".pdf", discovery_type="mime_sniff"
                )
            
            # Step B: Decision (History-Aware)
            domain_hint = next((hint for domain, hint in SITE_HINTS.items() if domain in url), SITE_HINTS["default"])
            
            # We pass the memory (action_history) to the LLM so it knows where it has been
            action, usage = await llm.decide_action(html, url, list(action_history), site_hint=domain_hint)
            
            # Record tokens
            sm.metrics["total_tokens"] += usage.get("total_tokens", 0)
            sm.metrics["prompt_tokens"] += usage.get("prompt_tokens", 0)
            sm.metrics["completion_tokens"] += usage.get("completion_tokens", 0)
            
            investigate_selectors = action.get("specific_subpages", [])
            next_page_selector = action.get("next_page")
            reason = action.get("reason", "No reason provided")
            
            elapsed = time.time() - start_time
            logging.info(f"AI Decision: Navigation Plan | Reason: {reason} | Time: {elapsed:.2f}s")
            
            # Update history for the NEXT loop iteration
            num_inv = len(investigate_selectors)
            num_np = 1 if next_page_selector else 0
            action_history.append(f"URL: {url} -> AI planned {num_inv} investigations and {num_np} next page.")

            # Step C: Execution (Discovery & Navigation)
            success = False
            new_urls_found = 0
            
            def process_link(link_str, priority=False):
                nonlocal new_urls_found
                if not link_str or not isinstance(link_str, str): return False
                
                target_url = urljoin(url, link_str.strip())
                
                # Edge case check: DO NOT add known files to the navigation queue!
                if any(urlparse(target_url).path.lower().endswith(ext) for ext in [".pdf", ".docx", ".xlsx", ".csv"]):
                    logging.warning(f"Blocked document URL from navigation queue: {target_url}")
                    return False
                    
                if target_url not in sm.visited and target_url not in sm.queue:
                    if urlparse(target_url).netloc == base_domain:
                        if priority:
                            sm.queue.appendleft(target_url)
                        else:
                            sm.queue.append(target_url)
                        new_urls_found += 1
                        return True
                    else:
                        logging.warning(f"Domain Confinement: Blocked external link {target_url}")
                return False

            # Process Next Page FIRST (so it goes deeper into the left side of the queue)
            if next_page_selector:
                if process_link(next_page_selector, priority=True):
                    success = True

            # Process Specific Subpages SECOND (in reverse, so the first subpage is at the very front of the queue)
            for link in reversed(investigate_selectors):
                if process_link(link, priority=True):
                    success = True

            logging.info(f"Added {new_urls_found} new target pages to queue.")
            
            action_type = "NAVIGATION" if new_urls_found > 0 else "FINISH"

            if new_urls_found == 0:
                logging.info(f"Agent finished at {url}. Reason: No valid navigation links found or generated.")
                success = True # Allow checkpoint logic below to run for completion

            # Step D: Finalize & Checkpoint [Fault Tolerance Fix]
            if success:
                sm.visited.add(url)
                sm.metrics["pages_crawled"] += 1
                sm.log_action(url, action_type, "SUCCESS")
                
                # Save state every 5 pages to prevent data loss on crash
                if sm.metrics["pages_crawled"] % 5 == 0:
                    sm.save_state()
                    sm.log_metrics_snapshot()

    # Final Save and Cleanup
    sm.save_state()
    nav.close()
    
    # Offline parser sequence
    await offline_html_parser_and_downloader(sm, seed_url)
    
    logging.info("Crawl session complete.")

if __name__ == "__main__":
    asyncio.run(main())