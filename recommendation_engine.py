from bson.objectid import ObjectId

def get_recommended_projects(user_id, users_collection, projects_collection):
    """
    Core logic to match user skills with project requirements.
    """
    user_data = users_collection.find_one({'_id': ObjectId(user_id)})
    if not user_data:
        return []

    # Combine all user skills (Known + Learning) into a lowercase set
    user_skills = set([s.lower() for s in (user_data.get('known_skills', []) + user_data.get('learning_skills', []))])
    
    all_projects = list(projects_collection.find())
    recommended = []

    for project in all_projects:
        project_skills = set([s.lower() for s in project.get('skills_needed', [])])
        
        # Calculate intersection (common skills)
        matches = user_skills.intersection(project_skills)
        if matches:
            project['match_count'] = len(matches)
            project['matched_skills'] = list(matches)
            recommended.append(project)

    # Sort by highest match count (Ranking System)
    recommended.sort(key=lambda x: x.get('match_count', 0), reverse=True)
    return recommended