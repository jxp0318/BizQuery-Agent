# 一键启动依赖服务

在项目根目录执行（Windows）：

```powershell
docker-compose -f docker/docker-compose.yaml up -d
```

一次启动（同一 compose 项目）：

| 服务 | 端口 |
| --- | --- |
| redis | 6379 |
| mysql | 3306 |
| elasticsearch | 9200 |
| kibana | 5601 |
| qdrant | 6333-6334 |
| embedding | 8081 |

停止全部：

```powershell
docker-compose -f docker/docker-compose.yaml down
```

只启 redis / mysql：

```powershell
docker-compose -f docker/docker-compose.yaml up -d redis mysql
```

说明：

- 本机使用独立 **`docker-compose`**（v5.x）；`docker compose` 可能报 unknown command。
- **不要**对本项目容器使用单独 `docker run --name redis`，否则会脱离 compose 项目，一键启停会漏掉它。
