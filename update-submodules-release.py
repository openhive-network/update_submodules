#!/usr/bin/env python3

import os
import sys
import yaml
import git
import argparse
import logging
import requests
import configparser
import subprocess
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
from collections import defaultdict
from ruamel.yaml import YAML
from git import Repo, GitCommandError

# Configuration
CONFIG_FILE = 'repos.yaml'
BASE_DIR = 'repositories'  # Directory to clone repositories into

@dataclass
class RepoOperationResult:
    """Track the result of processing a single repository."""
    repo_url: str
    branch_name: Optional[str] = None
    submodules_updated: Dict = None
    yaml_files_updated: Dict = None
    tag_to_create: Optional[str] = None
    success: bool = True
    error_message: Optional[str] = None
    repo_object: Optional[Repo] = None  # Store the repo object for Phase 2
    settings: Dict = None  # Store repository settings for Phase 2
    rebase_performed: bool = False  # Track if rebase was done
    conflicts_resolved: List = None  # Track resolved conflicts
    sanity_check_passed: bool = True  # Track sanity check result

    def __post_init__(self):
        if self.submodules_updated is None:
            self.submodules_updated = {}
        if self.yaml_files_updated is None:
            self.yaml_files_updated = {}
        if self.settings is None:
            self.settings = {}
        if self.conflicts_resolved is None:
            self.conflicts_resolved = []

def setup_logging(log_level):
    """Configure the logging settings."""
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        print(f"Invalid log level: {log_level}")
        sys.exit(1)
    logging.basicConfig(
        level=numeric_level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('update_submodules.log')  # File-based logging
        ]
    )

def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Update Git repositories and their submodules.")
    parser.add_argument('--dry-run', '-d', action='store_true', help='Perform a dry run without committing or pushing changes.')
    parser.add_argument('--log-level', '-l', default='INFO', help='Set the logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL). Default is INFO.')
    parser.add_argument('--tag', '-t', type=str, help='Name of the Git tag to create for each repository.')
    parser.add_argument('--retag', '-r', action='store_true', help='Overwrite existing tags with the same name when using --tag.')
    parser.add_argument('--no-push-tags', action='store_true', help='Create tags locally but do not push them to remote (useful for release workflow)')
    parser.add_argument('--cleanup', action='store_true', help='Clean up branches, tags, and MRs from a previous failed run.')
    parser.add_argument('--cleanup-tag', type=str, help='Specific tag to clean up when using --cleanup.')
    parser.add_argument('--release-to-master', action='store_true', help='Rebase source commits onto target branch for release')
    parser.add_argument('--force', action='store_true', help='Skip prompts and force continue on warnings')
    return parser.parse_args()

def get_gitlab_credentials():
    """Get GitLab API credentials from environment variables or config file."""
    # Priority: environment variables > config file
    token = os.environ.get('GITLAB_TOKEN')
    api_url = os.environ.get('GITLAB_API_URL', 'https://gitlab.syncad.com/api/v4')
    
    if not token:
        # Fall back to config.ini if exists
        config = configparser.ConfigParser()
        if os.path.exists('config.ini'):
            config.read('config.ini')
            token = config.get('gitlab', 'token', fallback=None)
            api_url = config.get('gitlab', 'api_url', fallback=api_url)
    
    return token, api_url

def get_gitlab_project_id(repo_url):
    """Get the GitLab project ID from the repository URL."""
    token, api_url = get_gitlab_credentials()
    if not token:
        return None
        
    # Extract namespace and project from URL
    if repo_url.startswith('git@'):
        # Format: git@gitlab.syncad.com:namespace/project.git
        path = repo_url.split(':')[1]
        if path.endswith('.git'):
            path = path[:-4]  # Remove exactly '.git' suffix
        parts = path.split('/')
        namespace = parts[0]
        project = parts[1] if len(parts) > 1 else parts[0]
    elif repo_url.startswith('http'):
        # Format: https://gitlab.syncad.com/namespace/project.git
        path = repo_url
        if path.endswith('.git'):
            path = path[:-4]  # Remove exactly '.git' suffix
        parts = path.split('/')
        namespace = parts[-2]
        project = parts[-1]
    else:
        logging.error(f"Unsupported repository URL format: {repo_url}")
        return None
    
    # Query GitLab API for project ID
    url = f"{api_url}/projects/{namespace}%2F{project}"
    headers = {"PRIVATE-TOKEN": token}
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()['id']
        else:
            logging.error(f"Failed to get project ID for {namespace}/{project}: {response.status_code}")
            return None
    except Exception as e:
        logging.error(f"Error getting project ID: {e}")
        return None

def delete_gitlab_tag_via_api(repo_url, tag):
    """Delete a tag using the GitLab API (works for protected tags)."""
    token, api_url = get_gitlab_credentials()
    if not token:
        logging.warning("No GitLab token available, cannot delete protected tags via API")
        return False
        
    project_id = get_gitlab_project_id(repo_url)
    if not project_id:
        return False
        
    url = f"{api_url}/projects/{project_id}/repository/tags/{tag}"
    headers = {"PRIVATE-TOKEN": token}
    
    try:
        response = requests.delete(url, headers=headers)
        if response.status_code == 204:
            logging.info(f"Successfully deleted tag '{tag}' via GitLab API")
            return True
        elif response.status_code == 404:
            logging.debug(f"Tag '{tag}' not found on remote")
            return True  # Tag doesn't exist, which is what we wanted
        else:
            logging.error(f"Failed to delete tag '{tag}' via API: {response.status_code} {response.text}")
            return False
    except Exception as e:
        logging.error(f"Error deleting tag via API: {e}")
        return False

# ============================================================================
# NEW FUNCTIONS FOR RELEASE WORKFLOW
# ============================================================================

def find_rebase_base(repo_path: str, source_ref: str = 'develop',
                     target_branch: str = 'origin/master', max_commits: int = 500) -> Optional[str]:
    """
    Find the commit on source_ref that corresponds to a commit on target_branch.

    Walks backwards from HEAD of both branches looking for matching commits by patch-id.
    This handles rebased commits and doesn't assume any specific commit patterns.

    Args:
        repo_path: Path to the repository
        source_ref: Reference to rebase from (branch, tag, or commit)
        target_branch: Branch to rebase onto
        max_commits: Maximum number of commits to check on each branch

    Returns:
        Commit hash on source_ref that matches a commit on target_branch, or None
    """
    original_dir = os.getcwd()

    def get_commit_patch_id(commit: str) -> Optional[str]:
        """Helper to get patch-id for a commit, handling binary data properly."""
        try:
            # Get the patch (don't decode as text - may have binary data)
            result = subprocess.run([
                'git', 'show', commit, '--format=', '--patch'
            ], capture_output=True, check=True)

            if not result.stdout:
                return None

            # Calculate patch-id (work with bytes)
            patch_result = subprocess.run([
                'git', 'patch-id'
            ], input=result.stdout, capture_output=True)

            if patch_result.stdout:
                return patch_result.stdout.decode('utf-8', errors='ignore').split()[0]
        except subprocess.CalledProcessError:
            pass
        return None

    try:
        os.chdir(repo_path)

        # First, resolve source_ref to a commit
        result = subprocess.run([
            'git', 'rev-parse', source_ref
        ], capture_output=True, text=True, check=True)
        source_head = result.stdout.strip()

        logging.debug(f"Source ref {source_ref} resolves to {source_head[:8]}")

        # Get commits from source_ref going backwards
        result = subprocess.run([
            'git', 'rev-list', '--max-count', str(max_commits), source_ref
        ], capture_output=True, text=True, check=True)

        source_commits = result.stdout.strip().split('\n') if result.stdout.strip() else []
        logging.info(f"Checking {len(source_commits)} commits from {source_ref}")

        # Build patch-id map for source commits
        source_patch_map = {}
        for commit in source_commits:
            patch_id = get_commit_patch_id(commit)
            if patch_id:
                source_patch_map[patch_id] = commit

        # Get commits from target_branch going backwards
        result = subprocess.run([
            'git', 'rev-list', '--max-count', str(max_commits), target_branch
        ], capture_output=True, text=True, check=True)

        target_commits = result.stdout.strip().split('\n') if result.stdout.strip() else []
        logging.info(f"Checking {len(target_commits)} commits from {target_branch}")

        # Walk through target commits looking for matches
        for i, target_commit in enumerate(target_commits):
            target_patch_id = get_commit_patch_id(target_commit)

            if target_patch_id and target_patch_id in source_patch_map:
                matching_source = source_patch_map[target_patch_id]

                # Log what we found
                result = subprocess.run([
                    'git', 'log', '--oneline', '-1', matching_source
                ], capture_output=True, text=True)
                source_msg = result.stdout.strip()
                logging.info(f"Found matching commit: {source_msg}")

                # If this is the HEAD of target and matches source HEAD, no rebase needed
                if i == 0 and matching_source == source_head:
                    logging.info(f"Source HEAD matches target HEAD - no commits to rebase!")

                return matching_source

        logging.warning(f"No matching commits found in first {max_commits} commits")

        # Fall back to merge-base as last resort
        try:
            result = subprocess.run([
                'git', 'merge-base', target_branch, source_ref
            ], capture_output=True, text=True, check=True)
            merge_base = result.stdout.strip()
            logging.warning(f"Using merge-base as fallback: {merge_base[:8]}")
            return merge_base
        except:
            return None

    except subprocess.CalledProcessError as e:
        logging.error(f"Error finding rebase base: {e}")
        return None
    finally:
        os.chdir(original_dir)


def perform_release_rebase(repo_path: str, rebase_base: str, source_branch: str,
                          target_branch: str, commit_count: int = None) -> Tuple[bool, List[str]]:
    """
    Rebase the current branch (created from source_branch) onto target_branch.

    Args:
        repo_path: Path to the repository
        rebase_base: Commit to use as the starting point for rebase
        source_branch: Not used anymore (kept for compatibility)
        target_branch: Branch to rebase onto (e.g., origin/master)
        commit_count: Number of commits being rebased (for setting iteration limit)

    Returns:
        Tuple of (success: bool, conflicts_resolved: list)
    """
    original_dir = os.getcwd()
    conflicts_resolved = []

    try:
        os.chdir(repo_path)

        # Set up environment to prevent editor prompts
        env = os.environ.copy()
        env['GIT_EDITOR'] = 'true'  # Use 'true' command as editor (always succeeds without opening)
        env['EDITOR'] = 'true'

        # Start rebase
        # We're on a release branch (created from develop) and want to rebase it onto master
        # This preserves the original develop branch while applying commits to release branch

        logging.info(f"Rebasing current branch onto {target_branch} from {rebase_base[:8]}...")

        # Rebase current branch onto target, keeping only commits after rebase_base
        result = subprocess.run([
            'git', '-c', 'core.editor=true', 'rebase', '--onto', target_branch, rebase_base, 'HEAD'
        ], capture_output=True, text=True, env=env)

        if result.returncode == 0:
            logging.info("Rebase completed without conflicts")
            return True, conflicts_resolved

        # Handle conflicts in a loop
        # Set a generous limit: 3x the number of commits (to handle multiple loops per commit)
        # Default to 1000 if commit_count not provided
        max_iterations = (commit_count * 3) if commit_count else 1000
        iteration = 0
        consecutive_same_state = 0
        last_status = None

        while iteration < max_iterations:
            iteration += 1

            # Check if rebase is still in progress
            if not (os.path.exists('.git/rebase-merge') or os.path.exists('.git/rebase-apply')):
                logging.debug("Rebase finished - no rebase in progress")
                break

            # Get current status
            result = subprocess.run(['git', 'status', '--porcelain'],
                                  capture_output=True, text=True)
            status_lines = result.stdout.strip().split('\n') if result.stdout.strip() else []
            current_status = result.stdout.strip()

            # Detect if we're stuck in the same state
            if current_status == last_status:
                consecutive_same_state += 1
                if consecutive_same_state > 10:  # Increased from 5 to 10 to allow for slower operations
                    logging.error(f"Stuck in same state for {consecutive_same_state} iterations")
                    logging.error(f"Status: {current_status[:200]}")
                    # Try to get more info about what's wrong
                    info_result = subprocess.run(['git', 'status'], capture_output=True, text=True)
                    logging.error(f"Full status:\n{info_result.stdout[:1000]}")

                    # Also check what git rebase --continue says
                    cherry_pick_result = subprocess.run(['git', 'rebase', '--continue'],
                                                   capture_output=True, text=True, env=env)
                    logging.error(f"Rebase continue output: {cherry_pick_result.stderr[:500]}")

                    return False, conflicts_resolved
            else:
                consecutive_same_state = 0
                last_status = current_status

            if iteration % 100 == 0:
                logging.info(f"Rebase progress: iteration {iteration}/{max_iterations}")

            logging.debug(f"Rebase iteration {iteration}, status lines: {len(status_lines)}")

            conflicts_found = False
            for line in status_lines:
                if line.startswith('UU ') or line.startswith('AA '):
                    # Both modified - conflict
                    file_path = line[3:].strip()

                    # Check if it's a submodule by looking at .gitmodules
                    is_submodule = False
                    try:
                        # Get all submodule paths from .gitmodules
                        result = subprocess.run([
                            'git', 'config', '--file', '.gitmodules',
                            '--get-regexp', 'path'
                        ], capture_output=True, text=True)
                        if result.returncode == 0:
                            # Check if our file_path matches any submodule path
                            for line in result.stdout.strip().split('\n'):
                                if line and file_path in line:
                                    # Extract the actual path value
                                    parts = line.split()
                                    if len(parts) >= 2 and parts[-1] == file_path:
                                        is_submodule = True
                                        break
                    except:
                        pass

                    if is_submodule:
                        # Submodule conflict - simpler approach
                        logging.debug(f"Resolving submodule conflict: {file_path}")

                        # During rebase of develop onto master, we want to keep develop's submodule versions
                        # The simplest approach is to use git rm + git add to accept the incoming version

                        # Remove the conflicted submodule entry
                        subprocess.run(['git', 'rm', '--cached', file_path],
                                     capture_output=True, text=True)

                        # Get the commit that develop wants for this submodule
                        theirs_commit_result = subprocess.run([
                            'git', 'ls-tree', 'REBASE_HEAD', file_path
                        ], capture_output=True, text=True)

                        if theirs_commit_result.returncode == 0 and theirs_commit_result.stdout:
                            # Extract the commit hash from the output
                            # Format is: "160000 commit <hash>\t<path>"
                            parts = theirs_commit_result.stdout.split()
                            if len(parts) >= 3:
                                theirs_commit = parts[2]
                                logging.debug(f"Will use {file_path} at commit {theirs_commit} from develop")

                                # Update the index to point to this commit
                                # This is equivalent to accepting "theirs" version
                                update_result = subprocess.run([
                                    'git', 'update-index', '--add', '--cacheinfo',
                                    '160000', theirs_commit, file_path
                                ], capture_output=True, text=True)

                                if update_result.returncode != 0:
                                    logging.warning(f"Could not update index for {file_path}, trying alternative")
                                    # Alternative: just stage the current state
                                    subprocess.run(['git', 'add', file_path], capture_output=True)
                        else:
                            # Fallback: just add the current state
                            logging.debug(f"Could not determine theirs commit for {file_path}, using current state")
                            subprocess.run(['git', 'add', file_path], capture_output=True)

                        conflicts_resolved.append(f"submodule:{file_path}")
                        conflicts_found = True
                    elif file_path.endswith(('.yml', '.yaml')):
                        # YAML conflict - take theirs (from source branch)
                        logging.debug(f"Resolving YAML conflict: {file_path}")
                        subprocess.run(['git', 'checkout', '--theirs', file_path], check=True)
                        subprocess.run(['git', 'add', file_path], check=True)
                        conflicts_resolved.append(f"yaml:{file_path}")
                        conflicts_found = True
                    else:
                        # Any other file conflict - take theirs (from source branch) for release
                        # Log a warning since we don't expect conflicts in other files
                        logging.warning(f"Unexpected conflict in {file_path} - taking version from develop branch")
                        subprocess.run(['git', 'checkout', '--theirs', file_path], check=True)
                        subprocess.run(['git', 'add', file_path], check=True)
                        conflicts_resolved.append(f"other:{file_path}")
                        conflicts_found = True

            if not conflicts_found:
                # No conflicts found, but rebase might still be in progress
                # Check if we need to continue
                if os.path.exists('.git/rebase-merge') or os.path.exists('.git/rebase-apply'):
                    # Try to continue
                    result = subprocess.run(['git', '-c', 'core.editor=true', 'rebase', '--continue'],
                                          capture_output=True, text=True, env=env)
                    if result.returncode != 0:
                        if 'nothing to commit' in result.stderr:
                            # Empty commit, skip it
                            logging.debug("Skipping empty commit")
                            subprocess.run(['git', '-c', 'core.editor=true', 'rebase', '--skip'],
                                         check=True, env=env)
                        elif 'You must edit all merge conflicts' in result.stderr or 'fix conflicts' in result.stderr.lower():
                            # There are still unresolved conflicts, but we didn't detect them
                            # This might be a different type of conflict marker
                            logging.warning(f"Unhandled conflict detected at iteration {iteration}")
                            logging.warning(f"stderr: {result.stderr[:500]}")
                            # Check if we're stuck in a loop
                            if iteration > 50:
                                logging.error("Possible infinite loop detected - same conflict not resolving")
                                return False, conflicts_resolved
                        else:
                            # Some other error
                            logging.debug(f"Rebase continue failed: {result.stderr[:200]}")
                            # Don't loop infinitely on unknown errors
                            if iteration > 50 and result.returncode != 0:
                                logging.error(f"Repeated rebase errors - aborting after {iteration} iterations")
                                logging.error(f"Last error: {result.stderr}")
                                return False, conflicts_resolved
                else:
                    break
            else:
                # Continue rebase after resolving conflicts
                result = subprocess.run(['git', '-c', 'core.editor=true', 'rebase', '--continue'],
                                      capture_output=True, text=True, env=env)
                if result.returncode != 0:
                    if 'nothing to commit' in result.stderr:
                        # Empty commit after conflict resolution, skip it
                        logging.debug("Skipping empty commit after conflict resolution")
                        subprocess.run(['git', '-c', 'core.editor=true', 'rebase', '--skip'],
                                     check=True, env=env)
                    elif 'You must edit all merge conflicts' in result.stderr or 'fix conflicts' in result.stderr.lower():
                        # We resolved conflicts but git says there are still conflicts
                        logging.warning(f"Conflicts remain after resolution attempt at iteration {iteration}")
                        logging.warning(f"stderr: {result.stderr[:500]}")
                        # Check for infinite loop
                        if iteration > 50:
                            logging.error("Stuck in conflict resolution loop")
                            return False, conflicts_resolved
                    else:
                        logging.debug(f"Rebase continue failed after conflict resolution: {result.stderr[:200]}")
                        # Don't loop infinitely
                        if iteration > 50:
                            logging.error(f"Repeated errors after conflict resolution - aborting")
                            logging.error(f"Last error: {result.stderr}")
                            return False, conflicts_resolved
                    # Otherwise, we'll loop again to handle next conflict

        if iteration >= max_iterations:
            logging.error(f"Rebase failed - exceeded maximum iterations ({max_iterations})")
            return False, conflicts_resolved

        logging.info(f"Rebase completed with {len(conflicts_resolved)} auto-resolved conflicts")
        return True, conflicts_resolved

    except subprocess.CalledProcessError as e:
        logging.error(f"Error during rebase: {e}")
        # Try to abort the rebase
        try:
            subprocess.run(['git', 'rebase', '--abort'], check=True)
        except:
            pass
        return False, conflicts_resolved
    finally:
        os.chdir(original_dir)


def verify_release_changes(repo_path: str, source_ref: str, release_ref: str,
                          expected_patterns: List[str]) -> Tuple[bool, Optional[str]]:
    """
    Verify that only expected files changed between source and release branches.

    Args:
        repo_path: Path to the repository
        source_ref: Reference to compare from (e.g., original develop HEAD)
        release_ref: Reference to compare to (e.g., current HEAD after rebase)
        expected_patterns: List of file patterns that are expected to change

    Returns:
        Tuple of (is_valid: bool, diff_info: str or None)
    """
    original_dir = os.getcwd()

    try:
        os.chdir(repo_path)

        # Get diff between source and release
        result = subprocess.run([
            'git', 'diff', '--name-only', source_ref, release_ref
        ], capture_output=True, text=True, check=True)

        if not result.stdout.strip():
            # No differences - this is fine, especially for repos without submodules
            logging.info("No differences between source and release branches - all changes were from rebase only")
            return True, None

        changed_files = result.stdout.strip().split('\n')
        unexpected_files = []

        for file in changed_files:
            is_expected = False

            # Check if file matches any expected pattern
            for pattern in expected_patterns:
                if file == pattern or file.endswith(pattern):
                    is_expected = True
                    break

            # Check if it's a submodule by checking .gitmodules
            if not is_expected:
                try:
                    result = subprocess.run([
                        'git', 'config', '--file', '.gitmodules', '--get-regexp', 'path'
                    ], capture_output=True, text=True)

                    if result.stdout:
                        for line in result.stdout.strip().split('\n'):
                            if file in line:
                                is_expected = True
                                break
                except:
                    pass

            if not is_expected:
                unexpected_files.append(file)

        if unexpected_files:
            # Get the actual diffs for unexpected files
            result = subprocess.run([
                'git', 'diff', source_ref, release_ref, '--'
            ] + unexpected_files, capture_output=True, text=True, check=True)

            full_diff = result.stdout

            # If diff is small, return it directly; otherwise save to file
            if len(full_diff) < 1000:
                return False, f"Unexpected files changed:\n{', '.join(unexpected_files)}\n\nDiff:\n{full_diff}"
            else:
                # Write to file
                repo_name = os.path.basename(repo_path)
                diff_file = f'/tmp/unexpected_diffs_{repo_name}.diff'
                with open(diff_file, 'w') as f:
                    f.write(f"Unexpected files changed: {', '.join(unexpected_files)}\n\n")
                    f.write(full_diff)
                return False, f"Unexpected files changed: {', '.join(unexpected_files)}\nLarge diff saved to: {diff_file}"

        logging.info(f"Sanity check passed - {len(changed_files)} files changed, all expected")
        return True, None

    except subprocess.CalledProcessError as e:
        logging.error(f"Error during sanity check: {e}")
        return False, f"Error running sanity check: {e}"
    finally:
        os.chdir(original_dir)

# ============================================================================
# END OF NEW FUNCTIONS
# ============================================================================

def load_config(config_path):
    """Load the YAML configuration file."""
    if not os.path.exists(config_path):
        logging.error(f"Configuration file '{config_path}' does not exist.")
        sys.exit(1)
    with open(config_path, 'r') as file:
        try:
            config = yaml.safe_load(file)
            if not isinstance(config, dict):
                logging.error("Configuration file must contain a dictionary of repository URLs and their settings.")
                sys.exit(1)
            return config
        except yaml.YAMLError as e:
            logging.error(f"Error parsing YAML configuration: {e}")
            sys.exit(1)

def validate_config(config):
    """Validate the configuration for correctness."""
    for repo_url, settings in config.items():
        if not isinstance(repo_url, str) or not repo_url.strip():
            logging.error(f"Invalid repository URL: '{repo_url}'")
            sys.exit(1)
        if not isinstance(settings, dict):
            logging.error(f"Settings for repository '{repo_url}' must be a dictionary.")
            sys.exit(1)
        ref = settings.get('ref')
        ref_from_dir = settings.get('ref_from_dir')
        if (ref and ref_from_dir) or (not ref and not ref_from_dir):
            logging.error(f"Repository '{repo_url}' must specify either 'ref' or 'ref_from_dir', but not both or neither.")
            sys.exit(1)
        # Validate 'automerge' if present
        automerge = settings.get('automerge')
        if automerge is not None and not isinstance(automerge, bool):
            logging.error(f"'automerge' for repository '{repo_url}' must be a boolean value.")
            sys.exit(1)
        # Validate 'create_merge_request' if present
        create_mr = settings.get('create_merge_request')
        if create_mr is not None and not isinstance(create_mr, bool):
            logging.error(f"'create_merge_request' for repository '{repo_url}' must be a boolean value.")
            sys.exit(1)
        # Validate 'update_yaml' if present
        update_yaml = settings.get('update_yaml')
        if update_yaml:
            if not isinstance(update_yaml, list):
                logging.error(f"'update_yaml' for repository '{repo_url}' must be a list of update instructions.")
                sys.exit(1)
            for edit in update_yaml:
                if not isinstance(edit, dict):
                    logging.error(f"Each entry in 'update_yaml' for repository '{repo_url}' must be a dictionary.")
                    sys.exit(1)
                if 'filename' not in edit or 'key_to_update' not in edit or 'submodule_referenced' not in edit:
                    logging.error(f"Each 'update_yaml' entry for repository '{repo_url}' must contain 'filename', 'key_to_update', and 'submodule_referenced'.")
                    sys.exit(1)
                if not isinstance(edit['filename'], str) or not edit['filename'].strip():
                    logging.error(f"Invalid 'filename' in 'update_yaml' for repository '{repo_url}'.")
                    sys.exit(1)
                if not isinstance(edit['key_to_update'], str) or not edit['key_to_update'].strip():
                    logging.error(f"Invalid 'key_to_update' in 'update_yaml' for repository '{repo_url}'.")
                    sys.exit(1)
                if not isinstance(edit['submodule_referenced'], str) or not edit['submodule_referenced'].strip():
                    logging.error(f"Invalid 'submodule_referenced' in 'update_yaml' for repository '{repo_url}'.")
                    sys.exit(1)
    logging.info("Configuration validation passed.")

def normalize_gitlab_url(url):
    """
    Normalize GitLab URLs to consistent git@ SSH format.

    This ensures that HTTPS and SSH URLs pointing to the same repository
    are recognized as identical for dependency matching.

    Examples:
        https://gitlab.syncad.com/hive/HAfAH.git -> git@gitlab.syncad.com:hive/HAfAH.git
        http://gitlab.syncad.com/hive/HAfAH.git  -> git@gitlab.syncad.com:hive/HAfAH.git
        git@gitlab.syncad.com:hive/HAfAH.git     -> git@gitlab.syncad.com:hive/HAfAH.git
    """
    if url.startswith('https://gitlab.syncad.com/'):
        # Convert HTTPS to SSH format
        url = url.replace('https://gitlab.syncad.com/', 'git@gitlab.syncad.com:')
    elif url.startswith('http://gitlab.syncad.com/'):
        # Convert HTTP to SSH format
        url = url.replace('http://gitlab.syncad.com/', 'git@gitlab.syncad.com:')

    return url

def resolve_submodule_url(parent_repo_url, submodule_url):
    """Resolve a submodule URL relative to the parent repo URL if necessary."""
    # If the submodule URL is already absolute (starts with git@, http, ssh, etc.),
    # normalize it and return
    if submodule_url.startswith('git@') or submodule_url.startswith('http://') \
       or submodule_url.startswith('https://') or submodule_url.startswith('ssh://'):
        return normalize_gitlab_url(submodule_url)

    # Split the parent URL into host part and path part.
    # Example:
    #   parent: git@gitlab.syncad.com:hive/reputation_tracker.git
    #   submodule: ../haf.git
    if ':' not in parent_repo_url:
        # If the parent URL doesn't fit the expected pattern, return submodule_url as-is.
        return submodule_url

    host_part, base_dir = parent_repo_url.split(':', 1)  # e.g., 'git@gitlab.syncad.com' and 'hive/reputation_tracker.git'

    # Combine the base directory with the submodule's relative URL
    combined_path = os.path.normpath(os.path.join(base_dir, submodule_url))

    # Construct the absolute URL
    absolute_url = f"{host_part}:{combined_path}"
    return absolute_url

def cleanup_existing_repo(repo):
    """Clean up an existing repository to ensure a clean state."""
    try:
        # Reset any uncommitted changes
        repo.git.reset('--hard', 'HEAD')
        
        # Clean untracked files and directories
        repo.git.clean('-fd')
        
        # Delete local branches from previous runs
        current_branch = repo.active_branch.name if not repo.head.is_detached else None
        for branch in repo.heads:
            if 'update-submodules' in branch.name:
                if branch.name != current_branch:
                    try:
                        repo.delete_head(branch, force=True)
                        logging.debug(f"Deleted local branch '{branch.name}'")
                    except GitCommandError as e:
                        logging.warning(f"Could not delete branch '{branch.name}': {e}")
        
        # Ensure we're on a valid branch (not detached HEAD)
        if repo.head.is_detached:
            # Try to checkout main or master
            for default_branch in ['main', 'master', 'develop']:
                if default_branch in [b.name for b in repo.heads]:
                    repo.git.checkout(default_branch)
                    logging.debug(f"Checked out default branch '{default_branch}'")
                    break
        
        # Fetch latest from origin with prune
        repo.remotes.origin.fetch(prune=True)
        
    except GitCommandError as e:
        logging.error(f"Error cleaning up repository: {e}")
        raise

def clone_repo(repo_url, clone_path):
    """Clone the repository if not already cloned, or clean up existing clone."""
    if os.path.exists(clone_path):
        logging.info(f"Repository '{repo_url}' already cloned at '{clone_path}'.")
        try:
            repo = Repo(clone_path)
            if repo.bare:
                logging.error(f"Repository at '{clone_path}' is bare. Expected a non-bare repository.")
                return None
            
            # Clean up the existing repository
            cleanup_existing_repo(repo)
            return repo
            
        except git.exc.InvalidGitRepositoryError:
            logging.warning(f"Directory '{clone_path}' is not a valid Git repository. Removing and re-cloning.")
            import shutil
            shutil.rmtree(clone_path)
            # Fall through to clone fresh
            
    # Clone fresh repository
    logging.info(f"Cloning repository '{repo_url}' into '{clone_path}'...")
    try:
        repo = Repo.clone_from(repo_url, clone_path)
        logging.debug(f"Cloned '{repo_url}' successfully.")
        return repo
    except GitCommandError as e:
        logging.error(f"Failed to clone repository '{repo_url}': {e}")
        return None

def parse_submodules(repo):
    """Parse submodules from a repository and return absolute URLs."""
    submodules = []
    gitmodules_path = os.path.join(repo.working_tree_dir, '.gitmodules')
    if os.path.exists(gitmodules_path):
        # Get the parent repo URL to resolve relative submodule URLs
        try:
            parent_repo_url = repo.remotes.origin.url
        except AttributeError:
            parent_repo_url = None

        for submodule in repo.submodules:
            sub_url = submodule.url
            if parent_repo_url:
                sub_url = resolve_submodule_url(parent_repo_url, sub_url)
            submodules.append(sub_url)
    return submodules

def build_dependency_graph(config):
    """Build a dependency graph based on submodules."""
    graph = defaultdict(list)
    # Normalize all repo URLs in the config for consistent matching
    repo_urls = set(normalize_gitlab_url(url) for url in config.keys())

    for repo_url in config.keys():
        # Normalize the repo URL for consistent processing
        normalized_repo_url = normalize_gitlab_url(repo_url)

        clone_path = os.path.join(BASE_DIR, get_repo_name(repo_url))
        repo = clone_repo(repo_url, clone_path)
        if repo is None:
            logging.error(f"Skipping repository '{repo_url}' due to cloning issues.")
            continue
        submodules = parse_submodules(repo)
        # Filter submodules to include only those in config (after normalization)
        filtered_submodules = [s for s in submodules if s in repo_urls]
        # Use the original repo_url as key to match config.keys()
        graph[repo_url].extend(filtered_submodules)
        logging.debug(f"Repository '{repo_url}' has submodules: {filtered_submodules}")

    return graph

def topological_sort(graph):
    """Perform a topological sort on the dependency graph."""
    in_degree = defaultdict(int)
    all_nodes = set(graph.keys())
    for node in graph:
        for neighbor in graph[node]:
            in_degree[neighbor] += 1
            all_nodes.add(neighbor)

    from collections import deque
    queue = deque([node for node in all_nodes if in_degree[node] == 0])
    sorted_list = []

    while queue:
        node = queue.popleft()
        sorted_list.append(node)
        for neighbor in graph.get(node, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(sorted_list) != len(all_nodes):
        logging.error("Cycle detected or missing dependencies in the dependency graph.")
        sys.exit(1)

    return sorted_list

def get_repo_name(repo_url):
    """Extract the repository name from the URL without the .git extension."""
    repo_name = repo_url.rstrip('/').split('/')[-1]
    if repo_name.endswith('.git'):
        repo_name = repo_name[:-4]
    return repo_name

def get_submodule_current_commit(repo, submodule):
    """Get the current commit hash of the submodule as recorded in the parent repo."""
    return submodule.hexsha

def get_submodule_desired_commit(submodule_repo, desired_ref, submodule_url=None):
    """Get the commit hash of the desired reference in the submodule.

    Args:
        submodule_repo: The submodule repository object
        desired_ref: The reference to checkout (branch, tag, or commit)
        submodule_url: If provided, indicates we MUST use local remote (for updated repos)

    Returns:
        The commit hash, or None if the reference cannot be found
    """
    # If submodule_url is provided, we MUST use local remote (it has local commits)
    local_remote_used = False
    local_remote_required = submodule_url is not None

    if local_remote_required:
        # Use absolute path for local remote
        local_path = os.path.abspath(os.path.join(BASE_DIR, get_repo_name(submodule_url)))
        if not os.path.exists(local_path):
            logging.error(f"Local repository required but not found at '{local_path}'")
            logging.error(f"Cannot resolve dependency for submodule '{get_repo_name(submodule_url)}'")
            return None

        try:
            # Add temporary local remote
            local_remote_name = 'local_temp'
            if local_remote_name not in [r.name for r in submodule_repo.remotes]:
                submodule_repo.create_remote(local_remote_name, local_path)
                logging.debug(f"Added local remote '{local_remote_name}' pointing to '{local_path}'")

            # Fetch from local remote
            submodule_repo.remotes[local_remote_name].fetch()
            local_remote_used = True
            logging.debug(f"Fetched from local remote for submodule '{get_repo_name(submodule_url)}'")

        except GitCommandError as e:
            logging.error(f"Failed to use required local remote for '{submodule_url}': {e}")
            logging.error(f"This is required because '{get_repo_name(submodule_url)}' has local changes not yet pushed")
            return None
    
    try:
        # Fetch from origin if not using local remote
        if not local_remote_used:
            submodule_repo.remotes.origin.fetch()
        
        # Check if ref exists as a local branch
        if is_branch(desired_ref, submodule_repo):
            submodule_repo.git.checkout(desired_ref)
            if not local_remote_used:
                submodule_repo.remotes.origin.pull()
            desired_commit = submodule_repo.head.commit.hexsha
            return desired_commit
            
        # Check if ref exists as a remote branch (try local_temp first if available)
        if local_remote_used:
            try:
                remote_branch = f"local_temp/{desired_ref}"
                submodule_repo.git.checkout('-b', desired_ref, remote_branch)
                desired_commit = submodule_repo.head.commit.hexsha
                return desired_commit
            except GitCommandError:
                pass
        
        # Try origin remote branch
        try:
            remote_branch = f"origin/{desired_ref}"
            submodule_repo.git.checkout('-b', desired_ref, remote_branch)
            if not local_remote_used:
                submodule_repo.remotes.origin.pull()
            desired_commit = submodule_repo.head.commit.hexsha
            return desired_commit
        except GitCommandError:
            pass
            
        # Check if ref exists as a tag
        if desired_ref in [tag.name for tag in submodule_repo.tags]:
            submodule_repo.git.checkout(desired_ref)
            desired_commit = submodule_repo.head.commit.hexsha
            return desired_commit
            
        # Attempt to resolve as a commit hash
        try:
            desired_commit = submodule_repo.commit(desired_ref).hexsha
            submodule_repo.git.checkout(desired_commit)
            return desired_commit
        except (git.BadName, ValueError):
            logging.error(f"Reference '{desired_ref}' does not exist in submodule '{get_repo_name(submodule_repo.working_tree_dir)}'.")
            return None
            
    except GitCommandError as e:
        # Check if this is a shallow clone issue
        if 'failed to unpack tree object' in str(e) or 'Unable to checkout' in str(e):
            logging.warning(f"Shallow clone issue detected in submodule, attempting to unshallow...")
            try:
                # First, unshallow nested submodules (do this FIRST before the parent)
                # The error is often in nested submodules, not the parent
                try:
                    logging.info("Unshallowing nested submodules...")
                    submodule_repo.git.submodule('foreach', '--recursive',
                                                'git fetch --unshallow || git fetch --depth=10000 || true')
                    logging.info("Successfully unshallowed nested submodules")
                except Exception as nested_error:
                    logging.debug(f"Note: nested submodule unshallow reported: {nested_error}")
                    # Continue anyway - this is best effort

                # Now try to unshallow this submodule itself (might already be unshallowed)
                try:
                    submodule_repo.git.execute(['git', 'fetch', '--unshallow'])
                    logging.info(f"Successfully unshallowed parent submodule")
                except GitCommandError as unshallow_error:
                    if 'does not make sense' in str(unshallow_error):
                        logging.debug("Parent submodule already unshallowed")
                    else:
                        logging.warning(f"Could not unshallow parent: {unshallow_error}")

                # Retry the checkout based on what type of ref it is
                # Use --no-recurse-submodules to avoid issues with nested submodules during checkout

                # First check if it's a local branch (might exist from previous run)
                if is_branch(desired_ref, submodule_repo):
                    submodule_repo.git.checkout('--no-recurse-submodules', desired_ref)
                    desired_commit = submodule_repo.head.commit.hexsha
                    logging.info(f"Successfully checked out existing local branch '{desired_ref}' after unshallowing")
                    return desired_commit

                # Check if it's a tag
                if desired_ref in [tag.name for tag in submodule_repo.tags]:
                    submodule_repo.git.checkout('--no-recurse-submodules', desired_ref)
                    desired_commit = submodule_repo.head.commit.hexsha
                    logging.info(f"Successfully checked out tag '{desired_ref}' after unshallowing")
                    return desired_commit

                # Check if it's a remote branch (and create local tracking branch)
                if is_remote_branch(desired_ref, submodule_repo):
                    remote_branch = f"origin/{desired_ref}"
                    # Don't use -b if branch already exists, just checkout the remote
                    try:
                        submodule_repo.git.checkout('--no-recurse-submodules', '-b', desired_ref, remote_branch)
                    except GitCommandError as branch_exists:
                        if 'already exists' in str(branch_exists):
                            # Branch exists, just checkout
                            submodule_repo.git.checkout('--no-recurse-submodules', desired_ref)
                        else:
                            raise
                    desired_commit = submodule_repo.head.commit.hexsha
                    logging.info(f"Successfully checked out remote branch '{desired_ref}' after unshallowing")
                    return desired_commit

                # Try as commit hash
                try:
                    desired_commit = submodule_repo.commit(desired_ref).hexsha
                    submodule_repo.git.checkout('--no-recurse-submodules', desired_commit)
                    logging.info(f"Successfully checked out commit '{desired_ref}' after unshallowing")
                    return desired_commit
                except (git.BadName, ValueError):
                    pass  # Not a valid commit hash

                # If nothing worked, raise an error
                raise GitCommandError(f"Could not checkout '{desired_ref}' - not a branch, tag, or commit")

            except Exception as e2:
                logging.error(f"Failed to checkout '{desired_ref}' even after unshallowing: {e2}")
                logging.error(f"Original error: {e}")
                return None
        else:
            logging.error(f"Error fetching or checking out '{desired_ref}' in submodule '{get_repo_name(submodule_repo.working_tree_dir)}': {e}")
            return None
    finally:
        # Clean up temporary local remote if it was added
        if local_remote_used and 'local_temp' in [r.name for r in submodule_repo.remotes]:
            try:
                submodule_repo.delete_remote('local_temp')
                logging.debug(f"Removed temporary local remote")
            except GitCommandError:
                pass

def is_branch(ref, repo):
    """Check if the reference is a branch in the repository."""
    try:
        repo.git.rev_parse('--verify', f'refs/heads/{ref}')
        return True
    except GitCommandError:
        return False

def is_remote_branch(ref, repo):
    """Check if the reference exists as a remote branch."""
    return any(r.name.split('/')[-1] == ref for r in repo.remotes.origin.refs)

def validate_yaml_operations(config):
    """Pre-validate all YAML file operations to ensure they will succeed."""
    errors = []
    warnings = []
    
    for repo_url, settings in config.items():
        update_yaml_entries = settings.get('update_yaml', [])
        if not update_yaml_entries:
            continue
            
        clone_path = os.path.join(BASE_DIR, get_repo_name(repo_url))
        if not os.path.exists(clone_path):
            # This will be cloned later, skip validation for now
            warnings.append(f"Repository '{repo_url}' not yet cloned, YAML validation skipped")
            continue
            
        for edit in update_yaml_entries:
            yaml_path = os.path.join(clone_path, edit['filename'])
            
            # Check if file exists
            if not os.path.exists(yaml_path):
                errors.append(f"YAML file '{edit['filename']}' not found in repository '{repo_url}'")
                continue
                
            # Try to load and validate the YAML file
            try:
                yaml_obj = YAML()
                yaml_obj.preserve_quotes = True
                with open(yaml_path, 'r') as f:
                    data = yaml_obj.load(f)
                    
                # Validate key path exists
                key_to_update = edit['key_to_update']
                keys = key_to_update.split('.')
                current = data
                
                try:
                    for key in keys[:-1]:
                        if '[' in key and ']' in key:
                            # Parse conditional access
                            list_key, condition = key.split('[', 1)
                            condition = condition.rstrip(']')
                            field, value = condition.split('=', 1)
                            
                            if list_key not in current or not isinstance(current[list_key], list):
                                errors.append(f"Key '{list_key}' is not a list in YAML file '{edit['filename']}' in repository '{repo_url}'")
                                raise KeyError
                                
                            # Find matching item
                            matched_item = None
                            for item in current[list_key]:
                                if isinstance(item, dict) and item.get(field) == value:
                                    matched_item = item
                                    break
                                    
                            if not matched_item:
                                errors.append(f"No item found in list '{list_key}' with condition '{field}={value}' in YAML file '{edit['filename']}' in repository '{repo_url}'")
                                raise KeyError
                                
                            current = matched_item
                        else:
                            if key not in current:
                                errors.append(f"Key '{key}' not found in path '{key_to_update}' in YAML file '{edit['filename']}' in repository '{repo_url}'")
                                raise KeyError
                            current = current[key]
                            
                    # Check last key exists
                    last_key = keys[-1]
                    if last_key not in current:
                        warnings.append(f"Key '{last_key}' not found in path '{key_to_update}' in YAML file '{edit['filename']}' in repository '{repo_url}' (will be created)")
                        
                except KeyError:
                    # Error already added above
                    pass
                    
            except Exception as e:
                errors.append(f"Failed to validate YAML file '{edit['filename']}' in repository '{repo_url}': {e}")
                
    if errors:
        for error in errors:
            logging.error(error)
        return False, errors, warnings
    else:
        if warnings:
            for warning in warnings:
                logging.warning(warning)
        logging.info("YAML validation passed")
        return True, errors, warnings

def validate_refs(config, tag=None):
    """Validate that all refs in the config are valid."""
    invalid_refs = []
    for repo_url, settings in config.items():
        if 'ref' in settings:
            desired_ref = settings['ref']
        elif 'ref_from_dir' in settings:
            desired_ref = None  # Will derive from directory
        else:
            logging.error(f"Repository '{repo_url}' must specify either 'ref' or 'ref_from_dir'.")
            sys.exit(1)

        # Skip validation for ref_from_dir; handle it separately
        if 'ref_from_dir' in settings:
            ref_from_dir = settings['ref_from_dir']
            if not os.path.exists(ref_from_dir):
                logging.error(f"ref_from_dir path '{ref_from_dir}' for repository '{repo_url}' does not exist.")
                invalid_refs.append((repo_url, ref_from_dir))
                continue
            try:
                local_repo = Repo(ref_from_dir)
                if local_repo.bare:
                    logging.error(f"ref_from_dir path '{ref_from_dir}' for repository '{repo_url}' is a bare repository.")
                    invalid_refs.append((repo_url, ref_from_dir))
                    continue
                current_head = local_repo.head
                if current_head.is_detached:
                    desired_commit = current_head.commit.hexsha
                    # Attempt to resolve commit
                    try:
                        local_repo.commit(desired_commit)
                    except (git.BadName, ValueError):
                        logging.error(f"Commit '{desired_commit}' in ref_from_dir '{ref_from_dir}' for repository '{repo_url}' is invalid.")
                        invalid_refs.append((repo_url, ref_from_dir))
                        continue
                else:
                    branch_name = current_head.reference.name
                    # Check if branch exists on remote
                    local_repo.git.fetch()
                    remote_branches = [ref.name for ref in local_repo.remotes.origin.refs]
                    if branch_name not in [ref.split('/')[-1] for ref in remote_branches]:
                        logging.error(f"Branch '{branch_name}' from ref_from_dir '{ref_from_dir}' for repository '{repo_url}' does not exist on remote.")
                        invalid_refs.append((repo_url, branch_name))
                        continue
            except git.exc.InvalidGitRepositoryError:
                logging.error(f"ref_from_dir path '{ref_from_dir}' for repository '{repo_url}' is not a valid Git repository.")
                invalid_refs.append((repo_url, ref_from_dir))
                continue
            except Exception as e:
                logging.error(f"Error validating ref_from_dir '{ref_from_dir}' for repository '{repo_url}': {e}")
                invalid_refs.append((repo_url, ref_from_dir))
                continue
        else:
            # Validate 'ref'
            clone_path = os.path.join(BASE_DIR, get_repo_name(repo_url))
            repo = clone_repo(repo_url, clone_path)
            if repo is None:
                logging.error(f"Skipping repository '{repo_url}' due to cloning issues during ref validation.")
                invalid_refs.append((repo_url, 'cloning failed'))
                continue
            try:
                # Fetch all refs
                repo.remotes.origin.fetch()
                # Check if ref exists as branch, tag, or commit
                if is_branch(desired_ref, repo) or desired_ref in [tag.name for tag in repo.tags] or is_remote_branch(desired_ref, repo):
                    continue
                else:
                    # Attempt to resolve as a commit hash
                    try:
                        repo.commit(desired_ref)
                        continue
                    except (git.BadName, ValueError):
                        invalid_refs.append((repo_url, desired_ref))
            except GitCommandError as e:
                logging.error(f"Error validating ref '{desired_ref}' for repository '{repo_url}': {e}")
                invalid_refs.append((repo_url, desired_ref))

    if invalid_refs:
        for repo_url, invalid_ref in invalid_refs:
            logging.error(f"Invalid ref '{invalid_ref}' for repository '{repo_url}'.")
        logging.error("Ref validation failed. Please correct the invalid refs and try again.")
        if tag:
            logging.error("If you intend to overwrite existing tags, you can re-run the script with the '--retag' option.")
        sys.exit(1)
    else:
        logging.info("All refs in the configuration are valid.")

def create_branch_name(repo_url, tag=None, counter=None):
    """Generate a unique branch name."""
    base_name = "update-submodules"
    if tag:
        base_name += f"-for-{tag}"
    if counter:
        base_name += f"-{counter}"
    return base_name

def create_merge_request(repo, source_branch, target_branch, automerge=False, mr_title=None):
    """Create a merge request using GitLab push options."""
    # GitLab specific push options
    push_options = [
        'merge_request.create=true',
        f'merge_request.target={target_branch}',
        'merge_request.remove_source_branch=true'
    ]

    # Add custom title if provided
    if mr_title:
        push_options.append(f'merge_request.title={mr_title}')

    if automerge:
        push_options.append('merge_request.merge_when_pipeline_succeeds=true')
    return push_options

def update_repo(repo_url, desired_ref, config, tag=None, retag=False, push_enabled=False, updated_repos_branches=None,
                release_to_master=False, force=False):
    """
    Update a single repository and its submodules.

    Args:
        repo_url (str): The URL of the repository to update.
        desired_ref (str): The desired Git reference (branch or commit hash).
        config (dict): The configuration dictionary loaded from repos.yaml.
        tag (str, optional): The name of the Git tag to create. Defaults to None.
        retag (bool, optional): Whether to overwrite existing tags. Defaults to False.
        push_enabled (bool, optional): If True, push changes to remote. If False, only process locally. Defaults to False.
        updated_repos_branches (dict, optional): A mapping of repository URLs to their updated feature branch names.
                                                 Defaults to None.
        release_to_master (bool, optional): Whether to perform release workflow with rebase. Defaults to False.
        force (bool, optional): Skip prompts and force continue on warnings. Defaults to False.

    Returns:
        RepoOperationResult: The result of the operation.
    """
    if updated_repos_branches is None:
        updated_repos_branches = {}

    result = RepoOperationResult(repo_url=repo_url)
    
    clone_path = os.path.join(BASE_DIR, get_repo_name(repo_url))
    repo = clone_repo(repo_url, clone_path)

    if repo is None:
        logging.error(f"Skipping repository '{repo_url}' due to cloning issues.")
        result.success = False
        result.error_message = "Failed to clone repository"
        return result
        
    result.repo_object = repo

    # Retrieve repository-specific settings
    settings = config.get(repo_url, {})
    result.settings = settings  # Store settings for Phase 2
    ref_from_dir = settings.get('ref_from_dir')
    automerge = settings.get('automerge', False)
    create_mr = settings.get('create_merge_request', True)

    # Initialize updated_yaml_files to ensure it's always defined
    updated_yaml_files = {}

    # Handle release workflow if requested
    source_head = None  # Track source HEAD for sanity check
    if release_to_master:
        # Get source branch configuration
        source_ref = settings.get('source_ref', 'develop')
        target_branch = desired_ref  # This should be 'master' or 'main'

        # Validate target branch
        if target_branch not in ['master', 'main']:
            logging.error(f"For --release-to-master, repo {repo_url} must have ref: master or main, got: {target_branch}")
            result.success = False
            result.error_message = f"Invalid target branch for release: {target_branch}"
            return result

        # Fetch updates first
        fetch_updates(repo, repo_url)

        # Ensure source_ref exists - if it's just a branch name without origin/, check if we need to add it
        if not source_ref.startswith('origin/') and '/' not in source_ref:
            # Check if local branch exists
            try:
                subprocess.run(['git', 'rev-parse', '--verify', source_ref],
                              cwd=clone_path, capture_output=True, text=True, check=True)
                # Local branch exists, use it as-is
            except subprocess.CalledProcessError:
                # Local branch doesn't exist, try origin/source_ref
                try:
                    subprocess.run(['git', 'rev-parse', '--verify', f'origin/{source_ref}'],
                                  cwd=clone_path, capture_output=True, text=True, check=True)
                    # Remote branch exists, use it
                    source_ref = f'origin/{source_ref}'
                    logging.debug(f"Using remote reference: {source_ref}")
                except subprocess.CalledProcessError:
                    logging.error(f"Neither {source_ref} nor origin/{source_ref} exists")
                    result.success = False
                    result.error_message = f"Source reference {source_ref} not found"
                    return result

        logging.info(f"Release workflow: {source_ref} -> {target_branch}")

        # Find rebase base
        logging.info(f"Finding rebase base for {repo_url}...")
        rebase_base = find_rebase_base(clone_path, source_ref, f'origin/{target_branch}')

        if not rebase_base:
            logging.warning("Could not find rebase base using patch-id, using merge-base")
            # Fallback to merge-base
            try:
                result_cmd = subprocess.run([
                    'git', 'merge-base', f'origin/{target_branch}', source_ref
                ], cwd=clone_path, capture_output=True, text=True, check=True)
                rebase_base = result_cmd.stdout.strip()
            except subprocess.CalledProcessError:
                result.success = False
                result.error_message = "Failed to find rebase base"
                return result

        # First, check if this is a fast-forward situation
        # If the target branch HEAD is already on the source branch, we can fast-forward
        is_fast_forward = False
        try:
            # Get the target branch HEAD
            target_head_result = subprocess.run([
                'git', 'rev-parse', f'origin/{target_branch}'
            ], cwd=clone_path, capture_output=True, text=True, check=True)
            target_head = target_head_result.stdout.strip()

            # Check if target HEAD is reachable from source (i.e., is it on the source branch?)
            ancestor_check = subprocess.run([
                'git', 'merge-base', '--is-ancestor', target_head, source_ref
            ], cwd=clone_path, capture_output=True, text=True)

            if ancestor_check.returncode == 0:
                # Target is an ancestor of source - this is a fast-forward situation!
                logging.info(f"Target branch {target_branch} HEAD ({target_head[:8]}) is already on {source_ref}")
                logging.info(f"This is a fast-forward situation - no rebase needed")
                is_fast_forward = True
                # For fast-forward, we'll use the target HEAD as our starting point
                rebase_base = target_head
        except subprocess.CalledProcessError as e:
            logging.debug(f"Error checking for fast-forward: {e}")
            is_fast_forward = False

        # Count commits to rebase/fast-forward
        try:
            result_cmd = subprocess.run([
                'git', 'rev-list', '--count', f'{rebase_base}..{source_ref}'
            ], cwd=clone_path, capture_output=True, text=True, check=True)
            commit_count = int(result_cmd.stdout.strip())
            if is_fast_forward:
                logging.info(f"Will fast-forward {commit_count} commits from {rebase_base[:8]}")
            else:
                logging.info(f"Will rebase {commit_count} commits from {rebase_base[:8]}")
        except:
            commit_count = 0

        # Check if there's actually anything to rebase
        if commit_count == 0:
            logging.info(f"No commits to rebase for {repo_url} - source and target are already aligned")
            # Skip the rebase workflow but continue with normal submodule updates
            result.rebase_performed = False

            # We'll create the release branch later if there are submodules to update
            # For now, just checkout the target branch
            try:
                subprocess.run([
                    'git', 'checkout', '-B', target_branch,
                    '--track', f'origin/{target_branch}',
                    '--no-recurse-submodules'
                ], cwd=clone_path, check=True, capture_output=True, text=True)

                # Set a flag to create release branch if needed later
                if tag:
                    release_branch = f'release/{tag}'
                else:
                    release_branch = f'release-{target_branch}-{get_repo_name(repo_url)}'

                # Store the planned branch name but don't create it yet
                result.branch_name = None  # Will be set later if needed
                desired_ref_actual = target_branch

                # Store release_branch in settings for later use
                settings['pending_release_branch'] = release_branch

                # Update repo object to reflect new state
                repo = Repo(clone_path)
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to checkout target branch: {e}")
                result.success = False
                result.error_message = str(e)
                return result
        else:
            # Get source HEAD for later sanity check
            try:
                result_cmd = subprocess.run([
                    'git', 'rev-parse', source_ref
                ], cwd=clone_path, capture_output=True, text=True, check=True)
                source_head = result_cmd.stdout.strip()
            except:
                source_head = source_ref

            # Create release branch
            if tag:
                release_branch = f'release/{tag}'
            else:
                release_branch = f'release-{target_branch}-{get_repo_name(repo_url)}'

            # Create local tracking branch for target
            try:
                # Clean up any existing local branch
                subprocess.run(['git', 'branch', '-D', target_branch],
                              cwd=clone_path, capture_output=True, text=True)
            except:
                pass  # It's okay if branch doesn't exist

            try:
                # First, ensure we're in a clean state
                subprocess.run(['git', 'reset', '--hard'], cwd=clone_path, capture_output=True, text=True)

                # SIMPLER FIX: Create release branch from source (develop) instead of target (master)
                # Then rebase the release branch onto master
                # This preserves the original develop branch

                # Checkout the source branch
                subprocess.run([
                    'git', 'checkout', source_ref
                ], cwd=clone_path, check=True, capture_output=True, text=True)

                # Create release branch from current position (source)
                subprocess.run(['git', 'checkout', '-b', release_branch],
                              cwd=clone_path, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to create release branch: {e}")
                result.success = False
                result.error_message = str(e)
                return result

            # Now handle based on whether this is a fast-forward or rebase situation
            if is_fast_forward:
                # This is a fast-forward case - just merge the source branch
                logging.info(f"Performing fast-forward merge from {source_ref}...")

                try:
                    # We're on release branch created from source, just verify we can fast-forward
                    result_cmd = subprocess.run([
                        'git', 'merge', '--ff-only', source_ref
                    ], cwd=clone_path, capture_output=True, text=True, check=True)

                    logging.info(f"Fast-forward merge completed successfully")
                    success = True
                    conflicts = []
                except subprocess.CalledProcessError as e:
                    logging.error(f"Fast-forward merge failed: {e}")
                    success = False
                    conflicts = []
                    result.success = False
                    result.error_message = "Fast-forward merge failed"
                    return result
            else:
                # Normal rebase case
                # We're on release_branch (created from develop), rebase it onto origin/master
                success, conflicts = perform_release_rebase(
                    clone_path, rebase_base, source_ref, f'origin/{target_branch}', commit_count
                )

            if not success:
                result.success = False
                result.error_message = "Rebase or merge failed"
                return result

            # Set common result fields
            result.branch_name = release_branch  # Set the branch name for Phase 2

            # CRITICAL: Record the branch immediately so child repos can use it
            # This must happen before we process submodule updates
            updated_repos_branches[repo_url] = release_branch
            logging.debug(f"Recorded release branch '{release_branch}' for repository '{repo_url}' in 'updated_repos_branches'.")

            if is_fast_forward:
                # Fast-forward case - we're already on the branch, no fixup needed
                result.rebase_performed = False
                result.conflicts_resolved = []
            else:
                # Rebase case - need to fix up the branch pointer
                result.rebase_performed = True
                result.conflicts_resolved = conflicts

                if conflicts:
                    logging.info(f"Auto-resolved {len(conflicts)} conflicts:")
                    for conflict in conflicts[:5]:
                        logging.info(f"  - {conflict}")
                    if len(conflicts) > 5:
                        logging.info(f"  ... and {len(conflicts)-5} more")

                # After rebase, we're in detached HEAD state.
                # We need to update the release branch to point to the new HEAD
                try:
                    # Get current HEAD commit
                    head_commit = subprocess.run(['git', 'rev-parse', 'HEAD'],
                                                cwd=clone_path, capture_output=True, text=True, check=True).stdout.strip()

                    # Update the release branch to point to this commit
                    subprocess.run(['git', 'branch', '-f', release_branch, head_commit],
                                  cwd=clone_path, check=True, capture_output=True)

                    # Checkout the release branch
                    subprocess.run(['git', 'checkout', release_branch],
                                  cwd=clone_path, check=True, capture_output=True)

                    logging.debug(f"Moved {release_branch} to rebased HEAD and checked it out")
                except subprocess.CalledProcessError as e:
                    logging.error(f"Failed to update release branch after rebase: {e}")
                    result.success = False
                    result.error_message = str(e)
                    return result

            # Update repo object to reflect new state
            repo = Repo(clone_path)

            # We're now on the release branch
            desired_ref_actual = release_branch
    else:
        # Normal flow: Determine the desired reference to checkout
        desired_ref_actual = get_desired_ref(repo, repo_url, ref_from_dir, desired_ref)

        # Fetch updates from the remote repository
        fetch_updates(repo, repo_url)

        # Checkout the desired reference
        checkout_reference(repo, repo_url, desired_ref_actual)

    # Initialize and update submodules
    update_submodules(repo, repo_url)

    # Identify submodule updates
    updated_submodules, submodule_commits = identify_submodule_updates(repo, config, updated_repos_branches)
    result.submodules_updated = updated_submodules

    # For release workflow with submodules, check if we should have updates
    # Note: It's okay to have no submodule updates if:
    # - The rebased commits didn't change submodule pointers, OR
    # - The submodules are already at the correct commits
    if release_to_master and result.rebase_performed and not updated_submodules:
        # Check if this repo even has submodules
        if len(list(repo.submodules)) > 0:
            # We have submodules but no updates detected
            # Log a warning but don't fail - the sanity check will catch real issues
            logging.warning(f"Note: Release workflow completed rebase but found no submodule updates")
            logging.warning("This could be normal if submodules are already at correct commits")
            logging.warning("Or it could indicate a detection issue - will be caught by sanity check if problematic")
        # Continue processing - sanity check will verify correctness

    if updated_submodules:
        # Create a feature branch for the updates (always create in Phase 1)
        # Skip if we already created a release branch during release workflow
        if release_to_master and result.branch_name:
            # Already have a release branch from the rebase workflow
            branch_name = result.branch_name
            logging.debug(f"Using existing release branch '{branch_name}' from rebase workflow")
        elif release_to_master and 'pending_release_branch' in settings:
            # We deferred creating the release branch (0 commits case)
            # Create it now since we have submodules to update
            release_branch = settings['pending_release_branch']
            try:
                subprocess.run(['git', 'checkout', '-b', release_branch],
                              cwd=clone_path, check=True, capture_output=True, text=True)
                branch_name = release_branch
                result.branch_name = branch_name
                logging.info(f"Created release branch '{branch_name}' for submodule updates")
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to create release branch: {e}")
                branch_name = None
        else:
            # Normal flow: create new feature branch
            branch_name = create_feature_branch(repo, repo_url, tag, push_enabled=False)  # Never skip branch creation
            result.branch_name = branch_name

        if branch_name:
            # Collect YAML file updates based on submodule updates
            updated_yaml_files = collect_yaml_updates(settings, updated_submodules)
            result.yaml_files_updated = updated_yaml_files

            # Process YAML file updates (always process in Phase 1)
            if updated_yaml_files:
                process_yaml_updates(clone_path, updated_yaml_files, submodule_commits)

            # Commit the submodule updates
            commit_changes(repo, repo_url, updated_submodules)

            # Record the branch to update parent repositories
            # This must happen in Phase 1 so dependent repos can use the branch
            # Skip if already recorded during release workflow
            if repo_url not in updated_repos_branches:
                updated_repos_branches[repo_url] = branch_name
                logging.debug(f"Recorded updated branch '{branch_name}' for repository '{repo_url}' in 'updated_repos_branches'.")
            else:
                logging.debug(f"Branch already recorded for '{repo_url}', skipping duplicate")

            # Only push if in Phase 2
            if push_enabled:
                push_branch(repo, repo_url, branch_name, settings, automerge, create_mr, dry_run=False,
                           release_to_master=release_to_master, tag=tag)
            else:
                logging.info(f"Phase 1: Created branch '{branch_name}' locally, not pushing yet")

    # Handle tagging
    if tag:
        # Determine if any changes were made
        changes_made = bool(updated_submodules) or result.rebase_performed

        # Always create tag locally in Phase 1 (to catch conflicts early)
        create_tag_locally(repo, repo_url, tag, retag, changes_made)
        result.tag_to_create = tag

        if push_enabled:
            # Phase 2: Push the tag to remote
            push_tag(repo, repo_url, tag)
        else:
            logging.info(f"Phase 1: Tag '{tag}' created locally, will push in Phase 2")

    # Log a summary of the commits
    log_commit_summary(repo_url, updated_submodules, updated_yaml_files, push_enabled)

    # Perform sanity check for release workflow
    if release_to_master and source_head:
        logging.info("Performing sanity check...")

        # Build list of expected changed files
        expected_patterns = ['.gitmodules']

        # Add submodule paths from .gitmodules
        try:
            result_cmd = subprocess.run([
                'git', 'config', '--file', '.gitmodules', '--get-regexp', 'path'
            ], cwd=clone_path, capture_output=True, text=True)

            if result_cmd.stdout:
                for line in result_cmd.stdout.strip().split('\n'):
                    if 'path' in line:
                        path = line.split()[-1] if line.split() else None
                        if path:
                            expected_patterns.append(path)
        except:
            pass

        # Add YAML files from config
        yaml_updates = settings.get('update_yaml', [])
        for yaml_update in yaml_updates:
            filename = yaml_update.get('filename')
            if filename:
                expected_patterns.append(filename)

        is_valid, diff_info = verify_release_changes(
            clone_path, source_head, 'HEAD', expected_patterns
        )

        result.sanity_check_passed = is_valid

        if not is_valid:
            logging.warning(f"⚠️  Sanity check failed for {repo_url}")
            if diff_info:
                if len(diff_info) < 1000:
                    logging.warning(diff_info)
                else:
                    # Just show first line with file path
                    first_line = diff_info.split('\n')[0] if diff_info else diff_info
                    logging.warning(first_line)

            if not force:
                response = input(f"\nUnexpected changes detected in {get_repo_name(repo_url)}. Continue? (y/n): ")
                if response.lower() != 'y':
                    result.success = False
                    result.error_message = "Aborted due to unexpected changes"
                    return result
            else:
                logging.warning("Continuing due to --force flag")
        else:
            logging.info("✅ Sanity check passed")

    return result

def get_desired_ref(repo, repo_url, ref_from_dir, desired_ref):
    """Determine the desired reference to checkout."""
    if ref_from_dir:
        return get_ref_from_directory(repo_url, ref_from_dir)
    else:
        return desired_ref

def get_ref_from_directory(repo_url, ref_from_dir):
    """Retrieve the desired reference from a local directory."""
    local_repo_path = os.path.abspath(ref_from_dir)
    try:
        local_repo = Repo(local_repo_path)
        current_head = local_repo.head
        if current_head.is_detached:
            desired_ref_actual = current_head.commit.hexsha
            logging.debug(f"Repository '{repo_url}' is at detached head '{desired_ref_actual}'.")
        else:
            desired_ref_actual = current_head.reference.name
            logging.debug(f"Repository '{repo_url}' is on branch '{desired_ref_actual}'.")
        return desired_ref_actual
    except git.exc.InvalidGitRepositoryError:
        logging.error(f"ref_from_dir path '{ref_from_dir}' for repository '{repo_url}' is not a valid Git repository.")
        sys.exit(1)

def fetch_updates(repo, repo_url):
    """Fetch updates from the remote repository."""
    logging.info(f"Fetching updates for '{get_repo_name(repo_url)}'...")
    try:
        repo.remotes.origin.fetch()
    except GitCommandError as e:
        logging.error(f"Failed to fetch updates for '{repo_url}': {e}")
        sys.exit(1)

def checkout_reference(repo, repo_url, desired_ref_actual):
    """Checkout the desired reference in the repository."""
    try:
        repo.git.checkout(desired_ref_actual)
        logging.debug(f"Checked out '{desired_ref_actual}' in '{get_repo_name(repo_url)}'.")
    except GitCommandError:
        logging.warning(f"Reference '{desired_ref_actual}' not found in '{repo_url}'. Attempting to create it from 'origin/{desired_ref_actual}'.")
        try:
            repo.git.checkout('-b', desired_ref_actual, f'origin/{desired_ref_actual}')
            logging.debug(f"Created and checked out branch '{desired_ref_actual}' from 'origin/{desired_ref_actual}' in '{get_repo_name(repo_url)}'.")
        except GitCommandError as e:
            logging.error(f"Failed to checkout reference '{desired_ref_actual}' in '{repo_url}': {e}")
            sys.exit(1)

def update_submodules(repo, repo_url):
    """Initialize and update submodules for the repository."""
    logging.info(f"Initializing and updating submodules for '{get_repo_name(repo_url)}'...")
    try:
        repo.git.submodule('init')

        # Try normal update first
        try:
            repo.git.submodule('update', '--recursive')
        except GitCommandError as shallow_error:
            # If it fails, it might be due to shallow clones
            if 'failed to unpack tree object' in str(shallow_error) or 'Unable to checkout' in str(shallow_error):
                logging.warning("Submodule update failed (likely shallow clone issue), attempting to unshallow...")

                # Unshallow all submodules recursively
                try:
                    # First, fetch with --unshallow for direct submodules
                    repo.git.submodule('foreach', '--recursive',
                                      'git fetch --unshallow || git fetch --depth=10000 || true')

                    # Now try update again
                    repo.git.submodule('update', '--recursive')
                    logging.info("Successfully updated submodules after unshallowing")
                except GitCommandError as e2:
                    # If still failing, try with --force
                    logging.warning("Trying submodule update with --force...")
                    try:
                        repo.git.submodule('update', '--recursive', '--force')
                        logging.info("Successfully updated submodules with --force")
                    except GitCommandError as e3:
                        logging.error(f"Failed to update submodules even after unshallowing and --force: {e3}")
                        raise
            else:
                # Some other error, re-raise it
                raise

    except GitCommandError as e:
        logging.error(f"Failed to initialize/update submodules in '{repo_url}': {e}")
        sys.exit(1)

def identify_submodule_updates(repo, config, updated_repos_branches):
    """Identify which submodules need to be updated."""
    updated_submodules = {}
    submodule_commits = {}

    # Get the parent repo URL to resolve submodule URLs
    try:
        parent_repo_url = repo.remotes.origin.url
    except AttributeError:
        parent_repo_url = None

    for submodule in repo.submodules:
        original_sub_url = submodule.url
        if parent_repo_url:
            resolved_sub_url = resolve_submodule_url(parent_repo_url, original_sub_url)
        else:
            resolved_sub_url = original_sub_url  # Fallback if origin is not set

        logging.debug(f"Working on submodule '{original_sub_url}' resolved to '{resolved_sub_url}' of repo '{parent_repo_url}'")

        if resolved_sub_url in config:
            logging.debug(f"Submodule '{resolved_sub_url}' is in the config.")

            # Determine the desired reference for the submodule
            desired_ref_submodule = determine_submodule_ref(submodule, config, updated_repos_branches, parent_repo_url)

            if desired_ref_submodule is None:
                logging.error(f"Desired ref is None for submodule '{get_repo_name(resolved_sub_url)}'. Skipping.")
                continue

            # Get the desired commit hash (pass submodule URL for local remote support)
            # We need local remotes for submodules that have been updated locally
            use_local_remote = resolved_sub_url in updated_repos_branches
            if use_local_remote:
                logging.debug(f"Submodule '{get_repo_name(resolved_sub_url)}' has local changes, will use local remote")
            else:
                logging.debug(f"Submodule '{get_repo_name(resolved_sub_url)}' has no local changes, will use origin")
                logging.debug(f"  resolved_sub_url: {resolved_sub_url}")
                logging.debug(f"  updated_repos_branches keys: {list(updated_repos_branches.keys())}")

            desired_commit = get_submodule_desired_commit(
                submodule.module(),
                desired_ref_submodule,
                resolved_sub_url if use_local_remote else None
            )
            logging.debug(f"Desired commit for submodule '{get_repo_name(resolved_sub_url)}' is {desired_commit}")

            if desired_commit is None:
                if use_local_remote:
                    # This is a critical failure - we needed local changes but couldn't get them
                    logging.error(f"CRITICAL: Failed to get commit from local repository for submodule '{get_repo_name(resolved_sub_url)}'")
                    logging.error("Cannot continue - local changes are required but not accessible")
                    sys.exit(1)
                else:
                    logging.error(f"Failed to determine desired commit for submodule '{get_repo_name(resolved_sub_url)}'.")
                    # For release workflow, submodule updates are critical - we can't continue without them
                    # Check if this is being called from update_repo (we're in Phase 1)
                    # A simple heuristic: if we're processing and there are items in updated_repos_branches,
                    # we're in a full update run and submodules matter
                    logging.error(f"Submodule '{get_repo_name(resolved_sub_url)}' could not be updated - this may cause issues")
                    logging.error("Skipping this submodule and continuing (this may result in an incomplete update)")
                    continue

            # Get the current commit hash of the submodule
            current_commit = get_submodule_current_commit(repo, submodule)

            if current_commit != desired_commit:
                logging.info(f"Submodule '{get_repo_name(resolved_sub_url)}' is at {current_commit}, needs to be updated to '{desired_ref_submodule}' ({desired_commit}).")
                updated_submodules[resolved_sub_url] = {
                    'name': get_repo_name(resolved_sub_url),
                    'ref': desired_ref_submodule,
                    'commit': desired_commit
                }
                submodule_commits[resolved_sub_url] = desired_commit

                # Stage the submodule path to update the pointer
                repo.git.add(submodule.path)
                logging.debug(f"Staged submodule '{get_repo_name(resolved_sub_url)}' for update.")

    return updated_submodules, submodule_commits

def determine_submodule_ref(submodule, config, updated_repos_branches, parent_repo_url):
    """Determine the desired reference for a submodule."""
    original_sub_url = submodule.url
    if parent_repo_url:
        resolved_sub_url = resolve_submodule_url(parent_repo_url, original_sub_url)
    else:
        resolved_sub_url = original_sub_url  # Fallback if origin is not set

    if resolved_sub_url in updated_repos_branches:
        # Use the feature branch from the mapping
        desired_ref_submodule = updated_repos_branches[resolved_sub_url]
        logging.debug(f"Submodule '{get_repo_name(resolved_sub_url)}' is being updated via branch '{desired_ref_submodule}'.")
    else:
        # Use the configured ref or ref_from_dir
        sub_settings = config[resolved_sub_url]
        sub_ref_from_dir = sub_settings.get('ref_from_dir')
        if sub_ref_from_dir:
            # Use the ref from the local directory
            desired_ref_submodule = get_ref_from_directory(resolved_sub_url, sub_ref_from_dir)
        else:
            desired_ref_submodule = sub_settings.get('ref')
    return desired_ref_submodule

def create_feature_branch(repo, repo_url, tag, push_enabled):
    """Create a new feature branch for committing the updates.
    
    Args:
        push_enabled: If False, we're in Phase 1 (local only). If True, we're in Phase 2.
    """
    # Always create branches locally in Phase 1

    # Fetch all remote branches to ensure up-to-date information
    try:
        repo.remotes.origin.fetch()
    except GitCommandError as e:
        logging.error(f"Failed to fetch remote branches for '{repo_url}': {e}")
        sys.exit(1)

    # Gather all existing branch names (local and remote)
    local_branches = set(branch.name for branch in repo.heads)
    remote_branches = set(ref.remote_head for ref in repo.remotes.origin.refs)
    existing_branches = local_branches.union(remote_branches)

    branch_created = False
    branch_name = None
    counter = 1

    while not branch_created:
        if tag:
            # Pass 'counter' only if it's greater than 1 to maintain naming consistency
            candidate_branch = create_branch_name(repo_url, tag=tag, counter=counter if counter > 1 else None)
        else:
            candidate_branch = create_branch_name(repo_url, counter=counter if counter > 1 else None)

        if candidate_branch not in existing_branches:
            branch_name = candidate_branch
            try:
                new_branch = repo.create_head(branch_name)
                new_branch.checkout()
                branch_created = True
                logging.debug(f"Created and checked out new branch '{branch_name}' in '{get_repo_name(repo_url)}'.")
            except GitCommandError as e:
                logging.error(f"Failed to create branch '{candidate_branch}' in '{repo_url}': {e}")
                sys.exit(1)
        else:
            logging.debug(f"Branch name '{candidate_branch}' already exists. Incrementing counter.")
            counter += 1

    return branch_name

def commit_changes(repo, repo_url, updated_submodules):
    """Commit the submodule updates with a detailed commit message."""
    commit_lines = ["Update submodules:"]
    for details in updated_submodules.values():
        commit_lines.append(f" - {details['name']}: {details['ref']} ({details['commit']})")
    commit_message = "\n".join(commit_lines)
    logging.info(f"Committing changes in '{get_repo_name(repo_url)}' with message:\n{commit_message}")

    try:
        repo.index.commit(commit_message)
        # After rebase, we might be in detached HEAD state, so handle that
        try:
            branch_name = repo.active_branch.name
            logging.debug(f"Committed changes in '{get_repo_name(repo_url)}' on branch '{branch_name}'.")
        except TypeError:
            # We're in detached HEAD state
            logging.debug(f"Committed changes in '{get_repo_name(repo_url)}' (detached HEAD).")
    except GitCommandError as e:
        logging.error(f"Failed to commit changes in '{get_repo_name(repo_url)}': {e}")
        sys.exit(1)

def push_branch(repo, repo_url, branch_name, settings, automerge, create_mr, dry_run, release_to_master=False, tag=None):
    """Push the feature branch to the remote repository and create a merge request if applicable."""
    if dry_run:
        logging.info("Dry run enabled. Push actions skipped.")
        return

    try:
        if create_mr:
            # Determine the target branch: use 'target_branch' if specified, else default to 'ref', else 'main'
            target_branch = settings.get('target_branch', settings.get('ref', 'main'))

            # Generate appropriate MR title
            if release_to_master and tag:
                mr_title = f"Release {tag}"
            elif release_to_master:
                mr_title = f"Merge changes to {target_branch}"
            elif tag:
                mr_title = f"Update submodules for {tag}"
            else:
                mr_title = "Update submodules"

            push_options = create_merge_request(repo, branch_name, target_branch, automerge=automerge, mr_title=mr_title)
            logging.debug(f"Pushing branch '{branch_name}' with push options: {push_options}")

            # Push without recursing into submodules
            # This prevents failures when submodule commits haven't been pushed yet
            repo.remotes.origin.push(
                refspec=f"{branch_name}:{branch_name}",
                push_option=push_options,
                no_recurse_submodules=True
            )
            logging.info(f"Pushed branch '{branch_name}' to '{repo_url}' with MR title '{mr_title}' targeting '{target_branch}'.")
        else:
            # Also use no-recurse-submodules for non-MR pushes
            repo.remotes.origin.push(refspec=f"{branch_name}:{branch_name}", no_recurse_submodules=True)
            logging.info(f"Pushed branch '{branch_name}' to '{repo_url}'.")
    except GitCommandError as e:
        logging.error(f"Failed to push branch '{branch_name}' to '{repo_url}': {e}")
        sys.exit(1)

def collect_yaml_updates(current_repo_settings, updated_submodules):
    """Collect YAML file updates based on submodule updates for the current repository."""
    updated_yaml_files = {}
    update_yaml_entries = current_repo_settings.get('update_yaml', [])
    for edit in update_yaml_entries:
        # The 'submodule_referenced' should be an absolute URL as per the config
        submodule_referenced_url = edit['submodule_referenced']
        if submodule_referenced_url in updated_submodules:
            filename = edit['filename']
            key_to_update = edit['key_to_update']
            if filename not in updated_yaml_files:
                updated_yaml_files[filename] = []
            updated_yaml_files[filename].append({
                'filename': filename,  # Ensure 'filename' is included
                'key_to_update': key_to_update,
                'submodule_referenced': submodule_referenced_url  # Use absolute URL
            })
    return updated_yaml_files

def process_yaml_updates(clone_path, updated_yaml_files, submodule_commits):
    """Process YAML file updates."""
    for filename, edits in updated_yaml_files.items():
        update_yaml_files(clone_path, edits, submodule_commits)
        # Always stage the updated YAML file (Phase 1 operation)
        try:
            repo = Repo(clone_path)
            repo.git.add(filename)
            logging.debug(f"Staged YAML file '{filename}' for commit.")
        except GitCommandError as e:
            logging.error(f"Failed to stage YAML file '{filename}': {e}")
            continue

def create_tag_locally(repo, repo_url, tag, retag, changes_made=True):
    """Create a tag locally (Phase 1 operation).

    Args:
        repo: Git repository object
        repo_url: URL of the repository
        tag: Tag name to create
        retag: Whether to overwrite existing tags
        changes_made: Whether any changes were made (rebase, submodule updates, etc.)
    """
    if not tag:
        return

    existing_tags = [t.name for t in repo.tags]
    if tag in existing_tags:
        if retag:
            logging.info(f"Deleting existing local tag '{tag}' in '{get_repo_name(repo_url)}' as '--retag' is specified.")

            # Delete local tag
            try:
                repo.delete_tag(tag)
                logging.debug(f"Deleted local tag '{tag}'")
            except GitCommandError as e:
                logging.warning(f"Could not delete local tag '{tag}': {e}")
        elif not changes_made:
            # Tag exists but no changes were made - this is fine
            logging.info(f"Tag '{tag}' already exists in '{repo_url}' and no changes were made - tag is already correct.")
            return
        else:
            # Tag exists and changes were made - this is an error
            logging.error(f"Tag '{tag}' already exists in '{repo_url}' but changes were made. Use '--retag' to overwrite.")
            sys.exit(1)

    # Create the tag locally
    try:
        repo.create_tag(tag, message=f"Release {tag}", force=retag)
        logging.debug(f"Created tag '{tag}' locally in '{get_repo_name(repo_url)}' at commit '{repo.head.commit.hexsha}'.")
    except GitCommandError as e:
        logging.error(f"Failed to create tag '{tag}' in '{repo_url}': {e}")
        sys.exit(1)

def push_tag(repo, repo_url, tag):
    """Push a tag to remote (Phase 2 operation)."""
    if not tag:
        return

    # Check if remote tag needs to be deleted first (for retag)
    try:
        # Try to push the tag
        repo.remotes.origin.push(tag)
        logging.info(f"Pushed tag '{tag}' to '{repo_url}'.")
    except GitCommandError as e:
        # If push fails, it might be because the tag already exists on remote
        if "already exists" in str(e) or "cannot lock ref" in str(e):
            logging.info(f"Tag '{tag}' already exists on remote, attempting to delete and repush...")

            # Try to delete remote tag via Git first
            try:
                repo.remotes.origin.push(refspec=f":refs/tags/{tag}")
                logging.debug(f"Deleted remote tag '{tag}' via Git")

                # Now try to push again
                repo.remotes.origin.push(tag)
                logging.info(f"Pushed tag '{tag}' to '{repo_url}' after deleting existing remote tag.")
            except GitCommandError as delete_error:
                # If Git push fails (likely protected tag), try API
                logging.info(f"Failed to delete tag via Git, trying GitLab API: {delete_error}")
                if delete_gitlab_tag_via_api(repo_url, tag):
                    # Try to push again after API deletion
                    try:
                        repo.remotes.origin.push(tag)
                        logging.info(f"Pushed tag '{tag}' to '{repo_url}' after deleting via API.")
                    except GitCommandError as push_error:
                        logging.error(f"Failed to push tag '{tag}' even after deletion: {push_error}")
                        sys.exit(1)
                else:
                    logging.error(f"Failed to delete protected tag '{tag}'. Please delete manually or provide GITLAB_TOKEN")
                    sys.exit(1)
        else:
            logging.error(f"Failed to push tag '{tag}' to '{repo_url}': {e}")
            sys.exit(1)

def handle_tagging(repo, repo_url, tag, retag, dry_run):
    """Legacy function for compatibility - redirects to new functions."""
    # This function is still called from push_all_operations
    if dry_run:
        logging.info(f"Would push tag '{tag}' to '{repo_url}'")
    else:
        push_tag(repo, repo_url, tag)

def log_commit_summary(repo_url, updated_submodules, updated_yaml_files, push_enabled):
    """Log a summary of the commits."""
    if not updated_submodules and not updated_yaml_files:
        logging.info(f"No submodule or YAML file updates to commit in '{get_repo_name(repo_url)}'.")
        return

    commit_lines = []

    if updated_submodules:
        commit_lines.append("Update submodules:")
        for details in updated_submodules.values():
            commit_lines.append(f" - {details['name']}: {details['ref']} ({details['commit']})")

    if updated_yaml_files:
        commit_lines.append("Update YAML files:")
        for filename, edits in updated_yaml_files.items():
            for edit in edits:
                submodule_name = get_repo_name(edit['submodule_referenced'])
                commit_lines.append(f" - {filename}: {edit['key_to_update']} updated for submodule '{submodule_name}'")

    commit_message = "\n".join(commit_lines)
    logging.info(f"Commit summary for '{get_repo_name(repo_url)}':\n{commit_message}")

    if not push_enabled:
        logging.info("Phase 1: Local changes committed, will push in Phase 2")
    else:
        logging.info("Phase 2: Changes pushed to remote")

def update_yaml_files(clone_path, update_yaml_entries, submodule_commits):
    """Update specified YAML files with new submodule commit hashes."""
    yaml_obj = YAML()
    yaml_obj.preserve_quotes = True  # Preserve existing quotes

    for edit in update_yaml_entries:
        filename = edit['filename']
        key_to_update = edit['key_to_update']
        submodule_referenced = edit['submodule_referenced']

        # Determine the new commit hash
        new_commit = submodule_commits.get(submodule_referenced)
        if not new_commit:
            logging.error(f"No commit hash found for submodule '{submodule_referenced}'. Cannot update YAML file '{filename}'.")
            continue

        # Construct full path to the YAML file
        yaml_path = os.path.join(clone_path, filename)
        if not os.path.exists(yaml_path):
            logging.error(f"YAML file '{yaml_path}' does not exist.")
            continue

        logging.info(f"Updating YAML file '{yaml_path}' at key '{key_to_update}' with commit '{new_commit}'.")

        # Load the YAML file
        try:
            with open(yaml_path, 'r') as f:
                data = yaml_obj.load(f)
        except Exception as e:
            logging.error(f"Failed to load YAML file '{yaml_path}': {e}")
            continue

        # Traverse the key path with conditional matching
        keys = key_to_update.split('.')
        current = data
        try:
            for key in keys[:-1]:
                if '[' in key and ']' in key:
                    # Parse condition, e.g., includes.[project=C]
                    list_key, condition = key.split('[', 1)
                    condition = condition.rstrip(']')
                    field, value = condition.split('=', 1)
                    if list_key not in current or not isinstance(current[list_key], list):
                        logging.error(f"Key '{list_key}' is not a list in YAML file '{yaml_path}'.")
                        raise KeyError
                    # Find the first item in the list where item[field] == value
                    matched_item = None
                    for item in current[list_key]:
                        if isinstance(item, dict) and item.get(field) == value:
                            matched_item = item
                            break
                    if not matched_item:
                        logging.error(f"No item found in list '{list_key}' with condition '{field}={value}' in YAML file '{yaml_path}'.")
                        raise KeyError
                    current = matched_item
                else:
                    if key not in current:
                        current[key] = {}
                    current = current[key]
            last_key = keys[-1]
        except KeyError:
            logging.error(f"Key path '{key_to_update}' does not exist in YAML file '{yaml_path}'.")
            continue

        # Update the key with the new commit hash
        old_value = current.get(last_key, None)
        current[last_key] = new_commit
        logging.debug(f"Updated '{key_to_update}' from '{old_value}' to '{new_commit}'.")

        # Save the YAML file (always save in Phase 1)
        try:
            with open(yaml_path, 'w') as f:
                yaml_obj.dump(data, f)
            logging.debug(f"Saved updated YAML file '{yaml_path}'.")
        except Exception as e:
            logging.error(f"Failed to save updated YAML file '{yaml_path}': {e}")
            continue

def run_cleanup(config, tag=None, dry_run=False):
    """Clean up branches, tags, and MRs from a previous failed run."""
    if dry_run:
        logging.info("Running cleanup mode (DRY RUN - no changes will be made)...")
    else:
        logging.info("Running cleanup mode...")

    for repo_url in config.keys():
        clone_path = os.path.join(BASE_DIR, get_repo_name(repo_url))
        if not os.path.exists(clone_path):
            logging.debug(f"Repository '{repo_url}' not cloned, skipping cleanup")
            continue

        try:
            repo = Repo(clone_path)

            # Delete local update-submodules branches
            for branch in repo.heads:
                if 'update-submodules' in branch.name:
                    if branch != repo.active_branch:
                        if dry_run:
                            logging.info(f"Would delete local branch '{branch.name}' in '{get_repo_name(repo_url)}'")
                        else:
                            try:
                                repo.delete_head(branch, force=True)
                                logging.info(f"Deleted local branch '{branch.name}' in '{get_repo_name(repo_url)}'")
                            except GitCommandError as e:
                                logging.warning(f"Could not delete local branch '{branch.name}': {e}")

            # Delete remote update-submodules branches
            repo.remotes.origin.fetch()
            for ref in repo.remotes.origin.refs:
                if 'update-submodules' in ref.remote_head:
                    if dry_run:
                        logging.info(f"Would delete remote branch '{ref.remote_head}' in '{get_repo_name(repo_url)}'")
                    else:
                        try:
                            repo.remotes.origin.push(refspec=f":{ref.remote_head}")
                            logging.info(f"Deleted remote branch '{ref.remote_head}' in '{get_repo_name(repo_url)}'")
                        except GitCommandError as e:
                            logging.warning(f"Could not delete remote branch '{ref.remote_head}': {e}")

            # Delete specified tag if provided
            if tag:
                # Delete local tag
                if tag in [t.name for t in repo.tags]:
                    if dry_run:
                        logging.info(f"Would delete local tag '{tag}' in '{get_repo_name(repo_url)}'")
                    else:
                        try:
                            repo.delete_tag(tag)
                            logging.info(f"Deleted local tag '{tag}' in '{get_repo_name(repo_url)}'")
                        except GitCommandError as e:
                            logging.warning(f"Could not delete local tag '{tag}': {e}")

                # Delete remote tag (try Git first, then API)
                if dry_run:
                    # Check if tag exists on remote
                    try:
                        repo.remotes.origin.fetch(refspec=f"refs/tags/{tag}:refs/tags/{tag}", no_tags=True)
                        logging.info(f"Would delete remote tag '{tag}' in '{get_repo_name(repo_url)}'")
                    except GitCommandError:
                        # Tag doesn't exist on remote, nothing to do
                        pass
                else:
                    try:
                        repo.remotes.origin.push(refspec=f":refs/tags/{tag}")
                        logging.info(f"Deleted remote tag '{tag}' in '{get_repo_name(repo_url)}'")
                    except GitCommandError:
                        # Try API for protected tags
                        if delete_gitlab_tag_via_api(repo_url, tag):
                            logging.info(f"Deleted protected tag '{tag}' via API in '{get_repo_name(repo_url)}'")
                        else:
                            logging.warning(f"Could not delete remote tag '{tag}' in '{get_repo_name(repo_url)}'")

        except Exception as e:
            logging.error(f"Error during cleanup of '{repo_url}': {e}")

    if dry_run:
        logging.info("Cleanup dry run completed - no changes were made")
    else:
        logging.info("Cleanup completed")

def push_all_operations(operations, sorted_repos, no_push_tags=False, release_to_master=False, tag=None):
    """Phase 2: Push all operations to remote in topological order."""
    logging.info("=" * 60)
    logging.info("PHASE 2: Pushing all changes to remote repositories")
    if no_push_tags:
        logging.info("(Tags will NOT be pushed - --no-push-tags specified)")
    logging.info("=" * 60)

    push_failures = []

    # Push in topological order to ensure dependencies are available
    for repo_url in sorted_repos:
        if repo_url not in operations:
            continue

        operation = operations[repo_url]
        if not operation.success:
            continue

        # Skip if no branch and no tag to push
        if not operation.branch_name and not operation.tag_to_create:
            continue

        logging.info(f"Pushing changes for '{get_repo_name(repo_url)}'...")

        try:
            repo = operation.repo_object
            settings = operation.settings

            # Push the branch and create MR
            if operation.branch_name:
                automerge = settings.get('automerge', False)
                create_mr = settings.get('create_merge_request', True)
                push_branch(repo, repo_url, operation.branch_name, settings, automerge, create_mr, dry_run=False,
                           release_to_master=release_to_master, tag=tag)

            # Push tag (already created locally in Phase 1)
            if operation.tag_to_create and not no_push_tags:
                push_tag(repo, repo_url, operation.tag_to_create)
            elif operation.tag_to_create and no_push_tags:
                logging.info(f"Skipping tag push for '{operation.tag_to_create}' (--no-push-tags specified)")
                
        except Exception as e:
            logging.error(f"Failed to push changes for '{repo_url}': {e}")
            push_failures.append(repo_url)
            
    if push_failures:
        logging.error(f"Failed to push changes for the following repositories: {push_failures}")
        logging.error("You may need to manually push these or run with --cleanup to reset")
        return False
    else:
        logging.info("All changes pushed successfully!")
        return True

def main():
    """Main function to orchestrate the update process."""
    args = parse_arguments()
    setup_logging(args.log_level)
    logging.debug("Starting the repository update process.")
    
    # Load and validate configuration
    config = load_config(CONFIG_FILE)
    validate_config(config)
    
    # Handle cleanup mode
    if args.cleanup:
        run_cleanup(config, args.cleanup_tag, args.dry_run)
        sys.exit(0)

    if not os.path.exists(BASE_DIR):
        os.makedirs(BASE_DIR)
        logging.debug(f"Created base directory '{BASE_DIR}' for cloning repositories.")

    # Validate refs and build dependency graph
    validate_refs(config, tag=args.tag)
    dependency_graph = build_dependency_graph(config)
    sorted_repos = topological_sort(dependency_graph)
    # Reverse the sorted list to process submodules first
    sorted_repos.reverse()

    logging.info("Processing repositories in the following order:")
    for repo_url in sorted_repos:
        logging.info(f"- {get_repo_name(repo_url)}")

    # ========================================================================
    # PHASE 1: Local Processing and Validation
    # ========================================================================
    logging.info("=" * 60)
    logging.info("PHASE 1: Local processing and validation")
    logging.info("=" * 60)
    
    # Validate YAML operations before starting
    yaml_valid, yaml_errors, yaml_warnings = validate_yaml_operations(config)
    if not yaml_valid:
        logging.error("YAML validation failed. Please fix the errors above and try again.")
        sys.exit(1)
    
    # Initialize a mapping to track repositories updated via branches
    updated_repos_branches = {}
    operations = {}

    for repo_url in sorted_repos:
        settings = config.get(repo_url, {})
        if not settings:
            logging.error(f"No settings found for repository '{repo_url}'. Skipping.")
            continue
            
        tag = args.tag
        retag = args.retag
        desired_ref = settings.get('ref') if 'ref' in settings else None
        desired_ref_from_dir = settings.get('ref_from_dir') if 'ref_from_dir' in settings else None
        
        logging.info(f"\nProcessing repository '{get_repo_name(repo_url)}' to '{desired_ref if desired_ref else 'ref_from_dir'}'...")
        
        # Phase 1: Process locally only
        result = update_repo(
            repo_url=repo_url,
            desired_ref=desired_ref,
            config=config,
            tag=tag,
            retag=retag,
            push_enabled=False,  # Phase 1: local only
            updated_repos_branches=updated_repos_branches,
            release_to_master=args.release_to_master,
            force=args.force
        )
        
        operations[repo_url] = result
        
        if not result.success:
            logging.error(f"Failed to process repository '{repo_url}': {result.error_message}")
            logging.error("Stopping due to error in Phase 1. No changes pushed to remote.")
            sys.exit(1)
    
    # If dry-run, stop here (Phase 1 only)
    if args.dry_run:
        logging.info("=" * 60)
        logging.info("DRY RUN COMPLETE - No changes pushed to remote")
        logging.info("=" * 60)
        logging.info("\nSummary of what would be pushed in Phase 2:")

        for repo_url, operation in operations.items():
            settings = operation.settings
            if operation.branch_name or operation.tag_to_create:
                logging.info(f"\n{get_repo_name(repo_url)}:")

                # Branch and MR info
                if operation.branch_name:
                    create_mr = settings.get('create_merge_request', True)
                    automerge = settings.get('automerge', False)
                    target_branch = settings.get('target_branch', settings.get('ref', 'main'))

                    logging.info(f"  - Would push branch: {operation.branch_name}")
                    if create_mr:
                        mr_desc = f"  - Would create MR to {target_branch}"
                        if automerge:
                            mr_desc += " (with auto-merge)"
                        logging.info(mr_desc)

                    # Show what's in the branch
                    if operation.submodules_updated or operation.yaml_files_updated:
                        logging.info("    Branch contains:")
                        if operation.submodules_updated:
                            for sub_url, sub_details in operation.submodules_updated.items():
                                logging.info(f"      - Updated submodule: {sub_details['name']} to {sub_details['ref']}")
                        if operation.yaml_files_updated:
                            for filename in operation.yaml_files_updated.keys():
                                logging.info(f"      - Modified YAML: {filename}")

                # Tag info
                if operation.tag_to_create:
                    if args.no_push_tags:
                        logging.info(f"  - Tag created locally: {operation.tag_to_create} (will NOT be pushed due to --no-push-tags)")
                    else:
                        logging.info(f"  - Would push tag: {operation.tag_to_create} (already created locally)")
        sys.exit(0)
    
    # ========================================================================
    # PHASE 2: Push to Remote (in topological order)
    # ========================================================================
    success = push_all_operations(operations, sorted_repos, no_push_tags=args.no_push_tags,
                                  release_to_master=args.release_to_master, tag=args.tag)
    
    if success:
        logging.info("=" * 60)
        logging.info("Repository update process completed successfully!")
        logging.info("=" * 60)
    else:
        logging.error("Some operations failed. See errors above.")
        sys.exit(1)

if __name__ == "__main__":
    main()
