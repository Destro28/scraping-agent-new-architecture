import json
import logging
import re
import asyncio
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
from bs4 import BeautifulSoup
from collections import deque

class LLMEngine:
    def __init__(self, api_key=None, mock_mode=False):
        self.mock_mode = mock_mode
        if not mock_mode and api_key:
            genai.configure(api_key=api_key)
            self.model = genai.GenerativeModel(
                model_name='gemini-2.5-flash-lite',
                # temperature=0.0,
                generation_config={
                    "temperature": 0.0,
                    "top_p": 0.95,
                    "top_k": 64,
                    "max_output_tokens": 8192,
                },
                safety_settings={
                    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
                }
            )

    def _clean_html(self, html):
        """Removes noise (scripts, styles) to focus the LLM on content and links."""
        soup = BeautifulSoup(html, 'html.parser')
        # Remove elements that don't help navigation or file finding
        for element in soup(["script", "style", "svg", "path", "header", "footer"]):
            element.decompose()
            
        # Convert hyperlinks to markdown format so the LLM can see the actual hrefs
        for a in soup.find_all('a'):
            if a.get('href'):
                markdown_text = f" [{a.get_text(separator=' ', strip=True)}]({a.get('href')}) "
                new_tag = soup.new_string(markdown_text)
                a.replace_with(new_tag)
                
        # Return a compact version of the text and links
        return soup.get_text(separator=' ', strip=True)[:100000] # increased limit of slicing the html text by ~3x

    async def decide_action(self, html, url, history, site_hint={"mit.edu": "You are navigating a research repository. Prioritize links containing '/handle/' as these lead to community and collection hierarchies. When on an individual item page, focus on finding 'View/Open' links for the primary PDF bitstream. Ignore administrative sidebar links like 'Login', 'Register', or 'Statistics'."}):  
        """
        Determines next step. 
        site_hint: Optional custom instructions for specific domains.
        """
        if self.mock_mode:
            return {"tool": "SCAN_FOR_FILES", "reason": "Mock mode active"}

        cleaned_content = self._clean_html(html)
        
        prompt = f"""
        Current URL: {url}
        Action History (last 5): {history[-5:]}
        SITE-SPECIFIC INSTRUCTIONS: {site_hint}
        
        SOP: You are the Navigation Engine. Your ONLY job is to find the best links to explore to find documents.
        (Note: All document files like PDFs are already downloaded automatically by a background script. DO NOT select direct document links. DO NOT try to 'download' anything).
        
        1. **specific_subpages**: Exact URLs (the href values) for specific items, reports, or chapters that likely contain documents inside them. Provide a list.
        2. **next_page**: Exact URL (the href value) for general pagination (e.g., "Next Page", "Older Posts"). Use this to traverse lists. Use null if none exists.
        3. **CRITICAL**: ONLY provide the exact URL string found in the parentheses of the markdown links (e.g., /path/to/page or https://...). DO NOT output CSS selectors.
        
        HTML CONTENT:
        {cleaned_content}

        Return ONLY a JSON object:
        {{
            "specific_subpages": ["/link1", "/link2"], 
            "next_page": "/next_page_link" | null,
            "reason": "Why exploring these paths is the optimal next step"
        }}
        """
        
        WAIT_TIMES = [5, 15, 30, 45, 60, 120] # Custom patient backoff schedule
        MAX_RETRIES = len(WAIT_TIMES)
        
        for attempt in range(MAX_RETRIES):
            try:
                response = await asyncio.to_thread(self.model.generate_content, prompt)
                raw_text = response.text
                
                # Extract usage metadata if available
                usage = {
                    "prompt_tokens": getattr(response.usage_metadata, 'prompt_token_count', 0),
                    "completion_tokens": getattr(response.usage_metadata, 'candidates_token_count', 0),
                    "total_tokens": getattr(response.usage_metadata, 'total_token_count', 0)
                }
                
                # Use Regex to extract only the JSON block [Fixes KeyError]
                json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group(0)), usage
                
                logging.error(f"No JSON found in LLM response for {url}")
                return {"specific_subpages": [], "next_page": None, "reason": "Malformed LLM response"}, usage
                
            except Exception as e:
                if "429" in str(e):
                    if attempt < MAX_RETRIES - 1:
                        wait_time = WAIT_TIMES[attempt]
                        logging.warning(f"Rate limited (429). Waiting {wait_time}s... (Attempt {attempt + 1}/{MAX_RETRIES})")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        logging.error(f"LLM Decision failed after {MAX_RETRIES} attempts due to rate limits: {e}")
                else:
                    logging.error(f"LLM Decision failed: {e}")
                
                return {"specific_subpages": [], "next_page": None, "reason": "LLM Error"}, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}