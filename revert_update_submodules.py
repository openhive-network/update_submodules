import os
import subprocess
import sys
from typing import Set
import requests
import configparser

# Read configuration
config = configparser.ConfigParser()
config.read('config.ini')

GITLAB_API_URL = config.get('gitlab', 'api_url', fallback="https://gitlab.syncad.com/api/v4")
# Prefer env var, fall back to config file
GITLAB_TOKEN = os.environ.get('GITLAB_TOKEN') or config.get('gitlab', 'token', fallback=None)
if not GITLAB_TOKEN:
    print("Error: GITLAB_TOKEN environment variable or config.ini gitlab.token required")
    sys.exit(1)

def run_git_command(command, cwd=None, hide_output=False):
    print(f"Running command: {' '.join(command)} in directory: {cwd}")
    if hide_output:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Command failed: {' '.join(command)} in directory: {cwd}")
            print(result.stderr)
            sys.exit(1)
    else:
        result = subprocess.run(command, cwd=cwd)
        if result.returncode != 0:
            print(f"Command failed: {' '.join(command)} in directory: {cwd}")
            sys.exit(1)

def get_checked_out_repos(directory_name: str) -> Set[str]:
    repos = set()
    for item in os.listdir(directory_name):
        item_path = os.path.join(directory_name, item)
        if os.path.isdir(item_path) and '.git' in os.listdir(item_path):
            repos.add(item)
    return repos

def delete_gitlab_tag(project_id: str, tag: str) -> None:
    url = f"{GITLAB_API_URL}/projects/{project_id}/repository/tags/{tag}"
    headers = {"PRIVATE-TOKEN": GITLAB_TOKEN}
    print(f"Making API call: DELETE {url}")
    response = requests.delete(url, headers=headers)
    if response.status_code == 204:
        print(f"Deleted tag {tag} in project {project_id}")
    else:
        print(f"Failed to delete tag {tag} in project {project_id}: {response.status_code} {response.text}")

def get_gitlab_project_id(repo_path: str) -> str:
    result = subprocess.run(['git', 'config', '--get', 'remote.origin.url'], cwd=repo_path, capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError(f"Could not get remote URL for repo {repo_path}")
    remote_url = result.stdout.strip()
    print(f"Remote URL: {remote_url}")  # Debug output
    
    if remote_url.startswith('http'):
        project_name = remote_url.split('/')[-1].replace('.git', '')
        namespace = remote_url.split('/')[-2]
    elif remote_url.startswith('git@'):
        parts = remote_url.split(':')[1].lstrip('/').split('/')
        namespace = parts[0]
        project_name = parts[1].replace('.git', '')
    else:
        raise ValueError(f"Unsupported remote URL format: {remote_url}")
    
    print(f"Namespace: {namespace}, Project Name: {project_name}")  # Debug output
    
    url = f"{GITLAB_API_URL}/projects/{namespace}%2F{project_name}"
    headers = {"PRIVATE-TOKEN": GITLAB_TOKEN}
    print(f"Making API call: GET {url}")
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        project_id = response.json()['id']
        return project_id
    else:
        raise ValueError(f"Could not get project ID for {namespace}/{project_name}: {response.status_code} {response.text}")

def delete_updated_submodule_branches_and_tag(directory_name: str, tag: str = None) -> None:
    checked_out_repos = get_checked_out_repos(directory_name)
    print(f"Checked out repos: {checked_out_repos}")

    for repo in checked_out_repos:
        repo_path = os.path.join(directory_name, repo)
        if not os.path.exists(repo_path):
            print(f"Skipping {repo}, no repo directory found")
            continue

        print(f"PROCESSING {repo_path}")

        # Fetch all branches and tags
        run_git_command(['git', 'fetch', '--all'], cwd=repo_path)

        # Get list of branches
        result = subprocess.run(['git', 'branch', '-r'], cwd=repo_path, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Failed to list branches in {repo_path}")
            sys.exit(1)

        branches = result.stdout.splitlines()
        for branch in branches:
            branch = branch.strip()
            if 'origin/update-submodules-py' in branch:
                branch_name = branch.replace('origin/', '')
                print(f"Deleting remote branch {branch_name} in {repo_path}")
                run_git_command(['git', 'push', 'origin', '--delete', branch_name], cwd=repo_path)

        # Get list of local branches
        result = subprocess.run(['git', 'branch'], cwd=repo_path, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Failed to list local branches in {repo_path}")
            sys.exit(1)

        local_branches = result.stdout.splitlines()
        for branch in local_branches:
            branch = branch.strip()
            if branch == 'update-submodules-py':
                print(f"Deleting local branch {branch} in {repo_path}")
                run_git_command(['git', 'branch', '-d', branch], cwd=repo_path)

        # Delete the specified tag if it exists
        if tag:
            # Check if local tag exists
            result = subprocess.run(['git', 'tag', '-l', tag], cwd=repo_path, capture_output=True, text=True)
            if result.returncode == 0 and tag in result.stdout:
                # Delete local tag
                run_git_command(['git', 'tag', '-d', tag], cwd=repo_path)
            else:
                print(f"Tag {tag} does not exist locally in {repo_path}")

            result = subprocess.run(['git', 'ls-remote', '--tags', 'origin', tag], cwd=repo_path, capture_output=True, text=True)
            if result.returncode == 0 and tag in result.stdout:
                project_id = get_gitlab_project_id(repo_path)
                delete_gitlab_tag(project_id, tag)
            else:
                print(f"Tag {tag} does not exist in the remote repository")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python delete-updated-submodule-branches.py <directory_name> [tag]")
        sys.exit(1)
    
    directory_name = sys.argv[1]
    tag = sys.argv[2] if len(sys.argv) > 2 else None
    delete_updated_submodule_branches_and_tag(directory_name, tag)
