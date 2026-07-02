import asyncio
import json
import os
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import inspect, text

# Read the connection string from the environment (same var as the app). No
# hardcoded credential in source.
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    sys.exit("DATABASE_URL is not set. Export it before running the introspection script.")
TARGET_SCHEMA = "app"
BASE_PATH = Path(__file__).parent
OUTPUT_JSON = BASE_PATH / "db_schema_report.json"
OUTPUT_MD = BASE_PATH / "db_schema_report.md"


def collect_schema(sync_conn):
    insp = inspect(sync_conn)
    schema_report = OrderedDict()
    tables = sorted(insp.get_table_names(schema=TARGET_SCHEMA))
    for table_name in tables:
        columns = []
        for col in insp.get_columns(table_name, schema=TARGET_SCHEMA):
            columns.append({
                "name": col["name"],
                "type": str(col["type"]),
                "nullable": col["nullable"],
                "default": col.get("default"),
                "autoincrement": col.get("autoincrement"),
                "comment": col.get("comment"),
            })
        pk = insp.get_pk_constraint(table_name, schema=TARGET_SCHEMA)
        fks = insp.get_foreign_keys(table_name, schema=TARGET_SCHEMA)
        indexes = insp.get_indexes(table_name, schema=TARGET_SCHEMA)
        uniques = insp.get_unique_constraints(table_name, schema=TARGET_SCHEMA)
        table_comment = insp.get_table_comment(table_name, schema=TARGET_SCHEMA).get("text")
        schema_report[table_name] = {
            "columns": columns,
            "primary_key": pk,
            "foreign_keys": fks,
            "indexes": indexes,
            "unique_constraints": uniques,
            "comment": table_comment,
        }
    return schema_report


def build_markdown(schema_report):
    lines = []
    now = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
    lines.append("# FitPilot - Reporte de esquema (schema app)")
    lines.append("")
    lines.append(f"- Generado: {now}")
    lines.append(f"- Tablas encontradas: {len(schema_report)}")
    lines.append("")

    for table_name, info in schema_report.items():
        row_count = info.get("row_count")
        row_note = f"filas: {row_count}" if row_count is not None else "filas: n/d"
        lines.append(f"## {table_name} ({row_note})")
        if info.get("comment"):
            lines.append(f"Comentario: {info['comment']}")
        lines.append("**Columnas**")
        for col in info["columns"]:
            parts = [f"{col['name']} ({col['type']})"]
            if not col["nullable"]:
                parts.append("NOT NULL")
            if col.get("default"):
                parts.append(f"default {col['default']}")
            if col.get("autoincrement"):
                parts.append("auto")
            if col.get("comment"):
                parts.append(f"nota: {col['comment']}")
            lines.append("- " + "; ".join(parts))
        pk = info.get("primary_key") or {}
        pk_cols = pk.get("constrained_columns") or []
        if pk_cols:
            lines.append("**Llave primaria**")
            lines.append(f"- {pk.get('name')}: {', '.join(pk_cols)}")
        indexes = info.get("indexes") or []
        if indexes:
            lines.append("**Indices**")
            for idx in indexes:
                cols = ", ".join(idx.get("column_names") or [])
                label = f"{idx.get('name')}"
                if idx.get("unique"):
                    label += " (UNIQUE)"
                lines.append(f"- {label}: {cols}")
        fks = info.get("foreign_keys") or []
        if fks:
            lines.append("**Relaciones (FK)**")
            for fk in fks:
                source_cols = ", ".join(fk.get("constrained_columns") or [])
                target_cols = ", ".join(fk.get("referred_columns") or [])
                target = f"{fk.get('referred_schema')}.{fk.get('referred_table')}({target_cols})"
                options = fk.get("options") or {}
                extra = []
                if options.get("ondelete"):
                    extra.append(f"ON DELETE {options['ondelete']}")
                if options.get("onupdate"):
                    extra.append(f"ON UPDATE {options['onupdate']}")
                suffix = f" ({', '.join(extra)})" if extra else ""
                lines.append(f"- {fk.get('name')}: {source_cols} -> {target}{suffix}")
        uniques = info.get("unique_constraints") or []
        if uniques:
            lines.append("**Restricciones unicas**")
            for uq in uniques:
                cols = ", ".join(uq.get("column_names") or [])
                lines.append(f"- {uq.get('name')}: {cols}")
        lines.append("")

    return "\n".join(lines)


async def main():
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as conn:
        schema_report = await conn.run_sync(collect_schema)
        for table_name in schema_report:
            result = await conn.execute(
                text(f'SELECT COUNT(*) FROM "{TARGET_SCHEMA}"."{table_name}"')
            )
            schema_report[table_name]["row_count"] = result.scalar_one()
    await engine.dispose()
    with OUTPUT_JSON.open("w", encoding="utf-8") as fh:
        json.dump(schema_report, fh, indent=2, ensure_ascii=False)
    OUTPUT_MD.write_text(build_markdown(schema_report), encoding="utf-8")
    print(f"Schema snapshot guardado en {OUTPUT_JSON}")
    print(f"Reporte Markdown generado en {OUTPUT_MD}")


if __name__ == "__main__":
    asyncio.run(main())
