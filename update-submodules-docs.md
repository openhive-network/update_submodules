# Git Submodule Updater

## Introduction

The **Git Submodule Updater** is a Python-based automation tool designed to streamline the management of multiple Git repositories and their submodules. It facilitates updating submodule references, modifying YAML configuration files based on submodule changes, and handling branch operations seamlessly. This tool is especially beneficial for projects with complex repository structures and interdependent submodules.

## Features

- **Automated Repository Cloning:** Clone multiple repositories based on a configuration file.
- **Submodule Management:** Initialize and update submodules recursively.
- **YAML Configuration Updates:** Automatically update specified keys in YAML files when submodules are updated.
- **Branch Handling:** Create unique feature branches for updates, commit changes, and push to remote repositories.
- **Merge Request Integration:** Optionally create merge requests with customizable settings.
- **Tagging Support:** Create and manage Git tags with options to overwrite existing tags.
- **Dry Run Mode:** Simulate operations without making actual changes, aiding in testing and verification.
- **Comprehensive Logging:** Detailed logs for monitoring and debugging purposes.

## Prerequisites

Before running the script, ensure that your environment meets the following requirements:

- **Operating System:** Unix-based systems (Linux, macOS) are recommended.
- **Python Version:** Python 3.6 or higher.
- **Git:** Git must be installed and accessible via the command line.
- **Python Packages:** The script relies on several Python libraries. Install them using `pip`:

On Ubuntu, you can install the Python requirements from system packages:
```bash
sudo apt install python3-git python3-yaml python3-ruamel.yaml
```

Or install via PIP:

```bash
pip install GitPython ruamel.yaml PyYAML
````

_Alternatively, you can install the required packages using the provided `requirements.txt` file:_

```bash
pip install -r requirements.txt
```

## Installation

1. **Clone the Repository:**

   ```bash
   git clone https://your-repo-url.git
   cd your-repo-directory
   ```
2. **Install Python Dependencies:**

   ```bash
   pip install -r requirements.txt
   ```
3. **Configure SSH Access:**

   Ensure that your SSH keys are properly configured to allow Git operations on your repositories without manual authentication.

## Usage

### Running the Script

Execute the script using Python:

```bash
python3 update-submodules.py [OPTIONS]
```

### Command-Line Options

The script supports several command-line options to customize its behavior:

- `--dry-run`, `-d`:\
  **Description:** Perform a dry run without committing or pushing changes.\
  **Usage:**

  ```bash
  python3 update-submodules.py --dry-run
  ```
- `--log-level`, `-l`:\
  **Description:** Set the logging level. Options include `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Default is `INFO`.\
  **Usage:**

  ```bash
  python3 update-submodules.py --log-level DEBUG
  ```
- `--tag`, `-t`:\
  **Description:** Specify the name of the Git tag to create for each repository.\
  **Usage:**

  ```bash
  python3 update-submodules.py --tag v1.2.3
  ```
- `--retag`, `-r`:\
  **Description:** Overwrite existing tags with the same name when using `--tag`.\
  **Usage:**

  ```bash
  python3 update-submodules.py --tag v1.2.3 --retag
  ```

### Example Command

Perform an actual update with tagging and create merge requests automatically:

```bash
python3 update-submodules.py --tag v1.2.3
```

Perform a dry run with debug-level logging:

```bash
python3 update-submodules.py --dry-run --log-level DEBUG
```

## Configuration File (`repos.yaml`)

The script relies on a YAML configuration file named `repos.yaml` to define the repositories to manage and their respective settings.

### Structure

```yaml
repository_url:
  ref: <branch_or_commit_hash>
  create_merge_request: <true|false>
  automerge: <true|false>
  update_yaml:
    - filename: <path_to_yaml_file>
      key_to_update: <yaml_key_path>
      submodule_referenced: <submodule_repository_url>
```

### Field Descriptions

- **`repository_url`**:\
  **Type:** String\
  **Description:** The SSH URL of the Git repository to manage.\
  **Example:**

  ```yaml
  ssh://steem-8.syncad.com/home/syncad/repos/B:
  ```
- **`ref`**:\
  **Type:** String\
  **Description:** The Git reference (branch name or commit hash) to check out in the repository.\
  **Example:**

  ```yaml
  ref: master
  ```
- **`create_merge_request`**:\
  **Type:** Boolean (`true` or `false`)\
  **Description:** Determines whether to automatically create a merge request after pushing changes.\
  **Default:** `true`\
  **Example:**

  ```yaml
  create_merge_request: false
  ```
- **`automerge`**:\
  **Type:** Boolean (`true` or `false`)\
  **Description:** If `true`, the merge request will be merged automatically upon successful pipeline completion.\
  **Default:** `false`\
  **Example:**

  ```yaml
  automerge: false
  ```
- **`update_yaml`**:\
  **Type:** List\
  **Description:** Defines the YAML files to update based on submodule changes. Each entry specifies the file, the YAML key path to update, and the associated submodule.\
  **Example:**

  ```yaml
  update_yaml:
    - filename: .gitlab-ci.yml
      key_to_update: include[project=ssh://steem-8.syncad.com/home/syncad/repos/C].ref
      submodule_referenced: ssh://steem-8.syncad.com/home/syncad/repos/C
  ```

### `update_yaml` Entry Details

- **`filename`**:\
  **Type:** String\
  **Description:** The relative path to the YAML file within the repository that needs to be updated.\
  **Example:**

  ```yaml
  filename: .gitlab-ci.yml
  ```
- **`key_to_update`**:\
  **Type:** String\
  **Description:** The YAML key path indicating where the update should occur. Supports conditional matching within lists using the syntax `list_key[field=value].sub_key`.\
  **Example:**

  ```yaml
  key_to_update: include[project=ssh://steem-8.syncad.com/home/syncad/repos/C].ref
  ```
- **`submodule_referenced`**:\
  **Type:** String\
  **Description:** The SSH URL of the submodule repository whose commit hash should be used to update the YAML file.\
  **Example:**

  ```yaml
  submodule_referenced: ssh://steem-8.syncad.com/home/syncad/repos/C
  ```

### Complete Example

```yaml
ssh://steem-8.syncad.com/home/syncad/repos/A:
  ref: master
  create_merge_request: false
  automerge: false

ssh://steem-8.syncad.com/home/syncad/repos/B:
  ref: master
  create_merge_request: false
  automerge: false
  update_yaml:
    - filename: .gitlab-ci.yml
      key_to_update: include[project=ssh://steem-8.syncad.com/home/syncad/repos/C].ref
      submodule_referenced: ssh://steem-8.syncad.com/home/syncad/repos/C

ssh://steem-8.syncad.com/home/syncad/repos/C:
  ref: master
  create_merge_request: false
  automerge: false

ssh://steem-8.syncad.com/home/syncad/repos/D:
  ref: master
  create_merge_request: false
  automerge: false
```

## How It Works

 1. **Configuration Loading:**
    - The script reads the `repos.yaml` file to identify the repositories to manage and their specific settings.
 2. **Dependency Management:**
    - It builds a dependency graph based on repository submodules to determine the order of processing, ensuring that submodules are updated before their parent repositories.
 3. **Repository Cloning:**
    - For each repository, the script clones it into a designated `repositories` directory if it's not already cloned.
 4. **Fetching and Checkout:**
    - It fetches the latest updates from the remote repository and checks out the specified reference (`ref`).
 5. **Submodule Initialization and Update:**
    - Initializes and updates all submodules recursively within the repository.
 6. **Identifying Submodule Updates:**
    - Compares the current submodule commit hashes with the desired references as specified in `repos.yaml`.
    - Identifies submodules that require updates based on configuration.
 7. **Branch Creation:**
    - Creates a unique feature branch (e.g., `update-submodules-1`) to commit the updates. It ensures that the branch name doesn't conflict with existing local or remote branches.
 8. **Committing Changes:**
    - Commits the submodule updates with a descriptive commit message detailing the changes.
 9. **Pushing and Merge Requests:**
    - Pushes the feature branch to the remote repository. If configured, it creates a merge request with optional automerge settings.
10. **YAML File Updates:**
    - Updates specified YAML files based on submodule changes. For example, it can update the `ref` of a submodule within a `.gitlab-ci.yml` file.
11. **Tagging:**
    - Creates and pushes Git tags if specified, with options to overwrite existing tags.
12. **Logging and Reporting:**
    - Logs all actions, errors, and summaries to both the console and a log file (`update_submodules.log`) for monitoring and debugging purposes.

## Troubleshooting

- **Branch Push Failures:**
  - Ensure that the script has the necessary permissions to push branches to the remote repository.
  - Check for branch protection rules that might prevent branch creation or pushing.
- **YAML Parsing Errors:**
  - Verify that the `key_to_update` paths in `repos.yaml` accurately reflect the structure of the target YAML files.
  - Ensure that conditional path segments (e.g., `include[project=...]`) are correctly formatted.
- **SSH Authentication Issues:**
  - Confirm that your SSH keys are properly configured and that you have access rights to the specified repositories.

## Contributing

Contributions are welcome! Please open issues or submit pull requests for enhancements, bug fixes, or additional features.

## License

[MIT License](LICENSE)

## Contact

For any questions or support, please contact your-email@example.com.
