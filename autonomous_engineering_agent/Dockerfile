FROM python:3.11-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git docker-cli ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY agent ./agent
COPY migrations ./migrations
COPY agent.example.yaml ./

RUN python -m pip install --upgrade pip \
    && python -m pip install .

EXPOSE 8080
CMD ["agent", "dashboard", "--host", "0.0.0.0", "--port", "8080"]
