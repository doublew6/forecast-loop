FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ARG UV_VERSION=0.9.8
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
ENV VERICOUNCIL_WIKI_PATH="/app/wiki"

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
