import os
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("API_KEY") or os.getenv("GEMINI_API_KEY")

genai.configure(api_key=api_key)
model = genai.GenerativeModel('gemini-2.5-flash-lite')

try:
    print("Testing Gemini API...")
    response = model.generate_content("Hello! Are you online? Reply with just 'Yes'.")
    print(f"Success! Response: {response.text}")
except Exception as e:
    print(f"FAILED! Error: {e}")
