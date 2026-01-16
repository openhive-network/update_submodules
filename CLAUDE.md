# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository contains Python scripts for automating Git submodule updates across multiple interconnected Hive blockchain repositories. The scripts handle:
- Cloning repositories and updating submodule references in topological order
- Modifying YAML configuration files (like `.gitlab-ci.yml`) to match submodule commits
- Creating feature branches, commits, merge requests, and tags
- Two-phase operation: local validation (Phase 1), then remote push (Phase 2)

## Scripts

| Script | Purpose |
|--------|---------|
| `update-submodules.py` | Main script for updating submodule references across repos (develop branch workflow) |
| `update-submodules-release.py` | Extended version with `--release-to-master` for rebasing develop onto master |
| `checkout_develop_versions.py` | Utility to checkout develop branch in all repos in a directory |
| `revert_update_submodules.py` | Cleanup script to delete `update-submodules-*` branches and tags |

## Configuration Files

- `repos.yaml` - Config for develop branch workflow
- `repos.yaml.to-master` - Config for master branch releases (uses `source_ref` for specific commits)
- `config.ini` - GitLab API credentials (optional, can use `GITLAB_TOKEN` env var instead)

### repos.yaml Structure

```yaml
git@gitlab.syncad.com:hive/repo.git:
  ref: 'develop'           # Branch/tag/commit to checkout
  # ref_from_dir: /path    # Alternative: use ref from local checkout
  automerge: true          # Auto-merge MR when pipeline passes
  create_merge_request: true  # Default true
  target_branch: develop   # MR target (defaults to ref)
  update_yaml:             # YAML files to update with submodule commits
    - filename: '.gitlab-ci.yml'
      key_to_update: 'include[project=hive/haf].ref'
      submodule_referenced: 'git@gitlab.syncad.com:hive/haf.git'
```

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt
# Or on Ubuntu: sudo apt install python3-git python3-yaml python3-ruamel.yaml

# Dry run (validates without pushing)
python3 update-submodules.py --dry-run

# Full update with tag
python3 update-submodules.py --tag v1.28.0

# Overwrite existing tags
python3 update-submodules.py --tag v1.28.0 --retag

# Debug logging
python3 update-submodules.py --log-level DEBUG

# Cleanup branches and tags from failed run
python3 update-submodules.py --cleanup --cleanup-tag v1.28.0

# Test all projects with a HAF feature branch
python3 update-submodules.py --haf-branch feature/my-haf-branch

# Prepare all repos on develop branch
python3 checkout_develop_versions.py ../src

# Revert changes from update script
python3 revert_update_submodules.py ../src v1.28.0
```

### Testing with a HAF Feature Branch

The `--haf-branch` option allows testing all dependent projects with a specific HAF branch:

```bash
python3 update-submodules.py --haf-branch feature/my-haf-branch --dry-run
```

This automatically:
1. Overrides HAF's ref to the specified branch
2. Updates submodule references in all repos that have HAF as a submodule
3. Sets `UPSTREAM_OVERRIDE_TAG` in CI files for projects using dynamic HAF detection
4. Creates MRs in all affected projects

**Prerequisites**: The HAF branch must have CI-built Docker images available at `registry.gitlab.syncad.com/hive/haf:<commit-sha>`

## Architecture

### Dependency Resolution
The scripts build a dependency graph from submodule relationships and process repos in topological order (leaf submodules first). This ensures parent repos see updated commits from their submodules.

### Two-Phase Operation
1. **Phase 1 (Local)**: Clone repos, create branches, update submodules, modify YAML files, create commits and tags locally
2. **Phase 2 (Push)**: Push all changes to remotes in topological order

Dry-run (`--dry-run`) only executes Phase 1.

### YAML Updates
The `update_yaml` feature modifies CI config files to reference the correct submodule commits. Key path syntax supports conditional matching: `include[project=hive/haf].ref` finds the list item where `project=hive/haf` and updates its `ref` field.

### Branch Naming
Feature branches are named `update-submodules` or `update-submodules-for-<tag>`, with numeric suffixes (`-2`, `-3`) if the name already exists.

## Key Dependencies

- `GitPython` - Git operations
- `ruamel.yaml` - YAML parsing with comment/format preservation
- `PyYAML` - Additional YAML support
- `requests` - GitLab API calls (for protected tag deletion)

## GitLab Integration

- Uses GitLab push options to create MRs: `merge_request.create=true`, `merge_request.target=<branch>`
- Protected tags require API deletion via `GITLAB_TOKEN` or `config.ini`
- MRs can auto-merge when pipelines pass (`automerge: true`)
