-- P3 只读账号：只新增 shopkeeper_ro，绝不修改/删除现有 shopkeeper。
-- 执行方式（容器需已启动）：
--   docker exec -i mysql mysql -uroot -proot_dev < docker/mysql/create_dw_readonly.sql
-- 回滚（如需）：
--   DROP USER IF EXISTS 'shopkeeper_ro'@'%';  -- 不会影响 shopkeeper

CREATE USER IF NOT EXISTS 'shopkeeper_ro'@'%'
  IDENTIFIED BY 'shopkeeper_ro_dev';

-- 仅数仓库只读；不授予 meta，不授予写权限
GRANT SELECT ON dw.* TO 'shopkeeper_ro'@'%';

-- 允许查看/终止自己的连接（取消查询 KILL 用）；不授予 SUPER
GRANT PROCESS ON *.* TO 'shopkeeper_ro'@'%';

-- 明确不触碰初始账号
-- （此处无 ALTER/DROP/REVOKE shopkeeper 语句）

FLUSH PRIVILEGES;

-- 自检（应能看到 ro 账号且 shopkeeper 仍在）
SELECT user, host FROM mysql.user WHERE user LIKE 'shopkeeper%';
SHOW GRANTS FOR 'shopkeeper_ro'@'%';
