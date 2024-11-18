import os
import subprocess
import sys
from collections import defaultdict, deque
from typing import Dict, Set

from ruamel.yaml import YAML
yaml = YAML()
yaml.preserve_quotes = True
yaml.line_break = '\n'  # Set line break to LF

def get_submodule_dependencies(directory_name: str, checked_out_repos: Set[str]) -> Dict[str, Set[str]]:
    dependencies = defaultdict(set)
    for repo in checked_out_repos:
        gitmodules_path = os.path.join(directory_name, repo, '.gitmodules')
        if not os.path.exists(gitmodules_path):
            print(f"No .gitmodules file found in {repo}")
            continue

        with open(gitmodules_path, 'r') as file:
            content = file.read()
        
        submodule_path = None
        for line in content.splitlines():
            if line.strip().startswith('path ='):
                submodule_path = line.split('=')[1].strip()
            if line.startswith('[submodule'):
                submodule_name = line.split('"')[1]
            elif line.strip().startswith('url ='):
                submodule_url = line.split('=')[1].strip()
                submodule_url = os.path.normpath(submodule_url).replace('.git', '')
                if submodule_url.startswith('..\\'):
                    submodule_url = submodule_url[3:]
                print(f"Checking submodule URL: {submodule_url} against checked out repos")
                if submodule_url in checked_out_repos:
                    print(f"Submodule {submodule_path} found in checked out repos")
                    dependencies[repo].add(submodule_path)
                    if submodule_path not in dependencies:
                        dependencies[submodule_path] = set()  # Ensure all submodules are in the dependencies dictionary
                else:
                    # Check if the submodule is nested within another repo
                    for checked_out_repo in checked_out_repos:
                        if submodule_url.startswith(checked_out_repo):
                            print(f"Submodule {submodule_path} found nested within {checked_out_repo}")
                            dependencies[repo].add(submodule_path)
                            if submodule_path not in dependencies:
                                dependencies[submodule_path] = set()
                            break
    return dependencies

def topological_sort(dependencies: Dict[str, Set[str]]) -> list:
    in_degree = {u: 0 for u in dependencies}
    for u in dependencies:
        for v in dependencies[u]:
            if v not in in_degree:
                in_degree[v] = 0
            in_degree[v] += 1

    queue = deque([u for u in in_degree if in_degree[u] == 0])
    sorted_list = []

    while queue:
        u = queue.popleft()
        sorted_list.append(u)
        for v in dependencies[u]:
            in_degree[v] -= 1
            if in_degree[v] == 0:
                queue.append(v)

    if len(sorted_list) == len(in_degree):
        return sorted_list[::-1]  # Reverse the order
    else:
        raise ValueError("A cycle was detected in the dependencies")

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

def get_revision(repo_path: str) -> str:
    result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    else:
        raise ValueError(f"Could not determine the revision for directory {repo_path}")

def get_current_branch(repo_path: str) -> str:
    result = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    else:
        raise ValueError(f"Could not determine the current branch for directory {repo_path}")

def update_submodule(submodule_path: str) -> None:
    run_git_command(['git', 'submodule', 'update', '--init', '--recursive'], cwd=submodule_path)

def get_submodule_url(repo_path: str, submodule_path: str) -> str:
    print(f"Getting submodule URL for {submodule_path} in {repo_path}")
    result = subprocess.run(['git', 'config', '--file', '.gitmodules', '--get-regexp', f'submodule.*.path'], cwd=repo_path, capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError(f"Could not get submodule URL for {submodule_path} in {repo_path}")
    print(f"Found .gitmodules file: {result.stdout}")
    submodule_urls = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        submodule_name = parts[0].split('.')[1]
        submodule_urls[submodule_name] = parts[1]
    
    print(f"Submodule paths found: {submodule_urls}")
    
    for submodule_name, path in submodule_urls.items():
        if path == submodule_path:
            result = subprocess.run(['git', 'config', '--file', '.gitmodules', '--get', f'submodule.{submodule_name}.url'], cwd=repo_path, capture_output=True, text=True)
            if result.returncode == 0:
                submodule_url = result.stdout.strip()
                print(f"Found URL for submodule {submodule_name}: {submodule_url}")
                return submodule_url
            else:
                raise ValueError(f"Could not get URL for submodule {submodule_name} in {repo_path}")
    
    raise ValueError(f"Submodule path {submodule_path} not found in {repo_path}")

def update_submodules(directory_name: str, tag: str = None) -> None:
    print(f"Updating repos in {directory_name}")
    checked_out_repos = get_checked_out_repos(directory_name)
    print(f"Checked out repos: {checked_out_repos}")
    dependencies = get_submodule_dependencies(directory_name, checked_out_repos)
    print(f"Dependencies found: {dependencies}")
    sorted_dirs = topological_sort(dependencies)
    print(f"Directories to update in order: {sorted_dirs}")

    for dir_name in sorted_dirs:
        repo_path = os.path.join(directory_name, dir_name)
        if not os.path.exists(repo_path):
            print(f"Skipping {dir_name}, no repo directory found")
            continue

        print(f"PROCESSING {repo_path}")
        changed = False

        for submodule in dependencies[dir_name]:
            submodule_path = os.path.join(repo_path, submodule)
            if not os.path.exists(submodule_path):
                print(f"Warning: Submodule directory {submodule_path} does not exist. Ignoring {submodule}.")
                continue

            update_submodule(submodule_path)

            submodule_url = get_submodule_url(repo_path, submodule)
            latest_revision = get_revision(os.path.join(directory_name, os.path.splitext(submodule_url.split('/')[-1])[0]))
            
            current_revision = get_revision(submodule_path)
            if current_revision != latest_revision:
                run_git_command(['git', 'fetch'], cwd=submodule_path, hide_output=True)
                run_git_command(['git', 'checkout', latest_revision], cwd=submodule_path)
                changed = True
            else:
                print(f"{submodule} is already at {latest_revision}")

        if changed:
            run_git_command(['git', 'checkout', '-b', 'update-submodules-py'], cwd=repo_path)
            if tag:
                run_git_command(['git', 'commit', '-am', f'Update submodules to {tag}'], cwd=repo_path)
            else:
                run_git_command(['git', 'commit', '-am', f'Update_submodules.py'], cwd=repo_path)
            new_hash = get_revision(repo_path)
            print(f"Committed changes in {dir_name} with new hash {new_hash}")
            run_git_command(['git', 'push', '--set-upstream', 'origin', 'update-submodules-py'], cwd=repo_path, hide_output=True)
            run_git_command(['git', 'checkout', '--detach', 'HEAD'], cwd=repo_path)
            run_git_command(['git', 'branch', '-d', 'update-submodules-py'], cwd=repo_path)

        if tag:
            run_git_command(['git', 'tag', tag], cwd=repo_path)
            run_git_command(['git', 'push', 'origin', tag], cwd=repo_path)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python update_submodules.py <directory_name> [tag]")
        sys.exit(1)
    
    directory_name = sys.argv[1]
    tag = sys.argv[2] if len(sys.argv) > 2 else None
    update_submodules(directory_name, tag)