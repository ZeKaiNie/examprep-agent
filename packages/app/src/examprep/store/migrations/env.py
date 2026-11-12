"""Alembic 迁移环境。

连接由调用方通过 ``config.attributes["connection"]`` 传入（见 ``Database.migrate``），这里不读 alembic.ini、
不配置日志。SQLite 改表用 batch 模式。
"""

from __future__ import annotations

from alembic import context

from examprep.store.models import Base

connection = context.config.attributes["connection"]
context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    render_as_batch=True,
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
