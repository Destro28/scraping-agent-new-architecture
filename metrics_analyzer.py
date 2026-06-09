import re
import os
import json

def analyze_logs():
    trace_path = "state/execution_trace.log"
    if not os.path.exists(trace_path):
        return "No execution trace found."

    with open(trace_path, "r", encoding="utf-8") as f:
        log_lines = f.readlines()

    # Metrics
    redundancy_blocked = 0
    triage_documents = 0
    triage_pages_with_docs = 0
    total_pages_visited = 0
    empty_pages = 0
    errors_429 = 0

    for line in log_lines:
        if "File already exists, skipping" in line or "Skipping binary/document URL" in line:
            redundancy_blocked += 1
        
        if "MIME Sniffing found" in line:
            # Extract number
            match = re.search(r"found (\d+) PDF endpoints", line)
            if match:
                count = int(match.group(1))
                triage_documents += count
                if count > 0:
                    triage_pages_with_docs += 1
                else:
                    empty_pages += 1
                    
        if "--- Processing:" in line:
            total_pages_visited += 1
            
        if "Rate limited (429)" in line:
            errors_429 += 1

    # False Discovery Rate (pages visited that had no files)
    # Since it's a crawler, list pages have no files (usually). But let's see.
    
    print(f"--- Analysis Results ---")
    print(f"Total Processing Steps: {total_pages_visited}")
    print(f"Redundancy Shield (RS) Blocks: {redundancy_blocked}")
    print(f"Total Triage Documents Sniffed: {triage_documents}")
    print(f"Pages with Documents: {triage_pages_with_docs}")
    print(f"Pages without Documents (FDR proxy): {total_pages_visited - triage_pages_with_docs}")
    print(f"429 Errors Survived: {errors_429}")

if __name__ == "__main__":
    analyze_logs()
