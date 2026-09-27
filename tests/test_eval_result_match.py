"""result_match 形态等价判分单元测试。

覆盖 P4 遗留的 C-007/C-008 场景：金标把两年 GMV 拼成单行 {y2024, y2025}，
模型生成 GROUP BY 年份的两行结果。数值一致仅形态不同，应判匹配；
数值缺失或不一致时不得因放宽规则而误判通过。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.metrics import result_match


def test_gold_single_row_vs_pred_grouped_rows():
    """C-007 真实形态：金标单行双列，预测按年分组两行且多带维度列。"""

    gold = {"y2025": 2329671.18166399, "y2024": 2047008.255829811}
    pred = [
        {"年份": 2024, "地区": "华东", "GMV": 2047008.255829811},
        {"年份": 2025, "地区": "华东", "GMV": 2329671.18166399},
    ]
    assert result_match(gold, pred)


def test_grouped_rows_string_numbers():
    """SSE 传输可能把数值变成字符串，归一化后仍应匹配。"""

    gold = {"y2025": 5285446.74, "y2024": 4638482.44}
    pred = [
        {"年份": "2024", "GMV": "4638482.44"},
        {"年份": "2025", "GMV": "5285446.74"},
    ]
    assert result_match(gold, pred)


def test_grouped_rows_missing_value_not_matched():
    """预测少了一个金标值（如 WHERE 条件漏了一年）不得通过。"""

    gold = {"y2025": 2329671.18, "y2024": 2047008.26}
    pred = [{"年份": 2025, "GMV": 2329671.18}]
    # 单预测行仍走规则 3 的子集判断：金标值 {2024列值} 缺失 → 不匹配
    assert not result_match(gold, pred)


def test_grouped_rows_wrong_value_not_matched():
    """数值算错（如指标口径用错）不得因形态放宽而通过。"""

    gold = {"y2025": 2329671.18, "y2024": 2047008.26}
    pred = [
        {"年份": 2024, "GMV": 2047008.26},
        {"年份": 2025, "GMV": 9999999.99},
    ]
    assert not result_match(gold, pred)


def test_existing_rules_unchanged():
    """原有三条规则的既有行为保持不变。"""

    # 规则 1：完全一致
    assert result_match({"GMV": 100.0}, [{"GMV": 100.0}])
    # 规则 2：列名不同仅数值一致
    assert result_match({"GMV": 100.0}, [{"销售额": 100.0}])
    # 规则 3：预测多带常量维度列
    assert result_match(
        [{"province": "浙江", "gmv": 100.0}],
        [{"省份": "浙江", "GMV": 100.0, "备注": "常量"}],
    )
    # 多行结果集合一致（忽略行序）
    assert result_match(
        [{"a": 1}, {"a": 2}],
        [{"a": 2}, {"a": 1}],
    )
    # 数值不同仍不匹配
    assert not result_match({"GMV": 100.0}, [{"GMV": 101.0}])


if __name__ == "__main__":
    test_gold_single_row_vs_pred_grouped_rows()
    test_grouped_rows_string_numbers()
    test_grouped_rows_missing_value_not_matched()
    test_grouped_rows_wrong_value_not_matched()
    test_existing_rules_unchanged()
    print("all ok")
