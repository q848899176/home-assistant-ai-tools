"""Read-only bounded SQLite access, with recorder entity views per current actor."""

import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

_TABLES = {
    "states",
    "states_meta",
    "state_attributes",
    "statistics",
    "statistics_short_term",
    "statistics_meta",
}


def _path(hass, value):
    if not value:
        value = str(Path(hass.config.config_dir) / "home-assistant_v2.db")
    if value.startswith("sqlite:///"):
        value = value.removeprefix("sqlite:///")
    elif value.startswith("file:"):
        parts = urlsplit(value)
        if parts.netloc or parts.fragment:
            raise ValueError("数据库路径无效")
        value = unquote(parts.path)
    elif ":" in value:
        raise ValueError("仅支持许可目录中的SQLite文件路径")
    root = Path(hass.config.config_dir).resolve()
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise ValueError("数据库路径超出HA配置目录范围或文件不存在")
    return candidate


async def async_query(hass, config, query, visible, is_admin):
    path = await hass.async_add_executor_job(_path, hass, config.get("db_url"))
    return await hass.async_add_executor_job(_query, path, config, query, visible, is_admin)


def _identifier(name):
    return '"' + name.replace('"', '""') + '"'


def _query(path, config, query, visible, is_admin):
    if not isinstance(query, str) or len(query) > 64000:
        raise ValueError("SQL必须是有效且不超过64KB的字符串")
    # Keep tokens separate from string literals/comments; a CTE cannot impersonate a
    # recorder security view when SQLite reports the source name to its authorizer.
    tokens = re.findall(
        r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`[^`]*`|\[[^\]]*\]|[A-Za-z_][A-Za-z0-9_]*|[^\s]",
        query,
    )
    words = [
        token.strip("\"`[]'").lower() for token in tokens if not token.startswith(("--", "/*"))
    ]
    if not words or words[0] not in {"select", "with"}:
        raise ValueError("SQLite仅允许只读SELECT查询")
    deadline = time.monotonic() + float(config.get("timeout", 2))
    max_rows = int(config.get("max_rows", 1000))
    if not 1 <= max_rows <= 2000:
        raise ValueError("SQLite返回行数上限无效")
    try:
        with sqlite3.connect(
            path.as_uri() + "?mode=ro", uri=True, timeout=min(float(config.get("timeout", 2)), 2)
        ) as conn:
            conn.enable_load_extension(False)
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 128000)
            conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 64000)
            conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 200)
            conn.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100)
            conn.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 50)
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            recorder = bool(tables & {"states_meta", "statistics_meta"})
            if not recorder and not is_admin:
                raise ValueError("自定义数据库查询仅允许管理员执行")
            secured = tables & _TABLES if recorder else set()
            if recorder:
                if any(
                    words[i] in {"main", "temp"} and words[i + 1] == "."
                    for i in range(len(words) - 1)
                ):
                    raise ValueError("SQL不能绕过实体过滤视图访问数据库schema")
                for index, word in enumerate(words):
                    if (
                        word in secured
                        and index + 1 < len(words)
                        and words[index + 1] in {"as", "("}
                        and (index == 0 or words[index - 1] in {"with", ",", "recursive"})
                    ):
                        raise ValueError("SQL不能覆盖实体过滤视图")
                ids = (
                    ",".join("'" + entity.replace("'", "''") + "'" for entity in sorted(visible))
                    or "NULL"
                )
                conditions = {
                    "states_meta": f"entity_id IN ({ids})",
                    "statistics_meta": f"statistic_id IN ({ids})",
                    "states": f"metadata_id IN (SELECT metadata_id FROM main.states_meta WHERE entity_id IN ({ids}))",
                    "state_attributes": f"attributes_id IN (SELECT attributes_id FROM main.states WHERE metadata_id IN (SELECT metadata_id FROM main.states_meta WHERE entity_id IN ({ids})))",
                    "statistics": f"metadata_id IN (SELECT id FROM main.statistics_meta WHERE statistic_id IN ({ids}))",
                    "statistics_short_term": f"metadata_id IN (SELECT id FROM main.statistics_meta WHERE statistic_id IN ({ids}))",
                }
                for table in secured:
                    conn.execute(
                        f"CREATE TEMP VIEW {_identifier(table)} AS SELECT * FROM main.{_identifier(table)} WHERE {conditions[table]}"
                    )

            def authorize(action, first, second, database, source):
                if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_FUNCTION:
                    return (
                        sqlite3.SQLITE_DENY
                        if (second or "").lower()
                        in {"load_extension", "readfile", "writefile", "fts3_tokenizer"}
                        else sqlite3.SQLITE_OK
                    )
                if action == sqlite3.SQLITE_READ:
                    if first.lower().startswith("sqlite_"):
                        return sqlite3.SQLITE_DENY
                    if not recorder:
                        return (
                            sqlite3.SQLITE_OK
                            if database == "main" and first in tables
                            else sqlite3.SQLITE_DENY
                        )
                    if database == "temp" and first in secured:
                        return sqlite3.SQLITE_OK
                    # Only generated temp views can read their underlying tables.
                    if database == "main" and first in secured and source in secured:
                        return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            conn.set_authorizer(authorize)
            cursor = conn.execute(query)
            names = [column[0] for column in cursor.description]
            if config.get("single", False):
                row = cursor.fetchone()
                return dict(zip(names, row, strict=True)) if row is not None else None
            result, size = [], 2
            for row in cursor:
                if len(result) >= max_rows:
                    raise ValueError("SQLite结果超出返回行数上限，请增加查询过滤条件")
                item = dict(zip(names, row, strict=True))
                size += len(json.dumps(item, default=str, ensure_ascii=False))
                if size > 128000:
                    raise ValueError("SQLite结果超出128KB上限")
                result.append(item)
            return result
    except sqlite3.Error as err:
        if "interrupted" in str(err).lower() or time.monotonic() > deadline:
            raise ValueError("SQLite查询超时") from err
        if isinstance(err, sqlite3.OperationalError) and (
            "no such table" in str(err) or "no such column" in str(err)
        ):
            raise ValueError("SQLite查询引用的表或列不存在") from err
        raise ValueError("SQLite查询失败或被只读权限策略拒绝") from err
