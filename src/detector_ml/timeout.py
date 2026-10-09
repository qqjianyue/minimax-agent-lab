"""检测器墙钟超时执行。

B2 的注释承诺："真正的按墙钟超时强制执行随 B5 的真实检测器（Presidio /
sentence-transformers / LLM judge）落地。" 本模块兑现这个承诺。

**为什么是 daemon 线程 + 结果队列，而不是 signal / asyncio / 线程池**：

- ``signal.alarm`` 只在主线程、POSIX 有效，Windows 本地测试直接失效；
- 检测器既有重 CPU（Presidio NER、嵌入推理）也有网络 IO（LLM judge），
  ``asyncio.wait_for`` 兜不住同步阻塞的 CPU 调用；
- ``ThreadPoolExecutor`` 的工作线程是**非 daemon** 的：挂起的检测器会让
  解释器在退出时永久等待它 —— 一个超时用例就能把整个测试进程拖死。
  手动 ``threading.Thread(daemon=True)`` 保证超时后的 worker 不阻塞进程退出。

**超时的语义是"丢弃结果"，不是"杀死检测器"**：Python 无法安全中断线程，
超时后底层 worker 可能仍在跑（结果被丢弃，daemon 线程随进程退出被回收）。
对审计与决策没有影响：超时即 :class:`DetectorTimeoutError`，由 pipeline
分类为 ``TIMEOUT`` 交给策略引擎按 fail_mode 处理。
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from guard_contract.port import DetectorTimeoutError
from guard_contract.result import DetectorResult


def run_detector_with_timeout(
    worker: Callable[[], tuple[DetectorResult, ...]],
    *,
    timeout_ms: int | None,
    detector_name: str,
) -> tuple[DetectorResult, ...]:
    """在墙钟超时内执行一次检测调用。

    Args:
        worker: 实际检测调用（无参闭包）。
        timeout_ms: 超时上限（毫秒）。None 或 <= 0 表示不设超时。
        detector_name: 超时报错里的检测器名（pipeline 会把 detail 写进审计）。

    Raises:
        DetectorTimeoutError: 超过时限未返回。
    """
    if timeout_ms is None or timeout_ms <= 0:
        return worker()

    box: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

    def _run() -> None:
        try:
            box.put(("ok", worker()))
        except Exception as exc:  # noqa: BLE001 - 传播检测器业务异常（ERROR 分类）
            box.put(("error", exc))

    runner = threading.Thread(
        target=_run,
        name=f"detector-{detector_name}",
        daemon=True,
    )
    runner.start()
    try:
        status, payload = box.get(timeout=timeout_ms / 1000.0)
    except queue.Empty as exc:
        raise DetectorTimeoutError(
            f"检测器 {detector_name} 超过 {timeout_ms}ms 未返回"
        ) from exc
    if status == "error":
        raise payload  # type: ignore[misc]
    return payload  # type: ignore[return-value]


__all__ = ["run_detector_with_timeout"]
