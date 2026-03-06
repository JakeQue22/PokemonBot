#!/bin/sh
# Ensure config and proxy paths are regular files.
# Docker bind-mounts create directories when the host path doesn't exist;
# this script fixes that before the application starts.

for f in /app/config.yaml /app/proxies.txt; do
    if [ -d "$f" ]; then
        echo "WARNING: $f is a directory (missing on host?) – replacing with empty file"
        rmdir "$f" 2>/dev/null && touch "$f" || {
            # Mount point that cannot be removed – write inside it instead.
            # The app's ensure_proxy_file() handles this at runtime too.
            echo "WARNING: could not replace $f – it may be a mount point"
        }
    fi
done

exec "$@"
