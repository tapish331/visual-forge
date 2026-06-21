from __future__ import annotations

import os
from pathlib import Path

from app.env import load_dotenv


def test_load_dotenv_sets_values_without_overriding_existing_env(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# local secrets",
                "HF_TOKEN=hf_example",
                "QUOTED_VALUE=\"hello world\"",
                "EXISTING_VALUE=from-file",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    os.environ.pop("HF_TOKEN", None)
    os.environ.pop("QUOTED_VALUE", None)
    os.environ["EXISTING_VALUE"] = "from-env"

    try:
        load_dotenv(env_file)

        assert os.environ["HF_TOKEN"] == "hf_example"
        assert os.environ["QUOTED_VALUE"] == "hello world"
        assert os.environ["EXISTING_VALUE"] == "from-env"
    finally:
        os.environ.pop("HF_TOKEN", None)
        os.environ.pop("QUOTED_VALUE", None)
        os.environ.pop("EXISTING_VALUE", None)


def test_load_dotenv_ignores_missing_file(tmp_path: Path) -> None:
    load_dotenv(tmp_path / ".env")
