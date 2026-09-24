"""为数仓执行链路切换只读账号（保留初始 shopkeeper，可回退）。

默认不改连接。创建只读账号后，在 .env 设置：
  MYSQL_DW_USER=shopkeeper_ro
  MYSQL_DW_PASSWORD=shopkeeper_ro_dev

db_meta 继续使用 MYSQL_USER/MYSQL_PASSWORD（初始 shopkeeper，会话与元数据需要写）。
"""

from __future__ import annotations

import os
import sys

SQL_PATH = "docker/mysql/create_dw_readonly.sql"


def print_usage() -> None:
    print(
        "安全步骤（只新增，不动初始账号）:\n"
        "1. 启动 Docker MySQL\n"
        f"2. docker exec -i mysql mysql -uroot -proot_dev < {SQL_PATH}\n"
        "   （脚本仅 CREATE USER IF NOT EXISTS + GRANT SELECT/PROCESS）\n"
        "3. 在 .env 增加两行：\n"
        "     MYSQL_DW_USER=shopkeeper_ro\n"
        "     MYSQL_DW_PASSWORD=shopkeeper_ro_dev\n"
        "4. 重启 FastAPI。\n"
        "回退: 删除第 3 步两行即可继续用初始 shopkeeper；\n"
        "      如需删除 ro 账号: DROP USER IF EXISTS 'shopkeeper_ro'@'%';\n"
        "      以上都不会 DROP/ALTER 初始账号 shopkeeper。"
    )


def verify_env_hint() -> None:
    user = os.getenv("MYSQL_DW_USER", "shopkeeper(默认)")
    print(f"当前 MYSQL_DW_USER={user}")


if __name__ == "__main__":
    if "--hint" in sys.argv:
        verify_env_hint()
    else:
        print_usage()
        verify_env_hint()
