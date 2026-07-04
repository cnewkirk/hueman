FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY hue_iac ./hue_iac
RUN pip install --no-cache-dir .
# Config + secrets + logs are mounted at runtime (not baked in).
WORKDIR /data
ENTRYPOINT ["hue-iac", "-c", "/data/hue.yaml", "circadian", "run"]
