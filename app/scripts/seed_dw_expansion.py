"""P4 前置：教学数仓扩表灌数（只写 dw）。

按 docs/p4-warehouse-expansion.md：
1. fact_order 增加 order_status
2. dim_date 铺 2024-01-01～2025-12-31
3. 扩省 / 客户 / 商品
4. 生成约 6k～8k 笔订单（含退款/取消状态、月度波动）

设计说明：
- 使用可写账号执行（Docker 演示默认 root）；问数链路仍用 shopkeeper_ro。
- 固定 random seed，同一环境可复现，便于评测金标稳定。
- 幂等：按脚本内开关 rebuild，可重复执行。
"""

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# 确定性随机，保证评测数据可复现
SEED = 20250924

STATUSES = ["已支付", "已退款", "已取消"]
STATUS_WEIGHTS = [0.88, 0.09, 0.03]

REGIONS = [
    # (region_id, province, region_name, country)
    ("R001", "广东省", "华南", "中国"),
    ("R002", "浙江省", "华东", "中国"),
    ("R003", "四川省", "西南", "中国"),
    ("R004", "北京市", "华北", "中国"),
    ("R005", "上海市", "华东", "中国"),
    ("R006", "湖北省", "华中", "中国"),
    ("R007", "江苏省", "华东", "中国"),
    ("R008", "山东省", "华东", "中国"),
    ("R009", "河南省", "华中", "中国"),
    ("R010", "湖南省", "华中", "中国"),
    ("R011", "福建省", "华东", "中国"),
    ("R012", "安徽省", "华东", "中国"),
    ("R013", "河北省", "华北", "中国"),
    ("R014", "天津市", "华北", "中国"),
    ("R015", "辽宁省", "东北", "中国"),
    ("R016", "吉林省", "东北", "中国"),
    ("R017", "黑龙江省", "东北", "中国"),
    ("R018", "陕西省", "西北", "中国"),
    ("R019", "甘肃省", "西北", "中国"),
    ("R020", "重庆市", "西南", "中国"),
    ("R021", "云南省", "西南", "中国"),
    ("R022", "贵州省", "西南", "中国"),
    ("R023", "广西", "华南", "中国"),
    ("R024", "海南省", "华南", "中国"),
    ("R025", "江西省", "华东", "中国"),
    ("R026", "山西省", "华北", "中国"),
    ("R027", "内蒙古", "华北", "中国"),
    ("R028", "新疆", "西北", "中国"),
    ("R029", "宁夏", "西北", "中国"),
    ("R030", "青海省", "西北", "中国"),
]

MEMBER_LEVELS = ["青铜", "白银", "黄金", "铂金", "钻石"]
MEMBER_WEIGHTS = [0.3, 0.28, 0.22, 0.14, 0.06]

CATEGORIES = [
    ("手机数码", ["苹果", "三星", "华为", "小米", "OPPO"], 15),
    ("家用电器", ["美的", "海尔", "戴森", "格力"], 12),
    ("鞋靴", ["耐克", "阿迪达斯", "李宁", "安踏"], 12),
    ("服饰", ["优衣库", "李维斯", "ZARA", "海澜之家"], 14),
    ("食品饮料", ["雀巢", "蒙牛", "伊利", "农夫山泉"], 12),
    ("休闲零食", ["乐事", "奥利奥", "三只松鼠", "良品铺子"], 10),
    ("美妆个护", ["欧莱雅", "兰蔻", "完美日记"], 8),
    ("图书文娱", ["人民文学", "机械工业", "中信"], 7),
    ("运动户外", ["迪卡侬", "始祖鸟", "李宁"], 6),
    ("家居日用", ["宜家", "名创优品", "苏泊尔"], 4),
]

# 品类价带（单价区间）
PRICE_BAND = {
    "手机数码": (399, 9999),
    "家用家电": (199, 5999),
    "家用电器": (199, 5999),
    "鞋靴": (199, 1599),
    "服饰": (79, 899),
    "食品饮料": (9, 199),
    "休闲零食": (5, 99),
    "美妆个护": (49, 999),
    "图书文娱": (15, 129),
    "运动户外": (99, 2599),
    "家居日用": (19, 699),
}


def _daterange(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def _month_weight(year: int, month: int) -> float:
    """Q4 偏高、春节月偏低，制造同比/趋势题可观察差异。"""

    base = 1.0
    if month in (11, 12):
        base *= 1.35
    if month in (5, 6):
        base *= 1.12
    if month == 2:
        base *= 0.72
    if year == 2025:
        base *= 1.08  # 同比整体略增
    return base


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db-url",
        default="mysql+asyncmy://root:root_dev@localhost:3306/dw?charset=utf8mb4",
        help="可写账号连接 dw；生产勿用 root",
    )
    parser.add_argument("--orders", type=int, default=7000)
    parser.add_argument("--rebuild", action="store_true", help="清空并重建维表与订单")
    args = parser.parse_args()

    rng = random.Random(SEED)
    engine = create_async_engine(args.db_url, pool_pre_ping=True)

    async with engine.begin() as conn:
        await conn.execute(text("SET NAMES utf8mb4"))

        # 1) order_status（MySQL 不一定支持 ADD COLUMN IF NOT EXISTS，先查再加）
        has_status = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'fact_order' "
                    "AND COLUMN_NAME = 'order_status'"
                )
            )
        ).scalar()
        if not has_status:
            await conn.execute(
                text(
                    "ALTER TABLE fact_order "
                    "ADD COLUMN order_status VARCHAR(20) "
                    "NOT NULL DEFAULT '已支付' COMMENT '订单状态'"
                )
            )

        # 2) rebuild or expand dims
        if args.rebuild:
            await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
            for t in (
                "fact_order",
                "dim_date",
                "dim_region",
                "dim_customer",
                "dim_product",
            ):
                await conn.execute(text(f"TRUNCATE TABLE {t}"))
            await conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))

        # dim_date 2024-01-01 ~ 2025-12-31
        dates = list(_daterange(date(2024, 1, 1), date(2025, 12, 31)))
        for d in dates:
            date_id = int(d.strftime("%Y%m%d"))
            q = f"Q{(d.month - 1) // 3 + 1}"
            await conn.execute(
                text(
                    "INSERT INTO dim_date (date_id, year, quarter, month, day) "
                    "VALUES (:id, :y, :q, :m, :d) "
                    "ON DUPLICATE KEY UPDATE year=VALUES(year), quarter=VALUES(quarter), "
                    "month=VALUES(month), day=VALUES(day)"
                ),
                {"id": date_id, "y": d.year, "q": q, "m": d.month, "d": d.day},
            )

        for rid, prov, region, country in REGIONS:
            await conn.execute(
                text(
                    "INSERT INTO dim_region (region_id, province, region_name, country) "
                    "VALUES (:id, :p, :r, :c) "
                    "ON DUPLICATE KEY UPDATE province=VALUES(province), "
                    "region_name=VALUES(region_name), country=VALUES(country)"
                ),
                {"id": rid, "p": prov, "r": region, "c": country},
            )

        # customers 250
        surnames = list("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦许何吕施张")
        for i in range(1, 251):
            cid = f"C{i:03d}"
            gender = "男" if i % 2 == 0 else "女"
            level = rng.choices(MEMBER_LEVELS, MEMBER_WEIGHTS)[0]
            name = rng.choice(surnames) + ("先生" if gender == "男" else "女士")
            await conn.execute(
                text(
                    "INSERT INTO dim_customer (customer_id, customer_name, gender, member_level) "
                    "VALUES (:id, :n, :g, :lv) "
                    "ON DUPLICATE KEY UPDATE customer_name=VALUES(customer_name), "
                    "gender=VALUES(gender), member_level=VALUES(member_level)"
                ),
                {"id": cid, "n": f"{name}{i:03d}", "g": gender, "lv": level},
            )

        # products
        product_keys = []
        pid = 1
        for category, brands, count in CATEGORIES:
            for j in range(count):
                brand = brands[j % len(brands)]
                product_id = f"P{pid:03d}"
                product_name = f"{brand}{category}商品{j + 1:02d}"
                await conn.execute(
                    text(
                        "INSERT INTO dim_product (product_id, product_name, category, brand) "
                        "VALUES (:id, :n, :c, :b) "
                        "ON DUPLICATE KEY UPDATE product_name=VALUES(product_name), "
                        "category=VALUES(category), brand=VALUES(brand)"
                    ),
                    {"id": product_id, "n": product_name, "c": category, "b": brand},
                )
                product_keys.append((product_id, category))
                pid += 1

        if args.rebuild:
            await conn.execute(text("DELETE FROM fact_order"))

        # 3) fact_order：按月配额生成
        months = []
        for y in (2024, 2025):
            for m in range(1, 13):
                months.append((y, m))
        weights = [_month_weight(y, m) for y, m in months]
        total_w = sum(weights)

        customer_ids = [f"C{i:03d}" for i in range(1, 251)]
        region_ids = [r[0] for r in REGIONS]
        date_by_month: dict[tuple[int, int], list[int]] = {}
        for d in dates:
            key = (d.year, d.month)
            date_by_month.setdefault(key, []).append(int(d.strftime("%Y%m%d")))

        rows = []
        seq = 0
        for (y, m), w in zip(months, weights):
            n = max(8, int(args.orders * w / total_w))
            days = date_by_month[(y, m)]
            for _ in range(n):
                seq += 1
                date_id = rng.choice(days)
                product_id, category = rng.choice(product_keys)
                lo, hi = PRICE_BAND.get(category, (20, 2000))
                unit = round(rng.uniform(lo, hi), 2)
                qty = rng.choices([1, 2, 3, 4, 5], [0.55, 0.22, 0.12, 0.07, 0.04])[0]
                amount = round(unit * qty, 2)
                # 退款在部分品类/月份略高
                w_status = list(STATUS_WEIGHTS)
                if category in ("鞋靴", "服饰") and m in (11, 12, 1):
                    w_status = [0.78, 0.18, 0.04]
                status = rng.choices(STATUSES, w_status)[0]
                order_id = f"ORD{y}{m:02d}{seq:05d}"
                rows.append(
                    {
                        "order_id": order_id,
                        "customer_id": rng.choice(customer_ids),
                        "product_id": product_id,
                        "date_id": date_id,
                        "region_id": rng.choice(region_ids),
                        "order_quantity": qty,
                        "order_amount": amount,
                        "order_status": status,
                    }
                )

        # 批量插入
        batch = 200
        for i in range(0, len(rows), batch):
            chunk = rows[i : i + batch]
            await conn.execute(
                text(
                    "INSERT INTO fact_order "
                    "(order_id, customer_id, product_id, date_id, region_id, "
                    "order_quantity, order_amount, order_status) VALUES "
                    "(:order_id, :customer_id, :product_id, :date_id, :region_id, "
                    ":order_quantity, :order_amount, :order_status)"
                ),
                chunk,
            )

        # 4) 汇总校验
        summary = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) AS orders, "
                    "SUM(order_status='已支付') AS paid, "
                    "SUM(order_status='已退款') AS refunded, "
                    "SUM(order_status='已取消') AS cancelled, "
                    "MIN(date_id) AS min_d, MAX(date_id) AS max_d FROM fact_order"
                )
            )
        ).mappings().first()
        dims = (
            await conn.execute(
                text(
                    "SELECT (SELECT COUNT(*) FROM dim_date) AS dates, "
                    "(SELECT COUNT(*) FROM dim_region) AS regions, "
                    "(SELECT COUNT(*) FROM dim_customer) AS customers, "
                    "(SELECT COUNT(*) FROM dim_product) AS products"
                )
            )
        ).mappings().first()

    await engine.dispose()
    print("seed done:", dict(summary or {}), dict(dims or {}))


if __name__ == "__main__":
    asyncio.run(main())
