"""集体备课领域服务：备课记录登记、签阅、存档，含跨模块派发同行评议。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.relay import RelayDispatchMixin, RelayFlow


class LessonPrepService(RelayDispatchMixin, BaseModuleService):
    MODULE = "lesson_prep"
    ENTITY = "备课记录"
    OPERATION_TERM = "签阅记录"
    LIST_FIELDS = ["备课章节", "主备教师", "参与教师", "备课日期", "研讨要点", "教学资源", "课后反思", "备注说明"]
    REQUIRED_FIELDS = ["备课章节", "主备教师", "参与教师"]
    STATUS_ORDER = ["草稿", "待签阅", "已签阅", "已存档"]
    ACTION_RULES = {"签阅记录": "已签阅", "补充意见": "草稿", "存档记录": "已存档"}
    NEGATIVE_ACTIONS = []

    # 签阅后若课后反思提出改进/评议，按共用接力流程派发同行评议待办。
    relay = RelayFlow(
        source_module=MODULE,
        source_entity=ENTITY,
        action="签阅记录",
        source_final_status="已签阅",
        source_status_order=STATUS_ORDER,
        target_module="peer_review",
        target_entity="同行评议",
        target_initial_status="待汇总",
        target_status_order=["草稿", "待汇总", "已汇总", "已公示"],
        marker_field="评议意见",
        build_pending=lambda entry, prep_id: {
            "被评教师": entry.get("主备教师", ""),
            "评议维度": "备课教学质量",
            "得分": "",
            "评议意见": f"由备课记录 #{prep_id} 课后反思触发 [lesson_prep:{prep_id}]",
            "评议人员": "",
            "评议日期": "",
            "是否匿名": "否",
            "备注说明": "备课签阅后自动生成评议请求",
        },
    )

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """执行动作，含跨模块联动：签阅后派发同行评议。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        # 签阅记录：需派发（含中断续办）时走共用接力流程；否则保持原有签阅行为。
        if action == "签阅记录":
            if self.relay.needs_relay(
                entry, entry_id, required=self.dispatch_required(entry)
            ):
                return self.relay.dispatch(
                    entry, entry_id,
                    duplicate_message=f"{self.ENTITY} {entry_id} 已是已签阅状态，无需重复签阅",
                )
            if entry.get("status") == "已签阅":
                return None, f"{self.ENTITY} {entry_id} 已是已签阅状态，无需重复签阅"
            return super().run_action(entry_id, action)

        return super().run_action(entry_id, action)

    def dispatch_required(self, entry: dict[str, Any]) -> bool:
        """触发条件：课后反思提出改进意见或要求同行评议。"""
        reflection = entry.get("课后反思", "")
        return bool(reflection) and ("改进" in str(reflection) or "评议" in str(reflection))
