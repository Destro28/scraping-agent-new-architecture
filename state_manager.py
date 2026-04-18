import json
import os
import shutil
from collections import deque
import logging

class StateManager:
    def __init__(self, base_path="./state"):
        self.base_path = base_path
        self.state_file = os.path.join(base_path, "agent_state.json")
        self.run_log_file = os.path.join(base_path, "run_log.csv")
        self.download_log_file = os.path.join(base_path, "download_log.csv")
        self.metrics_history_file = os.path.join(base_path, "metrics_history.csv")
        
        # Ensure state directory exists
        os.makedirs(base_path, exist_ok=True)
        
        # Initialize default state structures
        self.queue = deque()
        self.visited = set()
        self.html_map = {}  # URL -> Local File Path
        self.metrics = {
            "pages_crawled": 0, 
            "files_downloaded": 0, 
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0
        }

    def save_state(self):
        """
        Atomically saves the current state to prevent corruption on crash.
        Writes to .tmp first, then performs atomic rename.
        """
        state_data = {
            "queue": list(self.queue),  # Serialize deque
            "visited": list(self.visited),  # Serialize set
            "html_map": self.html_map,
            "metrics": self.metrics
        }
        
        temp_file = self.state_file + ".tmp"
        try:
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(state_data, f, indent=4)
                f.flush()
                os.fsync(f.fileno())  # Force write to disk
            
            # Atomic replacement
            os.replace(temp_file, self.state_file)
            logging.info(f"State saved successfully. Pages: {self.metrics['pages_crawled']}")
            return True
        except Exception as e:
            logging.error(f"Failed to save state: {e}")
            return False

    def load_state(self):
        """
        Loads state from disk if it exists.
        Returns: True if resumed, False if fresh start.
        """
        if not os.path.exists(self.state_file):
            logging.info("No state file found. Starting fresh.")
            return False
            
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            self.queue = deque(data.get("queue", []))
            self.visited = set(data.get("visited", []))
            self.html_map = data.get("html_map", {})
            self.metrics = data.get("metrics", {"pages_crawled": 0, "files_downloaded": 0, "tokens_used": 0})
            
            logging.info(f"Resumed state: {len(self.visited)} pages visited, {len(self.queue)} in queue.")
            return True
        except (json.JSONDecodeError, OSError) as e:
            logging.error(f"Corrupt state file found ({e}). Starting fresh.")
            return False

    def log_action(self, url, action_type, status, discovery_type=None):
        """Appends action to CSV log (non-blocking Append-Only)."""
        file_exists = os.path.exists(self.run_log_file)
        with open(self.run_log_file, 'a', encoding='utf-8') as f:
            if not file_exists:
                f.write("timestamp,url,action,status,discovery_type\n")
            
            # Simple timestamp could be added here
            log_line = f"{url},{action_type},{status}"
            if discovery_type:
                log_line += f",{discovery_type}"
            f.write(log_line + "\n")

    def log_download(self, file_url, source_url, status):
        """Appends a download attempt to a dedicated CSV log."""
        file_exists = os.path.exists(self.download_log_file)
        with open(self.download_log_file, 'a', encoding='utf-8') as f:
            if not file_exists:
                f.write("timestamp,file_url,source_url,status\n")
            f.write(f"{file_url},{source_url},{status}\n")

    def log_metrics_snapshot(self):
        """Saves a snapshot of current metrics for historical analysis (e.g. plotting)."""
        file_exists = os.path.exists(self.metrics_history_file)
        with open(self.metrics_history_file, 'a', encoding='utf-8') as f:
            headers = ["pages_crawled", "files_downloaded", "total_tokens", "prompt_tokens", "completion_tokens"]
            if not file_exists:
                f.write(",".join(headers) + "\n")
            
            row = [str(self.metrics.get(h, 0)) for h in headers]
            f.write(",".join(row) + "\n")
