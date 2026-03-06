FROM python:3.12-slim

WORKDIR /app

# Install project
COPY pyproject.toml README.md ./
COPY pokemonbot/ pokemonbot/
RUN pip install --no-cache-dir .

# Seed empty config/proxy files so bind-mounts have file targets
# (Docker creates directories when the host path is missing).
RUN touch /app/proxies.txt

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 3005

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["pokemonbot", "web", "--host", "0.0.0.0", "--port", "3005"]
