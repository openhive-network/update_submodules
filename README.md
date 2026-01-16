# Submodule Management Scripts

This repository contains scripts for managing Git submodule dependencies across multiple Hive blockchain repositories. The scripts automate updating submodule references, modifying CI configuration files, and creating merge requests.

## Quick Start

### Option 1: Run with Docker (Recommended)

```bash
# Build the image
docker build -t update-submodules .

# Dry run (validates without pushing)
docker run --rm -v ~/.ssh:/ssh-keys:ro update-submodules --dry-run

# Test with a HAF feature branch
docker run --rm -v ~/.ssh:/ssh-keys:ro update-submodules --haf-branch feature/my-branch --dry-run

# Full run with tag
docker run --rm -v ~/.ssh:/ssh-keys:ro update-submodules --tag v1.28.0
```

### Option 2: Run Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Dry run
python3 update-submodules.py --dry-run

# Full run with tag
python3 update-submodules.py --tag v1.28.0
```

## Scripts

### update-submodules.py

Main script for updating submodule references across repos. It:
- Clones repositories and updates submodule references in topological order
- Modifies CI configuration files to match submodule commits
- Creates feature branches, commits, and merge requests
- Supports two-phase operation: local validation (Phase 1), then remote push (Phase 2)

**Options:**
| Option | Description |
|--------|-------------|
| `--dry-run` | Validate without pushing changes |
| `--tag <name>` | Create a Git tag in each repository |
| `--retag` | Overwrite existing tags |
| `--haf-branch <branch>` | Test all projects with a specific HAF branch |
| `--cleanup` | Clean up branches/tags from a failed run |
| `--cleanup-tag <name>` | Specific tag to clean up |
| `--log-level <level>` | Set logging level (DEBUG, INFO, WARNING, ERROR) |

### Testing with a HAF Feature Branch

The `--haf-branch` option allows testing all dependent projects with a specific HAF branch:

```bash
docker run --rm -v ~/.ssh:/ssh-keys:ro update-submodules \
    --haf-branch feature/my-haf-branch --dry-run
```

This automatically:
1. Overrides HAF's ref to the specified branch
2. Updates submodule references in all repos that have HAF as a submodule
3. Sets `UPSTREAM_OVERRIDE_TAG` in CI files for projects using dynamic HAF detection
4. Creates MRs in all affected projects with branch name `update-submodules-for-haf-<branch-name>`

**Prerequisites**: The HAF branch must have CI-built Docker images available at `registry.gitlab.syncad.com/hive/haf:<commit-sha>`

### checkout_develop_versions.py

A utility script that updates all repositories in a directory to their `develop` branch:
- Checks out the develop branch
- Pulls latest changes
- Updates all submodules recursively

```bash
python3 checkout_develop_versions.py ../src
```

### revert_update_submodules.py

A cleanup script that reverts changes made by update-submodules.py:
- Removes `update-submodules-*` branches
- Deletes specified tags both locally and on GitLab

```bash
python3 revert_update_submodules.py ../src v1.28.0
```

## Configuration

### repos.yaml

Configuration for the develop branch workflow:

```yaml
git@gitlab.syncad.com:hive/repo.git:
  ref: 'develop'           # Branch/tag/commit to checkout
  automerge: true          # Auto-merge MR when pipeline passes
  create_merge_request: true
  target_branch: develop   # MR target branch
  update_yaml:             # CI files to update (optional)
    - filename: '.gitlab-ci.yml'
      key_to_update: 'variables.UPSTREAM_OVERRIDE_TAG'
      submodule_referenced: 'git@gitlab.syncad.com:hive/haf.git'
```

### GitLab Credentials

For protected tag operations, provide credentials via environment variable or config file:

```bash
export GITLAB_TOKEN=your_token
```

Or create `config.ini`:
```ini
[gitlab]
api_url = https://gitlab.syncad.com/api/v4
token = your_gitlab_token
```

## Branch Naming

Feature branches are named based on the operation:
- Default: `update-submodules`
- With `--tag v1.28.0`: `update-submodules-for-v1.28.0`
- With `--haf-branch feature/my-branch`: `update-submodules-for-haf-feature-my-branch`

Numeric suffixes (`-2`, `-3`) are added if the name already exists.

## Architecture

The scripts build a dependency graph from submodule relationships and process repos in topological order (leaf submodules first). This ensures parent repos see updated commits from their submodules.

### Two-Phase Operation

1. **Phase 1 (Local)**: Clone repos, create branches, update submodules, modify YAML files, create commits and tags locally
2. **Phase 2 (Push)**: Push all changes to remotes in topological order

Dry-run (`--dry-run`) only executes Phase 1.
