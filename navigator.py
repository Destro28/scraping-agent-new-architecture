import asyncio
import os
import hashlib
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

class Navigator:
    def __init__(self, headless=True, download_dir="./downloads"):
        self.download_dir = download_dir
        self.html_log_dir = "./html_logs"
        os.makedirs(self.download_dir, exist_ok=True)
        os.makedirs(self.html_log_dir, exist_ok=True)
        
        # Dedicated executor for Selenium blocking calls
        self.executor = ThreadPoolExecutor(max_workers=5)
        
        options = Options()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        
        self.driver = webdriver.Chrome(options=options)
        self.driver.set_page_load_timeout(30)

    def _save_html_snapshot(self, url, html):
        """Saves versioned HTML for offline recovery metrics."""
        url_hash = hashlib.md5(url.encode()).hexdigest()
        # Added timestamp to prevent overwrites [Fixes Experiment Evidence]
        timestamp = int(time.time())
        filename = f"{url_hash}_{timestamp}.html"
        path = os.path.join(self.html_log_dir, filename)
        
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html)
        return path

    def close(self):
        if self.driver:
            self.driver.quit()
        self.executor.shutdown(wait=False)
    

    def get_links(self, current_url):
        """Extracts every link currently visible on the page."""
        links = []
        try:
            base_current_url = current_url.split('#')[0]
            elements = self.driver.find_elements(By.TAG_NAME, "a")
            for el in elements:
                href = el.get_attribute("href")
                if href and href.startswith("http"):
                    base_href = href.split('#')[0]
                    if base_href == base_current_url and '#' in href:
                        continue # Skip on-page anchors (SPA Loop Prevention)
                    links.append(href)
        except Exception:
            pass
        return list(set(links)) # Unique links only

    async def go_back(self):
        """Repairs DOM state by physically navigating back to the previous page."""
        try:
            self.driver.back()
            await asyncio.sleep(1) # Give DOM time to re-render before taking snapshot
        except Exception as e:
            logging.error(f"Failed to go back: {e}")

    def get_url_from_selector(self, selector):
        """Finds an element and ensures it is a valid link."""
        try:
            # Use a slight wait in case the element is still rendering
            element = WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            url = element.get_attribute("href")
            if url:
                return url
            logging.warning(f"Selector '{selector}' found, but it has no href (not a link).")
        except Exception:
            logging.warning(f"AI suggested selector '{selector}', but it doesn't exist on this page.")
        return None

    async def visit_url(self, url):
        """Enhanced navigation with adaptive timeouts for heavy sites."""
        loop = asyncio.get_event_loop()
        try:
            # Increase timeout for sites like Archive.org
            await loop.run_in_executor(self.executor, self.driver.get, url)
            
            # More relaxed wait: check for 'body' first, then 'a'
            await loop.run_in_executor(self.executor, 
                lambda: WebDriverWait(self.driver, 20).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
            )
            
            # Short sleep to allow dynamic content/links to render
            await asyncio.sleep(2)
            
            html = self.driver.page_source
            file_path = self._save_html_snapshot(url, html)
            return html, file_path
        except Exception as e:
            logging.error(f"Failed to load {url} within 20s. Heavy site or network issue.")
            return None, None