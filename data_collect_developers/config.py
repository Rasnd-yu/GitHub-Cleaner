import os


class Config:
    # GitHub Token configuration
    GITHUB_TOKEN = "xxx"

    # Data storage paths
    DATA_DIR = 'data'
    USERS_FILE = os.path.join(DATA_DIR, 'users.json')  # Unified user data file
    METADATA_DIR = os.path.join(DATA_DIR, 'metadata')

    # Module state file
    MODULE_STATE_FILE = os.path.join(METADATA_DIR, 'module_states.json')

    # GitHub API configuration
    GITHUB_API_BASE = 'https://api.github.com'
    REQUEST_DELAY = 1  # Delay between API requests (seconds), to stay clear of the rate limit

    # Collection module configuration
    MODULES = {
        'github_repo_contributors': {
            'enabled': True,
            'force': False,
            'source_file': 'github_repos_hot_wd.json',
            'source_file_path': None,
            'username_filters': {
                'enabled': True,
                'patterns': [
                    r'^user_\d+$',
                    r'^bot_\d+$',
                    r'^test_\d+$',
                    r'^\d+$',
                    r'^unknown$',
                    r'^none$',
                    r'^example',
                    r'^demo',
                ],
                'min_username_length': 2,
                'max_username_length': 39,
                'valid_username_pattern': r'^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$'
            }
        },
        'github_leaderboard': {
            'enabled': True,
            'force': False,
            'source_file': 'github_leaderboard.json',
            'source_file_path': None,
            'source_label': 'GitHub Leaderboard',
            'username_filters': {
                'enabled': True,
                'patterns': [
                    r'^user_\d+$',
                    r'^bot_\d+$',
                    r'^test_\d+$',
                    r'^\d+$',
                    r'^unknown$',
                    r'^none$',
                    r'^example',
                    r'^demo',
                ],
                'min_username_length': 2,
                'max_username_length': 39,
                'valid_username_pattern': r'^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$'
            }
        },
        # Configuration of the gitstar_ranking_users module
        'gitstar_ranking_users': {
            'enabled': True,
            'force': False,
            'source_file': 'gitstar_ranking_users_top100.json',
            'source_file_path': None,
            'source_label': 'Gitstar Ranking_Users_top100',
            'username_filters': {
                'enabled': True,
                'patterns': [
                    r'^user_\d+$',
                    r'^bot_\d+$',
                    r'^test_\d+$',
                    r'^\d+$',
                    r'^unknown$',
                    r'^none$',
                    r'^example',
                    r'^demo',
                ],
                'min_username_length': 2,
                'max_username_length': 39,
                'valid_username_pattern': r'^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$'
            }
        }
    }
    @classmethod
    def init_dirs(cls):
        """Initialize the directories"""
        os.makedirs(cls.DATA_DIR, exist_ok=True)
        os.makedirs(cls.METADATA_DIR, exist_ok=True)