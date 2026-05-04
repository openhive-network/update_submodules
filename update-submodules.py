#!/usr/bin/env python3

import os
import sys
import time
import yaml
import git
import argparse
import logging
import requests
import configparser
import subprocess
import shutil
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
from collections import defaultdict
from ruamel.yaml import YAML
from git import Repo, GitCommandError, PushInfo

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
    rebase_performed: bool = False  # Release mode: track if rebase was done
    conflicts_resolved: List = None  # Release mode: track resolved conflicts
    sanity_check_passed: bool = True  # Release mode: track sanity check result

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
    parser.add_argument('--cleanup', action='store_true', help='Clean up branches, tags, and MRs from a previous failed run.')
    parser.add_argument('--cleanup-tag', type=str, help='Specific tag to clean up when using --cleanup.')
    parser.add_argument('--push-delay', type=int, default=15, help='Delay in seconds between pushing to each repository (default: 15). Set to 0 to disable.')
    parser.add_argument('--release-to-master', action='store_true', help='Rebase source commits onto target branch for release. Defaults config to repos.yaml.master.')
    parser.add_argument('--no-push-tags', action='store_true', help='Create tags locally but do not push them to remote (useful for release validation).')
    parser.add_argument('--force', action='store_true', help='Skip prompts and force continue on warnings (release mode only).')
    parser.add_argument('--config', type=str, default=None, help='Path to the YAML config file. Defaults to repos.yaml, or repos.yaml.master when --release-to-master is set.')
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
                     target_branch: str = 'origin/master',
                     max_target_walk: int = 2000) -> Optional[str]:
    """
    Find the commit on source_ref whose git tree matches target_branch's HEAD.

    Releases historically bring master/main to the same tree state as some commit
    on develop, but with a different SHA (because of how releases land — typically
    via a feature branch and MR rather than a merge).  Past hotfixes that landed on
    master without being mirrored onto develop can also accumulate, but their tree
    effect is usually re-introduced in develop's own history.  In both cases the
    correspondence we want is "same tree", not "same patch" — patch-id is sensitive
    to whitespace and rebase noise, tree-hash isn't.

    Algorithm:
      1. Walk source_ref from HEAD backwards, building a {tree_hash: commit} map
         (most recent occurrence per tree wins, since git log is reverse-chrono).
      2. Walk target_branch from HEAD up to max_target_walk commits looking for a
         commit whose tree exists in that map.
      3. Return the corresponding source_ref commit — the rebase base.

    If target HEAD's tree isn't on source, we walk back on target a bit (some
    master-only hotfixes may not have been mirrored to develop); the warning
    explains the divergence so it can be reconciled before release.

    Returns the source_ref commit, or None if no tree match is found.  Callers
    should NOT silently fall back to merge-base — that produces a much older
    rebase base and an enormous, conflict-heavy rebase.
    """
    original_dir = os.getcwd()

    try:
        os.chdir(repo_path)

        # Resolve target HEAD for log messages
        target_head = subprocess.run(
            ['git', 'rev-parse', target_branch],
            capture_output=True, text=True, check=True
        ).stdout.strip()

        # Build tree → commit map for source_ref. git log is reverse-chronological,
        # so the first time we see a tree, that's the most recent commit with it.
        result = subprocess.run(
            ['git', 'log', '--pretty=format:%H %T', source_ref],
            capture_output=True, text=True, check=True
        )
        tree_to_source = {}
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2:
                commit, tree = parts
                if tree not in tree_to_source:
                    tree_to_source[tree] = commit
        logging.debug(f"Indexed {len(tree_to_source)} unique trees on {source_ref}")

        # Walk target_branch from HEAD looking for a tree present on source_ref.
        result = subprocess.run(
            ['git', 'log', '--pretty=format:%H %T', '--max-count', str(max_target_walk), target_branch],
            capture_output=True, text=True, check=True
        )
        for i, line in enumerate(result.stdout.splitlines()):
            parts = line.split()
            if len(parts) != 2:
                continue
            target_commit, target_tree = parts
            if target_tree in tree_to_source:
                source_commit = tree_to_source[target_tree]
                if i == 0:
                    logging.info(
                        f"Tree-match: {target_branch} HEAD ({target_commit[:8]}) "
                        f"== {source_ref} commit {source_commit[:8]}"
                    )
                else:
                    logging.warning(
                        f"{target_branch} HEAD ({target_head[:8]}) has no tree-match on {source_ref}; "
                        f"using match {i} commits back: {target_branch}~{i} ({target_commit[:8]}) "
                        f"== {source_ref} {source_commit[:8]}."
                    )
                    logging.warning(
                        f"{target_branch} has {i} commit(s) past its last {source_ref} sync point. "
                        "The rebase will produce a release branch with develop's tree on top of "
                        f"{target_branch} HEAD; the post-rebase tree-alignment step will reconcile "
                        f"any drift so the final tree matches {source_ref}'s tip."
                    )
                return source_commit

        logging.error(
            f"No commit on {target_branch} within the last {max_target_walk} commits has a tree "
            f"present on {source_ref}. {target_branch} has diverged significantly — "
            "manual reconciliation is needed before running --release-to-master for this repo."
        )
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
        source_branch: Kept for diagnostics/compat; conflict resolution reads
                       directly from REBASE_HEAD on each iteration.
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
            # Resolution rule for release-to-master: take REBASE_HEAD's version of
            # every conflicted path.  REBASE_HEAD is the commit currently being
            # applied (theirs side of the cherry-pick), so this is "develop wins"
            # at the right granularity — each commit sees the state it expects,
            # rather than jumping straight to source's final tip (which causes
            # subsequent commits to re-conflict against future-state files).
            #
            # Reads mode + hash directly from `git ls-tree REBASE_HEAD`, so the
            # logic doesn't depend on .gitmodules being present in the working
            # tree — important because develop sometimes drops submodules that
            # master still has, taking .gitmodules with them.
            UNMERGED = ('UU', 'AA', 'DU', 'UA', 'UD', 'DD', 'AU')

            for status_line in status_lines:
                if len(status_line) < 4:
                    continue
                status = status_line[:2]
                if status not in UNMERGED:
                    continue
                file_path = status_line[3:].strip()

                # What does REBASE_HEAD (the commit being applied) have at this path?
                rh_lookup = subprocess.run(
                    ['git', 'ls-tree', 'REBASE_HEAD', '--', file_path],
                    capture_output=True, text=True
                )
                rh_has_path = bool(rh_lookup.returncode == 0 and rh_lookup.stdout.strip())

                # Clear the conflicted entry from the index, then re-stage at
                # REBASE_HEAD's version (or leave gone if REBASE_HEAD doesn't have it).
                subprocess.run(['git', 'rm', '--cached', '-f', '--', file_path],
                               capture_output=True, text=True)

                if rh_has_path:
                    parts = rh_lookup.stdout.split()
                    if len(parts) >= 3:
                        mode = parts[0]      # '100644', '100755', '120000', '160000', ...
                        obj_hash = parts[2]
                        update_result = subprocess.run(
                            ['git', 'update-index', '--add', '--cacheinfo', mode, obj_hash, file_path],
                            capture_output=True, text=True
                        )
                        if update_result.returncode != 0:
                            logging.warning(f"Could not stage {file_path} at REBASE_HEAD's version: {update_result.stderr.strip()}")

                        if mode == '160000':
                            kind = 'submodule'
                            logging.debug(f"Resolved submodule {file_path} → {obj_hash[:8]} from REBASE_HEAD (status {status})")
                        else:
                            # Restore working tree from index for regular files / symlinks
                            subprocess.run(['git', 'checkout-index', '-f', '--', file_path],
                                           capture_output=True, text=True)
                            kind = 'yaml' if file_path.endswith(('.yml', '.yaml')) else 'other'
                            if kind == 'other':
                                logging.debug(f"Resolved {file_path} from REBASE_HEAD (status {status})")
                            else:
                                logging.debug(f"Resolved YAML {file_path} from REBASE_HEAD (status {status})")
                        conflicts_resolved.append(f"{kind}:{file_path}")
                        conflicts_found = True
                        continue

                # REBASE_HEAD doesn't have this path → accept its absence.
                logging.debug(f"Path {file_path} absent on REBASE_HEAD — removing (status {status})")
                full_path = os.path.join(repo_path, file_path)
                if os.path.isdir(full_path) and not os.path.islink(full_path):
                    shutil.rmtree(full_path, ignore_errors=True)
                elif os.path.lexists(full_path):
                    try:
                        os.unlink(full_path)
                    except OSError:
                        pass
                conflicts_resolved.append(f"deleted:{file_path}")
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
                if 'filename' not in edit or 'key_to_update' not in edit:
                    logging.error(f"Each 'update_yaml' entry for repository '{repo_url}' must contain 'filename' and 'key_to_update'.")
                    sys.exit(1)
                if not isinstance(edit['filename'], str) or not edit['filename'].strip():
                    logging.error(f"Invalid 'filename' in 'update_yaml' for repository '{repo_url}'.")
                    sys.exit(1)
                if not isinstance(edit['key_to_update'], str) or not edit['key_to_update'].strip():
                    logging.error(f"Invalid 'key_to_update' in 'update_yaml' for repository '{repo_url}'.")
                    sys.exit(1)
                # Entries must have 'submodule_referenced', 'value', or 'action: remove'
                action = edit.get('action')
                has_value = 'value' in edit
                has_submodule = 'submodule_referenced' in edit
                if not has_submodule and not has_value and action != 'remove':
                    logging.error(f"'update_yaml' entry in '{repo_url}' must have 'submodule_referenced', 'value', or 'action: remove'.")
                    sys.exit(1)
                if has_submodule and (not isinstance(edit['submodule_referenced'], str) or not edit['submodule_referenced'].strip()):
                    logging.error(f"Invalid 'submodule_referenced' in 'update_yaml' for repository '{repo_url}'.")
                    sys.exit(1)
                if action is not None and action != 'remove':
                    logging.error(f"Invalid 'action' value '{action}' in 'update_yaml' for repository '{repo_url}'. Only 'remove' is supported.")
                    sys.exit(1)
                if 'when' in edit and edit['when'] != 'tag':
                    logging.error(f"Invalid 'when' value '{edit['when']}' in 'update_yaml' for repository '{repo_url}'. Only 'tag' is supported.")
                    sys.exit(1)
                if 'short_sha' in edit and not isinstance(edit['short_sha'], bool):
                    logging.error(f"'short_sha' must be a boolean in 'update_yaml' for repository '{repo_url}'.")
                    sys.exit(1)
    logging.info("Configuration validation passed.")

def normalize_gitlab_url(url):
    """
    Normalize GitLab URLs to consistent git@ SSH format.

    Ensures HTTPS and SSH URLs pointing to the same repository are recognized
    as identical for dependency matching.
    """
    if url.startswith('https://gitlab.syncad.com/'):
        url = url.replace('https://gitlab.syncad.com/', 'git@gitlab.syncad.com:')
    elif url.startswith('http://gitlab.syncad.com/'):
        url = url.replace('http://gitlab.syncad.com/', 'git@gitlab.syncad.com:')
    return url

def resolve_submodule_url(parent_repo_url, submodule_url):
    """Resolve a submodule URL relative to the parent repo URL if necessary."""
    # If the submodule URL is already absolute (starts with git@, http, ssh, etc.),
    # normalize it to the canonical SSH form for consistent dependency matching.
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
    repo_urls = set(config.keys())

    for repo_url in repo_urls:
        clone_path = os.path.join(BASE_DIR, get_repo_name(repo_url))
        repo = clone_repo(repo_url, clone_path)
        if repo is None:
            logging.error(f"Skipping repository '{repo_url}' due to cloning issues.")
            continue
        submodules = parse_submodules(repo)
        # Filter submodules to include only those in config
        filtered_submodules = [s for s in submodules if s in repo_urls]
        graph[repo_url].extend(filtered_submodules)
        logging.debug(f"Repository '{repo_url}' has submodules: {filtered_submodules}")

        # Also add dependencies from update_yaml submodule_referenced entries
        settings = config.get(repo_url, {})
        for edit in settings.get('update_yaml', []):
            ref_url = edit.get('submodule_referenced')
            if ref_url and ref_url in repo_urls and ref_url not in graph[repo_url]:
                graph[repo_url].append(ref_url)
                logging.debug(f"Repository '{repo_url}' depends on '{get_repo_name(ref_url)}' via update_yaml")

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

def project_to_repo_url(project_path):
    """Convert a GitLab project path (e.g., 'hive/common-ci-configuration') to a repo URL."""
    return f"git@gitlab.syncad.com:{project_path}.git"

def _looks_like_sha(ref):
    """Return True if ref looks like a hex commit SHA (7-40 hex chars)."""
    return len(ref) >= 7 and all(c in '0123456789abcdef' for c in ref.lower())

def detect_ci_includes(clone_path):
    """
    Detect CI includes from a repository's .gitlab-ci.yml or .gitlab-ci.yaml file.

    Returns a list of dicts with 'project', 'ref', and 'index' (position in include list).
    Only returns includes that have both 'project' and 'ref' fields.
    """
    yaml_obj = YAML()
    yaml_obj.preserve_quotes = True
    yaml_obj.allow_duplicate_keys = True

    # Try both possible CI file names
    ci_files = ['.gitlab-ci.yml', '.gitlab-ci.yaml']
    ci_file_path = None
    ci_filename = None

    for filename in ci_files:
        path = os.path.join(clone_path, filename)
        if os.path.exists(path):
            ci_file_path = path
            ci_filename = filename
            break

    if not ci_file_path:
        logging.debug(f"No CI file found in {clone_path}")
        return [], None

    try:
        with open(ci_file_path, 'r') as f:
            data = yaml_obj.load(f)
    except Exception as e:
        logging.warning(f"Failed to parse CI file {ci_file_path}: {e}")
        return [], None

    if not data or 'include' not in data:
        logging.debug(f"No 'include' section in {ci_file_path}")
        return [], ci_filename

    includes = data['include']
    if not isinstance(includes, list):
        logging.debug(f"'include' is not a list in {ci_file_path}")
        return [], ci_filename

    detected = []
    for idx, item in enumerate(includes):
        if isinstance(item, dict) and 'project' in item and 'ref' in item:
            project = item['project']
            ref = item['ref']
            # Normalize project path (remove quotes if present in string)
            if isinstance(project, str):
                project = project.strip("'\"")
            detected.append({
                'project': project,
                'ref': ref,
                'index': idx
            })
            logging.debug(f"Detected CI include: project={project}, ref={ref}")

    return detected, ci_filename

def collect_auto_detected_yaml_updates(clone_path, config, updated_repos_commits):
    """
    Auto-detect CI includes and collect updates for repos that have been updated.

    Args:
        clone_path: Path to the cloned repository
        config: The configuration dictionary
        updated_repos_commits: Dict mapping repo URLs to their new commit hashes

    Returns:
        Dict of filename -> list of update entries
    """
    detected_includes, ci_filename = detect_ci_includes(clone_path)

    if not detected_includes or not ci_filename:
        return {}

    updated_yaml_files = {}

    for include in detected_includes:
        project = include['project']
        current_ref = include['ref']
        idx = include['index']

        # Skip common-ci-configuration includes that use a branch name (e.g. 'develop')
        # since those are intentionally dynamic. But if the ref is already pinned to a
        # commit SHA, update it like any other include so it stays in sync with submodules.
        if project == 'hive/common-ci-configuration' and not _looks_like_sha(current_ref):
            logging.debug(f"Skipping CI include for {project} - ref '{current_ref}' is dynamic")
            continue

        # Convert project path to repo URL
        repo_url = project_to_repo_url(project)

        # Check if this project is in our config and has been updated
        if repo_url in updated_repos_commits:
            new_commit = updated_repos_commits[repo_url]

            # Skip if the ref is already the new commit
            if current_ref == new_commit:
                logging.debug(f"CI include for {project} already at {new_commit}")
                continue

            logging.info(f"Auto-detected CI include update: {project} {current_ref} -> {new_commit}")

            # Build the key path for this include
            key_to_update = f"include[project={project}].ref"

            if ci_filename not in updated_yaml_files:
                updated_yaml_files[ci_filename] = []

            updated_yaml_files[ci_filename].append({
                'filename': ci_filename,
                'key_to_update': key_to_update,
                'submodule_referenced': repo_url,  # Use repo URL for commit lookup
                'auto_detected': True
            })

    return updated_yaml_files

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

            # Fetch from local remote (--no-tags avoids exit code 1 when
            # a local tag conflicts with a tag in the source repo)
            submodule_repo.remotes[local_remote_name].fetch(no_tags=True)
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

        # When using local remote, ALWAYS checkout from local_temp to avoid stale local branches
        # This fixes a bug where old branches from previous runs could be used instead of fresh commits
        if local_remote_used:
            try:
                remote_branch = f"local_temp/{desired_ref}"
                # Delete any existing local branch to ensure we get the fresh commit
                if is_branch(desired_ref, submodule_repo):
                    logging.debug(f"Deleting stale local branch '{desired_ref}' to use fresh commit from local remote")
                    submodule_repo.git.branch('-D', desired_ref)
                submodule_repo.git.checkout('-b', desired_ref, remote_branch)
                desired_commit = submodule_repo.head.commit.hexsha
                logging.debug(f"Checked out '{desired_ref}' from local remote at commit {desired_commit[:12]}")
                return desired_commit
            except GitCommandError as e:
                logging.debug(f"Could not checkout from local_temp/{desired_ref}: {e}")
                # Fall through to try other methods

        # Check if ref exists as a local branch (only when NOT using local remote)
        if not local_remote_used and is_branch(desired_ref, submodule_repo):
            submodule_repo.git.checkout(desired_ref)
            submodule_repo.remotes.origin.pull()
            desired_commit = submodule_repo.head.commit.hexsha
            return desired_commit

        # Try origin remote branch
        try:
            remote_branch = f"origin/{desired_ref}"
            # Delete any existing local branch if we need to recreate from origin
            if is_branch(desired_ref, submodule_repo):
                submodule_repo.git.branch('-D', desired_ref)
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
    prefix = 'origin/'
    return any(
        r.name.startswith(prefix) and r.name[len(prefix):] == ref
        for r in repo.remotes.origin.refs
    )

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
            action = edit.get('action')
            is_remove = action == 'remove'
            is_value = 'value' in edit

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
                                if is_remove:
                                    warnings.append(f"Key '{list_key}' not found for removal in '{edit['filename']}' in '{repo_url}' (will be skipped)")
                                else:
                                    errors.append(f"Key '{list_key}' is not a list in YAML file '{edit['filename']}' in repository '{repo_url}'")
                                raise KeyError

                            # Find matching item
                            matched_item = None
                            for item in current[list_key]:
                                if isinstance(item, dict) and item.get(field) == value:
                                    matched_item = item
                                    break

                            if not matched_item:
                                if is_remove:
                                    warnings.append(f"No item found for removal condition '{field}={value}' in '{edit['filename']}' in '{repo_url}' (will be skipped)")
                                else:
                                    errors.append(f"No item found in list '{list_key}' with condition '{field}={value}' in YAML file '{edit['filename']}' in repository '{repo_url}'")
                                raise KeyError

                            current = matched_item
                        else:
                            if key not in current:
                                if is_remove:
                                    warnings.append(f"Key '{key}' not found for removal in path '{key_to_update}' in '{edit['filename']}' in '{repo_url}' (will be skipped)")
                                else:
                                    # Intermediate keys will be created during update
                                    warnings.append(f"Key '{key}' not found in path '{key_to_update}' in YAML file '{edit['filename']}' in repository '{repo_url}' (will be created)")
                                raise KeyError
                            current = current[key]

                    # Check last key exists
                    last_key = keys[-1]
                    if last_key not in current:
                        if is_remove:
                            warnings.append(f"Key '{last_key}' not found for removal in '{key_to_update}' in '{edit['filename']}' in '{repo_url}' (will be skipped)")
                        else:
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
    if automerge:
        push_options.append('merge_request.merge_when_pipeline_succeeds=true')
    if mr_title:
        push_options.append(f'merge_request.title={mr_title}')
    return push_options

def update_repo(repo_url, desired_ref, config, tag=None, retag=False, push_enabled=False,
                updated_repos_branches=None, updated_repos_commits=None,
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
        updated_repos_commits (dict, optional): A mapping of repository URLs to their commit hashes.
                                                Used for auto-detecting CI include updates.
        release_to_master (bool, optional): Run the release-to-master workflow (rebase source onto target). Defaults to False.
        force (bool, optional): Skip release-mode sanity-check prompts. Defaults to False.

    Returns:
        RepoOperationResult: The result of the operation.
    """
    if updated_repos_branches is None:
        updated_repos_branches = {}
    if updated_repos_commits is None:
        updated_repos_commits = {}

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

    # Track source HEAD across the function for the release-mode sanity check
    source_head = None

    if release_to_master:
        # ----- Release-to-master workflow: rebase source ref onto target branch -----
        source_ref = settings.get('source_ref', 'develop')
        target_branch = desired_ref  # expected to be 'master' or 'main'

        if target_branch not in ['master', 'main']:
            logging.error(f"For --release-to-master, repo {repo_url} must have ref: master or main, got: {target_branch}")
            result.success = False
            result.error_message = f"Invalid target branch for release: {target_branch}"
            return result

        # Fetch updates first so we have origin refs available
        fetch_updates(repo, repo_url)

        # Resolve source_ref: prefer local branch, fall back to origin/<branch>
        if not source_ref.startswith('origin/') and '/' not in source_ref:
            try:
                subprocess.run(['git', 'rev-parse', '--verify', source_ref],
                               cwd=clone_path, capture_output=True, text=True, check=True)
            except subprocess.CalledProcessError:
                try:
                    subprocess.run(['git', 'rev-parse', '--verify', f'origin/{source_ref}'],
                                   cwd=clone_path, capture_output=True, text=True, check=True)
                    source_ref = f'origin/{source_ref}'
                    logging.debug(f"Using remote reference: {source_ref}")
                except subprocess.CalledProcessError:
                    logging.error(f"Neither {source_ref} nor origin/{source_ref} exists")
                    result.success = False
                    result.error_message = f"Source reference {source_ref} not found"
                    return result

        logging.info(f"Release workflow: {source_ref} -> {target_branch}")

        # Find the rebase base by tree-hash matching.  Failure means master has
        # diverged from develop in a way the script can't auto-resolve — fail
        # loudly rather than fall back to merge-base (which would try to rebase
        # the entire history since the last release branch divergence).
        logging.info(f"Finding rebase base for {repo_url}...")
        rebase_base = find_rebase_base(clone_path, source_ref, f'origin/{target_branch}')

        if not rebase_base:
            result.success = False
            result.error_message = (
                f"Could not find a tree-match between {target_branch} and {source_ref}. "
                f"This repo needs manual reconciliation before --release-to-master."
            )
            return result

        # Detect fast-forward case: target HEAD is already on source ref
        is_fast_forward = False
        try:
            target_head_result = subprocess.run(
                ['git', 'rev-parse', f'origin/{target_branch}'],
                cwd=clone_path, capture_output=True, text=True, check=True
            )
            target_head = target_head_result.stdout.strip()
            ancestor_check = subprocess.run(
                ['git', 'merge-base', '--is-ancestor', target_head, source_ref],
                cwd=clone_path, capture_output=True, text=True
            )
            if ancestor_check.returncode == 0:
                logging.info(f"Target {target_branch} HEAD ({target_head[:8]}) is already on {source_ref} - fast-forward")
                is_fast_forward = True
                rebase_base = target_head
        except subprocess.CalledProcessError as e:
            logging.debug(f"Error checking for fast-forward: {e}")

        # Count commits to apply
        try:
            result_cmd = subprocess.run(
                ['git', 'rev-list', '--count', f'{rebase_base}..{source_ref}'],
                cwd=clone_path, capture_output=True, text=True, check=True
            )
            commit_count = int(result_cmd.stdout.strip())
            verb = "fast-forward" if is_fast_forward else "rebase"
            logging.info(f"Will {verb} {commit_count} commits from {rebase_base[:8]}")
        except Exception:
            commit_count = 0

        # 0 commits to rebase: defer release-branch creation; checkout target so we
        # can still pick up submodule updates if any.
        if commit_count == 0:
            logging.info(f"No commits to rebase for {repo_url} - source and target already aligned")
            result.rebase_performed = False

            try:
                subprocess.run(
                    ['git', 'checkout', '-B', target_branch,
                     '--track', f'origin/{target_branch}', '--no-recurse-submodules'],
                    cwd=clone_path, check=True, capture_output=True, text=True
                )
                release_branch = f'release/{tag}' if tag else f'release-{target_branch}-{get_repo_name(repo_url)}'
                result.branch_name = None  # set later if a release branch is needed
                desired_ref_actual = target_branch
                settings['pending_release_branch'] = release_branch
                repo = Repo(clone_path)
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to checkout target branch: {e}")
                result.success = False
                result.error_message = str(e)
                return result
        else:
            # Capture source HEAD before we move things around (used by sanity check)
            try:
                result_cmd = subprocess.run(
                    ['git', 'rev-parse', source_ref],
                    cwd=clone_path, capture_output=True, text=True, check=True
                )
                source_head = result_cmd.stdout.strip()
            except Exception:
                source_head = source_ref

            release_branch = f'release/{tag}' if tag else f'release-{target_branch}-{get_repo_name(repo_url)}'

            # Clean any previous local copy of the target branch then create a release
            # branch from source so we can rebase it onto target.
            try:
                subprocess.run(['git', 'branch', '-D', target_branch],
                               cwd=clone_path, capture_output=True, text=True)
            except Exception:
                pass

            try:
                subprocess.run(['git', 'reset', '--hard'], cwd=clone_path, capture_output=True, text=True)
                subprocess.run(['git', 'checkout', source_ref],
                               cwd=clone_path, check=True, capture_output=True, text=True)
                subprocess.run(['git', 'checkout', '-b', release_branch],
                               cwd=clone_path, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to create release branch: {e}")
                result.success = False
                result.error_message = str(e)
                return result

            if is_fast_forward:
                logging.info(f"Performing fast-forward merge from {source_ref}...")
                try:
                    subprocess.run(['git', 'merge', '--ff-only', source_ref],
                                   cwd=clone_path, capture_output=True, text=True, check=True)
                    logging.info("Fast-forward merge completed successfully")
                    success = True
                    conflicts = []
                except subprocess.CalledProcessError as e:
                    logging.error(f"Fast-forward merge failed: {e}")
                    result.success = False
                    result.error_message = "Fast-forward merge failed"
                    return result
            else:
                success, conflicts = perform_release_rebase(
                    clone_path, rebase_base, source_ref, f'origin/{target_branch}', commit_count
                )

            if not success:
                result.success = False
                result.error_message = "Rebase or merge failed"
                return result

            result.branch_name = release_branch
            updated_repos_branches[repo_url] = release_branch
            logging.debug(f"Recorded release branch '{release_branch}' for '{repo_url}'")

            if is_fast_forward:
                result.rebase_performed = False
                result.conflicts_resolved = []
            else:
                result.rebase_performed = True
                result.conflicts_resolved = conflicts
                if conflicts:
                    logging.info(f"Auto-resolved {len(conflicts)} conflicts:")
                    for c in conflicts[:5]:
                        logging.info(f"  - {c}")
                    if len(conflicts) > 5:
                        logging.info(f"  ... and {len(conflicts)-5} more")

                # After rebase we are detached; move release branch to new HEAD and check it out.
                try:
                    head_commit = subprocess.run(['git', 'rev-parse', 'HEAD'],
                                                 cwd=clone_path, capture_output=True, text=True, check=True).stdout.strip()
                    subprocess.run(['git', 'branch', '-f', release_branch, head_commit],
                                   cwd=clone_path, check=True, capture_output=True)
                    subprocess.run(['git', 'checkout', release_branch],
                                   cwd=clone_path, check=True, capture_output=True)
                except subprocess.CalledProcessError as e:
                    logging.error(f"Failed to update release branch after rebase: {e}")
                    result.success = False
                    result.error_message = str(e)
                    return result

                # Post-rebase reconciliation: if HEAD's tree differs from the source
                # ref's tree, develop's non-linear history was linearized in a way
                # that produced an intermediate state at HEAD (e.g., a delete commit
                # placed before a related modify commit, leaving the file modified
                # instead of deleted).  Add one alignment commit so the release
                # branch's final tree exactly matches develop's tip.
                try:
                    head_tree = subprocess.run(['git', 'rev-parse', 'HEAD^{tree}'],
                                               cwd=clone_path, capture_output=True, text=True, check=True).stdout.strip()
                    src_tree = subprocess.run(['git', 'rev-parse', f'{source_ref}^{{tree}}'],
                                              cwd=clone_path, capture_output=True, text=True, check=True).stdout.strip()
                    if head_tree != src_tree:
                        logging.warning(
                            f"Post-rebase tree {head_tree[:8]} differs from {source_ref} tip {src_tree[:8]} — "
                            f"non-linear develop history was linearized into an intermediate state.  "
                            f"Adding a tree-alignment commit so the release branch matches develop's tip."
                        )
                        # Replace index + working tree with source's tree, then commit the diff.
                        subprocess.run(['git', 'read-tree', '--reset', '-u', source_ref],
                                       cwd=clone_path, check=True, capture_output=True, text=True)
                        diff_check = subprocess.run(['git', 'diff', '--cached', '--quiet'],
                                                    cwd=clone_path, capture_output=True, text=True)
                        if diff_check.returncode != 0:
                            subprocess.run(
                                ['git', 'commit', '-m',
                                 f'Align release branch to {source_ref} tip\n\n'
                                 f'Reconciles drift introduced by rebase linearization of '
                                 f"{source_ref}'s non-linear history."],
                                cwd=clone_path, check=True, capture_output=True, text=True
                            )
                            conflicts.append('reconciliation:tree_sync')
                            logging.info("Added tree-alignment commit")
                except subprocess.CalledProcessError as e:
                    logging.error(f"Tree reconciliation failed: {e.stderr if e.stderr else e}")
                    result.success = False
                    result.error_message = f"Tree reconciliation failed: {e}"
                    return result

            repo = Repo(clone_path)
            result.repo_object = repo
            desired_ref_actual = release_branch
    else:
        # ----- Normal develop-branch workflow -----
        desired_ref_actual = get_desired_ref(repo, repo_url, ref_from_dir, desired_ref)
        fetch_updates(repo, repo_url)
        checkout_reference(repo, repo_url, desired_ref_actual)

    # Record this repo's commit hash for CI include updates in downstream repos
    current_commit = repo.head.commit.hexsha
    updated_repos_commits[repo_url] = current_commit
    logging.debug(f"Recorded commit '{current_commit}' for repository '{get_repo_name(repo_url)}'")

    # Initialize and update submodules
    update_submodules(repo, repo_url)

    # Identify submodule updates
    updated_submodules, submodule_commits = identify_submodule_updates(repo, config, updated_repos_branches)
    result.submodules_updated = updated_submodules

    # Release mode: warn if rebase happened but no submodule updates were detected.
    if release_to_master and result.rebase_performed and not updated_submodules:
        if len(list(repo.submodules)) > 0:
            logging.warning("Release workflow completed rebase but found no submodule updates")
            logging.warning("This may be normal if submodules were already at correct commits")
            logging.warning("(sanity check below will catch real issues)")

    # Auto-detect CI include updates (regardless of submodule updates)
    auto_detected_yaml_updates = collect_auto_detected_yaml_updates(clone_path, config, updated_repos_commits)

    # Collect manual YAML file updates (from config) — includes standalone entries
    # that don't depend on submodule changes (e.g., value or action: remove entries)
    manual_yaml_updates = collect_yaml_updates(settings, updated_submodules, tag_name=tag, all_repo_commits=updated_repos_commits)

    # Determine if we have any updates to make
    has_submodule_updates = bool(updated_submodules)
    has_ci_include_updates = bool(auto_detected_yaml_updates)
    has_manual_yaml_updates = bool(manual_yaml_updates)
    has_any_updates = has_submodule_updates or has_ci_include_updates or has_manual_yaml_updates

    if has_any_updates:
        # In release mode we may already have a release branch from the rebase; reuse it.
        if release_to_master and result.branch_name:
            branch_name = result.branch_name
            logging.debug(f"Using existing release branch '{branch_name}' from rebase workflow")
        elif release_to_master and 'pending_release_branch' in settings:
            # 0-commit release path that now needs a branch for submodule/yaml updates.
            release_branch = settings['pending_release_branch']
            try:
                subprocess.run(['git', 'checkout', '-b', release_branch],
                               cwd=clone_path, check=True, capture_output=True, text=True)
                branch_name = release_branch
                result.branch_name = branch_name
                logging.info(f"Created release branch '{branch_name}' for submodule/YAML updates")
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to create release branch: {e}")
                branch_name = None
        else:
            # Normal flow: new feature branch
            branch_name = create_feature_branch(repo, repo_url, tag, push_enabled=False)
            result.branch_name = branch_name

        if branch_name:
            # Merge manual and auto-detected YAML updates
            updated_yaml_files = merge_yaml_updates(manual_yaml_updates, auto_detected_yaml_updates)
            result.yaml_files_updated = updated_yaml_files

            # Build complete commit lookup (submodule commits + all repo commits)
            all_commits = {**submodule_commits, **updated_repos_commits}

            # Process YAML file updates (always process in Phase 1)
            if updated_yaml_files:
                process_yaml_updates(clone_path, updated_yaml_files, all_commits)

            # Commit the changes
            commit_changes_extended(repo, repo_url, updated_submodules, updated_yaml_files)

            # Record the branch to update parent repositories (skip if already set in release mode)
            if repo_url not in updated_repos_branches:
                updated_repos_branches[repo_url] = branch_name
                logging.debug(f"Recorded updated branch '{branch_name}' for repository '{repo_url}'")

            # Update this repo's commit to the new commit after changes
            updated_repos_commits[repo_url] = repo.head.commit.hexsha
            logging.debug(f"Updated commit to '{repo.head.commit.hexsha}' for repository '{get_repo_name(repo_url)}'")

            # Only push if in Phase 2
            if push_enabled:
                push_branch(repo, repo_url, branch_name, settings, automerge, create_mr, dry_run=False,
                            release_to_master=release_to_master, tag=tag)
            else:
                logging.info(f"Phase 1: Created branch '{branch_name}' locally, not pushing yet")

    # Handle tagging
    if tag:
        # Treat a rebase as "changes" so an existing tag at the old commit is not silently kept.
        changes_made = has_any_updates or result.rebase_performed
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

    # Release-mode sanity check: verify only expected files (submodules / configured YAML) changed.
    if release_to_master and source_head:
        logging.info("Performing sanity check...")

        expected_patterns = ['.gitmodules']

        # Submodule paths from .gitmodules
        try:
            result_cmd = subprocess.run(
                ['git', 'config', '--file', '.gitmodules', '--get-regexp', 'path'],
                cwd=clone_path, capture_output=True, text=True
            )
            if result_cmd.stdout:
                for line in result_cmd.stdout.strip().split('\n'):
                    parts = line.split()
                    if parts:
                        expected_patterns.append(parts[-1])
        except Exception:
            pass

        # YAML files from config
        for yaml_update in settings.get('update_yaml', []) or []:
            filename = yaml_update.get('filename')
            if filename:
                expected_patterns.append(filename)

        is_valid, diff_info = verify_release_changes(
            clone_path, source_head, 'HEAD', expected_patterns
        )
        result.sanity_check_passed = is_valid

        if not is_valid:
            logging.warning(f"Sanity check failed for {repo_url}")
            if diff_info:
                if len(diff_info) < 1000:
                    logging.warning(diff_info)
                else:
                    logging.warning(diff_info.split('\n')[0] if diff_info else diff_info)

            if not force:
                response = input(f"\nUnexpected changes detected in {get_repo_name(repo_url)}. Continue? (y/n): ")
                if response.lower() != 'y':
                    result.success = False
                    result.error_message = "Aborted due to unexpected changes"
                    return result
            else:
                logging.warning("Continuing due to --force flag")
        else:
            logging.info("Sanity check passed")

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
    """Checkout the desired reference in the repository.

    For branches that exist on origin, reset to origin to ensure we have the latest code.
    This prevents creating tags from stale local branches that are behind origin.
    """
    try:
        repo.git.checkout(desired_ref_actual)
        logging.debug(f"Checked out '{desired_ref_actual}' in '{get_repo_name(repo_url)}'.")

        # If this is a branch that exists on origin, reset to origin to get latest code
        # This fixes a bug where local branches behind origin would result in stale tags
        try:
            origin_ref = f'origin/{desired_ref_actual}'
            repo.git.rev_parse('--verify', origin_ref)  # Check if origin ref exists
            repo.git.reset('--hard', origin_ref)
            logging.debug(f"Reset '{desired_ref_actual}' to '{origin_ref}' in '{get_repo_name(repo_url)}'.")
        except GitCommandError:
            # Not a branch on origin (could be a tag or detached HEAD), that's fine
            pass

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
        repo.git.submodule('update', '--recursive')
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
        logging.debug(f"Committed changes in '{get_repo_name(repo_url)}' on branch '{repo.active_branch}'.")
    except GitCommandError as e:
        logging.error(f"Failed to commit changes in '{get_repo_name(repo_url)}': {e}")
        sys.exit(1)

def commit_changes_extended(repo, repo_url, updated_submodules, updated_yaml_files):
    """Commit changes with a detailed message covering submodules and CI includes."""
    commit_lines = []

    # Add submodule updates to commit message
    if updated_submodules:
        commit_lines.append("Update submodules:")
        for details in updated_submodules.values():
            commit_lines.append(f" - {details['name']}: {details['ref']} ({details['commit']})")

    # Add CI include updates to commit message
    ci_include_updates = []
    for filename, edits in updated_yaml_files.items():
        for edit in edits:
            if edit.get('auto_detected'):
                # Extract project name from key_to_update
                key = edit['key_to_update']
                # key format: include[project=hive/common-ci-configuration].ref
                if 'project=' in key:
                    project = key.split('project=')[1].split(']')[0]
                    ci_include_updates.append(project)

    if ci_include_updates:
        if commit_lines:
            commit_lines.append("")  # Add blank line between sections
        commit_lines.append("Update CI includes:")
        for project in sorted(set(ci_include_updates)):
            commit_lines.append(f" - {project}")

    # Add manual YAML updates (value/remove entries) to commit message
    manual_yaml_updates = []
    for filename, edits in updated_yaml_files.items():
        for edit in edits:
            if edit.get('auto_detected'):
                continue  # Already handled above
            if edit.get('action') == 'remove':
                manual_yaml_updates.append(f" - {filename}: removed {edit['key_to_update']}")
            elif 'value' in edit:
                manual_yaml_updates.append(f" - {filename}: set {edit['key_to_update']} to '{edit['value']}'")

    if manual_yaml_updates:
        if commit_lines:
            commit_lines.append("")
        commit_lines.append("Update CI variables:")
        commit_lines.extend(manual_yaml_updates)

    if not commit_lines:
        commit_lines.append("Update dependencies")

    commit_message = "\n".join(commit_lines)
    logging.info(f"Committing changes in '{get_repo_name(repo_url)}' with message:\n{commit_message}")

    try:
        repo.index.commit(commit_message)
        try:
            branch_label = repo.active_branch.name
            logging.debug(f"Committed changes in '{get_repo_name(repo_url)}' on branch '{branch_label}'.")
        except TypeError:
            # active_branch raises TypeError when HEAD is detached
            logging.debug(f"Committed changes in '{get_repo_name(repo_url)}' (detached HEAD).")
    except GitCommandError as e:
        logging.error(f"Failed to commit changes in '{get_repo_name(repo_url)}': {e}")
        sys.exit(1)

def push_branch(repo, repo_url, branch_name, settings, automerge, create_mr, dry_run,
                release_to_master=False, tag=None):
    """Push the feature branch to the remote repository and create a merge request if applicable."""
    if dry_run:
        logging.info("Dry run enabled. Push actions skipped.")
        return

    try:
        if create_mr:
            # Determine the target branch: use 'target_branch' if specified, else default to 'ref', else 'main'
            target_branch = settings.get('target_branch', settings.get('ref', 'main'))

            # Generate an MR title that reflects what this push is for
            if release_to_master and tag:
                mr_title = f"Release {tag}"
            elif release_to_master:
                mr_title = f"Merge changes to {target_branch}"
            elif tag:
                mr_title = f"Update submodules for {tag}"
            else:
                mr_title = "Update submodules"

            push_options = create_merge_request(repo, branch_name, target_branch,
                                                automerge=automerge, mr_title=mr_title)
            logging.debug(f"Pushing branch '{branch_name}' with push options: {push_options}")
            # Pass push_options as a single string within a list
            repo.remotes.origin.push(refspec=f"{branch_name}:{branch_name}", push_option=push_options, recurse_submodules='no')
            logging.info(f"Pushed branch '{branch_name}' to '{repo_url}' with MR title '{mr_title}' targeting '{target_branch}'.")
        else:
            repo.remotes.origin.push(refspec=f"{branch_name}:{branch_name}", recurse_submodules='no')
            logging.info(f"Pushed branch '{branch_name}' to '{repo_url}'.")
    except GitCommandError as e:
        logging.error(f"Failed to push branch '{branch_name}' to '{repo_url}': {e}")
        sys.exit(1)

def collect_yaml_updates(current_repo_settings, updated_submodules, tag_name=None, all_repo_commits=None):
    """Collect YAML file updates based on submodule updates and standalone entries.

    Handles four types of entries:
    - submodule_referenced: set key to the commit hash of the referenced repo.
      Matches against both local submodule updates and all tracked repo commits
      (so it works even when the referenced repo isn't a submodule).
      Use 'short_sha: true' to truncate the commit to 8 characters.
    - value: standalone entry with a literal value ($TAG is substituted with tag_name)
    - action: remove: standalone entry that removes a YAML key

    Entries with 'when: tag' are skipped if tag_name is None.
    """
    if all_repo_commits is None:
        all_repo_commits = {}
    updated_yaml_files = {}
    update_yaml_entries = current_repo_settings.get('update_yaml', [])
    for edit in update_yaml_entries:
        # Check 'when' condition
        if edit.get('when') == 'tag' and not tag_name:
            continue

        filename = edit['filename']
        key_to_update = edit['key_to_update']
        entry = {
            'filename': filename,
            'key_to_update': key_to_update,
        }

        if edit.get('action') == 'remove':
            # Standalone remove entry — always include (no submodule dependency)
            entry['action'] = 'remove'
        elif 'value' in edit:
            # Standalone value entry — substitute $TAG and include
            value = edit['value']
            if tag_name:
                value = value.replace('$TAG', tag_name)
            entry['value'] = value
        elif 'submodule_referenced' in edit:
            # Repo-dependent entry — include if the referenced repo is tracked
            # (as a submodule or as any repo in the config that has been processed)
            submodule_referenced_url = edit['submodule_referenced']
            if submodule_referenced_url not in updated_submodules and submodule_referenced_url not in all_repo_commits:
                continue
            entry['submodule_referenced'] = submodule_referenced_url
            if edit.get('short_sha'):
                entry['short_sha'] = True
        else:
            continue

        if filename not in updated_yaml_files:
            updated_yaml_files[filename] = []
        updated_yaml_files[filename].append(entry)
    return updated_yaml_files

def merge_yaml_updates(manual_updates, auto_detected_updates):
    """
    Merge manual (from config) and auto-detected YAML updates.

    Auto-detected updates take precedence over manual ones for the same key,
    since they reflect the actual CI file structure.
    """
    merged = {}

    # Start with manual updates
    for filename, edits in manual_updates.items():
        if filename not in merged:
            merged[filename] = []
        merged[filename].extend(edits)

    # Add auto-detected updates, avoiding duplicates
    for filename, edits in auto_detected_updates.items():
        if filename not in merged:
            merged[filename] = []

        existing_keys = {edit['key_to_update'] for edit in merged[filename]}
        for edit in edits:
            if edit['key_to_update'] not in existing_keys:
                merged[filename].append(edit)
                logging.debug(f"Added auto-detected update: {filename} -> {edit['key_to_update']}")
            else:
                logging.debug(f"Skipping duplicate key: {filename} -> {edit['key_to_update']}")

    return merged

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

    If the tag already exists and no changes were made (no submodule updates and no
    release-mode rebase), the existing tag is already correct — leave it in place.
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
            logging.info(f"Tag '{tag}' already exists in '{get_repo_name(repo_url)}' and no changes were made — leaving tag in place.")
            return
        else:
            logging.error(f"Tag '{tag}' already exists in '{repo_url}' but changes were made. Use '--retag' to overwrite.")
            sys.exit(1)

    # Create the tag locally
    try:
        repo.create_tag(tag, message=f"Release {tag}", force=retag)
        logging.debug(f"Created tag '{tag}' locally in '{get_repo_name(repo_url)}' at commit '{repo.head.commit.hexsha}'.")
    except GitCommandError as e:
        logging.error(f"Failed to create tag '{tag}' in '{repo_url}': {e}")
        sys.exit(1)

def _check_push_result(push_info_list, repo_url, ref_name):
    """Check PushInfoList for errors. Raises GitCommandError if push was rejected."""
    for info in push_info_list:
        if info.flags & (PushInfo.ERROR | PushInfo.REJECTED | PushInfo.REMOTE_REJECTED | PushInfo.REMOTE_FAILURE):
            summary = info.summary if hasattr(info, 'summary') else 'push rejected'
            raise GitCommandError(f"push {ref_name}", 2, stderr=summary)

def push_tag(repo, repo_url, tag):
    """Push a tag to remote (Phase 2 operation)."""
    if not tag:
        return

    # Check if remote tag needs to be deleted first (for retag)
    try:
        # Try to push the tag (--no-recurse-submodules avoids pushing into submodule checkouts)
        result = repo.remotes.origin.push(tag, recurse_submodules='no')
        _check_push_result(result, repo_url, tag)
        logging.info(f"Pushed tag '{tag}' to '{repo_url}'.")
    except GitCommandError as e:
        # If push fails, it might be because the tag already exists on remote
        if "already exists" in str(e) or "cannot lock ref" in str(e) or "rejected" in str(e).lower():
            logging.info(f"Tag '{tag}' already exists on remote, attempting to delete and repush...")

            # Try to delete remote tag via Git first
            try:
                delete_result = repo.remotes.origin.push(refspec=f":refs/tags/{tag}", recurse_submodules='no')
                _check_push_result(delete_result, repo_url, f":refs/tags/{tag}")
                logging.debug(f"Deleted remote tag '{tag}' via Git")

                # Now try to push again
                result = repo.remotes.origin.push(tag, recurse_submodules='no')
                _check_push_result(result, repo_url, tag)
                logging.info(f"Pushed tag '{tag}' to '{repo_url}' after deleting existing remote tag.")
            except GitCommandError as delete_error:
                # If Git push fails (likely protected tag), try API
                logging.info(f"Failed to delete tag via Git, trying GitLab API: {delete_error}")
                if delete_gitlab_tag_via_api(repo_url, tag):
                    # Try to push again after API deletion
                    try:
                        result = repo.remotes.origin.push(tag, recurse_submodules='no')
                        _check_push_result(result, repo_url, tag)
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
                if edit.get('action') == 'remove':
                    commit_lines.append(f" - {filename}: {edit['key_to_update']} removed")
                elif 'value' in edit:
                    commit_lines.append(f" - {filename}: {edit['key_to_update']} set to '{edit['value']}'")
                elif 'submodule_referenced' in edit:
                    submodule_name = get_repo_name(edit['submodule_referenced'])
                    commit_lines.append(f" - {filename}: {edit['key_to_update']} updated for submodule '{submodule_name}'")

    commit_message = "\n".join(commit_lines)
    logging.info(f"Commit summary for '{get_repo_name(repo_url)}':\n{commit_message}")

    if not push_enabled:
        logging.info("Phase 1: Local changes committed, will push in Phase 2")
    else:
        logging.info("Phase 2: Changes pushed to remote")

def _traverse_yaml_key_path(data, key_to_update, yaml_path):
    """Traverse a dotted key path in YAML data, returning (parent_dict, last_key).

    Supports conditional list matching, e.g., 'include[project=C].ref'.
    Creates intermediate dicts as needed for set operations.
    Raises KeyError if path cannot be traversed.
    """
    keys = key_to_update.split('.')
    current = data
    for key in keys[:-1]:
        if '[' in key and ']' in key:
            list_key, condition = key.split('[', 1)
            condition = condition.rstrip(']')
            field, value = condition.split('=', 1)
            if list_key not in current or not isinstance(current[list_key], list):
                logging.error(f"Key '{list_key}' is not a list in YAML file '{yaml_path}'.")
                raise KeyError
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
    return current, keys[-1]

def update_yaml_files(clone_path, update_yaml_entries, submodule_commits):
    """Update specified YAML files with new values, literal values, or remove keys.

    Each entry can be one of:
    - submodule_referenced: set key to the commit hash from submodule_commits
    - value: set key to a literal value (already substituted by collect_yaml_updates)
    - action: remove: delete the key from the YAML file
    """
    yaml_obj = YAML()
    yaml_obj.preserve_quotes = True  # Preserve existing quotes

    for edit in update_yaml_entries:
        filename = edit['filename']
        key_to_update = edit['key_to_update']
        action = edit.get('action')

        # Determine the new value based on entry type
        if action == 'remove':
            new_value = None  # sentinel for removal
        elif 'value' in edit:
            new_value = edit['value']
        elif 'submodule_referenced' in edit:
            submodule_referenced = edit['submodule_referenced']
            new_value = submodule_commits.get(submodule_referenced)
            if not new_value:
                logging.error(f"No commit hash found for '{submodule_referenced}'. Cannot update YAML file '{filename}'.")
                continue
            if edit.get('short_sha'):
                new_value = new_value[:8]
        else:
            logging.error(f"update_yaml entry for '{filename}' has no value source. Skipping.")
            continue

        # Construct full path to the YAML file
        yaml_path = os.path.join(clone_path, filename)
        if not os.path.exists(yaml_path):
            logging.error(f"YAML file '{yaml_path}' does not exist.")
            continue

        if action == 'remove':
            logging.info(f"Removing key '{key_to_update}' from YAML file '{yaml_path}'.")
        else:
            logging.info(f"Updating YAML file '{yaml_path}' at key '{key_to_update}' with value '{new_value}'.")

        # Load the YAML file
        try:
            with open(yaml_path, 'r') as f:
                data = yaml_obj.load(f)
        except Exception as e:
            logging.error(f"Failed to load YAML file '{yaml_path}': {e}")
            continue

        # Traverse the key path
        try:
            current, last_key = _traverse_yaml_key_path(data, key_to_update, yaml_path)
        except KeyError:
            logging.error(f"Key path '{key_to_update}' does not exist in YAML file '{yaml_path}'.")
            continue

        if action == 'remove':
            if last_key in current:
                old_value = current[last_key]
                del current[last_key]
                logging.debug(f"Removed '{key_to_update}' (was '{old_value}').")
            else:
                logging.debug(f"Key '{last_key}' not found in '{key_to_update}', nothing to remove.")
                continue
        else:
            old_value = current.get(last_key, None)
            current[last_key] = new_value
            logging.debug(f"Updated '{key_to_update}' from '{old_value}' to '{new_value}'.")

        # Save the YAML file
        try:
            with open(yaml_path, 'w') as f:
                yaml_obj.dump(data, f)
            logging.debug(f"Saved updated YAML file '{yaml_path}'.")
        except Exception as e:
            logging.error(f"Failed to save updated YAML file '{yaml_path}': {e}")

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
                            repo.remotes.origin.push(refspec=f":{ref.remote_head}", recurse_submodules='no')
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
                        repo.remotes.origin.push(refspec=f":refs/tags/{tag}", recurse_submodules='no')
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

def compute_dependency_waves(dependency_graph, sorted_repos):
    """
    Group repos into waves based on dependency depth.
    Wave 0 = leaf repos (no in-config dependencies)
    Wave N = repos that depend only on repos in waves 0 to N-1
    """
    waves = []
    assigned = set()
    remaining = set(sorted_repos)

    while remaining:
        # Find repos whose dependencies are all assigned to previous waves
        current_wave = []
        for repo_url in remaining:
            deps = dependency_graph.get(repo_url, [])
            if all(dep in assigned for dep in deps):
                current_wave.append(repo_url)

        if not current_wave:
            # Cycle or missing dependency (shouldn't happen after topo sort)
            logging.error("Cannot compute waves - circular dependency?")
            break

        waves.append(current_wave)
        assigned.update(current_wave)
        remaining -= set(current_wave)

    return waves


def verify_tag_exists(repo_url, tag, max_retries=3, retry_delay=2):
    """
    Verify a tag exists on GitLab via API.
    Retries a few times in case of propagation delay.
    """
    token, api_url = get_gitlab_credentials()
    if not token:
        logging.warning("No GitLab token - skipping tag verification")
        return True  # Assume success if we can't verify

    project_id = get_gitlab_project_id(repo_url)
    if not project_id:
        return True  # Assume success

    # URL-encode the tag name for the API
    import urllib.parse
    encoded_tag = urllib.parse.quote(tag, safe='')
    url = f"{api_url}/projects/{project_id}/repository/tags/{encoded_tag}"
    headers = {"PRIVATE-TOKEN": token}

    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                logging.debug(f"Verified tag '{tag}' exists on '{get_repo_name(repo_url)}'")
                return True
            elif response.status_code == 404:
                if attempt < max_retries - 1:
                    logging.debug(f"Tag '{tag}' not found yet, retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                continue
        except Exception as e:
            logging.warning(f"Error verifying tag: {e}")

    logging.warning(f"Could not verify tag '{tag}' on '{get_repo_name(repo_url)}' after {max_retries} attempts")
    return False


def push_tags_in_waves(operations, dependency_graph, sorted_repos):
    """
    Push tags in dependency waves, verifying each wave before proceeding.
    This ensures parent repo tags are indexed before dependent repos' CI runs.
    """
    waves = compute_dependency_waves(dependency_graph, sorted_repos)

    # Count how many waves have tags
    waves_with_tags = sum(1 for wave in waves if any(
        r in operations and operations[r].tag_to_create for r in wave))

    if waves_with_tags == 0:
        logging.debug("No tags to push")
        return

    logging.info(f"Pushing tags in {waves_with_tags} wave(s)")

    for wave_num, wave_repos in enumerate(waves):
        wave_tags = [(r, operations[r].tag_to_create)
                     for r in wave_repos
                     if r in operations and operations[r].tag_to_create]

        if not wave_tags:
            continue

        wave_repo_names = [get_repo_name(r) for r, _ in wave_tags]
        logging.info(f"Wave {wave_num + 1}: Pushing tags for {wave_repo_names}")

        # Push all tags in this wave
        for repo_url, tag in wave_tags:
            push_tag(operations[repo_url].repo_object, repo_url, tag)

        # Verify tags are indexed before next wave (skip for last wave)
        if wave_num < len(waves) - 1:
            # Check if there are more waves with tags
            remaining_waves_have_tags = any(
                any(r in operations and operations[r].tag_to_create for r in waves[i])
                for i in range(wave_num + 1, len(waves))
            )
            if remaining_waves_have_tags:
                logging.info(f"Verifying wave {wave_num + 1} tags are indexed...")
                for repo_url, tag in wave_tags:
                    verify_tag_exists(repo_url, tag)


def push_all_operations(operations, dependency_graph, sorted_repos,
                        no_push_tags=False, release_to_master=False, tag=None):
    """Phase 2: Push all operations to remote in topological order."""
    logging.info("=" * 60)
    logging.info("PHASE 2: Pushing all changes to remote repositories")
    logging.info("=" * 60)

    push_failures = []

    # Push tags first in dependency waves (ensures submodule commits are indexed
    # before downstream CI runs).  --no-push-tags skips the tag push entirely (e.g.
    # for release-validation runs where the tag is created locally only).
    if not no_push_tags:
        push_tags_in_waves(operations, dependency_graph, sorted_repos)
    else:
        logging.info("Skipping tag push (--no-push-tags specified)")

    # Then push branches and create MRs (in topological order)
    for repo_url in sorted_repos:
        if repo_url not in operations:
            continue

        operation = operations[repo_url]
        if not operation.success:
            continue

        # Skip if no branch to push (tags already handled above)
        if not operation.branch_name:
            continue

        logging.info(f"Pushing branch for '{get_repo_name(repo_url)}'...")

        try:
            repo = operation.repo_object
            settings = operation.settings

            # Push the branch and create MR
            automerge = settings.get('automerge', False)
            create_mr = settings.get('create_merge_request', True)
            push_branch(repo, repo_url, operation.branch_name, settings, automerge, create_mr, dry_run=False,
                        release_to_master=release_to_master, tag=tag)

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

    # Resolve the config path: explicit --config wins, otherwise default to
    # repos.yaml.master in release-to-master mode and repos.yaml otherwise.
    if args.config:
        config_path = args.config
    elif args.release_to_master:
        config_path = 'repos.yaml.master'
    else:
        config_path = CONFIG_FILE
    logging.info(f"Using configuration file: {config_path}")

    # Load and validate configuration
    config = load_config(config_path)
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
    
    # Initialize mappings to track repository state
    updated_repos_branches = {}  # Maps repo URLs to their feature branch names
    updated_repos_commits = {}   # Maps repo URLs to their commit hashes (for CI include updates)
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
            updated_repos_commits=updated_repos_commits,
            release_to_master=args.release_to_master,
            force=args.force,
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
                            for filename, edits in operation.yaml_files_updated.items():
                                for edit in edits:
                                    if edit.get('auto_detected'):
                                        # Extract project from key
                                        key = edit['key_to_update']
                                        project = key.split('project=')[1].split(']')[0] if 'project=' in key else 'unknown'
                                        logging.info(f"      - Auto-detected CI include: {project} in {filename}")
                                    elif edit.get('action') == 'remove':
                                        logging.info(f"      - Remove YAML key: {filename} ({edit['key_to_update']})")
                                    elif 'value' in edit:
                                        logging.info(f"      - Set YAML key: {filename} ({edit['key_to_update']} = '{edit['value']}')")
                                    else:
                                        logging.info(f"      - Modified YAML: {filename} ({edit['key_to_update']})")

                # Tag info
                if operation.tag_to_create:
                    logging.info(f"  - Would push tag: {operation.tag_to_create} (already created locally)")
        sys.exit(0)
    
    # ========================================================================
    # PHASE 2: Push to Remote (in topological order)
    # ========================================================================
    success = push_all_operations(operations, dependency_graph, sorted_repos,
                                  no_push_tags=args.no_push_tags,
                                  release_to_master=args.release_to_master,
                                  tag=args.tag)
    
    if success:
        logging.info("=" * 60)
        logging.info("Repository update process completed successfully!")
        logging.info("=" * 60)
    else:
        logging.error("Some operations failed. See errors above.")
        sys.exit(1)

if __name__ == "__main__":
    main()
