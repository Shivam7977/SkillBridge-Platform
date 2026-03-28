from google import genai
from googleapiclient.discovery import build
import json
import os

client = None 

def configure_ai():
    """Configures the new Gemini 2.0 Client."""
    global client
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not found. Make sure it's in your Render environment variables.")
    client = genai.Client(api_key=api_key)

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

def generate_roadmap_with_ai(skill_to_learn):
    """Generates a learning roadmap structure with search queries."""
    prompt = f"""
    As a world-class expert in curriculum design and project-based learning, your task is to generate a hyper-detailed, logically structured learning roadmap for a user wanting to learn: "{skill_to_learn}".
    **CRITICAL INSTRUCTIONS:**
    1.  **Project-Based Learning:** The roadmap MUST be centered around practical projects. Every stage MUST include a "project_idea" and the roadmap MUST conclude with a final "capstone_project". For each project, include a "core_features" list.
    2.  **Autonomous Structure:** You MUST independently determine the most logical number of stages.
    3.  **Resource Rules:** For free resources, provide a "youtube_search_query" to find a relevant YouTube Playlist. At the end of each stage, include a "Paid Course" resource.
    4.  **VALID JSON OUTPUT ONLY:** Your entire response MUST be a single, perfectly structured JSON object. Do NOT wrap it in markdown code fences. Do NOT include any text before or after the JSON.
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
    print(f"\n🤖 Calling Gemini AI for '{skill_to_learn}'...")
    try:
        global client
        if client is None:
            configure_ai()

        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config={"max_output_tokens": 8192}
        )

        # --- DEBUGGING: see the raw response ---
        print("\n--- RAW AI RESPONSE ---")
        print(response.text)
        print("-----------------------\n")

        response_text = response.text.strip()

        # Strip markdown code fences if Gemini wraps response in them
        if response_text.startswith("```"):
            parts = response_text.split("```")
            # parts[1] will be like "json\n{...}" or just "{...}"
            if len(parts) >= 2:
                response_text = parts[1]
                if response_text.startswith("json"):
                    response_text = response_text[4:]
                response_text = response_text.strip()

        # Extract JSON object from response
        start_index = response_text.find('{')
        end_index = response_text.rfind('}')

        if start_index != -1 and end_index != -1 and end_index > start_index:
            json_str = response_text[start_index:end_index+1]
            roadmap_data = json.loads(json_str)
            
            # Validate the parsed data has required fields
            if not isinstance(roadmap_data, dict):
                print("❌ Parsed data is not a dictionary.")
                return None
            if not isinstance(roadmap_data.get('stages'), list):
                print("❌ Parsed data missing 'stages' list.")
                return None
            if len(roadmap_data.get('stages', [])) == 0:
                print("❌ Stages list is empty.")
                return None
                
            print(f"✅ Successfully parsed roadmap with {len(roadmap_data['stages'])} stages.")
            return roadmap_data
        else:
            print("❌ Could not find a valid JSON object in the AI's response.")
            return None

    except json.JSONDecodeError as e:
        print(f"❌ Error decoding JSON: {e}")
        print(f"❌ Attempted to parse: {json_str[:500] if 'json_str' in locals() else 'N/A'}")
        return None
    except Exception as e:
        print(f"❌ An error occurred while processing the AI response: {e}")
        return None