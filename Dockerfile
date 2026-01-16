FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt requests

COPY *.py .
COPY *.yaml .

# Add GitLab to known hosts
RUN mkdir -p /root/.ssh && \
    chmod 700 /root/.ssh && \
    ssh-keyscan gitlab.syncad.com >> /root/.ssh/known_hosts 2>/dev/null

# Entrypoint script to copy SSH keys with correct permissions
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
