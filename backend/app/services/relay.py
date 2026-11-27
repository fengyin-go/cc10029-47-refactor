"""跨模块派发接力流程：处理签收请求 → 递交待办 → 记回执。

质量简报签收后派发复盘会议、集体备课签阅后派发同行评议，原本各自写了一遍
同样的派发顺序。这里把顺序收拢到共用的接力流程，各业务只保留自己的触发
条件和待办内容：

1. ``handle_request``：处理签收/签阅请求，把来源单据推进到目标状态；
2. ``submit_pending``：在目标模块递交一条待办，先以“待分发”占位；
3. ``record_receipt``：待办完整受理后转为可办理状态，写入来源标记并登记回执。

派发中途失败时（例如来源状态已更新，但待办还没发出），占位待办和回执都会
保留下来：重新提交原动作即可从中断处续办，已完整受理的派发不会因为重复
提交而生成第二份待办。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from app.store import store

# 待办递交后、记回执前的中转状态。处于该状态的待办还不能办理。
PROVISIONAL_STATUS = "待分发"

STATE_PENDING = "待分发"
STATE_ACCEPTED = "已受理"
STATE_ABORTED = "派发中止"
STATE_NOT_STARTED = "未派发"
STATE_NOT_REQUIRED = "无需派发"

_RELAY_INTERNAL_KEYS = ("_relay_state", "_relay_source", "_relay_receipt_id")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RelayLedger:
    """派发回执台账：记录每次接力从发起到完整受理的过程。"""

    def __init__(self) -> None:
        self._receipts: list[dict[str, Any]] = []

    def rows(self) -> list[dict[str, Any]]:
        return list(self._receipts)

    def find(
        self, source_module: str, source_id: int, action: str
    ) -> dict[str, Any] | None:
        """查找同一来源、同一动作的最新一条回执。"""
        for receipt in reversed(self._receipts):
            if (
                receipt.get("source_module") == source_module
                and int(receipt.get("source_id", 0)) == int(source_id)
                and receipt.get("action") == action
            ):
                return receipt
        return None

    def begin(
        self,
        *,
        source_module: str,
        source_id: int,
        action: str,
        target_module: str,
        target_entity: str,
        source_ref: str,
        marker_field: str,
    ) -> dict[str, Any]:
        """开始（或续办）一次派发，返回可继续写入的回执。"""
        receipt = self.find(source_module, source_id, action)
        if receipt is None:
            receipt = {
                "id": max((int(r.get("id", 0)) for r in self._receipts), default=0) + 1,
                "source_module": source_module,
                "source_id": int(source_id),
                "action": action,
                "target_module": target_module,
                "target_entity": target_entity,
                "source_ref": source_ref,
                "marker_field": marker_field,
                "target_id": None,
                "state": STATE_PENDING,
                "error": "",
                "attempts": 0,
                "created_at": _now(),
                "accepted_at": "",
                "updated_at": _now(),
                "receipt": {},
                "qualification": {},
            }
            self._receipts.append(receipt)
        # 续办时只清掉上一次的失败痕迹，来源和已递交的待办保持不变。
        receipt["state"] = STATE_PENDING
        receipt["error"] = ""
        receipt["attempts"] = int(receipt.get("attempts", 0)) + 1
        receipt["updated_at"] = _now()
        return receipt

    def clear(self) -> None:
        self._receipts.clear()

    def for_target(self, target_module: str, target_id: int) -> list[dict[str, Any]]:
        return [
            receipt for receipt in self._receipts
            if receipt.get("target_module") == target_module
            and receipt.get("target_id") is not None
            and int(receipt.get("target_id", 0)) == int(target_id)
        ]

    def acceptance_for_target(
        self, target_module: str, target_id: int, row: dict[str, Any]
    ) -> bool | None:
        """判断目标待办是否来自接力且已完整受理。

        返回 ``None`` 表示这条待办不是接力生成的（例如示例数据），沿用原有
        业务规则；返回 ``False`` 表示接力尚未完整受理，不能办理。
        """
        receipts = self.for_target(target_module, target_id)
        if not receipts:
            return None
        receipt = receipts[-1]
        if receipt.get("state") != STATE_ACCEPTED:
            return False
        marker_field = str(receipt.get("marker_field", ""))
        source_ref = str(receipt.get("source_ref", ""))
        if not marker_field or source_ref not in str(row.get(marker_field, "")):
            return False
        if _is_provisional(row):
            return False
        return True


relay_ledger = RelayLedger()


def _is_provisional(row: dict[str, Any]) -> bool:
    return bool(row.get("_relay_state")) or row.get("status") == PROVISIONAL_STATUS


def target_relay_info(target_module: str, row: dict[str, Any]) -> dict[str, Any]:
    """返回目标待办的接收信息：来源、回执和办理资格。"""
    target_id = int(row.get("id", 0))
    receipts = relay_ledger.for_target(target_module, target_id)
    latest = receipts[-1] if receipts else None
    accepted = relay_ledger.acceptance_for_target(target_module, target_id, row)
    return {
        "module": target_module,
        "entry_id": target_id,
        "from_relay": latest is not None,
        "accepted": accepted,
        "complete": accepted is True,
        "state": latest.get("state") if latest else "",
        "source": latest.get("source_ref", "") if latest else "",
        "source_module": latest.get("source_module", "") if latest else "",
        "source_id": latest.get("source_id") if latest else None,
        "action": latest.get("action", "") if latest else "",
        "receipt": latest.get("receipt", {}) if latest else {},
        "qualification": latest.get("qualification", {}) if latest else {},
    }


def target_acceptance(target_module: str, row: dict[str, Any]) -> bool | None:
    """目标模块判断一条待办是否已完整受理（None 表示非接力生成）。"""
    return relay_ledger.acceptance_for_target(
        target_module, int(row.get("id", 0)), row
    )


def guard_target_action(
    target_module: str, entry: dict[str, Any], entity: str, action: str
) -> str | None:
    """接力待办未完整受理时，阻止任何办理动作。返回拦截说明。"""
    if target_acceptance(target_module, entry) is False:
        return (
            f"{entity} {entry.get('id')} 由派发生成但尚未完整受理，"
            f"暂不可{action}，请待来源派发完成后再办理"
        )
    return None


class RelayFlow:
    """一条“来源动作 → 目标待办”的共用接力流程。"""

    def __init__(
        self,
        *,
        source_module: str,
        source_entity: str,
        action: str,
        source_final_status: str,
        source_status_order: list[str],
        target_module: str,
        target_entity: str,
        target_initial_status: str,
        target_status_order: list[str],
        marker_field: str,
        build_pending: Callable[[dict[str, Any], int], dict[str, Any]],
    ) -> None:
        self.source_module = source_module
        self.source_entity = source_entity
        self.action = action
        self.source_final_status = source_final_status
        self.source_status_order = source_status_order
        self.target_module = target_module
        self.target_entity = target_entity
        self.target_initial_status = target_initial_status
        self.target_status_order = target_status_order
        self.marker_field = marker_field
        self.build_pending = build_pending

    # ------------------------------------------------------------------
    # 三个共用步骤
    # ------------------------------------------------------------------
    def handle_request(
        self, entry: dict[str, Any], receipt: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """处理签收请求：把来源单据推进到目标状态。

        状态只向前推进、不重复执行：续办时来源单据已经是目标状态（或已推进
        到后续状态，例如简报已传达），直接沿用，不回退。
        """
        if not self._is_signed(entry):
            entry["status"] = self.source_final_status
            entry["pending"] = self.source_final_status != self.source_status_order[-1]
            entry["abnormal"] = False
        return entry

    def submit_pending(
        self, entry: dict[str, Any], entry_id: int, receipt: dict[str, Any]
    ) -> dict[str, Any]:
        """递交待办：复用中断时已落下的占位待办，否则新建一条待分发记录。"""
        source_ref = receipt["source_ref"]

        # 上一次已经递交过占位待办，直接复用，避免出现第二份。
        existing = self._find_target(source_ref)
        if existing is not None:
            receipt["target_id"] = int(existing.get("id", 0))
            return existing

        target_rows = store.rows(self.target_module)
        pending = self.build_pending(entry, entry_id)
        pending["id"] = max(
            (int(row.get("id", 0)) for row in target_rows), default=0
        ) + 1
        pending["status"] = PROVISIONAL_STATUS
        pending["pending"] = True
        pending["abnormal"] = False
        pending["_relay_state"] = PROVISIONAL_STATUS
        pending["_relay_source"] = source_ref
        pending["_relay_receipt_id"] = receipt.get("id")
        target_rows.append(pending)
        receipt["target_id"] = int(pending["id"])
        receipt["updated_at"] = _now()
        return pending

    def record_receipt(
        self,
        entry: dict[str, Any],
        entry_id: int,
        pending: dict[str, Any],
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        """记回执：待办转为可办理状态，登记来源与办理资格。"""
        source_ref = receipt["source_ref"]
        if source_ref not in str(pending.get(self.marker_field, "")):
            raise ValueError(f"待办缺少来源标记 {source_ref}，不能记回执")

        pending["status"] = self.target_initial_status
        pending["pending"] = self.target_initial_status != self.target_status_order[-1]
        pending["abnormal"] = False
        for key in _RELAY_INTERNAL_KEYS:
            pending.pop(key, None)

        receipt["target_id"] = int(pending.get("id", 0))
        receipt["state"] = STATE_ACCEPTED
        receipt["error"] = ""
        receipt["accepted_at"] = _now()
        receipt["updated_at"] = receipt["accepted_at"]
        receipt["receipt"] = {
            "来源": source_ref,
            "动作": self.action,
            "待办": f"{self.target_module}:{receipt['target_id']}",
            "状态": receipt["state"],
            "格式": f"[{source_ref}]",
        }
        receipt["qualification"] = {
            "来源": source_ref,
            "来源模块": self.source_module,
            "来源ID": int(entry_id),
            "待办模块": self.target_module,
            "待办ID": int(pending.get("id", 0)),
            "待办状态": pending.get("status"),
            "完整受理": True,
            "可办理": True,
        }
        return receipt

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------
    def needs_relay(
        self, entry: dict[str, Any], entry_id: int, *, required: bool = False
    ) -> bool:
        """是否需要为本单据续办一次接力。

        触发条件成立（``required``）且尚未完整受理时返回 True；此前派发中断留
        下未受理回执时也返回 True。已经完整受理、或从未发起过派发的单据返回
        False，交回原有业务规则。
        """
        receipt = relay_ledger.find(self.source_module, entry_id, self.action)
        if receipt is None:
            return required
        return receipt.get("state") != STATE_ACCEPTED

    def dispatch(
        self, entry: dict[str, Any], entry_id: int, *, duplicate_message: str
    ) -> tuple[dict[str, Any] | None, str]:
        """按“处理签收请求 → 递交待办 → 记回执”的顺序执行接力。"""
        receipt = relay_ledger.find(self.source_module, entry_id, self.action)
        signed = self._is_signed(entry)

        if not signed:
            return self._execute(entry, entry_id, resume=False)

        # 来源已经是签收状态：只有确实发起过派发才续办，否则维持原有幂等提示。
        if receipt is None:
            return None, duplicate_message
        if self._receipt_accepted(receipt):
            return None, duplicate_message
        return self._execute(entry, entry_id, resume=True)

    def _execute(
        self, entry: dict[str, Any], entry_id: int, *, resume: bool
    ) -> tuple[dict[str, Any] | None, str]:
        receipt = relay_ledger.begin(
            source_module=self.source_module,
            source_id=entry_id,
            action=self.action,
            target_module=self.target_module,
            target_entity=self.target_entity,
            source_ref=f"{self.source_module}:{entry_id}",
            marker_field=self.marker_field,
        )

        # 第 1 步：处理签收请求。
        self.handle_request(entry, receipt)

        # 第 2、3 步：递交待办、记回执；任何一步失败都保留回执供续办。
        try:
            pending = self.submit_pending(entry, entry_id, receipt)
            self.record_receipt(entry, entry_id, pending, receipt)
        except Exception as error:  # noqa: BLE001 - 派发失败要落回执而不是丢状态
            receipt["state"] = STATE_ABORTED
            receipt["error"] = str(error) or error.__class__.__name__
            receipt["updated_at"] = _now()
            prefix = "续办未完成" if resume else f"已{self.action}，但"
            return entry, (
                f"{prefix}{self.target_entity}待办派发中止（{receipt['error']}），"
                f"可重新提交「{self.action}」续办"
            )

        return entry, f"{self.source_entity}已{self.action}"

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def status(
        self, entry: dict[str, Any], entry_id: int, *, required: bool
    ) -> dict[str, Any]:
        """给发起人查看派发进度和是否还能重试。"""
        receipt = relay_ledger.find(self.source_module, entry_id, self.action)
        signed = self._is_signed(entry)
        accepted = self._receipt_accepted(receipt)

        if not required:
            state, retryable = STATE_NOT_REQUIRED, False
            message = "未触发派发条件，无需派发"
        elif receipt is None:
            state, retryable = STATE_NOT_STARTED, False
            message = (
                f"已{self.action}，但未发起派发" if signed
                else f"尚未{self.action}，按正常{self.action}办理"
            )
        elif accepted:
            state, retryable = STATE_ACCEPTED, False
            message = f"{self.target_entity}待办已完整受理"
        else:
            state = receipt.get("state", STATE_NOT_STARTED)
            retryable = signed
            detail = receipt.get("error") or "尚未记回执"
            message = (
                f"{self.target_entity}待办派发未完成（{detail}），"
                f"可重新提交「{self.action}」续办"
                if signed
                else f"尚未{self.action}，按正常{self.action}办理"
            )

        return {
            "module": self.source_module,
            "entry_id": int(entry_id),
            "action": self.action,
            "target_module": self.target_module,
            "target_entity": self.target_entity,
            "required": bool(required),
            "state": state,
            "has_dispatch": receipt is not None,
            "accepted": accepted,
            "complete": accepted,
            "retryable": retryable,
            "can_retry": retryable,
            "source": f"{self.source_module}:{entry_id}",
            "target_id": receipt.get("target_id") if receipt else None,
            "attempts": receipt.get("attempts", 0) if receipt else 0,
            "error": receipt.get("error", "") if receipt else "",
            "receipt": receipt.get("receipt", {}) if receipt else {},
            "qualification": receipt.get("qualification", {}) if receipt else {},
            "message": message,
        }

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _is_signed(self, entry: dict[str, Any]) -> bool:
        """来源单据是否已经签收（含签收之后的后续状态）。"""
        status = str(entry.get("status", ""))
        if status == self.source_final_status:
            return True
        if self.source_final_status in self.source_status_order and status in self.source_status_order:
            return (
                self.source_status_order.index(status)
                >= self.source_status_order.index(self.source_final_status)
            )
        return False

    def _find_target(self, source_ref: str) -> dict[str, Any] | None:
        for row in store.rows(self.target_module):
            if source_ref in str(row.get(self.marker_field, "")):
                return row
        return None

    def _receipt_accepted(self, receipt: dict[str, Any] | None) -> bool:
        if not receipt or receipt.get("state") != STATE_ACCEPTED:
            return False
        target_id = receipt.get("target_id")
        if target_id is None:
            return False
        row = store.find(self.target_module, int(target_id))
        if row is None or _is_provisional(row):
            return False
        return receipt["source_ref"] in str(row.get(self.marker_field, ""))


class RelayDispatchMixin:
    """给来源业务服务补上派发进度查询和续办入口。"""

    relay: RelayFlow

    def dispatch_required(self, entry: dict[str, Any]) -> bool:  # 业务覆写
        raise NotImplementedError

    def relay_status(self, entry_id: int) -> dict[str, Any] | None:
        entry = self.get_entry(entry_id)  # type: ignore[attr-defined]
        if entry is None:
            return None
        # 触发条件已在签收时成立过、派发还没完整受理时，仍按“需派发”处理。
        receipt = relay_ledger.find(self.relay.source_module, entry_id, self.relay.action)
        required = self.dispatch_required(entry) or (
            receipt is not None and receipt.get("state") != STATE_ACCEPTED
        )
        return self.relay.status(entry, entry_id, required=required)

    def retry_dispatch(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """重新提交原动作，从中断处续办派发。"""
        info = self.relay_status(entry_id)
        if info is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"  # type: ignore[attr-defined]
        if not info["retryable"]:
            return None, info["message"]
        return self.run_action(entry_id, self.relay.action)  # type: ignore[attr-defined]
