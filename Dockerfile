FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy TRALHA_DATA_DIR=/data
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev \
 && rm /bin/uv \
 && useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin tralha \
 && mkdir /data && chown tralha:tralha /data

ENV PATH="/app/.venv/bin:$PATH"
USER tralha
VOLUME /data
EXPOSE 8000 8001
CMD ["tralha", "serve"]
