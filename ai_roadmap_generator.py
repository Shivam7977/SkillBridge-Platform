import requests as http_requests
from googleapiclient.discovery import build
import json
import os


def get_api_keys():
    """Get all available Mistral API keys for rotation."""
    keys = [
        os.getenv("MISTRAL_API_KEY_1"),
        os.getenv("MISTRAL_API_KEY_2"),
    ]
    keys = [k for k in keys if k]  # remove None/empty

    if not keys:
        raise ValueError("No MISTRAL_API_KEY found in environment variables.")

    return keys


def configure_ai():
    """Validates at least one API key exists on startup."""
    keys = get_api_keys()
    print(f"✅ Mistral AI configured with {len(keys)} API key(s).")


def get_youtube_service():
    """Initializes the YouTube Data API service."""
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        print("Warning: YOUTUBE_API_KEY not found in .env file.")
        return None
    return build('youtube', 'v3', developerKey=api_key)


def find_youtube_playlist(query):
    """Searches YouTube for a playlist and returns the top result."""
    youtube = get_youtube_service()
    if not youtube:
        print("❌ YouTube service is None — API key missing or build() failed")
        return "#", "YouTube API Key Not Configured"
    try:
        print(f"🔍 Searching YouTube for: {query}")
        request = youtube.search().list(part="snippet", q=query, type="playlist", maxResults=1)
        response = request.execute()
        print(f"📺 YouTube response items: {len(response.get('items', []))}")
        if response.get('items'):
            playlist_id = response['items'][0]['id']['playlistId']
            title = response['items'][0]['snippet']['title']
            print(f"✅ Found playlist: {title}")
            return f"https://www.youtube.com/playlist?list={playlist_id}", title
        else:
            print("⚠️ YouTube returned 0 items for this query")
    except Exception as e:
        print(f"❌ YouTube playlist search failed: {type(e).__name__}: {e}")
    return "#", "No playlist found"


def call_mistral(prompt, api_key):
    """Call Mistral API and return response text."""
    response = http_requests.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        json={
            "model": "mistral-small-latest",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 8192,
            "temperature": 0.7
        },
        timeout=60
    )
    if response.status_code == 429:
        raise Exception("429 quota exhausted")
    response.raise_for_status()
    return response.json()['choices'][0]['message']['content']


def generate_roadmap_with_ai(skill_to_learn):
    """Generates a learning roadmap, rotating Mistral API keys on quota errors."""
    keys = get_api_keys()

    prompt = f"""
    As a world-class expert in curriculum design and project-based learning, your task is to generate a hyper-detailed, logically structured learning roadmap for a user wanting to learn: "{skill_to_learn}".
    **CRITICAL INSTRUCTIONS:**
    1.  **Project-Based Learning:** The roadmap MUST be centered around practical projects. Every stage MUST include a "project_idea" and the roadmap MUST conclude with a final "capstone_project". For each project, include a "core_features" list.
    2.  **Autonomous Structure:** You MUST independently determine the most logical number of stages.
    3.  **Resource Rules:** For free resources, provide a "youtube_search_query" to find a relevant YouTube Playlist. At the end of each stage, include a "Paid Course" resource.
    4.  **VALID JSON OUTPUT ONLY:** Your entire response MUST be a single, perfectly structured JSON object. Do NOT wrap it in markdown code fences. Do NOT include any text before or after the JSON. Every key MUST be in double quotes. Every string value MUST be in double quotes. No trailing commas. No single quotes anywhere.
    5.  **JSON Structure Requirements:**
        {{
          "title": "A Project-Based Roadmap for Learning {skill_to_learn}",
          "assessed_complexity": "State the assessed complexity here",
          "estimated_stages": "State the number of stages you generated here",
          "description": "A comprehensive, project-based guide to master {skill_to_learn}.",
          "stages": [
            {{
              "name": "Stage 1: The Absolute Basics",
              "description": "A brief description of this stage's goal.",
              "learning_modules": [
                {{ "name": "Module 1", "concepts": ["Concept A", "Concept B"], "resources": [{{"type": "Free YouTube Playlist", "title": "Playlist for this module", "youtube_search_query": "The perfect YouTube search query"}}] }}
              ],
              "project_idea": {{ "title": "Project Title for Stage 1", "description": "A detailed description...", "core_features": ["Feature 1", "Feature 2"] }}
            }}
          ],
          "capstone_project": {{ "title": "Final Capstone Project Title", "description": "A description...", "core_features": ["Core feature 1", "Core feature 2"] }}
        }}
    """

    # Try each key in rotation until one works
    for i, key in enumerate(keys):
        try:
            print(f"\n🤖 Trying Mistral key {i+1}/{len(keys)} for '{skill_to_learn}'...")
            response_text = call_mistral(prompt, key)

            print("\n--- RAW AI RESPONSE ---")
            print(response_text)
            print("-----------------------\n")

            response_text = response_text.strip()

            # Strip markdown code fences if present
            if response_text.startswith("```"):
                parts = response_text.split("```")
                if len(parts) >= 2:
                    response_text = parts[1]
                    if response_text.startswith("json"):
                        response_text = response_text[4:]
                    response_text = response_text.strip()

            # Extract JSON
            start_index = response_text.find('{')
            end_index = response_text.rfind('}')

            if start_index != -1 and end_index != -1 and end_index > start_index:
                json_str = response_text[start_index:end_index+1]
                roadmap_data = json.loads(json_str)

                if not isinstance(roadmap_data, dict):
                    print("❌ Parsed data is not a dictionary.")
                    return None
                if not isinstance(roadmap_data.get('stages'), list):
                    print("❌ Parsed data missing 'stages' list.")
                    return None
                if len(roadmap_data.get('stages', [])) == 0:
                    print("❌ Stages list is empty.")
                    return None

                print(f"✅ Roadmap parsed with {len(roadmap_data['stages'])} stages using key {i+1}.")
                return roadmap_data
            else:
                print("❌ Could not find valid JSON in response.")
                return None

        except json.JSONDecodeError as e:
            print(f"❌ JSON decode error with key {i+1}: {e}")
            print(f"⚠️ Retrying with next key...")
            continue

        except Exception as e:
            error_str = str(e)
            if '429' in error_str or 'quota' in error_str.lower() or 'rate' in error_str.lower():
                print(f"⚠️ Mistral key {i+1} quota exhausted — trying next key...")
                continue
            else:
                print(f"❌ Error with key {i+1}: {e}")
                return None

    print("❌ All Mistral keys exhausted or failed.")
    return None