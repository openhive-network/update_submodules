# Submodule Management Scripts

This repository contains scripts for managing Git submodule dependencies across multiple repositories. The first step in using these scripts is to create a directory that these scripts will operate on, then clone all the repos with submodules to be updated into this new directory.E.g.

```bash
mkdir ../src
cd ../src
git clone git@gitlab.syncad.com:hive/hive
git clone git@gitlab.syncad.com:hive/haf
git clone git@gitlab.syncad.com:hive/HAfAH
git clone git@gitlab.syncad.com:hive/reputation_tracker
git clone git@gitlab.syncad.com:hive/balance_tracker
git clone git@gitlab.syncad.com:hive/haf_block_explorer
git clone git@gitlab.syncad.com:hive/hivemind
git clone git@gitlab.syncad.com:hive/haf_api_node
```

Next, checkout the versions of each app you want to use. If you want to use the head of develop for everything, you can use the `checkout_develop_versions.py` script to move all the repos to the latest head of develop. Once you have the versions you want, run the `update_submodules.py` script to update all the repos and push the changes back to the origin repo. If you change your mind, or something goes wrong, you can use `revert_update_submodules.py` to revert your changes.

If you run `update_submodules.py` with the tag option, you will likely want to create MRs from `update-submodules-py` branch to `develop` branch in each repo. Probably this step should also be automated.

## Scripts

### checkout_develop_versions.py
A utility script that updates all repositories in a directory to their `develop` branch (this script assumes a develop branch exists in each repo). It:

Checks out the develop branch
Pulls latest changes
Updates all submodules recursively
Usage:

`python checkout_develop_versions.py ../src`

### update_submodules.py
A script that updates Git submodules across multiple repositories in a specified directory. It:
- Analyzes dependencies between repositories
- Updates submodules in the correct order
- Creates branches with the submodule updateds called `update-submodules-py`
- Optionally adds a common tag to all the repos
- Pushes changes to remote repositories

Usage:
`python update_submodules.py ../src [tag]`

### revert_update_submodules.py
A cleanup script that reverts changes made by update_submodules.py. It:

Removes update-submodules-py branches created by update_submodules.py
Deletes specified tags both locally and on GitLab

#### Configuration
To enable revert_update_submodules.py to be able to remove protected tags, create a config.ini file with GitLab credentials:

[gitlab]
api_url = https://gitlab.syncad.com/api/v4
token = your_gitlab_token