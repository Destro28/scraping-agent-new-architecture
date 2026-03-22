import os
import time
import asyncio
import logging
import json
import re
import hashlib
import tempfile
import shutil
import uuid
import random
from collections import deque
from urllib.parse import urlparse, urljoin

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException
import aiohttp
import aiofiles
from bs4 import BeautifulSoup
from langchain_google_genai import ChatGoogleGenerativeAI
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import nest_asyncio

# Apply the patch for asyncio on Windows if needed
nest_asyncio.apply()

# ==============================================================================
# CONFIGURATION
# ==============================================================================
API_KEY = "need a new api key for gemini, remind yourself later" # Replace with your actual Gemini API key
if not API_KEY:
    raise ValueError("GEMINI_API_KEY is not set.")

# --- v1.03 File Paths ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads-v1.03")
HTML_LOG_DIR = os.path.join(BASE_DIR, "html_logs-v1.03")
RUN_LOG_FILE = os.path.join(BASE_DIR, "run_log-v1.03.csv")
DOWNLOAD_LOG_FILE = os.path.join(BASE_DIR, "download_log-v1.03.csv")
URL_MAP_FILE = os.path.join(BASE_DIR, "url_map-v1.03.json")
# --- NEW: State file for resumable crawls ---
AGENT_STATE_FILE = os.path.join(BASE_DIR, "agent_state-v1.03.json")


FILE_TYPES = ['.pdf', '.docx', '.xlsx', '.pptx']
START_URLS = ["https://www.rfc-editor.org/info/rfc9566"] # Example start URL
MAX_AGENT_STEPS = 100
LOG_LEVEL = logging.INFO

# --- Robustness & Politeness Configuration ---
MAX_DOWNLOAD_RETRIES = 3
CONCURRENT_DOWNLOAD_LIMIT = 10
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(CONCURRENT_DOWNLOAD_LIMIT)
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:126.0) Gecko/20100101 Firefox/126.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0',
]

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(HTML_LOG_DIR, exist_ok=True)

# ==============================================================================
# LOGGER SETUP
# ==============================================================================
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=LOG_LEVEL,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

run_log_handler = logging.FileHandler(RUN_LOG_FILE, encoding='utf-8')
run_log_handler.setFormatter(logging.Formatter("%(asctime)s,%(message)s"))
run_logger = logging.getLogger('RunLog')


download_log_handler = logging.FileHandler(DOWNLOAD_LOG_FILE, encoding='utf-8')
download_log_handler.setFormatter(logging.Formatter("%(asctime)s,%(message)s"))
download_logger = logging.getLogger('DownloadLog')
download_logger.addHandler(download_log_handler)
download_logger.setLevel(logging.INFO)

if not os.path.exists(RUN_LOG_FILE) or os.path.getsize(RUN_LOG_FILE) == 0:
    run_logger.info("timestamp,url,action,selector,reason,status,files_found")
if not os.path.exists(DOWNLOAD_LOG_FILE) or os.path.getsize(DOWNLOAD_LOG_FILE) == 0:
    download_logger.info("timestamp,file_url,source_url,status")

# ==============================================================================
# AUTONOMOUS AGENT CLASS
# ==============================================================================
class AutonomousAgent:
    """An AI-powered agent that navigates websites to find and download documents."""

    def __init__(self, start_urls, file_types, api_key):
        self.start_urls = start_urls
        self.base_domain = urlparse(start_urls[0]).netloc if start_urls else None
        self.file_types = file_types

        self.llm = ChatGoogleGenerativeAI(
            model="gemini-1.5-flash",
            google_api_key=api_key,
            temperature=0.0,
            safety_settings={
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            }
        )
        
        self.user_data_dir = None
        self.driver = self._init_driver()
        
        self.user_agent = random.choice(USER_AGENTS)
        logger.info(f"Session User-Agent: {self.user_agent}")
        headers = {'User-Agent': self.user_agent}
        self.session = aiohttp.ClientSession(headers=headers)

        self.visited_urls = set()
        self.processed_for_files = set()
        self.action_history = []
        self.url_map = self._load_url_map()

    def _init_driver(self):
        """Initializes the Selenium WebDriver."""
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--disable-gpu")
        options.add_experimental_option('excludeSwitches', ['enable-logging'])
        
        self.user_data_dir = os.path.join(tempfile.gettempdir(), f"selenium_user_data_{uuid.uuid4()}")
        logger.info(f"Using temporary user data directory: {self.user_data_dir}")
        options.add_argument(f"--user-data-dir={self.user_data_dir}")

        return webdriver.Chrome(options=options)

    def _load_url_map(self):
        if os.path.exists(URL_MAP_FILE):
            with open(URL_MAP_FILE, 'r') as f:
                try:
                    return json.load(f)
                except json.JSONDecodeError:
                    return {}
        return {}

    def _save_url_map(self):
        with open(URL_MAP_FILE, 'w') as f:
            json.dump(self.url_map, f, indent=4)

    # --- NEW: Methods for saving and loading agent state ---
    async def _save_state(self, queue, steps):
        """Saves the current crawling state to a file."""
        state = {
            'queue': list(queue),
            'visited_urls': list(self.visited_urls),
            'steps': steps
        }
        try:
            async with aiofiles.open(AGENT_STATE_FILE, 'w') as f:
                await f.write(json.dumps(state, indent=4))
            logger.debug(f"Saved agent state. Queue size: {len(queue)}, Steps: {steps}")
        except Exception as e:
            logger.error(f"Failed to save agent state: {e}")


    def _load_state(self):
        """Loads the crawling state from a file if it exists."""
        if os.path.exists(AGENT_STATE_FILE):
            try:
                with open(AGENT_STATE_FILE, 'r') as f:
                    state = json.load(f)
                    queue = deque(state.get('queue', []))
                    visited = set(state.get('visited_urls', []))
                    steps = state.get('steps', 0)
                    logger.info(f"Resuming agent from saved state. Queue size: {len(queue)}, Steps: {steps}")
                    return queue, visited, steps
            except (json.JSONDecodeError, FileNotFoundError):
                logger.warning("Could not load state file, starting fresh.")
        return deque(self.start_urls), set(), 0


    async def _get_page_state(self, url):
        """Navigates to a URL and returns the page's HTML content."""
        logger.info(f"Navigating to: {url}")
        self.driver.get(url)
        self.visited_urls.add(url)
        
        try:
            WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            logger.warning(f"Page body did not load within 20 seconds for {url}, proceeding with caution.")
        
        html = self.driver.page_source
        
        url_hash = hashlib.md5(url.encode()).hexdigest()
        filepath = os.path.join(HTML_LOG_DIR, f"{url_hash}.html")
        async with aiofiles.open(filepath, 'w', encoding='utf-8') as f:
            await f.write(html)
        
        self.url_map[url_hash] = url
        self._save_url_map()
            
        return html

    async def _decide_next_action(self, html, current_url, history):
        """Uses the LLM to decide the next action based on the page state."""
        file_extensions_str = ", ".join(self.file_types)
        history_str = "\n".join([f"- {h}" for h in history]) if history else "None"

        prompt = (
            "You are an autonomous web scraping agent. Your goal is to find all documents of specified types. "
            "Follow this Standard Operating Procedure:\n"
            "1.  **Analyze Structure:** First, analyze the page's purpose. Is it a main navigation page, a list of items, or a detail page?\n"
            "2.  **Prioritize General Navigation:** If you see a primary navigation menu (`<nav>`), `CLICK` the most relevant general link (e.g., 'Downloads', 'Publications', 'Reports').\n"
            "3.  **Investigate Specific Items:** If the page contains a list of specific items (e.g., a list of chapters, a table of reports), use `INVESTIGATE_AND_DOWNLOAD` on the most promising link. This tool will click the link, find the real download on the next page, and return automatically.\n"
            "4.  **Scan for Direct Links:** Only use `SCAN_FOR_FILES` if you are certain the current page itself contains direct download links (e.g., links ending in .pdf).\n"
            "5.  **Finish as a Last Resort:** Only use `FINISH` if the page is a dead end.\n\n"
            f"**Current State:**\n- URL: {current_url}\n- Goal: Find files of type {file_extensions_str}\n- Recent Actions (do not repeat): {history_str}\n\n"
            f"**Page HTML (first 35k chars):**\n```html\n{html[:35000]}\n```\n\n"
            "**Choose your next single action from the tool list below. Your response must be a single, valid JSON object.**\n"
            "**Tools:**\n"
            "1. `CLICK(selector, reason)`: To click a general navigation link (like 'Next Page' or 'Library').\n"
            "2. `INVESTIGATE_AND_DOWNLOAD(selector, description)`: To click a link that leads to a specific item's download page (e.g., a chapter or a report title).\n"
            "3. `SCAN_FOR_FILES(reason)`: If you see direct file links on this page right now.\n"
            "4. `FINISH(reason)`: If there are no more promising actions on this page."
        )

        try:
            response = await self.llm.ainvoke(prompt)
            match = re.search(r'```json\s*(\{.*?\})\s*```', response.content, re.DOTALL)
            json_str = match.group(1) if match else response.content
            action_json = json.loads(json_str)
            logger.info(f"AI Action Decision: {action_json}")
            return action_json
        except Exception as e:
            logger.error(f"Failed to get a valid action from LLM: {e}")
            return {"tool": "FINISH", "reason": "LLM failed to provide a valid action."}

    async def _execute_action(self, action):
        """Executes the action decided by the LLM."""
        tool = action.get("tool")
        selector = action.get("selector")
        reason = action.get("reason", "No reason provided.")
        description = action.get("description", "No description provided.")
        current_url = self.driver.current_url
        
        self.action_history.append(f"{tool}: {selector or description}" if selector or description else tool)
        if len(self.action_history) > 5: self.action_history.pop(0)

        run_logger.info(f'"{current_url}","{tool}","{selector or description}","{reason}","PENDING",0')

        if tool == "CLICK":
            try:
                wait = WebDriverWait(self.driver, 10)
                element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                self.driver.execute_script("arguments[0].click();", element)
                logger.info(f"Successfully executed CLICK on '{selector}'")
                run_logger.info(f'"{current_url}","{tool}","{selector}","{reason}","SUCCESS",0')
                await asyncio.sleep(3) # Wait for page to potentially load
                return True
            except Exception as e:
                logger.error(f"Failed to execute CLICK on '{selector}': {e}")
                run_logger.info(f'"{current_url}","{tool}","{selector}","{reason}","FAILURE",0')
                return False

        elif tool == "INVESTIGATE_AND_DOWNLOAD":
            try:
                wait = WebDriverWait(self.driver, 10)
                element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                
                target_url = urljoin(current_url, element.get_attribute('href'))
                
                self.driver.execute_script("arguments[0].click();", element)
                await asyncio.sleep(3) 

                if len(self.driver.window_handles) > 1:
                    self.driver.switch_to.window(self.driver.window_handles[-1])
                    target_url = self.driver.current_url

                logger.info(f"Investigating {target_url} for download links...")
                
                file_links = get_file_links_from_page(self.driver, self.file_types, target_url)
                if file_links:
                    logger.info(f"Found {len(file_links)} files on investigation page. Downloading...")
                    await download_files_concurrently(self.session, file_links, target_url, DOWNLOAD_SEMAPHORE)
                    run_logger.info(f'"{current_url}","{tool}","{description}","{reason}","SUCCESS",{len(file_links)}')
                else:
                    logger.warning(f"No download links found after investigating {target_url}")
                    run_logger.info(f'"{current_url}","{tool}","{description}","{reason}","SUCCESS",0')

                if len(self.driver.window_handles) > 1:
                    self.driver.close()
                    self.driver.switch_to.window(self.driver.window_handles[0])
                else:
                    self.driver.back()
                
                await asyncio.sleep(2)
                return True

            except Exception as e:
                logger.error(f"Failed to execute INVESTIGATE on '{selector}': {e}")
                run_logger.info(f'"{current_url}","{tool}","{description}","{reason}","FAILURE",0')
                if self.driver.current_url != current_url:
                    self.driver.back()
                return False

        elif tool == "SCAN_FOR_FILES":
            if current_url in self.processed_for_files:
                logger.info("Already scanned this URL for files. Skipping.")
                return True
            
            logger.info("Executing SCAN_FOR_FILES...")
            self.processed_for_files.add(current_url)
            
            links_to_download = get_file_links_from_page(self.driver, self.file_types, current_url)

            if links_to_download:
                logger.info(f"Found {len(links_to_download)} potential files to download.")
                await download_files_concurrently(self.session, links_to_download, current_url, DOWNLOAD_SEMAPHORE)
                run_logger.info(f'"{current_url}","{tool}","N/A","{reason}","SUCCESS",{len(links_to_download)}')
            else:
                logger.info("No files of specified types found on this page.")
                run_logger.info(f'"{current_url}","{tool}","N/A","{reason}","SUCCESS",0')
            return True

        elif tool == "FINISH":
            logger.info(f"Agent decided to FINISH. Reason: {reason}")
            run_logger.info(f'"{current_url}","{tool}","N/A","{reason}","COMPLETE",0')
            return True 
            
        return False

    async def run(self):
        """Main execution loop for the agent."""
        queue, self.visited_urls, steps = self._load_state()

        while queue and steps < MAX_AGENT_STEPS:
            current_url = queue.popleft()
            if current_url in self.visited_urls: continue
            
            try:
                if urlparse(current_url).netloc != self.base_domain:
                    logger.warning(f"Skipping URL from different domain: {current_url}")
                    continue

                html = await self._get_page_state(current_url)
                action = await self._decide_next_action(html, current_url, self.action_history)
                await self._execute_action(action)
                
                all_links = self.driver.find_elements(By.TAG_NAME, 'a')
                logger.info(f"Found {len(all_links)} links on page. Performing triage...")
                
                nav_queue = deque()
                file_queue = set()

                base_current_url = current_url.split('#')[0]

                for link in all_links:
                    try:
                        href = link.get_attribute('href')
                        if not href: continue
                        
                        abs_href = urljoin(current_url, href)
                        
                        base_abs_href = abs_href.split('#')[0]
                        if base_abs_href == base_current_url and '#' in abs_href:
                            logger.debug(f"Skipping on-page SPA link: {abs_href}")
                            continue

                        if any(abs_href.lower().endswith(ft) for ft in self.file_types):
                            if abs_href not in self.processed_for_files:
                                file_queue.add(abs_href)
                        elif urlparse(abs_href).netloc == self.base_domain:
                            if abs_href not in self.visited_urls and abs_href not in queue:
                                nav_queue.append(abs_href)
                    except StaleElementReferenceException:
                        continue
                
                if file_queue:
                    logger.info(f"Triage found {len(file_queue)} direct file links to download.")
                    await download_files_concurrently(self.session, file_queue, current_url, DOWNLOAD_SEMAPHORE)
                    self.processed_for_files.update(file_queue)

                if nav_queue:
                    queue.extend(nav_queue)
                    logger.info(f"Triage added {len(nav_queue)} new pages to the navigation queue.")

            except Exception as e:
                logger.error(f"A critical error occurred while processing {current_url}: {e}", exc_info=True)
            
            steps += 1
            await self._save_state(queue, steps) # Save state after each step
            await asyncio.sleep(1)
            
        logger.info("Agent run finished: queue empty or max steps reached.")
        if os.path.exists(AGENT_STATE_FILE):
             os.remove(AGENT_STATE_FILE) # Clean up state file on successful completion
             logger.info("Crawl complete. Removed agent state file.")


    async def close(self):
        """Cleans up resources."""
        if self.session and not self.session.closed: await self.session.close()
        if self.driver: self.driver.quit()
        if self.user_data_dir and os.path.exists(self.user_data_dir):
            try:
                shutil.rmtree(self.user_data_dir)
                logger.info(f"Successfully cleaned up temp user data dir: {self.user_data_dir}")
            except Exception as e:
                logger.warning(f"Could not clean up temp dir {self.user_data_dir}: {e}")

# ==============================================================================
# HELPER AND WORKER FUNCTIONS
# ==============================================================================
def get_file_links_from_page(driver_instance, file_types, source_url):
    """Scans the current page in the driver for direct file links."""
    links = set()
    selectors = [f"a[href$='{ext}' i]" for ext in file_types]
    css_selector = ", ".join(selectors)
    try:
        elements = driver_instance.find_elements(By.CSS_SELECTOR, css_selector)
        for el in elements:
            try:
                href = el.get_attribute('href')
                if href: links.add(urljoin(source_url, href))
            except StaleElementReferenceException: continue
    except Exception as e:
        logger.error(f"Error finding file links on {source_url}: {e}")
    return links

async def download_file(session, file_url, source_url, semaphore):
    """Downloads a single file with retries, exponential backoff, and concurrency limiting."""
    async with semaphore:
        for attempt in range(MAX_DOWNLOAD_RETRIES):
            try:
                local_name = re.sub(r'[\\/*?:"<>|]', "_", urlparse(file_url).path.split('/')[-1])
                path = os.path.join(DOWNLOAD_DIR, local_name)
                
                if os.path.exists(path):
                    logger.info(f"File already exists, skipping: {file_url}")
                    return

                logger.info(f"Attempt {attempt + 1}: Downloading {file_url}")
                async with session.get(file_url, timeout=120) as response:
                    response.raise_for_status()
                    content = await response.read()
                    async with aiofiles.open(path, 'wb') as f:
                        await f.write(content)
                    
                    logger.info(f"SUCCESS downloading {file_url}")
                    download_logger.info(f'"{file_url}","{source_url}","SUCCESS"')
                    return

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"Attempt {attempt + 1}/{MAX_DOWNLOAD_RETRIES} FAILED for {file_url}: {e}")
                if attempt + 1 == MAX_DOWNLOAD_RETRIES:
                    logger.error(f"Final attempt FAILED for {file_url}. Giving up.")
                    download_logger.info(f'"{file_url}","{source_url}","FAILURE: {e}"')
                    break
                
                delay = 2 ** attempt
                logger.info(f"Waiting for {delay} seconds before retrying...")
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"An unexpected error occurred downloading {file_url}: {e}")
                download_logger.info(f'"{file_url}","{source_url}","FAILURE: {e}"')
                break

async def download_files_concurrently(session, links, source_url, semaphore):
    """Manages the concurrent download of multiple files."""
    tasks = [download_file(session, link, source_url, semaphore) for link in links]
    await asyncio.gather(*tasks)

async def offline_html_parser_and_downloader():
    """Parses all saved HTML files to find and download any missed file links."""
    logger.info("--- Starting Offline HTML Parser Safety Net ---")
    
    if not os.path.exists(URL_MAP_FILE):
        logger.warning("URL map file not found. Can't process offline HTML.")
        return
    with open(URL_MAP_FILE, 'r') as f:
        url_map = json.load(f)

    all_found_links = set()
    html_files = os.listdir(HTML_LOG_DIR)
    logger.info(f"Found {len(html_files)} HTML files to parse.")

    for filename in html_files:
        if filename.endswith(".html"):
            file_hash = filename.replace(".html", "")
            source_url = url_map.get(file_hash)
            if not source_url: continue

            filepath = os.path.join(HTML_LOG_DIR, filename)
            with open(filepath, 'r', encoding='utf-8') as f:
                soup = BeautifulSoup(f, 'html.parser')
                
                for a_tag in soup.find_all('a', href=True):
                    href = a_tag['href']
                    if any(href.lower().endswith(ft) for ft in FILE_TYPES):
                        full_url = urljoin(source_url, href)
                        
                        local_name = re.sub(r'[\\/*?:"<>|]', "_", urlparse(full_url).path.split('/')[-1])
                        path = os.path.join(DOWNLOAD_DIR, local_name)
                        if not os.path.exists(path):
                            all_found_links.add(full_url)
                        else:
                            logger.debug(f"Offline parser skipping existing file: {local_name}")
    
    if not all_found_links:
        logger.info("Offline parser found no new file links to download.")
        return

    logger.info(f"Offline parser found a total of {len(all_found_links)} unique, missing file links. Starting download...")
    
    headers = {'User-Agent': random.choice(USER_AGENTS)}
    async with aiohttp.ClientSession(headers=headers) as session:
        await download_files_concurrently(session, all_found_links, "offline_parser", DOWNLOAD_SEMAPHORE)
    
    logger.info("--- Offline HTML Parser Safety Net Finished ---")

# ==============================================================================
# SCRIPT EXECUTION
# ==============================================================================
async def main():
    agent = AutonomousAgent(start_urls=START_URLS, file_types=FILE_TYPES, api_key=API_KEY)
    try:
        await agent.run()
    except Exception as e:
        logger.error(f"An unhandled exception occurred in main: {e}", exc_info=True)
    finally:
        await agent.close()
        await offline_html_parser_and_downloader()

if __name__ == '__main__':
    asyncio.run(main())
