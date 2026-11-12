# The testnet stack image: one install of the `sccp` package, used by the node,
# the relay and every miner in docker-compose.yml.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SCCP_GENESIS_DIR=/app/genesis

WORKDIR /app

# Only what the wheel build and the runtime commands need.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY data ./data
COPY genesis ./genesis
COPY deploy ./deploy

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 sccp \
    && mkdir -p /data /stack \
    && chown -R sccp:sccp /data /stack

USER sccp

# ``/data`` holds the node's SQLite chain, ``/stack`` the generated keys and
# relay config. Both are declared as volumes in docker-compose.yml, which seeds
# them with this image's ownership on first use.
VOLUME ["/data", "/stack"]

ENTRYPOINT ["sccp"]
CMD ["--help"]
