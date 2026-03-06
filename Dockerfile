FROM python:3.12-slim

WORKDIR /app

# Install project
COPY pyproject.toml README.md ./
COPY pokemonbot/ pokemonbot/
RUN pip install --no-cache-dir .

# Config and proxy files should be bind-mounted via docker-compose;
# do NOT declare them as VOLUME (Docker creates directories for missing
# volume targets which causes EBUSY errors when the app tries to write files).

EXPOSE 3005

CMD ["pokemonbot", "web", "--host", "0.0.0.0", "--port", "3005"]
