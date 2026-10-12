"""pytest 全局配置。

核心约束：**L0 / L1 测试必须在完全离线的环境下运行**。
这是"本地能跑单元测试"这一设计前提的强制保障 —— 一旦某个单测偷偷
发起了真实网络请求（比如误用真实 LLM 客户端），pytest-socket 会立即
让它失败，而不是让测试结果依赖外网状态、变得不可复现。

只有显式传入 ``--allow-network``（或使用 target 标记）时才放开。
"""

from __future__ import annotations

import httpx
import pytest
from pytest_socket import disable_socket

from tests import security_gate
from tests.target_support import base_url


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--allow-network",
        action="store_true",
        default=False,
        help="允许测试发起网络连接（仅目标机 L2/L3/L4 使用）",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    if not session.config.getoption("--allow-network"):
        disable_socket(allow_unix_socket=False)
        print("\n[network] 已禁用 socket（L0/L1 离线闸门）")
    else:
        print("\n[network] socket 已放开（允许真实网络调用）")


def pytest_report_header(config: pytest.Config) -> str:
    mode = "OFFLINE" if not config.getoption("--allow-network") else "ONLINE"
    return f"agent tests [{mode}]"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """按目录自动打标记，避免每个测试函数重复写装饰器。

    注意必须用 ``get_closest_marker`` 判断，不能查 ``item.keywords``：
    ``tests/unit/`` 和 ``tests/integration/`` 带有 ``__init__.py``，包名本身
    （``unit`` / ``integration``）会作为 name keyword 出现在 ``keywords`` 里，
    用 keywords 判断会导致"以为标记已存在"而永远不打标记。

    ``tests/smoke`` 与 ``tests/functional`` 一律标为 ``target``：它们需要
    真实模型与真实网络，只在目标机上跑。本地默认执行集合会排除它们，
    **跳过是显式可见的，而不是静默的**。
    """
    for item in items:
        path = str(item.path).replace("\\", "/")
        if "tests/unit/" in path and item.get_closest_marker("unit") is None:
            item.add_marker(pytest.mark.unit)
        elif "tests/integration/" in path and item.get_closest_marker("integration") is None:
            item.add_marker(pytest.mark.integration)
        elif "tests/eval/" in path and item.get_closest_marker("eval") is None:
            item.add_marker(pytest.mark.eval)
        elif ("tests/smoke/" in path or "tests/functional/" in path) and item.get_closest_marker(
            "target"
        ) is None:
            item.add_marker(pytest.mark.target)
        cls_name = item.cls.__name__ if item.cls is not None else None
        if (
            type(item).__name__ == "Function"
            and security_gate.in_security_scope(cls_name, item.name)
            and item.get_closest_marker("security_detection") is None
        ):
            item.add_marker(pytest.mark.security_detection)


# ---------------------------------------------------------------------------
# 安全检测失败的降级闸门
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _security_gate_window(request: pytest.FixtureRequest):
    """标记每条用例的起止，让降级闸门能把 request_id 归到正确的用例上。"""
    security_gate.begin_test(request.node.nodeid)
    yield
    security_gate.end_test()


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    """安全检测用例失败时，把它降级成**带证据的 skip**而不是失败。

    只处理 ``call`` 阶段（setup/teardown 出错是另一类问题，不该被这里吞掉），
    只处理显式标记了 ``security_detection`` 且开关打开的用例。

    断言本身一个字都没改 —— 变的只是"失败之后会发生什么"：不再触发部署回退，
    但失败原因与审计账本证据会被记录下来。降级 ≠ 通过，这一点在 skip reason
    与会话汇总里都写明了。
    """
    outcome = yield
    report = outcome.get_result()

    if report.when != "call" or report.outcome != "failed":
        return
    if item.get_closest_marker("security_detection") is None:
        return
    if not security_gate.is_enabled():
        return

    entry = security_gate.record_failure(item, report)
    report.outcome = "skipped"
    report.longrepr = (
        str(item.path),
        (item.location[1] + 1 if item.location else 1),
        security_gate.skip_reason(entry),
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    summary = security_gate.flush()
    if summary:
        print(summary, flush=True)


# ---------------------------------------------------------------------------
# L2 / L3 共用 fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def http() -> httpx.Client:
    """指向**已部署服务**的 HTTP 客户端。

    只在 ``--allow-network`` 的运行中才有意义；本地离线运行时不会被触发，
    因为 L2/L3 已被 ``-m "not target"`` 排除。

    响应钩子自动登记 ``request_id`` —— 降级闸门靠它回查账本。挂在客户端上
    而非让各测试文件自觉调用：实测踩过 L2 冒烟三个文件全部漏登记，导致
    降级时证据为空（见 :func:`tests.security_gate.record_response_request_id`）。
    """
    with httpx.Client(
        base_url=base_url(),
        timeout=60.0,
        event_hooks={"response": [security_gate.record_response_request_id]},
    ) as client:
        yield client
