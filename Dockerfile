FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY hueman ./hueman
RUN pip install --no-cache-dir .
# Config + secrets + logs are mounted at runtime (not baked in).
WORKDIR /data
ENTRYPOINT ["hueman", "-c", "/data/hue.yaml", "circadian", "run"]
