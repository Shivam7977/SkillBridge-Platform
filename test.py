import google.generativeai as genai
import os
from dotenv import load_dotenv

# Load the API key from your .env file
load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    print("Error: GEMINI_API_KEY not found in .env file.")
else:
    try:
        genai.configure(api_key=api_key)
        print("--- Available Models ---")

        # List all models and check which ones support 'generateContent'
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                print(f"- {m.name}")

        print("-------------------------")

    except Exception as e:
        print(f"\n--- AN ERROR OCCURRED ---")
        print(f"{e}")
        print("\nThis likely means your 'google-generativeai' library is outdated.")
        print("Please run the command: pip install --upgrade google-generativeai")