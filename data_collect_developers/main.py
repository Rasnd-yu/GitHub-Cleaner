#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import logging
import argparse
from datetime import datetime
from typing import Dict, Any

from config import Config
from utils import file_utils
from collectors.github_repos_contributors import GitHubRepoContributorsCollector
from collectors.github_leaderboard import GitHubLeaderboardCollector
from collectors.gitstar_ranking_users import GitstarRankingUsersCollector

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('collector.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


class DataCollector:
    """Main data collection controller"""

    def __init__(self):
        """Initialization"""
        Config.init_dirs()
        self.modules = {}
        self.module_states = {}
        self._load_module_states()

    def _load_module_states(self):
        """Load the module states"""
        self.module_states = file_utils.load_module_state(Config.MODULE_STATE_FILE)

    def _save_module_state(self, module_name: str, state: Dict[str, Any]):
        """Save the module states"""
        file_utils.save_module_state(Config.MODULE_STATE_FILE, module_name, state)
        self._load_module_states()

    def _should_run_module(self, module_name: str) -> bool:
        """Decide whether the module should run"""
        module_config = Config.MODULES.get(module_name, {})

        # Check whether the module is enabled
        if not module_config.get('enabled', False):
            logger.info(f"Module '{module_name}' is disabled")
            return False

        # Check the force flag
        if module_config.get('force', False):
            logger.info(f"Module '{module_name}' force run enabled")
            return True

        # Check whether a previous run succeeded
        state = self.module_states.get('modules', {}).get(module_name, {})
        if state.get('status') == 'completed':
            logger.info(f"Module '{module_name}' already completed successfully, skipping...")
            return False

        return True

    def _update_module_state(self, module_name: str, result: Dict[str, Any]):
        """Update the module state"""
        state = {
            'status': result.get('status'),
            'reason': result.get('reason'),
            'collected': result.get('collected', 0),
            'skipped': result.get('skipped', 0),
            'failed': result.get('failed', 0),
            'total_users': result.get('total_users_after', 0),
            'timestamp': datetime.now().isoformat()
        }
        self._save_module_state(module_name, state)

    def register_module(self, name: str, module_class: type, config: Dict[str, Any]):
        """Register a module"""
        self.modules[name] = {
            'class': module_class,
            'config': config
        }
        logger.info(f"Registered module: {name}")

    def run_module(self, module_name: str) -> Dict[str, Any]:
        """Run the given module"""
        if module_name not in self.modules:
            logger.error(f"Module '{module_name}' not registered")
            return {'status': 'failed', 'reason': 'not_registered'}

        if not self._should_run_module(module_name):
            return {'status': 'skipped', 'reason': 'already_completed_or_disabled'}

        logger.info(f"Running module: {module_name}")

        module_info = self.modules[module_name]
        collector = module_info['class'](module_info['config'])
        result = collector.run()

        self._update_module_state(module_name, result)
        return result

    def run_all_modules(self) -> Dict[str, Any]:
        """Run every registered module"""
        results = {}
        for module_name in self.modules:
            logger.info(f"\n{'=' * 60}")
            logger.info(f"Processing module: {module_name}")
            logger.info(f"{'=' * 60}")
            result = self.run_module(module_name)
            results[module_name] = result
        return results

    def show_stats(self):
        """Show the statistics"""
        logger.info("\n" + "=" * 60)
        logger.info("Data Collection Statistics")
        logger.info("=" * 60)

        # User statistics
        total_users = file_utils.get_user_count(Config.USERS_FILE)
        logger.info(f"Total users collected: {total_users}")

        # Statistics per source
        if total_users > 0:
            users_data = file_utils.load_users(Config.USERS_FILE)
            sources = {}
            for username, user_data in users_data.items():
                source = user_data.get('source', 'Unknown')
                sources[source] = sources.get(source, 0) + 1

            logger.info("\nUsers by source:")
            for source, count in sorted(sources.items(), key=lambda x: x[1], reverse=True):
                logger.info(f"  - {source}: {count} users")

        # Module statistics
        logger.info("\nModule Status:")
        for module_name, module_config in Config.MODULES.items():
            state = self.module_states.get('modules', {}).get(module_name, {})
            status = state.get('status', 'not_run')
            collected = state.get('collected', 0)
            total_users = state.get('total_users', 0)
            timestamp = state.get('timestamp', 'N/A')
            logger.info(f"  - {module_name}:")
            logger.info(f"      Status: {status}")
            logger.info(f"      New users collected: {collected}")
            logger.info(f"      Total users in DB: {total_users}")
            logger.info(f"      Last run: {timestamp}")

        logger.info("=" * 60)

    def export_by_source(self, source: str, output_file: str = None):
        """Export the user data grouped by source"""
        users_by_source = file_utils.get_users_by_source(Config.USERS_FILE, source)

        if not users_by_source:
            logger.warning(f"No users found with source: {source}")
            return

        if output_file is None:
            output_file = os.path.join(Config.DATA_DIR, f"users_{source.replace(' ', '_')}.json")

        if file_utils.save_json_file(output_file, users_by_source):
            logger.info(f"Exported {len(users_by_source)} users from source '{source}' to {output_file}")
        else:
            logger.error(f"Failed to export users to {output_file}")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='GitHub Contributors Data Collector')
    parser.add_argument('--module', '-m', type=str, help='Run specific module only')
    parser.add_argument('--force', '-f', action='store_true', help='Force run (overrides config)')
    parser.add_argument('--stats', '-s', action='store_true', help='Show statistics only')
    parser.add_argument('--token', '-t', type=str, help='GitHub token (overrides env/config)')
    parser.add_argument('--export', '-e', type=str, help='Export users by source (e.g., "2025 GitHub Trending")')
    parser.add_argument('--export-output', '-o', type=str, help='Output file for export')

    args = parser.parse_args()

    # Override the GitHub token
    if args.token:
        Config.GITHUB_TOKEN = args.token
        logger.info("Using GitHub token from command line")

    # Check the GitHub token
    if Config.GITHUB_TOKEN == 'your_github_token_here':
        logger.warning("GitHub token not configured. API rate limit will be severely restricted.")
        logger.warning("Please set GITHUB_TOKEN environment variable or use --token option")

    # Create the main controller
    collector = DataCollector()

    # Register the modules
    collector.register_module(
        'github_repo_contributors',
        GitHubRepoContributorsCollector,
        Config.MODULES.get('github_repo_contributors', {})
    )

    collector.register_module(
        'github_leaderboard',
        GitHubLeaderboardCollector,
        Config.MODULES.get('github_leaderboard', {})
    )

    collector.register_module(
        'gitstar_ranking_users',
        GitstarRankingUsersCollector,
        Config.MODULES.get('gitstar_ranking_users', {})
    )

    # When force is given, override the force flag of every module
    if args.force:
        logger.info("Force mode enabled, will run all enabled modules regardless of previous status")
        for module_name in Config.MODULES:
            Config.MODULES[module_name]['force'] = True

    # Export the data
    if args.export:
        collector.export_by_source(args.export, args.export_output)
        return

    # Show the statistics
    if args.stats:
        collector.show_stats()
        return

    # Run the modules
    if args.module:
        result = collector.run_module(args.module)
        logger.info(f"Result: {result}")
    else:
        results = collector.run_all_modules()
        logger.info(f"\nAll modules completed. Results: {results}")
        collector.show_stats()


if __name__ == '__main__':
    main()