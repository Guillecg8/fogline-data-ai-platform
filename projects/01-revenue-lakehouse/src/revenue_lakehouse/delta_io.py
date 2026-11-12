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


def sql_list(values) -> str:
    """Strings as a SQL ``in (...)`` list, quotes escaped (the values come from source files)."""
    return ", ".join("'" + str(value).replace("'", "''") + "'" for value in values)


IN_CHUNK = 100  # a huge ``in (...)`` list overflows the 1 MB stack of Windows threads


def read_where_in(path: Path, column: str, values: list) -> pa.Table | None:
    """Rows whose ``column`` is in ``values``, queried in small chunks (see ``IN_CHUNK``)."""
    if not exists(path):
        return None
    values = list(values)
    if not values:
        return read_arrow(path, "select * from t where false")
    parts = [
        read_arrow(
            path, f"select * from t where {column} in ({sql_list(values[i : i + IN_CHUNK])})"
        )
        for i in range(0, len(values), IN_CHUNK)
    ]
    return pa.concat_tables(parts)


def merge(path: Path, table: pa.Table, keys: list[str], update: bool = False) -> None:
    """Insert the rows whose key is new; with ``update`` also replace the ones that exist.

    Running it twice with the same rows changes nothing.
    """
    predicate = " and ".join(f"t.{key} = s.{key}" for key in keys)
    builder = DeltaTable(str(path)).merge(
        source=table, predicate=predicate, source_alias="s", target_alias="t"
    )
    if update:
        builder = builder.when_matched_update_all()
    builder.when_not_matched_insert_all().execute()
