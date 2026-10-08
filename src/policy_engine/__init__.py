"""policy-engine（C3）：把多个检测器的信号求值成一个动作。

本包是"detector signals → policy decisions"这一层的全部实现，
对应架构方案 §5.2 / §5.6 / §5.7 以及 JD 职责 3。

零 I/O、零时钟、零随机 —— 策略正确性可以在完全不碰模型和网络的情况下被
完整验证，这也是 threshold calibration 能够反复进行的前提。
"""

from __future__ import annotations

from policy_engine.decision import Decision
from policy_engine.engine import PolicyEngine
from policy_engine.pipeline import GuardPipeline

__all__ = ["Decision", "GuardPipeline", "PolicyEngine"]
