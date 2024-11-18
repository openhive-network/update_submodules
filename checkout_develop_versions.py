import os
import subprocess
import sys

def update_repos(directory):
    for subdir in os.listdir(directory):
        subdir_path = os.path.join(directory, subdir)
        if os.path.isdir(subdir_path):
            os.chdir(subdir_path)
            subprocess.run(['git', 'checkout', 'develop'])
            subprocess.run(['git', 'pull'])
            subprocess.run(['git', 'submodule', 'update', '--init', '--recursive'])
            os.chdir('..')

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python checkout_develop_versions.py <directory>")
        sys.exit(1)
    
    directory = sys.argv[1]
    if not os.path.isdir(directory):
        print(f"Error: {directory} is not a valid directory")
        sys.exit(1)
    
    update_repos(directory)
