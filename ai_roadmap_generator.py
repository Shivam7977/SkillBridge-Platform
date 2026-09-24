import requests as http_requests
from googleapiclient.discovery import build
import json
import os
import httplib2

# Groq: OpenAI-compatible endpoint, extremely generous free tier — no
# "thinking budget" weirdness like Gemini, and no credit card needed.
# https://console.groq.com/docs/rate-limits
#
# NOTE: llama-3.1-8b-instant and llama-3.3-70b-versatile were DEPRECATED by
# Groq on 16 Aug 2026 (shutdown — calls now 404). Using Groq's own recommended
# replacements below.
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# Used for roadmap generation — bigger model, better at following the
# detailed JSON structure instructions. (was llama-3.3-70b-versatile)
GROQ_MODEL = "openai/gpt-oss-120b"
# Used for chatbot + interview (app.py) — smaller/faster, same generous
# free-tier limits, plenty good for conversational Q&A and evaluation.
# (was llama-3.1-8b-instant)
GROQ_MODEL_FAST = "openai/gpt-oss-20b"


def get_api_keys():
    """Get all available Groq API keys for rotation."""
    keys = [
        os.getenv("GROQ_API_KEY_1"),
        os.getenv("GROQ_API_KEY_2"),
    ]
    keys = [k for k in keys if k]  # remove None/empty

    if not keys:
        raise ValueError("No GROQ_API_KEY found in environment variables.")

    return keys


def configure_ai():
    """Validates at least one API key exists on startup."""
    keys = get_api_keys()
    print(f"✅ Groq AI configured with {len(keys)} API key(s).")


def get_youtube_service():
    """Initializes the YouTube Data API service with a 10s timeout.

    httplib2 is what googleapiclient uses internally — setting timeout here
    is the only reliable way to prevent it from blocking Gunicorn workers.
    Without this, a slow YouTube response kills the entire worker process.
    """
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        print("Warning: YOUTUBE_API_KEY not found in .env file.")
        return None
    http = httplib2.Http(timeout=10)  # 10s hard cap — well within Gunicorn's 30s
    return build('youtube', 'v3', developerKey=api_key, http=http)


def find_youtube_playlist(query):
    """Searches YouTube for a playlist and returns the top result."""
    youtube = get_youtube_service()
    if not youtube:
        print("❌ YouTube service is None — API key missing or build() failed")
        return "#", "YouTube API Key Not Configured"
    try:
        print(f"🔍 Searching YouTube for: {query}")
        request = youtube.search().list(part="snippet", q=query, type="playlist", maxResults=1)
        # num_retries=0 prevents silent retries that eat into Gunicorn's 30s window
        response = request.execute(num_retries=0)
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


def call_groq(prompt, api_key, model=GROQ_MODEL, max_tokens=8192, json_mode=True):
    """Call Groq's OpenAI-compatible chat completions API and return response text."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7
    }
    if json_mode:
        # Groq's structured-output JSON mode — model is constrained to emit
        # valid JSON, so we don't have to strip markdown fences most of the time.
        payload["response_format"] = {"type": "json_object"}

    response = http_requests.post(
        GROQ_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=60
    )
    if response.status_code == 429:
        raise Exception("429 quota exhausted")
    response.raise_for_status()
    data = response.json()

    choices = data.get("choices") or []
    if not choices:
        raise Exception(f"No choices returned by Groq. Response: {data}")

    finish_reason = choices[0].get("finish_reason")
    text = (choices[0].get("message", {}) or {}).get("content", "")

    if not text:
        raise Exception(f"Empty response text from Groq (finish_reason={finish_reason})")

    return text


def generate_roadmap_with_ai(skill_to_learn):
    """Generates a learning roadmap, rotating Groq API keys on quota errors."""
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
            print(f"\n🤖 Trying Groq key {i+1}/{len(keys)} for '{skill_to_learn}'...")
            response_text = call_groq(prompt, key, model=GROQ_MODEL, max_tokens=8192, json_mode=True)

            print("\n--- RAW AI RESPONSE ---")
            print(response_text)
            print("-----------------------\n")

            response_text = response_text.strip()

            # Strip markdown code fences if present (json_mode usually
            # prevents this, but it's a harmless safety net).
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
                print(f"⚠️ Groq key {i+1} quota exhausted — trying next key...")
                continue
            else:
                print(f"❌ Error with key {i+1}: {e}")
                return None

    print("❌ All Groq keys exhausted or failed.")
    return None