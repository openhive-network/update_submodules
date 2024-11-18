## Requirements Specification

### **1. Project Overview**

**Project Name:** Git Submodule Updater

**Objective:**  
Develop a Python-based automation tool to manage multiple Git repositories and their submodules. The tool should handle cloning repositories, updating submodule references, modifying YAML configuration files based on submodule changes, managing branches and tags, and integrating with merge requests seamlessly.

### **2. Problem Statement**

Managing multiple interconnected Git repositories with submodules can be complex and error-prone, especially when updates to submodules need to propagate to parent repositories and corresponding configuration files. Manual management increases the risk of inconsistencies, outdated references, and configuration mismatches. There is a need for an automated solution to streamline this process, ensuring that all repositories and their configurations remain synchronized and up-to-date.

### **3. Functional Requirements**

#### **3.1. Repository Management**

- **Cloning Repositories:**
  - Clone a list of specified Git repositories into a designated local directory.
  - Skip cloning if the repository already exists locally unless specified otherwise.

- **Fetching Updates:**
  - Fetch the latest changes from the remote repositories to ensure up-to-date information.

- **Checkout Specific References:**
  - Check out a specified Git reference (branch name or commit hash) in each repository.
  - Support both branches and detached commit states.

#### **3.2. Submodule Management**

- **Initialization and Update:**
  - Initialize all submodules within each repository.
  - Update submodules recursively to ensure all nested submodules are correctly initialized and updated.

- **Identifying Updates:**
  - Compare the current submodule commit hashes with desired references.
  - Identify submodules that require updates based on configuration.

#### **3.3. YAML Configuration File Updates**

- **Targeted Updates:**
  - Update specific keys in YAML files (e.g., `.gitlab-ci.yml`) based on submodule changes.
  - Support complex YAML structures, including lists of dictionaries with conditional matching.

- **Conditional Matching:**
  - Allow specifying conditions to identify the correct list items within YAML files for updates.

#### **3.4. Branch and Tag Handling**

- **Feature Branch Creation:**
  - Create unique feature branches (e.g., `update-submodules-1`, `update-submodules-2`, etc.) for committing updates.
  - Ensure branch names are unique across both local and remote repositories to prevent conflicts.

- **Committing Changes:**
  - Commit updates to submodule references and YAML configuration files with descriptive commit messages.

- **Pushing Branches:**
  - Push the feature branches to the remote repositories.
  - Handle push errors gracefully, providing meaningful feedback.

- **Merge Request Integration:**
  - Optionally create merge requests for the pushed branches.
  - Support automerging upon successful pipeline completion if specified.

- **Tagging:**
  - Create and push Git tags with specified names.
  - Support overwriting existing tags when the `--retag` option is used.

#### **3.5. Configuration Management**

- **Configuration File (`repos.yaml`):**
  - Define repositories to manage, their desired references, and YAML update specifications.
  - Support specifying whether to create merge requests and automerge settings.

- **YAML Update Specifications:**
  - Allow specifying which YAML files to update, the key paths within those files, and the associated submodules that trigger the updates.

#### **3.6. Command-Line Interface**

- **Supported Options:**
  - `--dry-run`, `-d`: Perform a simulation of operations without making actual changes.
  - `--log-level`, `-l`: Set the logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`).
  - `--tag`, `-t`: Specify the name of the Git tag to create.
  - `--retag`, `-r`: Overwrite existing tags with the same name when using `--tag`.

- **Usage Examples:**
  - Perform an actual update with tagging:
    ```bash
    python3 update-submodules.py --tag v1.2.3
    ```
  - Perform a dry run with debug logging:
    ```bash
    python3 update-submodules.py --dry-run --log-level DEBUG
    ```

#### **3.7. Logging and Reporting**

- **Comprehensive Logging:**
  - Log all actions, including repository cloning, fetching, updating, committing, pushing, and YAML modifications.
  - Log errors and warnings with sufficient detail to facilitate troubleshooting.

- **Log Outputs:**
  - Output logs to both the console and a log file (`update_submodules.log`).

### **4. Non-Functional Requirements**

#### **4.1. Reliability**

- Ensure the script handles errors gracefully, providing meaningful messages without crashing unexpectedly.
- Implement checks to prevent data loss, such as backing up YAML files before modifications.

#### **4.2. Usability**

- Provide clear and comprehensive documentation (`README.md`) to guide users on setup, configuration, and usage.
- Offer helpful logging to inform users of the script's actions and any issues encountered.

#### **4.3. Performance**

- Optimize repository cloning and fetching to minimize execution time, especially when managing a large number of repositories.
- Implement efficient branch naming and checking mechanisms to reduce the likelihood of conflicts.

#### **4.4. Maintainability**

- Write clean, modular, and well-documented code to facilitate future enhancements and maintenance.
- Structure the configuration file (`repos.yaml`) logically, allowing easy updates and scalability.

### **5. Environment Details**

- **Programming Language:** Python 3.6+
- **Operating System:** Unix-based systems (Linux, macOS) are recommended for compatibility with Git operations and file system handling.
- **Dependencies:**
  - `GitPython`: For interacting with Git repositories.
  - `ruamel.yaml`: For parsing and modifying YAML files.
  - `PyYAML`: For additional YAML support.
- **Version Control:** Git must be installed and accessible via the command line.
- **Access Permissions:**  
  - Ensure SSH keys are properly configured for accessing the specified repositories.
  - The user executing the script must have sufficient permissions to clone, fetch, push branches, and create tags in the target repositories.

### **6. Assumptions and Constraints**

- **SSH Access:** The script uses SSH URLs for repositories. It assumes that the executing environment has SSH keys configured for accessing these repositories without manual authentication.
- **Repository Structure:**  
  - Repositories have a standard structure with `.gitmodules` files defining submodules.
  - YAML files specified in the configuration exist and follow predictable structures for key path updates.
- **Branch Naming Convention:**  
  - The script follows a specific naming convention for feature branches (`update-submodules`, optionally suffixed with numbers or timestamps) to manage updates.
- **Merge Request Integration:**  
  - The remote Git server (e.g., GitLab) supports push options for creating merge requests and automerge functionality.

### **7. Detailed Feature Breakdown**

#### **7.1. Repository Cloning and Setup**

- **Clone Repositories:**
  - For each repository URL specified in `repos.yaml`, clone the repository into the `repositories` directory unless it already exists locally.
  
- **Initialize Submodules:**
  - After cloning, initialize and update all submodules recursively to ensure the repository is fully set up.

#### **7.2. Fetching and Checking Out References**

- **Fetch Latest Changes:**
  - Fetch all updates from the remote repository to ensure the local copy is up-to-date.
  
- **Checkout Reference:**
  - Check out the specified reference (`ref`) for each repository, supporting both branches and commit hashes.

#### **7.3. Submodule Update Identification and Handling**

- **Identify Required Updates:**
  - Compare current submodule commit hashes with the desired references as specified in `repos.yaml`.
  
- **Prepare Updates:**
  - For submodules requiring updates, stage the changes to update the submodule pointers.

#### **7.4. Branch Creation and Commit Operations**

- **Create Feature Branch:**
  - Generate a unique branch name (e.g., `update-submodules-1`) ensuring it doesn't conflict with existing local or remote branches.
  
- **Commit Changes:**
  - Commit the staged submodule updates with a descriptive message detailing the changes.

#### **7.5. Pushing and Merge Request Creation**

- **Push Feature Branch:**
  - Push the newly created feature branch to the remote repository.
  
- **Create Merge Request:**
  - If enabled, create a merge request for the feature branch, optionally setting it to automerge upon successful pipeline completion.

#### **7.6. YAML Configuration File Updates**

- **Parse and Modify YAML Files:**
  - Locate specified YAML files and update targeted keys based on submodule changes.
  - Support conditional updates within lists using key path syntax (e.g., `include[project=repo_url].ref`).

- **Backup Before Modification:**
  - Create backups of YAML files before applying any changes to prevent data loss.

#### **7.7. Tagging Support**

- **Create and Push Tags:**
  - Create Git tags with specified names and push them to the remote repository.
  
- **Retagging:**
  - If the `--retag` option is used, overwrite existing tags with the same name.

#### **7.8. Logging and Reporting**

- **Detailed Logs:**
  - Log all actions, including cloning, fetching, checking out references, updating submodules, committing, pushing, and YAML modifications.
  
- **Error Logging:**
  - Log errors and warnings with detailed messages to aid in troubleshooting.

- **Log Outputs:**
  - Output logs to both the console and a log file (`update_submodules.log`).

### **8. User Interface**

- **Command-Line Interface (CLI):**
  - Users interact with the script via the command line, providing necessary options and flags to customize behavior.
  
- **Configuration File (`repos.yaml`):**
  - Users define repositories and their settings in a YAML configuration file, allowing easy customization without modifying the script.

### **9. Security Considerations**

- **SSH Key Management:**
  - Ensure that SSH keys used for repository access are securely managed and have appropriate permissions.
  
- **Data Integrity:**
  - Implement checks and backups to maintain data integrity during YAML file modifications and Git operations.

### **10. Future Enhancements**

- **Parallel Processing:**
  - Implement parallel cloning and updating of repositories to improve performance for large sets of repositories.
  
- **Advanced YAML Parsing:**
  - Integrate more sophisticated YAML parsing capabilities to handle a wider range of YAML structures and conditions.
  
- **Interactive Mode:**
  - Introduce an interactive mode for users to review changes before applying them.
  
- **Integration with Other CI/CD Tools:**
  - Extend support for other CI/CD platforms beyond GitLab, such as GitHub Actions or Jenkins.

### **11. Glossary**

- **Submodule:** A Git repository embedded within another Git repository, allowing you to include external projects as dependencies.
- **Merge Request:** A request to merge changes from one branch to another, commonly used in GitLab.
- **Automerge:** Automatically merge a merge request once all pipeline checks pass successfully.
- **Feature Branch:** A branch created to develop a specific feature or set of changes, isolated from the main codebase until ready to merge.
