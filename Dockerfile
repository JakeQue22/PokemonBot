FROM python:3.12-slim

WORKDIR /app

# Install project
COPY pyproject.toml README.md ./
COPY pokemonbot/ pokemonbot/
RUN pip install --no-cache-dir .

# Default config and proxy files can be mounted as volumes
VOLUME ["/app/config.yaml", "/app/proxies.txt"]

EXPOSE 3005

CMD ["pokemonbot", "web", "--host", "0.0.0.0", "--port", "3005"]
