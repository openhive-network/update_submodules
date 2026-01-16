#!/bin/bash
if [ -d /ssh-keys ]; then
    cp /ssh-keys/id_* /root/.ssh/ 2>/dev/null || true
    chmod 600 /root/.ssh/id_* 2>/dev/null || true
fi
exec python3 update-submodules.py "$@"
