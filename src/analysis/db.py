"""Read-only access to the analysis database (credentials from .env)."""
from __future__ import annotations

import os
from functools import lru_cache

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from src.ingest.http_cache import PROJECT_ROOT

OUTPUT_DIR = PROJECT_ROOT / "outputs"


@lru_cache(maxsize=1)
def engine():
    load_dotenv(PROJECT_ROOT / ".env")
    url = URL.create("postgresql+psycopg", username=os.environ["PGUSER"], password=os.environ["PGPASSWORD"],
                     host=os.environ["PGHOST"], port=int(os.environ["PGPORT"]), database=os.environ["PGDATABASE"])
    return create_engine(url)


def query(sql: str, **params) -> pd.DataFrame:
    return pd.read_sql_query(sql, engine(), params=params or None)
