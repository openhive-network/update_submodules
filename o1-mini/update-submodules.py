#!/usr/bin/env python3

import os
import sys
import yaml
import git
import argparse
import logging
from collections import defaultdict
from ruamel.yaml import YAML
from git import Repo, GitCommandError

# Configuration
CONFIG_FILE = 'repos.yaml'
BASE_DIR = 'repositories'  # Directory to clone repositories into

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
            logging.StreamHandler(sys.stdout)
        ]
    )

def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Update Git repositories and their submodules.")
    parser.add_argument('--dry-run', '-d', action='store_true', help='Perform a dry run without committing or pushing changes.')
    parser.add_argument('--log-level', '-l', default='INFO', help='Set the logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL). Default is INFO.')
    parser.add_argument('--tag', '-t', type=str, help='Name of the Git tag to create for each repository.')
    parser.add_argument('--retag', '-r', action='store_true', help='Overwrite existing tags with the same name when using --tag.')
    return parser.parse_args()

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

def clone_repo(repo_url, clone_path):
    """Clone the repository if not already cloned."""
    if os.path.exists(clone_path):
        logging.info(f"Repository '{repo_url}' already cloned at '{clone_path}'.")
        try:
            repo = Repo(clone_path)
            if repo.bare:
                logging.error(f"Repository at '{clone_path}' is bare. Expected a non-bare repository.")
                sys.exit(1)
            return repo
        except git.exc.InvalidGitRepositoryError:
            logging.error(f"Directory '{clone_path}' is not a valid Git repository.")
            sys.exit(1)
    else:
        logging.info(f"Cloning repository '{repo_url}' into '{clone_path}'...")
        try:
            repo = Repo.clone_from(repo_url, clone_path)
            logging.debug(f"Cloned '{repo_url}' successfully.")
            return repo
        except GitCommandError as e:
            logging.error(f"Failed to clone repository '{repo_url}': {e}")
            sys.exit(1)

def parse_submodules(repo):
    """Parse submodules from a repository."""
    submodules = []
    gitmodules_path = os.path.join(repo.working_tree_dir, '.gitmodules')
    if os.path.exists(gitmodules_path):
        for submodule in repo.submodules:
            submodules.append(submodule.url)
    return submodules

def build_dependency_graph(config):
    """Build a dependency graph based on submodules."""
    graph = defaultdict(list)
    repo_urls = set(config.keys())

    for repo_url in repo_urls:
        clone_path = os.path.join(BASE_DIR, get_repo_name(repo_url))
        repo = clone_repo(repo_url, clone_path)
        submodules = parse_submodules(repo)
        # Filter submodules to include only those in config
        filtered_submodules = [s for s in submodules if s in repo_urls]
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
    """Extract the short repository name from the URL."""
    return os.path.basename(repo_url.rstrip('.git'))

def get_submodule_current_commit(repo, submodule):
    """Get the current commit hash of the submodule as recorded in the parent repo."""
    return submodule.hexsha

def get_submodule_desired_commit(submodule_repo, desired_ref):
    """Get the commit hash of the desired reference in the submodule."""
    try:
        submodule_repo.git.fetch()
        # Check if ref exists as a local branch
        if is_branch(desired_ref, submodule_repo):
            submodule_repo.git.checkout(desired_ref)
            submodule_repo.remotes.origin.pull()
            desired_commit = submodule_repo.head.commit.hexsha
            return desired_commit
        # Check if ref exists as a remote branch
        try:
            remote_branch = f"origin/{desired_ref}"
            submodule_repo.git.checkout('-b', desired_ref, remote_branch)
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

def is_branch(ref, repo):
    """Check if the reference is a branch in the repository."""
    try:
        repo.git.rev_parse('--verify', f'refs/heads/{ref}')
        return True
    except GitCommandError:
        return False

def validate_refs(config):
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
            try:
                # Fetch all refs
                repo.remotes.origin.fetch()
                # Check if ref exists as branch, tag, or commit
                if is_branch(desired_ref, repo) or desired_ref in [tag.name for tag in repo.tags]:
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
        if any(repo_url for repo_url, _ in invalid_refs):
            logging.error("If you intend to overwrite existing tags, you can re-run the script with the '--retag' option.")
        sys.exit(1)
    else:
        logging.info("All refs in the configuration are valid.")

def generate_branch_name(repo_url, tag=None, counter=None):
    """Generate a unique branch name."""
    base_name = "update-submodules"
    if tag:
        base_name += f"-for-{tag}"
    if counter:
        base_name += f"-{counter}"
    return base_name

def create_merge_request(repo, source_branch, target_branch, automerge=False):
    """Create a merge request using GitLab push options."""
    # GitLab specific push options
    push_options = [
        'merge_request.create=true',
        f'merge_request.target={target_branch}',
        'merge_request.remove_source_branch=true'
    ]
    if automerge:
        push_options.append('merge_request.merge_when_pipeline_succeeds=true')
    # Join the options with commas
    push_options_str = ','.join(push_options)
    return push_options_str

def update_yaml_files(clone_path, update_yaml_entries, submodule_commits, dry_run=False):
    """Update specified YAML files with new submodule commit hashes."""
    yaml_obj = YAML()
    yaml_obj.preserve_quotes = True  # Preserve quotes in YAML

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

        # Traverse the key path
        keys = key_to_update.split('.')
        current = data
        try:
            for key in keys[:-1]:
                current = current[key]
            last_key = keys[-1]
        except KeyError:
            logging.error(f"Key path '{key_to_update}' does not exist in YAML file '{yaml_path}'.")
            continue

        # Update the key with the new commit hash
        old_value = current.get(last_key, None)
        current[last_key] = new_commit
        logging.debug(f"Updated '{key_to_update}' from '{old_value}' to '{new_commit}'.")

        # Save the YAML file
        if dry_run:
            logging.info(f"Dry run: Would save updated YAML file '{yaml_path}'.")
            continue
        try:
            with open(yaml_path, 'w') as f:
                yaml_obj.dump(data, f)
            logging.debug(f"Saved updated YAML file '{yaml_path}'.")
        except Exception as e:
            logging.error(f"Failed to save updated YAML file '{yaml_path}': {e}")
            continue

def update_repo(repo_url, desired_ref, config, tag=None, retag=False, dry_run=False, updated_repos_branches={}):
    """Update a single repository and its submodules."""
    clone_path = os.path.join(BASE_DIR, get_repo_name(repo_url))
    repo = clone_repo(repo_url, clone_path)

    # Retrieve repository-specific settings
    settings = config[repo_url]
    ref_from_dir = settings.get('ref_from_dir')
    automerge = settings.get('automerge', False)
    create_merge_request = settings.get('create_merge_request', True)

    if ref_from_dir:
        # Use the ref from the local directory
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
        except git.exc.InvalidGitRepositoryError:
            logging.error(f"ref_from_dir path '{ref_from_dir}' for repository '{repo_url}' is not a valid Git repository.")
            sys.exit(1)
    else:
        desired_ref_actual = desired_ref

    # Fetch all remote branches/tags
    logging.info(f"Fetching updates for '{get_repo_name(repo_url)}'...")
    try:
        repo.remotes.origin.fetch()
    except GitCommandError as e:
        logging.error(f"Failed to fetch updates for '{repo_url}': {e}")
        sys.exit(1)

    # Checkout the desired reference
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

    # Ensure the desired branch is up-to-date
    if is_branch(desired_ref_actual, repo):
        logging.info(f"Pulling latest changes for branch '{desired_ref_actual}' in '{get_repo_name(repo_url)}'...")
        try:
            repo.remotes.origin.pull()
            logging.debug(f"Pulled latest changes for branch '{desired_ref_actual}' in '{get_repo_name(repo_url)}'.")
        except GitCommandError as e:
            logging.error(f"Failed to pull latest changes for '{desired_ref_actual}' in '{repo_url}': {e}")
            sys.exit(1)
    else:
        logging.debug(f"'{desired_ref_actual}' is not a branch in '{get_repo_name(repo_url)}'.")

    # Initialize and update submodules (only those in config)
    logging.info(f"Initializing and updating submodules for '{get_repo_name(repo_url)}'...")
    try:
        repo.git.submodule('init')
        repo.git.submodule('update', '--recursive')
    except GitCommandError as e:
        logging.error(f"Failed to initialize/update submodules in '{repo_url}': {e}")
        sys.exit(1)

    updated_submodules = {}
    submodule_commits = {}
    updated_yaml_files = {}  # Initialize here to avoid UnboundLocalError

    # Iterate over submodules to identify updates
    for submodule in repo.submodules:
        if submodule.url in config:
            # Determine if this submodule has been updated via a feature branch
            if submodule.url in updated_repos_branches:
                # Use the feature branch from the mapping
                desired_ref_submodule = updated_repos_branches[submodule.url]
                logging.debug(f"Submodule '{get_repo_name(submodule.url)}' is being updated via branch '{desired_ref_submodule}'.")
            else:
                # Use the configured ref or ref_from_dir
                sub_settings = config[submodule.url]
                sub_ref_from_dir = sub_settings.get('ref_from_dir')
                if sub_ref_from_dir:
                    # Use the ref from the local directory
                    local_sub_repo_path = os.path.abspath(sub_ref_from_dir)
                    try:
                        local_sub_repo = Repo(local_sub_repo_path)
                        sub_current_head = local_sub_repo.head
                        if sub_current_head.is_detached:
                            desired_ref_submodule = sub_current_head.commit.hexsha
                            logging.debug(f"Submodule '{get_repo_name(submodule.url)}' is at detached head '{desired_ref_submodule}'.")
                        else:
                            desired_ref_submodule = sub_current_head.reference.name
                            logging.debug(f"Submodule '{get_repo_name(submodule.url)}' is on branch '{desired_ref_submodule}'.")
                    except git.exc.InvalidGitRepositoryError:
                        logging.error(f"ref_from_dir path '{sub_ref_from_dir}' for submodule '{submodule.url}' is not a valid Git repository.")
                        sys.exit(1)
                else:
                    desired_ref_submodule = sub_settings.get('ref')

            # Get the desired commit hash
            desired_commit = get_submodule_desired_commit(submodule.module(), desired_ref_submodule)

            if desired_ref_submodule is None or desired_commit is None:
                logging.error(f"Failed to determine desired commit for submodule '{get_repo_name(submodule.url)}'.")
                continue

            current_commit = get_submodule_current_commit(repo, submodule)

            if current_commit != desired_commit:
                logging.info(f"Submodule '{get_repo_name(submodule.url)}' is at {current_commit}, needs to be updated to '{desired_ref_submodule}' ({desired_commit}).")
                updated_submodules[submodule.url] = {
                    'name': get_repo_name(submodule.url),
                    'ref': desired_ref_submodule,
                    'commit': desired_commit
                }
                submodule_commits[submodule.url] = desired_commit
                # Stage the submodule path to update the pointer
                repo.git.add(submodule.path)
                logging.debug(f"Staged submodule '{get_repo_name(submodule.url)}' for update.")

    # If there are submodule updates, create a single branch, commit all, and push
    if updated_submodules:
        commit_lines = []
        commit_lines.append("Update submodules:")
        for details in updated_submodules.values():
            commit_lines.append(f" - {details['name']}: {details['ref']} ({details['commit']})")
        commit_message = "\n".join(commit_lines)
        logging.info(f"Preparing to commit changes in '{get_repo_name(repo_url)}' with message:\n{commit_message}")

        if dry_run:
            logging.info("Dry run enabled. Commit and push actions skipped.")
        else:
            # Create a new branch for the commit
            branch_created = False
            branch_name = None
            counter = 1
            while not branch_created:
                if tag:
                    candidate_branch = generate_branch_name(repo_url, tag=tag, counter=counter if counter >1 else None)
                else:
                    candidate_branch = generate_branch_name(repo_url, counter=counter if counter >1 else None)
                if candidate_branch not in repo.heads:
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
                    counter +=1

            # Commit the changes
            try:
                repo.index.commit(commit_message)
                logging.debug(f"Committed changes in '{get_repo_name(repo_url)}' on branch '{branch_name}'.")
            except GitCommandError as e:
                logging.error(f"Failed to commit changes in '{get_repo_name(repo_url)}': {e}")
                sys.exit(1)

            # Determine target branch for merge request
            if is_branch(desired_ref_actual, repo):
                target_branch = desired_ref_actual
            else:
                # Use default branch
                try:
                    target_branch_ref = repo.git.symbolic_ref('refs/remotes/origin/HEAD')
                    target_branch = target_branch_ref.split('/')[-1]
                except GitCommandError:
                    target_branch = 'main'  # Fallback
                logging.debug(f"Using default branch '{target_branch}' as merge request target for '{get_repo_name(repo_url)}'.")

            # Handle merge request settings
            automerge = settings.get('automerge', False)
            create_mr = settings.get('create_merge_request', True)
            if create_mr:
                push_options = create_merge_request(repo, branch_name, target_branch, automerge=automerge)
            else:
                push_options = []

            # Push the branch with or without push options
            try:
                if create_mr and push_options:
                    repo.remotes.origin.push(refspec=f"{branch_name}:{branch_name}", push_options=[push_options])
                    logging.info(f"Pushed branch '{branch_name}' to '{repo_url}' with push options for merge request.")
                else:
                    repo.remotes.origin.push(refspec=f"{branch_name}:{branch_name}")
                    logging.info(f"Pushed branch '{branch_name}' to '{repo_url}'.")
                # Record the branch to update parent repositories
                updated_repos_branches[repo_url] = branch_name
            except GitCommandError as e:
                logging.error(f"Failed to push branch '{branch_name}' to '{repo_url}': {e}")
                sys.exit(1)

        # Handle YAML file updates
        updated_yaml_files = {}
        for submodule_url, details in updated_submodules.items():
            # Find all update_yaml entries that reference this submodule
            for parent_repo_url, parent_settings in config.items():
                update_yaml_entries = parent_settings.get('update_yaml', [])
                for edit in update_yaml_entries:
                    if edit['submodule_referenced'] == submodule_url:
                        filename = edit['filename']
                        key_to_update = edit['key_to_update']
                        if filename not in updated_yaml_files:
                            updated_yaml_files[filename] = []
                        updated_yaml_files[filename].append({
                            'filename': filename,  # Include 'filename' key
                            'key_to_update': key_to_update,
                            'submodule_referenced': submodule_url
                        })

        if updated_yaml_files:
            for filename, edits in updated_yaml_files.items():
                update_yaml_files(clone_path, edits, submodule_commits, dry_run=dry_run)
                # Stage the updated YAML file
                if not dry_run:
                    try:
                        repo.git.add(filename)
                        logging.debug(f"Staged YAML file '{filename}' for commit.")
                    except GitCommandError as e:
                        logging.error(f"Failed to stage YAML file '{filename}': {e}")
                        continue
    # Handle tagging
    if tag:
        # Validate that the tag does not already exist
        existing_tags = [t.name for t in repo.tags]
        if tag in existing_tags:
            if retag:
                if dry_run:
                    logging.info(f"Dry run: Would delete existing tag '{tag}' in '{get_repo_name(repo_url)}'.")
                else:
                    logging.info(f"Deleting existing tag '{tag}' in '{get_repo_name(repo_url)}' as '--retag' is specified.")
                    try:
                        repo.delete_tag(tag)
                        repo.remotes.origin.push(refspec=f":refs/tags/{tag}")  # Delete remote tag
                        logging.debug(f"Deleted tag '{tag}' locally and remotely in '{get_repo_name(repo_url)}'.")
                    except GitCommandError as e:
                        logging.error(f"Failed to delete existing tag '{tag}' in '{repo_url}': {e}")
                        sys.exit(1)
            else:
                logging.error(f"Tag '{tag}' already exists in '{repo_url}'. Use '--retag' to overwrite.")
                sys.exit(1)
        # Create the tag
        if dry_run:
            logging.info(f"Dry run: Would create tag '{tag}' in '{get_repo_name(repo_url)}' at commit '{repo.head.commit.hexsha}'.")
        else:
            try:
                repo.create_tag(tag, message=f"Release {tag}", force=retag)
                repo.remotes.origin.push(tag)
                logging.info(f"Created and pushed tag '{tag}' in '{get_repo_name(repo_url)}'.")
            except GitCommandError as e:
                logging.error(f"Failed to create or push tag '{tag}' in '{repo_url}': {e}")
                sys.exit(1)

    # Commit and push changes in the parent repo with detailed commit message
    if updated_submodules or updated_yaml_files:
        if updated_submodules:
            commit_lines = []
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
            logging.info(f"Preparing to commit changes in '{get_repo_name(repo_url)}' with message:\n{commit_message}")

            if dry_run:
                logging.info("Dry run enabled. Commit and push actions skipped.")
            else:
                # Already committed and pushed above
                pass
    else:
        logging.info(f"No submodule or YAML file updates to commit in '{get_repo_name(repo_url)}'.")


def main():
    """Main function to orchestrate the update process."""
    args = parse_arguments()
    setup_logging(args.log_level)
    logging.debug("Starting the repository update process.")

    if not os.path.exists(BASE_DIR):
        os.makedirs(BASE_DIR)
        logging.debug(f"Created base directory '{BASE_DIR}' for cloning repositories.")

    config = load_config(CONFIG_FILE)
    validate_config(config)
    validate_refs(config)

    dependency_graph = build_dependency_graph(config)
    sorted_repos = topological_sort(dependency_graph)
    # Reverse the sorted list to process submodules first
    sorted_repos.reverse()

    logging.info("Processing repositories in the following order:")
    for repo in sorted_repos:
        logging.info(f"- {get_repo_name(repo)}")

    # Initialize a mapping to track repositories updated via branches
    updated_repos_branches = {}

    for repo in sorted_repos:
        settings = config[repo]
        tag = args.tag
        retag = args.retag
        desired_ref = settings.get('ref') if 'ref' in settings else None
        desired_ref_from_dir = settings.get('ref_from_dir') if 'ref_from_dir' in settings else None
        logging.info(f"\nUpdating repository '{get_repo_name(repo)}' to '{desired_ref if desired_ref else 'ref_from_dir'}'...")
        update_repo(
            repo,
            desired_ref,
            config,
            tag=tag,
            retag=retag,
            dry_run=args.dry_run,
            updated_repos_branches=updated_repos_branches
        )

    logging.debug("Repository update process completed.")

if __name__ == "__main__":
    main()