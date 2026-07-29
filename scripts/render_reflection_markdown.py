"""Render immutable top-level Markdown from a completed reflection."""

from __future__ import annotations

import argparse
import json

from app.config import Settings
from app.db import Database
from app.services.reflection_markdown import write_reflection_markdown


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a completed live reflection and its lesson candidates."
    )
    parser.add_argument("reflection_id")
    args = parser.parse_args()
    settings = Settings()
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            artifacts = write_reflection_markdown(
                session,
                args.reflection_id,
            )
        print(
            json.dumps(
                {
                    "reflection": {
                        "path": str(artifacts.reflection.path),
                        "payload_hash": artifacts.reflection.payload_hash,
                        "file_hash": artifacts.reflection.file_hash,
                    },
                    "lessons": [
                        {
                            "path": str(item.path),
                            "payload_hash": item.payload_hash,
                            "file_hash": item.file_hash,
                        }
                        for item in artifacts.lessons
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
