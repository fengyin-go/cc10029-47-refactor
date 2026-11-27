"""质量简报领域服务：简报登记、签收、传达，含跨模块派发复盘会议。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.relay import RelayDispatchMixin, RelayFlow


class QualityReportService(RelayDispatchMixin, BaseModuleService):
    MODULE = "quality_report"
    ENTITY = "质量简报"
    OPERATION_TERM = "签收简报"
    LIST_FIELDS = ["覆盖学期", "教学质量概况", "突出问题", "改进方向", "编制部门", "发布日期", "发放范围", "备注说明"]
    REQUIRED_FIELDS = ["覆盖学期", "教学质量概况", "突出问题"]
    STATUS_ORDER = ["草稿", "待签收", "已签收", "已传达"]
    ACTION_RULES = {"签收简报": "已签收", "补充说明": "草稿", "传达到教师": "已传达"}
    NEGATIVE_ACTIONS = []

    # 签收后若简报含严重/预警问题，按共用接力流程派发复盘会议待办。
    relay = RelayFlow(
        source_module=MODULE,
        source_entity=ENTITY,
        action="签收简报",
        source_final_status="已签收",
        source_status_order=STATUS_ORDER,
        target_module="review_meeting",
        target_entity="复盘会议",
        target_initial_status="待签认",
        target_status_order=["草稿", "待签认", "已签认", "已分发"],
        marker_field="决议事项",
        build_pending=lambda entry, report_id: {
            "会议主题": f"{entry.get('覆盖学期', '')}学期质量简报突出问题复盘",
            "参会人员": "教研室主任、相关教师",
            "召开日期": "",
            "决议事项": f"针对简报中问题制定整改决议 [quality_report:{report_id}]",
            "分歧观点": "",
            "记录人员": "",
            "下次会议": "",
            "备注说明": "由质量简报签收后自动生成",
        },
    )

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """执行动作，含跨模块联动：签收时检查突出问题派发复盘会议。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        # 签收简报：需派发（含中断续办）时走共用接力流程；否则保持原有签收行为。
        if action == "签收简报":
            if self.relay.needs_relay(
                entry, entry_id, required=self.dispatch_required(entry)
            ):
                return self.relay.dispatch(
                    entry, entry_id,
                    duplicate_message=f"{self.ENTITY} {entry_id} 已是已签收状态，无需重复签收",
                )
            if entry.get("status") == "已签收":
                return None, f"{self.ENTITY} {entry_id} 已是已签收状态，无需重复签收"
            return super().run_action(entry_id, action)

        # 传达到教师：必须先签收
        if action == "传达到教师":
            if entry.get("status") != "已签收":
                return None, f"{self.ENTITY} {entry_id} 必须先签收简报，才能传达"

        return super().run_action(entry_id, action)

    def dispatch_required(self, entry: dict[str, Any]) -> bool:
        """触发条件：突出问题中含有严重问题或教学预警。"""
        problem = entry.get("突出问题", "")
        return bool(problem) and ("严重" in str(problem) or "预警" in str(problem))
