#!/usr/bin/env python3
"""
GitHub repository dataset construction tool
Supports several collection modules, incremental collection, module execution state tracking and per-module switches
"""

import os
import json
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Optional, Any, Tuple, Callable
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('collector.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class GitHubRepoCollector:
    """Main GitHub repository collector class"""

    def __init__(self, output_file: str = "github_repos.json", state_file: str = "collector_state.json"):
        """
        Initialize the collector

        Args:
            output_file: Path to the output JSON file
            state_file: Path to the module state file
        """
        self.output_file = Path(output_file)
        self.state_file = Path(state_file)
        self.output_dir = self.output_file.parent
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Create the report directory
        self.report_dir = self.output_dir / "collection_report"
        self.report_dir.mkdir(parents=True, exist_ok=True)

        # List holding every repository
        self.repos: List[Dict[str, Any]] = []

        # Set used for deduplication
        self.full_names: Set[str] = set()

        # Module execution state
        self.module_states: Dict[str, Dict[str, Any]] = {}

        # Load the existing repository data and module states
        self._load_existing_data()

    def _create_session(self, token: Optional[str] = None) -> requests.Session:
        """Create a session with a retry strategy"""
        session = requests.Session()

        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )

        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        session.headers.update({
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'GitHub-Dataset-Collector'
        })

        if token:
            session.headers.update({'Authorization': f'token {token}'})

        return session

    def _load_existing_data(self):
        """Load the existing repository data and module states"""
        # Load the repository data
        if self.output_file.exists():
            try:
                with open(self.output_file, 'r', encoding='utf-8') as f:
                    self.repos = json.load(f)
                    self.full_names = {repo.get('full_name') for repo in self.repos if repo.get('full_name')}
                logger.info(f"Loaded {len(self.repos)} existing repositories")
            except Exception as e:
                logger.error(f"Failed to load the repository data: {e}")
                self.repos = []
                self.full_names = set()
        else:
            logger.info(f"The output file {self.output_file} does not exist, a new one will be created")

        # Load the module states
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.module_states = data.get('modules', {})
                logger.info(f"Loaded the execution state of {len(self.module_states)} modules")
            except Exception as e:
                logger.error(f"Failed to load the module states: {e}")
                self.module_states = {}

    def _save_module_state(self):
        """Save the module execution states"""
        try:
            state_to_save = {
                'modules': self.module_states,
                'last_updated': datetime.now().isoformat(),
                'total_repos': len(self.repos)
            }

            temp_file = self.state_file.with_suffix('.tmp')
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(state_to_save, f, ensure_ascii=False, indent=2)

            temp_file.replace(self.state_file)

        except Exception as e:
            logger.error(f"Failed to save the module states: {e}")

    def _save_repos(self):
        """Save every repository to the JSON file"""
        try:
            temp_file = self.output_file.with_suffix('.tmp')
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self.repos, f, ensure_ascii=False, indent=2)

            temp_file.replace(self.output_file)
            logger.info(f"Saved {len(self.repos)} repositories to {self.output_file}")

        except Exception as e:
            logger.error(f"Failed to save the repository data: {e}")

    def fetch_repo_info(self, full_name: str, token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Fetch repository information from the GitHub API, returned with the field layout of repo.json
        """
        url = f"https://api.github.com/repos/{full_name}"
        session = self._create_session(token)

        try:
            response = session.get(url)

            # Check the API rate limit
            if response.headers.get('X-RateLimit-Remaining') == '0':
                reset_time = int(response.headers.get('X-RateLimit-Reset', 0))
                wait_time = max(reset_time - time.time(), 0) + 1
                logger.warning(f"API rate limit reached, waiting {wait_time} seconds")
                time.sleep(wait_time)

            if response.status_code == 404:
                logger.warning(f"Repository {full_name} does not exist")
                return None
            elif response.status_code == 403:
                logger.error(f"Access denied, the API rate limit may have been reached: {full_name}")
                time.sleep(60)
                return None
            elif response.status_code == 451:
                logger.warning(f"Repository {full_name} is unavailable for legal reasons")
                return None

            response.raise_for_status()
            data = response.json()

            # Extract the fields with the layout of repo.json
            repo_data = {
                "id": data.get("id"),
                "node_id": data.get("node_id"),
                "name": data.get("name"),
                "full_name": data.get("full_name"),
                "private": data.get("private"),
                "owner": {
                    "login": data.get("owner", {}).get("login"),
                    "id": data.get("owner", {}).get("id"),
                    "node_id": data.get("owner", {}).get("node_id"),
                    "avatar_url": data.get("owner", {}).get("avatar_url"),
                    "gravatar_id": data.get("owner", {}).get("gravatar_id", ""),
                    "url": data.get("owner", {}).get("url"),
                    "html_url": data.get("owner", {}).get("html_url"),
                    "type": data.get("owner", {}).get("type"),
                    "user_view_type": data.get("owner", {}).get("user_view_type"),
                    "site_admin": data.get("owner", {}).get("site_admin", False)
                } if data.get("owner") else None,
                "html_url": data.get("html_url"),
                "description": data.get("description"),
                "fork": data.get("fork", False),
                "url": data.get("url"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "pushed_at": data.get("pushed_at"),
                "git_url": data.get("git_url"),
                "ssh_url": data.get("ssh_url"),
                "clone_url": data.get("clone_url"),
                "svn_url": data.get("svn_url"),
                "homepage": data.get("homepage"),
                "size": data.get("size", 0),
                "stargazers_count": data.get("stargazers_count", 0),
                "watchers_count": data.get("watchers_count", 0),
                "language": data.get("language"),
                "has_issues": data.get("has_issues", True),
                "has_projects": data.get("has_projects", True),
                "has_downloads": data.get("has_downloads", True),
                "has_wiki": data.get("has_wiki", False),
                "has_pages": data.get("has_pages", False),
                "has_discussions": data.get("has_discussions", False),
                "forks_count": data.get("forks_count", 0),
                "mirror_url": data.get("mirror_url"),
                "archived": data.get("archived", False),
                "disabled": data.get("disabled", False),
                "open_issues_count": data.get("open_issues_count", 0),
                "license": data.get("license"),
                "allow_forking": data.get("allow_forking", True),
                "is_template": data.get("is_template", False),
                "web_commit_signoff_required": data.get("web_commit_signoff_required", False),
                "has_pull_requests": data.get("has_pull_requests", True),
                "pull_request_creation_policy": data.get("pull_request_creation_policy", "all"),
                "topics": data.get("topics", []),
                "visibility": data.get("visibility", "public"),
                "forks": data.get("forks", 0),
                "open_issues": data.get("open_issues", 0),
                "watchers": data.get("watchers", 0),
                "default_branch": data.get("default_branch", "main"),
                "permissions": data.get("permissions"),  # Use the permissions field returned by the API, None when it is absent
                "custom_properties": data.get("custom_properties", {}),
                "network_count": data.get("network_count", 0),
                "subscribers_count": data.get("subscribers_count", 0)
            }

            return repo_data

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch repository {full_name}: {e}")
            return None
        finally:
            session.close()

    def add_repo(self, repo_info: Dict[str, Any], source: str = "GitHub") -> bool:
        """
        Add repository information to the dataset
        """
        full_name = repo_info.get('full_name')
        if not full_name:
            logger.error("The repository information lacks a full_name field")
            return False

        if full_name in self.full_names:
            logger.debug(f"Repository {full_name} already exists, skipping")
            return False

        # Add the source field when it is missing
        if 'source' not in repo_info:
            repo_info['source'] = source

        self.repos.append(repo_info)
        self.full_names.add(full_name)
        self._save_repos()

        logger.info(f"Repository added: {full_name}")
        return True

    def should_run_module(self, module_name: str, module_config: Dict[str, Any]) -> bool:
        """
        Decide whether the module has to run
        """
        # Check the module switch
        if not module_config.get('enabled', True):
            logger.info(f"Module {module_name} is disabled, skipping")
            return False

        # Check the force flag
        if module_config.get('force', False):
            logger.info(f"Module {module_name} runs in forced mode")
            return True

        # Check the empty-field repair mode - this mode must always run
        if module_config.get('check_empty', False) or module_config.get('only_empty', False):
            logger.info(f"Module {module_name} runs in empty-field repair mode")
            return True

        # First run
        if module_name not in self.module_states:
            logger.info(f"Module {module_name} runs for the first time")
            return True

        # Check the state of the previous run
        last_run = self.module_states[module_name]
        last_status = last_run.get('status', 'unknown')

        # Run again when the previous run failed or only partly succeeded
        if last_status in ('failed', 'partial'):
            logger.info(f"The previous run of module {module_name} ended as {last_status}, running it again")
            return True

        # Already completed successfully, skip it
        logger.info(f"Module {module_name} already ran successfully on {last_run.get('last_run_date', 'unknown')}, skipping")
        return False

    def update_module_state(self, module_name: str, result: Dict[str, Any]):
        """
        Update the module execution state
        """
        self.module_states[module_name] = {
            'last_run_date': datetime.now().isoformat(),
            'status': result.get('status', 'unknown'),
            'collected': result.get('collected', 0),
            'skipped': result.get('skipped', 0),
            'failed': result.get('failed', 0),
            'new_repos_count': result.get('new_repos_count', 0),
            'message': result.get('message', '')
        }
        self._save_module_state()

    def collect_from_module(self, module: Any, module_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Collect repositories from the given collection module

        Args:
            module: Module object
            module_config: Module configuration

        Returns:
            Dictionary describing the execution result
        """
        # Read the module name from the module object
        module_name = module.__name__.split('.')[-1]  # Module name without the package prefix

        # Check whether the module has to run
        if not self.should_run_module(module_name, module_config):
            return {
                'module': module_name,
                'status': 'skipped',
                'message': 'Module skipped'
            }

        logger.info(f"Running collection module: {module_name}")

        # Reload the existing data before every run
        self._load_existing_data()

        # Call the collect function of the module
        repos_to_collect = module.collect(module_config=module_config)

        if not repos_to_collect:
            logger.warning(f"Module {module_name} returned an empty list")
            result = {
                'module': module_name,
                'status': 'warning',
                'collected': 0,
                'skipped': 0,
                'failed': 0,
                'new_repos_count': 0,
                'message': 'The module returned an empty list'
            }
            self.update_module_state(module_name, result)
            return result

        logger.info(f"Module {module_name} returned {len(repos_to_collect)} repositories to process")

        # Process the repositories one by one
        collected = 0
        skipped = 0
        failed = 0
        new_repos = []

        token = module_config.get('token')
        source = module_config.get('source', module_name)

        for i, repo_item in enumerate(repos_to_collect, 1):
            full_name = None
            custom_source = None  # Holds a custom source when the module provides one

            # Extract full_name and the possible custom source uniformly
            if isinstance(repo_item, dict):
                full_name = repo_item.get('full_name')
                custom_source = repo_item.get('source')  # Keep the custom source
            elif isinstance(repo_item, str) and '/' in repo_item:
                full_name = repo_item

            if not full_name:
                logger.warning(f"Ignoring invalid repository entry: {repo_item}")
                failed += 1
                continue

            # Deduplication check
            if full_name in self.full_names:
                logger.debug(f"Repository {full_name} already exists, skipping")
                skipped += 1
                continue

            # Fetch the complete information from the API
            api_repo_info = self.fetch_repo_info(full_name, token)
            if api_repo_info:
                # A custom source overrides the default one
                if custom_source:
                    api_repo_info['source'] = custom_source

                if self.add_repo(api_repo_info, source):
                    collected += 1
                    new_repos.append(full_name)
                else:
                    failed += 1
            else:
                failed += 1

            # Stay clear of the API rate limit
            time.sleep(0.5)

            if i % 10 == 0:
                logger.info(f"Progress: {i}/{len(repos_to_collect)} collected: {collected}, skipped: {skipped}, failed: {failed}")

        # Determine the status
        if failed == 0:
            status = 'success'
        elif collected > 0:
            status = 'partial'
        else:
            status = 'failed'

        result = {
            'module': module_name,
            'collected': collected,
            'skipped': skipped,
            'failed': failed,
            'new_repos_count': len(new_repos),
            'status': status,
            'message': f'Processing complete, {collected} repositories added'
        }

        logger.info(f"Module {module_name} finished: {collected} collected, {skipped} skipped, {failed} failed")

        self.update_module_state(module_name, result)

    def collect_all(self, modules: List[Tuple[Any, Dict[str, Any]]]):
        """
        Run several collection modules

        Args:
            modules: List of modules, each element being (module_object, module_config)
        """
        results = []
        for module_obj, module_config in modules:
            result = self.collect_from_module(module_obj, module_config)
            if result:
                results.append(result)
            time.sleep(2)

        # Save the summary report to the collection_report directory
        report_path = self.report_dir / f"collection_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump({
                'timestamp': datetime.now().isoformat(),
                'output_file': str(self.output_file),
                'total_repos': len(self.repos),
                'new_repos_this_run': sum(r.get('collected', 0) for r in results if r),
                'modules_results': results,
                'module_states': self.module_states
            }, f, ensure_ascii=False, indent=2)

        logger.info(f"Report saved to: {report_path}")
        logger.info(f"Every module finished, {sum(r.get('collected', 0) for r in results if r)} repositories added in this run")
        logger.info(f"{len(self.repos)} repositories in total")

        return results

    def list_modules_status(self):
        """List the execution state of every module"""
        if not self.module_states:
            logger.info("No module execution record yet")
            return

        logger.info("=" * 60)
        logger.info("Module execution state:")
        logger.info("=" * 60)

        for module_name, state in sorted(self.module_states.items()):
            status = state.get('status', 'unknown')
            status_symbol = "✅" if status == 'success' else "⚠️" if status == 'partial' else "❌"
            last_date = state.get('last_run_date', 'unknown')[:19]
            collected = state.get('collected', 0)
            failed = state.get('failed', 0)

            logger.info(f"{status_symbol} {module_name}")
            logger.info(f"   Status: {status}, last run: {last_date}")
            logger.info(f"   Collected: {collected}, failed: {failed}")

        logger.info("=" * 60)
        logger.info(f"Total repositories: {len(self.repos)}")

    def reset_module(self, module_name: str):
        """Reset the state of the given module"""
        if module_name in self.module_states:
            del self.module_states[module_name]
            self._save_module_state()
            logger.info(f"State of module {module_name} has been reset")
            return True
        else:
            logger.warning(f"Module {module_name} does not exist or has no state record")
            return False


def main():
    """Main entry point"""
    import argparse
    import importlib

    parser = argparse.ArgumentParser(description='GitHub repository dataset construction tool')
    parser.add_argument('--output', '-o', default='github_repos.json', help='Path to the output JSON file')
    parser.add_argument('--state', '-s', default='collector_state.json', help='Path to the module state file')
    parser.add_argument('--global-token', help='Global GitHub API token (overridden by a module-specific token)')
    parser.add_argument('--modules', '-m', nargs='+', help='Modules to run')
    parser.add_argument('--force', '-f', action='store_true', help='Force every module to run again')
    parser.add_argument('--list', '-l', action='store_true', help='List the module execution states')
    parser.add_argument('--reset-module', help='Reset the state of the given module')
    parser.add_argument('--disable-module', nargs='+', help='Modules to disable')

    # Parameters specific to core_developers_enricher
    parser.add_argument('--check-empty', action='store_true',
                        help='Revisit the repositories whose core_developers field is empty')
    parser.add_argument('--only-empty', action='store_true',
                        help='Process only the repositories whose core_developers field is empty')

    args = parser.parse_args()

    collector = GitHubRepoCollector(output_file=args.output, state_file=args.state)

    if args.list:
        collector.list_modules_status()
        return

    if args.reset_module:
        collector.reset_module(args.reset_module)
        return

    # Import the modules dynamically
    modules_to_run = []

    if args.modules:
        for module_name in args.modules:
            try:
                # Import the module object
                module = importlib.import_module(f'collectors.{module_name}')

                if hasattr(module, 'collect'):
                    # Read the module configuration
                    module_config = getattr(module, 'MODULE_CONFIG', {}).copy()
                    module_config['output_file'] = args.output

                    # Command line arguments override the module configuration
                    if args.force:
                        module_config['force'] = True

                    # Support the new parameters of core_developers_enricher
                    if args.check_empty:
                        module_config['check_empty'] = True
                    if args.only_empty:
                        module_config['only_empty'] = True

                    # Token priority: the module token wins over the global token
                    if 'token' not in module_config and args.global_token:
                        module_config['token'] = args.global_token

                    modules_to_run.append((module, module_config))
                    logger.info(f"Module loaded: {module_name}")
            except ImportError as e:
                logger.error(f"Failed to load module {module_name}: {e}")
    else:
        # By default every available module runs
        import pkgutil

        try:
            import collectors
            for finder, name, ispkg in pkgutil.iter_modules(collectors.__path__):
                try:
                    # Import the module object
                    module = importlib.import_module(f'collectors.{name}')

                    if hasattr(module, 'collect'):
                        # Read the module configuration
                        module_config = getattr(module, 'MODULE_CONFIG', {}).copy()
                        module_config['output_file'] = args.output

                        # Check whether the module is disabled
                        if args.disable_module and name in args.disable_module:
                            module_config['enabled'] = False

                        # Command line arguments override the configuration
                        if args.force:
                            module_config['force'] = True

                        # Support the new parameters of core_developers_enricher
                        if args.check_empty:
                            module_config['check_empty'] = True
                        if args.only_empty:
                            module_config['only_empty'] = True

                        # Token priority: the module token wins over the global token
                        if 'token' not in module_config and args.global_token:
                            module_config['token'] = args.global_token

                        if module_config.get('enabled', True):
                            modules_to_run.append((module, module_config))
                            logger.info(f"Module loaded: {name}")
                        else:
                            logger.info(f"Module {name} is disabled")
                except Exception as e:
                    logger.error(f"Failed to load module {name}: {e}")
        except ImportError:
            logger.warning("No collectors module found, please create the collectors package")

    if not modules_to_run:
        logger.error("There is no module to run")
        return

    collector.collect_all(modules_to_run)


if __name__ == '__main__':
    main()