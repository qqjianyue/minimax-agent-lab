"""策略求值引擎。

纯函数：``(PolicySet, results, failures) -> Decision``，无 I/O、无时钟、无随机，
因此可以对着所有分支穷举测试。

---

## 核心设计决策：正向命中优先于 fail_mode

直觉上"fail-closed"意味着检测器一挂就全阻断，但那会让系统在任一检测器
偶发故障时完全不可用 —— 银行场景同样不可接受。

本引擎采用的语义是：

1. **能用就按检测结果判**。若某条规则命中了（哪怕同时有别的检测器失败），
   就返回该规则的动作。检测器正常给出的信号不该被"另一个检测器挂了"抹掉。
2. **判不出来才降级**。只有当没有任何规则命中、且确实有检测器失败时，
   才应用 fail_mode。

用一句话概括：**fail_mode 管的是"没有结论"的时候，不是"有结论"的时候。**
"""

from __future__ import annotations

from collections.abc import Sequence

from guard_contract.enums import FailMode, GuardStage, PolicyAction
from guard_contract.policy_schema import PolicySet
from guard_contract.port import DetectorFailure
from guard_contract.result import DetectorResult
from policy_engine.decision import Decision

_REASON_OUTAGE = "所有检测器均不可用，无法完成安全检查（fail-closed）"
_REASON_CLOSED = "检测器不可用且未能完成检查，按 fail-closed 阻断"
_REASON_OPEN = "检测器不可用，按 fail-open 放行（需事后补检）"
_REASON_DEGRADED = "检测器不可用，已降级至受信任检测器仍未命中规则"
_REASON_DEFAULT = "未命中任何策略规则，走 default_action"


class PolicyEngine:
    """把多个检测器的信号求值成一个动作。"""

    def __init__(self, policy: PolicySet) -> None:
        self._policy = policy
        # 排序一次求值多次：策略是配置，加载后不变
        self._rules = policy.enabled_rules()

    @property
    def policy(self) -> PolicySet:
        return self._policy

    def _has_trusted_success(self, succeeded: Sequence[str]) -> bool:
        """降级模式下是否还有"受信任且执行成功"的检测器。

        两个条件缺一不可：执行成功（没挂）**且**在 ``degraded_detectors``
        白名单里。只满足前者不够 —— 运营在白名单里划定的就是"降级时我认谁"，
        白名单外的检测器即使跑成功了，它的"干净"结论也不作数。
        """
        return any(self._policy.trusts_in_degraded_mode(name) for name in succeeded)

    def evaluate(
        self,
        *,
        request_id: str,
        stage: GuardStage,
        results: Sequence[DetectorResult] = (),
        failures: Sequence[DetectorFailure] = (),
        attempted_detectors: Sequence[str] = (),
    ) -> Decision:
        """求值。

        Args:
        results: 成功执行并返回了结果的检测器输出。
        failures: 未能给出结论的检测器。
        attempted_detectors: 流水线实际尝试过的全部检测器名。

            **这个参数不能省。** 没有它就无法区分两种 ``results`` 都为空的情况：
            "L1 跑完了、什么也没发现" 与 "所有检测器都挂了"。前者是一次**成功**
            的检查（结论是安全），后者根本没能完成检查。DEGRADED 模式必须据此
            区分，否则每次 L4 挂掉 + L1 判定干净，都会被误判成"完全不可用"而
            无故阻断 —— 降级机制反而变成了阻断机制。

            不传时从 ``results`` 推断（等价于认为"产出结果的检测器就是成功的
            检测器"）。这在直接调用引擎的单元测试里够用，生产路径请始终传入。
        """
        considered: tuple[DetectorResult, ...] = tuple(results)
        ignored: tuple[str, ...] = ()
        failed_names = {f.detector for f in failures}

        # 降级：先把不受信任的检测器结果剔掉，再看剩下的够不够判
        if failures and self._policy.fail_mode is FailMode.DEGRADED:
            kept = tuple(r for r in considered if self._policy.trusts_in_degraded_mode(r.detector))
            considered = kept
            kept_names = {r.detector for r in kept}
            ignored = tuple(sorted({r.detector for r in results} - kept_names))

        # 成功给出结论的检测器。注意"成功"包括结论为"什么都没发现" ——
        # 这与"执行失败"是两回事，正是 DEGRADED 模式必须区分的状态。
        if attempted_detectors:
            succeeded = [d for d in attempted_detectors if d not in failed_names]
        else:
            succeeded = list(dict.fromkeys(r.detector for r in considered))

        common = {
            "stage": stage,
            "request_id": request_id,
            "policy_version": self._policy.version,
            "use_case": self._policy.use_case,
            "tenant": self._policy.tenant,
            "failed_detectors": tuple(failures),
            "attempted_detectors": tuple(attempted_detectors),
            "considered_detectors": tuple(dict.fromkeys(r.detector for r in considered)),
            "ignored_detectors": ignored,
        }

        # --- 1. 规则命中优先 ---
        for rule in self._rules:
            if any(rule.matches(result) for result in considered):
                return Decision(
                    action=rule.action,
                    reason=rule.reason,
                    results=considered,
                    rule_name=rule.name,
                    matched=True,
                    fail_mode=self._policy.fail_mode if failures else None,
                    **common,
                )

        # --- 2. 没命中规则 ---
        if failures:
            fail_mode = self._policy.fail_mode
            if fail_mode is FailMode.DEGRADED and not self._has_trusted_success(succeeded):
                # 没有任何"受信任且执行成功"的检测器 —— 这不是"降级"，是彻底不可用。
                # degraded_detectors 为空 / 未配置时同样落在这里：运营没声明降级时
                # 该信谁，我们就不擅自替他们决定。
                return Decision(
                    action=PolicyAction.BLOCK,
                    reason=_REASON_OUTAGE,
                    results=(),
                    fail_mode=fail_mode,
                    **common,
                )
            if fail_mode is FailMode.CLOSED:
                return Decision(
                    action=PolicyAction.BLOCK,
                    reason=_REASON_CLOSED,
                    results=considered,
                    fail_mode=fail_mode,
                    **common,
                )
            if fail_mode is FailMode.OPEN:
                return Decision(
                    action=PolicyAction.ALLOW,
                    reason=_REASON_OPEN,
                    results=considered,
                    fail_mode=fail_mode,
                    **common,
                )
            # DEGRADED 且仍有检测器成功执行：接受降级后的结论
            return Decision(
                action=self._policy.default_action,
                reason=_REASON_DEGRADED,
                results=considered,
                fail_mode=fail_mode,
                **common,
            )

        return Decision(
            action=self._policy.default_action,
            reason=_REASON_DEFAULT,
            results=considered,
            **common,
        )


__all__ = ["PolicyEngine"]
