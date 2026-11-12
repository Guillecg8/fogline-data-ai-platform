"""Reading and writing Delta tables without ``pyarrow.parquet`` or ``pyarrow.dataset``.

Some locked-down Windows machines block those two libraries. Writes go through the Rust engine
of ``deltalake``; reads go through its SQL engine, which returns an Arrow stream that DuckDB and
``pyarrow.Table`` can consume directly.
"""

from pathlib import Path

import pyarrow as pa
from deltalake import DeltaTable, QueryBuilder, write_deltalake


def exists(path: Path) -> bool:
    return DeltaTable.is_deltatable(str(path))


def write(path: Path, table: pa.Table, mode: str = "append") -> None:
    """``append`` adds rows; ``overwrite`` replaces the table (and lets its schema evolve)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "overwrite":
        write_deltalake(str(path), table, mode="overwrite", schema_mode="overwrite")
    else:
        write_deltalake(str(path), table, mode="append")


def read_arrow(path: Path, sql: str = "select * from t") -> pa.Table | None:
    """Query a Delta table (it is called ``t`` in the SQL). None if the table does not exist."""
    if not exists(path):
        return None
    result = QueryBuilder().register("t", DeltaTable(str(path))).execute(sql).read_all()
    return pa.table(result)


def read_rows(path: Path, sql: str = "select * from t") -> list[dict]:
    table = read_arrow(path, sql)
    return [] if table is None else table.to_pylist()
