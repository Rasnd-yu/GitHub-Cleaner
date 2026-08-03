import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional, Set

logger = logging.getLogger(__name__)

# Standard user fields (aligned with user.json)
USER_FIELDS = [
    'login',
    'id',
    'node_id',
    'avatar_url',
    'gravatar_id',
    'url',
    'html_url',
    'type',
    'user_view_type',
    'site_admin',
    'name',
    'company',
    'blog',
    'location',
    'email',
    'hireable',
    'bio',
    'twitter_username',
    'public_repos',
    'public_gists',
    'followers',
    'following',
    'created_at',
    'updated_at',
    'source'
]


def filter_user_data(user_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Filter the user data, keeping only the standard fields
    """
    filtered_data = {}
    for field in USER_FIELDS:
        if field in user_data:
            filtered_data[field] = user_data[field]
    return filtered_data


def load_json_file(file_path: str) -> Optional[Any]:
    """Load a JSON file"""
    try:
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load {file_path}: {e}")
    return None


def save_json_file(file_path: str, data: Any, indent: int = 2) -> bool:
    """Save a JSON file"""
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        return True
    except Exception as e:
        logger.error(f"Failed to save {file_path}: {e}")
        return False


def append_json_line(file_path: str, data: Any) -> bool:
    """Append in JSON Lines format (one JSON object per line)"""
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(data, ensure_ascii=False) + '\n')
        return True
    except Exception as e:
        logger.error(f"Failed to append to {file_path}: {e}")
        return False


def load_users(users_file: str) -> Dict[str, Dict[str, Any]]:
    """Load every user record and return a mapping from user name to user data"""
    users = load_json_file(users_file)
    if users and isinstance(users, dict):
        return users
    return {}


def save_users(users_file: str, users: Dict[str, Dict[str, Any]]) -> bool:
    """Save every user record"""
    return save_json_file(users_file, users)


def add_user(users_file: str, username: str, user_data: Dict[str, Any]) -> bool:
    """Add or update a user incrementally (fields are filtered automatically)"""
    users = load_users(users_file)
    users[username] = filter_user_data(user_data)
    return save_users(users_file, users)


def add_users_batch(users_file: str, users_dict: Dict[str, Dict[str, Any]]) -> bool:
    """Add or update users in bulk (fields are filtered automatically)"""
    existing_users = load_users(users_file)
    for username, user_data in users_dict.items():
        existing_users[username] = filter_user_data(user_data)
    return save_users(users_file, existing_users)


def user_exists(users_file: str, username: str) -> bool:
    """Check whether the user exists (checked against the file)"""
    users = load_users(users_file)
    return username in users


def get_existing_usernames(users_file: str) -> Set[str]:
    """Get the set of existing user names"""
    users = load_users(users_file)
    return set(users.keys())


def get_user_count(users_file: str) -> int:
    """Get the total number of users"""
    users = load_users(users_file)
    return len(users)


def get_users_by_source(users_file: str, source: str) -> Dict[str, Dict[str, Any]]:
    """Select the users matching a source field"""
    users = load_users(users_file)
    return {k: v for k, v in users.items() if v.get('source') == source}


def load_module_state(state_file: str) -> Dict[str, Any]:
    """Load the module run states"""
    default_state = {
        'modules': {}
    }
    data = load_json_file(state_file)
    if data:
        return data
    return default_state


def save_module_state(state_file: str, module_name: str, state: Dict[str, Any]) -> bool:
    """Save the module run states"""
    full_state = load_module_state(state_file)
    full_state['modules'][module_name] = state
    full_state['last_updated'] = datetime.now().isoformat()
    return save_json_file(state_file, full_state)


def get_module_state(state_file: str, module_name: str) -> Optional[Dict[str, Any]]:
    """Get the run state of a module"""
    full_state = load_module_state(state_file)
    return full_state['modules'].get(module_name)


class UserCache:
    """User cache, used for efficient duplicate checks"""

    def __init__(self, users_file: str):
        self.users_file = users_file
        self._usernames_cache = None
        self._users_cache = None
        self._last_load_time = None

    def _load_if_needed(self):
        """Load the cache on demand"""
        # Reload when there is no cache, or when it is empty
        if self._usernames_cache is None or self._users_cache is None:
            self.refresh()

    def refresh(self):
        """Refresh the cache"""
        self._users_cache = load_users(self.users_file)
        self._usernames_cache = set(self._users_cache.keys()) if self._users_cache else set()
        self._last_load_time = datetime.now()
        logger.debug(f"User cache refreshed: {len(self._usernames_cache)} users")

    def exists(self, username: str) -> bool:
        """Check whether the user exists (using the cache)"""
        self._load_if_needed()
        return username in self._usernames_cache

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Get the user data"""
        self._load_if_needed()
        return self._users_cache.get(username)

    def add_user(self, username: str, user_data: Dict[str, Any]) -> bool:
        """Add a user to the cache and to the file"""
        # Update the cache
        if self._usernames_cache is not None:
            self._usernames_cache.add(username)
        if self._users_cache is not None:
            self._users_cache[username] = filter_user_data(user_data)

        # Save to the file
        return add_user(self.users_file, username, user_data)

    def add_users_batch(self, users_dict: Dict[str, Dict[str, Any]]) -> bool:
        """Add users to the cache and to the file in bulk"""
        if not users_dict:
            return True

        # Update the cache
        if self._usernames_cache is not None:
            self._usernames_cache.update(users_dict.keys())
        if self._users_cache is not None:
            for username, user_data in users_dict.items():
                self._users_cache[username] = filter_user_data(user_data)

        # Save to the file
        return add_users_batch(self.users_file, users_dict)

    def get_stats(self) -> Dict[str, Any]:
        """Get the cache statistics"""
        self._load_if_needed()
        return {
            'total_users': len(self._usernames_cache) if self._usernames_cache else 0,
            'cache_loaded': self._usernames_cache is not None,
            'last_load': self._last_load_time.isoformat() if self._last_load_time else None
        }