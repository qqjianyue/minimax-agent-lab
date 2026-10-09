"""C11 eval_harness · 数据集加载与校验单元测试。

数据集是评估的"被测对象清单"：加载器校验做错了，后面所有 P/R/F1 都是
在错误的数据上算的。因此这里穷举校验分支：合法加载、重复 id、非法期望
动作、缺字段、坏 JSON、文件缺失。
"""

from __future__ import annotations

import json

import pytest

from eval_harness.dataset import (
    CATEGORY_BENIGN,
    CATEGORY_DATA_LEAK,
    CATEGORY_DIRECT_INJECTION,
    CATEGORY_INDIRECT_INJECTION,
    CATEGORY_TOOL_ABUSE,
    EvalDataset,
    builtin_dataset,
    load_dataset,
)


class TestBuiltinDataset:
    def test_loads_and_has_expected_shape(self) -> None:
        ds = builtin_dataset()
        assert ds.name == "redteam-v1"
        # 四类攻击 + 良性，共 18 条；间接注入 2 条当前 disabled（B8 前不计分）
        assert len(ds.cases) == 18
        assert len(ds.enabled_cases) == 16
        assert len(ds.disabled_cases) == 2

    def test_covers_all_four_attack_categories_plus_benign(self) -> None:
        ds = builtin_dataset()
        cats = {c.category for c in ds.cases}
        assert cats == {
            CATEGORY_DIRECT_INJECTION,
            CATEGORY_INDIRECT_INJECTION,
            CATEGORY_DATA_LEAK,
            CATEGORY_TOOL_ABUSE,
            CATEGORY_BENIGN,
        }

    def test_indirect_injection_is_disabled_until_b8(self) -> None:
        ds = builtin_dataset()
        for case in ds.disabled_cases:
            assert case.category == CATEGORY_INDIRECT_INJECTION
            assert case.expected == "block"

    def test_benign_set_includes_fp_sentinel(self) -> None:
        """良性集特意包含易误触发的样本（如带"忽略"字眼的正常改口）。"""
        ds = builtin_dataset()
        benign = [c for c in ds.cases if c.category == CATEGORY_BENIGN]
        assert len(benign) == 6
        assert any("忽略" in c.text for c in benign)

    def test_load_dataset_none_returns_builtin(self) -> None:
        assert load_dataset(None).name == "redteam-v1"


class TestFromFile:
    def test_loads_valid_json(self, tmp_path) -> None:
        p = tmp_path / "ds.json"
        p.write_text(
            json.dumps(
                {
                    "name": "tiny",
                    "cases": [
                        {"id": "a", "text": "hi", "category": "benign", "expected": "allow"},
                        {"id": "b", "text": "leak", "category": "data_leak", "expected": "block"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        ds = EvalDataset.from_file(p)
        assert ds.name == "tiny"
        assert [c.id for c in ds.cases] == ["a", "b"]

    def test_duplicate_id_rejected(self, tmp_path) -> None:
        p = tmp_path / "dup.json"
        p.write_text(
            json.dumps(
                {
                    "name": "dup",
                    "cases": [
                        {"id": "a", "text": "x", "category": "benign", "expected": "allow"},
                        {"id": "a", "text": "y", "category": "benign", "expected": "allow"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="重复用例 id"):
            EvalDataset.from_file(p)

    def test_invalid_expected_rejected(self, tmp_path) -> None:
        p = tmp_path / "bad.json"
        p.write_text(
            json.dumps(
                {
                    "name": "bad",
                    "cases": [{"id": "a", "text": "x", "category": "benign", "expected": "maybe"}],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="非法的期望动作"):
            EvalDataset.from_file(p)

    def test_missing_cases_key_rejected(self, tmp_path) -> None:
        p = tmp_path / "nocases.json"
        p.write_text(json.dumps({"name": "bad"}), encoding="utf-8")
        with pytest.raises(ValueError, match="'cases'"):
            EvalDataset.from_file(p)

    def test_missing_file_rejected(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="不存在"):
            EvalDataset.from_file(tmp_path / "nope.json")

    def test_malformed_json_rejected(self, tmp_path) -> None:
        p = tmp_path / "broken.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="不是合法 JSON"):
            EvalDataset.from_file(p)

    def test_blank_text_rejected(self, tmp_path) -> None:
        p = tmp_path / "blank.json"
        p.write_text(
            json.dumps(
                {
                    "name": "blank",
                    "cases": [{"id": "a", "text": "  ", "category": "benign", "expected": "allow"}],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="不能为空"):
            EvalDataset.from_file(p)
