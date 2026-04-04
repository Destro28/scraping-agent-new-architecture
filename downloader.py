import os
import aiohttp
import aiofiles
import asyncio
import logging
import random
import re
from urllib.parse import urlparse
import requests

# --- Configuration ---
MAX_DOWNLOAD_RETRIES = 3
CONCURRENT_DOWNLOAD_LIMIT = 10
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(CONCURRENT_DOWNLOAD_LIMIT)

def verify_pdf_endpoint(url):
    """
    Validates if a URL is a PDF endpoint using HEAD request.
    Fallbacks to GET with stream=True if HEAD is restricted.
    """
    try:
        response = requests.head(url, timeout=10, allow_redirects=True)
        content_type = response.headers.get('Content-Type', '')
        
        if response.status_code == 405 or not content_type:
            response = requests.get(url, stream=True, timeout=10, allow_redirects=True)
            content_type = response.headers.get('Content-Type', '')
            response.close()
            
        return 'application/pdf' in content_type.lower()
    except Exception as e:
        logging.warning(f"Error sniffing MIME for {url}: {e}")
        return False

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:126.0) Gecko/20100101 Firefox/126.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0',
]

async def download_file(file_url, source_url, download_dir, state_manager, force_extension=None, discovery_type=None):
    """Downloads a single file with retries, exponential backoff, and concurrency limiting."""
    async with DOWNLOAD_SEMAPHORE:
        for attempt in range(MAX_DOWNLOAD_RETRIES):
            try:
                # Create a safe local filename based on the URL path
                local_name = re.sub(r'[\\/*?:"<>|]', "_", urlparse(file_url).path.split('/')[-1])
                if not local_name:
                     local_name = "downloaded_file"
                     
                if force_extension and not local_name.lower().endswith(force_extension.lower()):
                     local_name += force_extension
                     
                path = os.path.join(download_dir, local_name)
                
                if os.path.exists(path):
                    logging.info(f"File already exists, skipping: {file_url}")
                    # Log as successful skip or already downloaded
                    state_manager.log_download(file_url, source_url, "SKIPPED_EXISTS")
                    return

                logging.info(f"Attempt {attempt + 1}: Downloading {file_url}")
                
                headers = {'User-Agent': random.choice(USER_AGENTS)}
                
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(file_url, timeout=120) as response:
                        response.raise_for_status()
                        content = await response.read()
                        
                        # Save the file
                        async with aiofiles.open(path, 'wb') as f:
                            await f.write(content)
                        
                        logging.info(f"SUCCESS downloading {file_url}")
                        state_manager.log_download(file_url, source_url, "SUCCESS")
                        if discovery_type:
                            state_manager.log_action(file_url, "DOWNLOAD_MIME_SNIFF", "SUCCESS", discovery_type=discovery_type)
                        state_manager.metrics["files_downloaded"] += 1
                        return

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logging.warning(f"Attempt {attempt + 1}/{MAX_DOWNLOAD_RETRIES} FAILED for {file_url}: {e}")
                if attempt + 1 == MAX_DOWNLOAD_RETRIES:
                    logging.error(f"Final attempt FAILED for {file_url}. Giving up.")
                    state_manager.log_download(file_url, source_url, f"FAILURE: {e}")
                    break
                
                # Exponential backoff: 2, 4, 8 seconds
                delay = 2 ** attempt
                logging.info(f"Waiting for {delay} seconds before retrying...")
                await asyncio.sleep(delay)
                
            except Exception as e:
                logging.error(f"An unexpected error occurred downloading {file_url}: {e}")
                state_manager.log_download(file_url, source_url, f"FAILURE: {e}")
                break

async def download_files_concurrently(links, source_url, download_dir, state_manager, force_extension=None, discovery_type=None):
    """Manages the concurrent download of multiple files."""
    if not links:
        return
    logging.info(f"Queueing {len(links)} concurrent downloads...")
    tasks = [download_file(link, source_url, download_dir, state_manager, force_extension, discovery_type) for link in links]
    await asyncio.gather(*tasks)
