FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

ARG UV_VERSION=0.13.4
ARG APP_UID=10001
ARG APP_GID=10001

WORKDIR /app
RUN groupadd --gid "${APP_GID}" forecast-loop \
    && useradd \
        --uid "${APP_UID}" \
        --gid "${APP_GID}" \
        --no-create-home \
        --home-dir /nonexistent \
        --shell /usr/sbin/nologin \
        forecast-loop \
    && pip install --no-cache-dir "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock README.md ./
COPY backend ./backend
COPY wiki ./wiki
RUN uv sync --frozen --no-dev \
    && install -d -o "${APP_UID}" -g "${APP_GID}" -m 0750 /app/data \
    && chmod -R go-w /app/backend /app/wiki /app/.venv

ENV PATH="/app/.venv/bin:$PATH"
ENV VERICOUNCIL_DATABASE_URL="sqlite:////app/data/vericouncil.sqlite3"
ENV VERICOUNCIL_CHECKPOINT_PATH="/app/data/checkpoints/langgraph.sqlite3"
ENV VERICOUNCIL_WIKI_PATH="/app/data/wiki"
ENV FORECAST_LOOP_BUNDLED_WIKI_PATH="/app/wiki"

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
