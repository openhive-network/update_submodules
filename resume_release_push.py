#!/usr/bin/env python3
"""
Resume Phase 2 (push) for an already-prepared release-to-master run.

Use this when Phase 1 succeeded (release branches and local tags exist on
each clone in repositories/) but Phase 2 was interrupted partway through.
This avoids redoing the full rebase work, which for hivemind alone takes
many minutes and would produce slightly different commit SHAs (different
committer timestamps), which in turn would force-update tags that were
already pushed successfully.

Walks each repo in the configured config file, reads release branches and
local tags off disk, reconstructs the operations dict that the main script
builds during Phase 1, and calls push_all_operations() — same code path as
a normal Phase 2.

Usage:
    python3 resume_release_push.py --tag 1.28.6
    python3 resume_release_push.py --tag 1.28.6 --config repos.yaml.master
    python3 resume_release_push.py --tag 1.28.6 --no-push-tags
    python3 resume_release_push.py --tag 1.28.6 --skip <repo>

--skip can be repeated and matches by repo name (the last component of the
URL without .git).  Use it when a repo already had its tag and branch
pushed successfully on a previous attempt and you want to skip it now.
"""

import argparse
import importlib.util
import logging
import os
import sys
from pathlib import Path

from git import Repo


HERE = Path(__file__).resolve().parent


def load_main_script_module():
    """Import update-submodules.py as a module despite the hyphen in its name."""
    script_path = HERE / "update-submodules.py"
    spec = importlib.util.spec_from_file_location("update_submodules_main", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module spec from {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["update_submodules_main"] = module
    spec.loader.exec_module(module)
    return module


def reconstruct_operations(us, config, sorted_repos, tag, skip_repos):
    """Build the operations dict that Phase 2 expects, from on-disk state."""
    operations = {}
    release_branch = f"release/{tag}"

    for repo_url in sorted_repos:
        if repo_url not in config:
            continue
        repo_name = us.get_repo_name(repo_url)
        if repo_name in skip_repos:
            logging.info(f"Skipping '{repo_name}' (--skip)")
            continue

        clone_path = os.path.join(us.BASE_DIR, repo_name)
        if not os.path.isdir(clone_path):
            logging.warning(f"No local clone at '{clone_path}'; '{repo_name}' will be skipped")
            continue

        try:
            repo = Repo(clone_path)
        except Exception as e:
            logging.error(f"Could not open '{clone_path}': {e}")
            continue

        result = us.RepoOperationResult(repo_url=repo_url)
        result.repo_object = repo
        result.settings = config[repo_url]
        result.success = True

        local_branches = [b.name for b in repo.branches]
        if release_branch in local_branches:
            result.branch_name = release_branch
        else:
            logging.warning(f"'{repo_name}' has no local '{release_branch}' branch")

        local_tags = [t.name for t in repo.tags]
        if tag in local_tags:
            result.tag_to_create = tag
        else:
            logging.warning(f"'{repo_name}' has no local '{tag}' tag")

        if not result.branch_name and not result.tag_to_create:
            logging.warning(f"'{repo_name}' has neither branch nor tag — nothing to push, skipping")
            continue

        operations[repo_url] = result

    return operations


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--tag', required=True, help="Tag name (e.g. '1.28.6')")
    p.add_argument('--config', default=None,
                   help="Config YAML (default: repos.yaml.master if --release-to-master, else repos.yaml)")
    p.add_argument('--release-to-master', action='store_true', default=True,
                   help="(default true) Use release-to-master push semantics — MR titles and target.")
    p.add_argument('--no-release-to-master', dest='release_to_master', action='store_false',
                   help="Disable release-to-master mode (use the develop-tag MR title and target).")
    p.add_argument('--no-push-tags', action='store_true',
                   help="Push branches and create MRs but don't push tags.")
    p.add_argument('--skip', action='append', default=[], metavar='REPO',
                   help="Repo name to skip (already pushed in a previous attempt).  Repeatable.")
    p.add_argument('--log-level', default='INFO')
    return p.parse_args()


def main():
    args = parse_args()
    us = load_main_script_module()
    us.setup_logging(args.log_level)

    config_path = args.config or ('repos.yaml.master' if args.release_to_master else us.CONFIG_FILE)
    logging.info(f"Using configuration file: {config_path}")
    config = us.load_config(config_path)
    us.validate_config(config)

    dependency_graph = us.build_dependency_graph(config)
    sorted_repos = us.topological_sort(dependency_graph)
    sorted_repos.reverse()  # leaves first, matches Phase 1 order

    operations = reconstruct_operations(us, config, sorted_repos, args.tag, set(args.skip))

    if not operations:
        logging.error("Nothing to push (no repos with local release branch or tag).")
        sys.exit(1)

    logging.info(f"Resuming push for {len(operations)} repo(s)")
    success = us.push_all_operations(
        operations, dependency_graph, sorted_repos,
        no_push_tags=args.no_push_tags,
        release_to_master=args.release_to_master,
        tag=args.tag,
    )

    if success:
        logging.info("Resume push completed successfully.")
    else:
        logging.error("Resume push reported failures.")
        sys.exit(1)


if __name__ == "__main__":
    main()
