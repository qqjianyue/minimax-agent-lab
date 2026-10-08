"""L1 集成测试的 session 级约束。

## 为什么这里要放开 socket

L0 的默认闸门是**全面禁用 socket**（`conftest.py`）。但 L1 要用
``fastapi.testclient.TestClient`` 走真实 HTTP 契约，而它内部会启动事件循环 ——
事件循环需要自管道（Windows 的 ProactorEventLoop / Unix 的 selector loop
都会建 socketpair），在全面禁用的前提下根本跑不起来。

## 为什么不直接放开

直接 ``enable_socket()`` 等于把"离线"保证丢掉：任何测试误用真实客户端都会
悄悄发出真实请求，结果就会依赖外网状态，变得不可复现。

## 这里采取的折中

放开 socket，但**替换 ``socket.connect`` 只允许回环地址**。于是：

* 事件循环的内部管道、TestClient 的本地通信 → 正常；
* 任何指向真实主机（MiniMax、PyPI、公网 IP）的连接 → 立刻抛错。

比"全面禁用"或"全面放开"都更贴近我们真正想要的那条线。
"""

from __future__ import annotations

import socket

import pytest
from pytest_socket import disable_socket, enable_socket

#: 仅允许这些地址。它们是本机回环，不会离开这台机器。
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0"})


def _host_of(address) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return str(address)


@pytest.fixture(scope="session", autouse=True)
def _loopback_only():
    """允许本机回环，阻断一切真实出网。"""
    enable_socket()
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _guard(real):
        def wrapper(self, address, *args, **kwargs):
            host = _host_of(address)
            if host not in _ALLOWED_HOSTS:
                raise AssertionError(
                    f"L1 集成测试试图连接非回环地址 {host!r}。"
                    "集成测试必须使用 Fake 依赖；如果这是 L3，用例应标记为 target。"
                )
            return real(self, address, *args, **kwargs)

        return wrapper

    socket.socket.connect = _guard(real_connect)
    socket.socket.connect_ex = _guard(real_connect_ex)
    try:
        yield
    finally:
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex
        disable_socket()
