"""deploy/lib/deployconfig.py 单元测试。

这个解析器是部署脚本的配置入口，且**故意只用标准库**（目标机没有 jq，
首次安装时 venv 还不存在）。因此它的行为必须被钉死，尤其是两条：

1. **缺键必须报错**，绝不静默回退到默认值 —— 路径配错要在部署前炸掉。
2. **本机路径翻译**：POSIX 写法 ``/workspace/x.yaml`` 在 Windows 上必须翻译成
   ``C:\\workspace\\x.yaml``。不翻译的话 Git Bash 会把它映射到
   ``C:\\Program Files\\Git\\workspace\\``，找不到文件，报错信息还极具误导性。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy" / "lib"))

from deployconfig import (  # noqa: E402
    ConfigError,
    get_value,
    load_config,
    parse_two_level,
    translate_local,
)

REPO_CONFIG = Path(__file__).resolve().parents[2] / "deploy" / "config.yaml"


class TestParse:
    def test_two_level_mapping(self) -> None:
        text = "build:\n  mask-config: /a/b.yaml\nruntime:\n  mask-config: /c/d.yaml\n"
        assert parse_two_level(text) == {
            "build": {"mask-config": "/a/b.yaml"},
            "runtime": {"mask-config": "/c/d.yaml"},
        }

    def test_comments_and_blank_lines_ignored(self) -> None:
        text = "# 注释\n\nbuild:\n  # 另一个注释\n  k: v\n"
        assert parse_two_level(text) == {"build": {"k": "v"}}

    def test_quotes_stripped(self) -> None:
        text = 'build:\n  k: "/with/quotes"\n'
        assert parse_two_level(text) == {"build": {"k": "/with/quotes"}}

    def test_empty_section_allowed(self) -> None:
        assert parse_two_level("build:\nruntime:\n") == {"build": {}, "runtime": {}}

    def test_value_with_colon_preserved(self) -> None:
        """值里带冒号（URL）不能被截断。"""
        text = "build:\n  k: https://api.minimax.cn/v1\n"
        assert parse_two_level(text) == {"build": {"k": "https://api.minimax.cn/v1"}}

    def test_tab_indent_rejected(self) -> None:
        with pytest.raises(ConfigError, match="制表符"):
            parse_two_level("build:\n\tk: v\n")

    def test_toplevel_inline_value_rejected(self) -> None:
        """不支持 `a: b` 顶层写法 —— 避免两种结构混用产生歧义。"""
        with pytest.raises(ConfigError, match="顶层写法"):
            parse_two_level("a: b\n")

    def test_unparseable_line_rejected(self) -> None:
        with pytest.raises(ConfigError, match="无法解析"):
            parse_two_level("build:\n  ???\n")


class TestGetValue:
    CONFIG = {"build": {"mask-config": "/a/b.yaml"}, "runtime": {"mask-config": "/c/d.yaml"}}

    def test_dotted_lookup(self) -> None:
        assert get_value(self.CONFIG, "build.mask-config") == "/a/b.yaml"

    def test_hyphenated_key_supported(self) -> None:
        """键名带连字符 —— 用户配置里就是 ``mask-config``。"""
        assert get_value(self.CONFIG, "runtime.mask-config") == "/c/d.yaml"

    def test_missing_section_lists_available(self) -> None:
        with pytest.raises(ConfigError, match="可用: build, runtime"):
            get_value(self.CONFIG, "nope.mask-config")

    def test_missing_key_lists_available(self) -> None:
        with pytest.raises(ConfigError, match="该 section 可用: mask-config"):
            get_value(self.CONFIG, "build.config")

    def test_empty_value_rejected(self) -> None:
        with pytest.raises(ConfigError, match="值为空"):
            get_value({"build": {"k": ""}}, "build.k")

    def test_wrong_key_format_rejected(self) -> None:
        with pytest.raises(ConfigError, match="section.key"):
            get_value(self.CONFIG, "build")


class TestTranslateLocal:
    def test_posix_path_unchanged_on_unix(self) -> None:
        if os.name == "nt":
            pytest.skip("仅在 Unix 上适用")
        assert str(translate_local("/workspace/mask-config.yaml")) == "/workspace/mask-config.yaml"

    @pytest.mark.skipif(os.name != "nt", reason="仅 Windows 上需要盘符翻译")
    def test_existing_posix_path_resolves_to_windows_path(self, tmp_path) -> None:
        """核心场景：``/workspace/mask-config.yaml`` 必须解析到真实存在的文件。

        不翻译的话 Git Bash 会把它映射到 ``C:\\Program Files\\Git\\workspace\\``，
        报"文件不存在"且完全看不出是路径语义问题。
        """
        drive = os.environ.get("SYSTEMDRIVE", "C:")
        candidate = Path(f"{drive}\\workspace\\mask-config.yaml")
        if not candidate.parent.is_dir():
            pytest.skip("本机没有 C:\\workspace，跳过")
        candidate.write_text("k: v\n", encoding="utf-8")
        try:
            assert translate_local("/workspace/mask-config.yaml") == candidate
        finally:
            candidate.unlink(missing_ok=True)

    @pytest.mark.skipif(os.name != "nt", reason="仅 Windows 上需要盘符翻译")
    def test_nonexistent_path_returned_as_is(self) -> None:
        """都不存在时返回原值 —— 错误信息要如实显示"配置里写的是什么"。"""
        assert (
            str(translate_local("/definitely/not/here/x.yaml")) == "\\definitely\\not\\here\\x.yaml"
        )

    def test_relative_path_untouched(self) -> None:
        assert str(translate_local("mask-config.yaml")) == "mask-config.yaml"

    @pytest.mark.skipif(os.name != "nt", reason="仅 Windows 上需要盘符翻译")
    def test_windows_style_path_untouched(self) -> None:
        """已经是 Windows 绝对路径的，不该被再翻译一次。

        只能在 Windows 上断言：``translate_local`` 在 Unix 上直接原样返回，
        而 ``Path`` 在两个平台上是不同的类（``WindowsPath`` 会把 ``/`` 规范成
        ``\\``，``PosixPath`` 不会）。写成平台无关的断言，就会在目标机上
        以"路径格式不对"这种极具误导性的信息失败 —— 而路径本来是对的。
        """
        assert str(translate_local("C:/workspace/x.yaml")) == "C:\\workspace\\x.yaml"


class TestRealRepoConfig:
    """针对仓库里真实的 deploy/config.yaml —— 它是所有部署脚本的配置入口。"""

    def test_loads(self) -> None:
        config = load_config(REPO_CONFIG)
        assert "build" in config and "runtime" in config

    def test_declares_both_mask_paths(self) -> None:
        config = load_config(REPO_CONFIG)
        assert config["build"]["mask-config"].endswith("mask-config.yaml")
        assert config["runtime"]["mask-config"].endswith("mask-config.yaml")

    def test_runtime_path_is_under_agent_home(self) -> None:
        """runtime 路径必须落在 AGENT_HOME 内，否则部署产物会散落在别处。"""
        runtime = get_value(load_config(REPO_CONFIG), "runtime.mask-config")
        assert runtime.startswith("/data/workspace/minimax-agent/")

    def test_runtime_path_parent_exists_on_target(self) -> None:
        """只校验路径形状与 AGENT_HOME 一致；目标机是否可达由 preflight 负责。"""
        runtime = get_value(load_config(REPO_CONFIG), "runtime.mask-config")
        assert runtime == "/data/workspace/minimax-agent/mask-config.yaml"
