"""派发接力流程验收测试。

覆盖两段“改上游状态 → 派待办”的接力：
- 质量简报 Q-40（含教学预警）签收 → 派发复盘会议；
- 集体备课 L-12（含改进建议）签阅 → 派发同行评议。

重点验证派发中止后的续办：不产生第二份有效待办，待办来源、回执和办理资格
完整，且普通简报/不要求改进的备课仍按原条件完成动作、不生成派发事项。
"""
from __future__ import annotations

from app.routers.resource import (
    get_dispatch_status,
    get_entry as read_entry,
    retry_dispatch,
    run_action as route_action,
)
from app.schemas import ActionPayload
from app.services.cross_module import check_entity_status
from app.services.lesson_prep import LessonPrepService
from app.services.peer_review import PeerReviewService
from app.services.quality_report import QualityReportService
from app.services.relay import relay_ledger, target_relay_info
from app.services.review_meeting import ReviewMeetingService
from app.store import store


def clear_all():
    for name in store.module_names():
        store.rows(name).clear()
    relay_ledger.clear()


def make_report(report_id: int = 40, problem: str = "多门课程及格率低于60%，触发教学预警") -> dict:
    rows = store.rows("quality_report")
    rows.append({
        "id": report_id,
        "status": "待签收",
        "pending": True,
        "abnormal": False,
        "覆盖学期": "2025-秋季",
        "教学质量概况": "整体平稳",
        "突出问题": problem,
        "改进方向": "加强课堂互动",
        "编制部门": "教务处",
        "发布日期": "2025-10-08",
        "发放范围": "全体教研室",
        "备注说明": "Q-40",
    })
    return store.find("quality_report", report_id)


def make_prep(prep_id: int = 12, reflection: str = "教学主线需要改进，建议组织同行评议") -> dict:
    rows = store.rows("lesson_prep")
    rows.append({
        "id": prep_id,
        "status": "待签阅",
        "pending": True,
        "abnormal": False,
        "备课章节": "第5章 图结构",
        "主备教师": "杨老师",
        "参与教师": "李老师、王老师",
        "备课日期": "2025-10-12",
        "研讨要点": "图的遍历",
        "教学资源": "课件与习题",
        "课后反思": reflection,
        "备注说明": "L-12",
    })
    return store.find("lesson_prep", prep_id)


def meeting_rows_for(report_id: int) -> list[dict]:
    return [
        row for row in store.rows("review_meeting")
        if f"quality_report:{report_id}" in str(row.get("决议事项", ""))
    ]


def review_rows_for(prep_id: int) -> list[dict]:
    return [
        row for row in store.rows("peer_review")
        if f"lesson_prep:{prep_id}" in str(row.get("评议意见", ""))
    ]


class TestQualityReportRelay:
    """简报签收 → 复盘会议待办。"""

    def setup_method(self):
        clear_all()
        self.report_svc = QualityReportService()
        self.meeting_svc = ReviewMeetingService()

    def test_sign_with_warning_dispatches_complete_meeting(self):
        report = make_report()
        entry, message = self.report_svc.run_action(40, "签收简报")

        assert entry is not None, message
        assert store.find("quality_report", 40)["status"] == "已签收"

        meetings = meeting_rows_for(40)
        assert len(meetings) == 1
        meeting = meetings[0]
        # 待办格式沿用原样
        assert meeting["决议事项"] == "针对简报中问题制定整改决议 [quality_report:40]"
        assert meeting["会议主题"] == "2025-秋季学期质量简报突出问题复盘"
        assert meeting["参会人员"] == "教研室主任、相关教师"
        assert meeting["备注说明"] == "由质量简报签收后自动生成"
        assert meeting["status"] == "待签认"
        assert meeting["pending"] is True
        assert meeting["abnormal"] is False

        # 来源、回执、办理资格完整
        info = target_relay_info("review_meeting", meeting)
        assert info["from_relay"] is True
        assert info["source"] == "quality_report:40"
        assert info["receipt"]["格式"] == "[quality_report:40]"
        assert info["qualification"]["可办理"] is True
        assert info["qualification"]["待办ID"] == meeting["id"]

        # 完整受理的会议可以正常签认
        signed, msg = self.meeting_svc.run_action(meeting["id"], "签认纪要")
        assert signed is not None, msg
        assert store.find("review_meeting", meeting["id"])["status"] == "已签认"

    def test_abort_before_pending_then_resubmit_resumes(self, monkeypatch):
        report = make_report()
        flow = self.report_svc.relay
        original = flow.build_pending

        calls = {"count": 0}

        def flaky(entry, entry_id):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("会议服务暂时不可用")
            return original(entry, entry_id)

        monkeypatch.setattr(flow, "build_pending", flaky)

        entry, message = self.report_svc.run_action(40, "签收简报")
        # 上游状态已更新，但待办没发出
        assert store.find("quality_report", 40)["status"] == "已签收"
        assert "派发中止" in message
        assert meeting_rows_for(40) == []

        status = self.report_svc.relay_status(40)
        assert status["required"] is True
        assert status["retryable"] is True
        assert status["accepted"] is False
        assert "续办" in status["message"]

        # 再提交原动作：从中断处续办，且只落一份会议待办
        entry, message = self.report_svc.run_action(40, "签收简报")
        assert entry is not None, message
        meetings = meeting_rows_for(40)
        assert len(meetings) == 1
        status = self.report_svc.relay_status(40)
        assert status["accepted"] is True
        assert status["retryable"] is False
        assert status["receipt"]["来源"] == "quality_report:40"

    def test_abort_after_pending_meeting_not_actionable_until_resubmitted(self, monkeypatch):
        report = make_report()
        flow = self.report_svc.relay
        original = flow.record_receipt

        calls = {"count": 0}

        def flaky(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("回执登记超时")
            return original(*args, **kwargs)

        monkeypatch.setattr(flow, "record_receipt", flaky)

        _, message = self.report_svc.run_action(40, "签收简报")
        assert "派发中止" in message

        meetings = meeting_rows_for(40)
        assert len(meetings) == 1, "中断时占位待办应保留，供续办复用"
        pending_id = meetings[0]["id"]
        assert meetings[0]["status"] == "待分发"

        # 主任打开待分发的纪要申请签认：不能办理
        result, msg = self.meeting_svc.run_action(pending_id, "签认纪要")
        assert result is None
        assert "尚未完整受理" in msg
        assert store.find("review_meeting", pending_id)["status"] == "待分发"

        # 发起人查到仍可重试
        status = self.report_svc.relay_status(40)
        assert status["retryable"] is True
        assert status["accepted"] is False

        # 再提交原动作：复用占位待办，不产生第二份
        entry, message = self.report_svc.run_action(40, "签收简报")
        assert entry is not None, message
        meetings = meeting_rows_for(40)
        assert len(meetings) == 1
        assert meetings[0]["id"] == pending_id
        assert meetings[0]["status"] == "待签认"
        assert meetings[0]["pending"] is True

        signed, msg = self.meeting_svc.run_action(pending_id, "签认纪要")
        assert signed is not None, msg
        assert store.find("review_meeting", pending_id)["status"] == "已签认"

    def test_plain_report_signs_without_dispatch(self):
        report = make_report(problem="课堂互动有待加强，整体教学秩序良好")
        entry, message = self.report_svc.run_action(40, "签收简报")

        assert entry is not None, message
        assert store.find("quality_report", 40)["status"] == "已签收"
        assert meeting_rows_for(40) == []

        status = self.report_svc.relay_status(40)
        assert status["required"] is False
        assert status["state"] == "无需派发"
        assert status["retryable"] is False

        # 原有幂等行为不变
        again, msg = self.report_svc.run_action(40, "签收简报")
        assert again is None
        assert "无需重复签收" in msg

    def test_plain_report_can_be_transmitted_as_before(self):
        make_report(problem="课堂互动有待加强")
        self.report_svc.run_action(40, "签收简报")
        entry, message = self.report_svc.run_action(40, "传达到教师")
        assert entry is not None, message
        assert store.find("quality_report", 40)["status"] == "已传达"
        assert meeting_rows_for(40) == []


class TestLessonPrepRelay:
    """备课签阅 → 同行评议待办。"""

    def setup_method(self):
        clear_all()
        self.prep_svc = LessonPrepService()
        self.review_svc = PeerReviewService()

    def test_sign_with_improvement_dispatches_complete_review(self):
        make_prep()
        entry, message = self.prep_svc.run_action(12, "签阅记录")

        assert entry is not None, message
        assert store.find("lesson_prep", 12)["status"] == "已签阅"

        reviews = review_rows_for(12)
        assert len(reviews) == 1
        review = reviews[0]
        # 待办格式沿用原样
        assert review["评议意见"] == "由备课记录 #12 课后反思触发 [lesson_prep:12]"
        assert review["被评教师"] == "杨老师"
        assert review["评议维度"] == "备课教学质量"
        assert review["备注说明"] == "备课签阅后自动生成评议请求"
        assert review["status"] == "待汇总"
        assert review["pending"] is True
        assert review["abnormal"] is False

        info = target_relay_info("peer_review", review)
        assert info["source"] == "lesson_prep:12"
        assert info["receipt"]["格式"] == "[lesson_prep:12]"
        assert info["qualification"]["可办理"] is True

        reviewed, msg = self.review_svc.run_action(review["id"], "汇总评议")
        assert reviewed is not None, msg
        assert store.find("peer_review", review["id"])["status"] == "已汇总"

    def test_abort_after_pending_resubmit_keeps_single_invitation(self, monkeypatch):
        make_prep()
        flow = self.prep_svc.relay
        original = flow.record_receipt

        calls = {"count": 0}

        def flaky(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("评议服务写入失败")
            return original(*args, **kwargs)

        monkeypatch.setattr(flow, "record_receipt", flaky)

        _, message = self.prep_svc.run_action(12, "签阅记录")
        assert "派发中止" in message
        assert store.find("lesson_prep", 12)["status"] == "已签阅"

        reviews = review_rows_for(12)
        assert len(reviews) == 1
        pending_id = reviews[0]["id"]
        assert reviews[0]["status"] == "待分发"

        # 评议未派完时，评议表还不能汇总
        result, msg = self.review_svc.run_action(pending_id, "汇总评议")
        assert result is None
        assert "尚未完整受理" in msg

        # 备课的签阅可以续办
        entry, message = self.prep_svc.run_action(12, "签阅记录")
        assert entry is not None, message
        reviews = review_rows_for(12)
        assert len(reviews) == 1, "续办不能产生第二份有效邀请"
        assert reviews[0]["id"] == pending_id
        assert reviews[0]["status"] == "待汇总"

        # 后一次操作不能产生第二份有效邀请：已完整受理后再提交仍是幂等
        again, msg = self.prep_svc.run_action(12, "签阅记录")
        assert again is None
        assert "无需重复签阅" in msg
        assert len(review_rows_for(12)) == 1

        reviewed, msg = self.review_svc.run_action(pending_id, "汇总评议")
        assert reviewed is not None, msg

    def test_abort_before_pending_then_resubmit(self, monkeypatch):
        make_prep()
        flow = self.prep_svc.relay
        original = flow.build_pending
        calls = {"count": 0}

        def flaky(entry, entry_id):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("评议服务暂时不可用")
            return original(entry, entry_id)

        monkeypatch.setattr(flow, "build_pending", flaky)

        _, message = self.prep_svc.run_action(12, "签阅记录")
        assert "派发中止" in message
        assert review_rows_for(12) == []
        assert self.prep_svc.relay_status(12)["retryable"] is True

        entry, message = self.prep_svc.run_action(12, "签阅记录")
        assert entry is not None, message
        assert len(review_rows_for(12)) == 1
        assert self.prep_svc.relay_status(12)["accepted"] is True

    def test_prep_without_improvement_signs_without_dispatch(self):
        make_prep(reflection="本次备课研讨充分，教学资源准备齐全")
        entry, message = self.prep_svc.run_action(12, "签阅记录")

        assert entry is not None, message
        assert store.find("lesson_prep", 12)["status"] == "已签阅"
        assert review_rows_for(12) == []
        status = self.prep_svc.relay_status(12)
        assert status["required"] is False
        assert status["state"] == "无需派发"

        # 原有幂等行为不变
        again, msg = self.prep_svc.run_action(12, "签阅记录")
        assert again is None
        assert "无需重复签阅" in msg

    def test_prep_without_improvement_can_archive_as_before(self):
        make_prep(reflection="研讨充分")
        self.prep_svc.run_action(12, "签阅记录")
        entry, message = self.prep_svc.run_action(12, "存档记录")
        assert entry is not None, message
        assert store.find("lesson_prep", 12)["status"] == "已存档"
        assert review_rows_for(12) == []


class TestRelayReceiptFormat:
    """两条接力的回执格式和来源标记保持一致。"""

    def setup_method(self):
        clear_all()

    def test_receipts_record_source_action_and_target(self):
        make_report()
        make_prep()
        report_svc = QualityReportService()
        prep_svc = LessonPrepService()

        report_svc.run_action(40, "签收简报")
        prep_svc.run_action(12, "签阅记录")

        report_status = report_svc.relay_status(40)
        prep_status = prep_svc.relay_status(12)
        assert report_status["receipt"]["来源"] == "quality_report:40"
        assert report_status["receipt"]["格式"] == "[quality_report:40]"
        assert report_status["receipt"]["状态"] == "已受理"
        assert prep_status["receipt"]["来源"] == "lesson_prep:12"
        assert prep_status["receipt"]["格式"] == "[lesson_prep:12]"
        assert prep_status["receipt"]["状态"] == "已受理"

        # 回执里的待办可定位到真实记录
        meeting = store.find("review_meeting", report_status["target_id"])
        review = store.find("peer_review", prep_status["target_id"])
        assert meeting is not None and review is not None
        assert target_relay_info("review_meeting", meeting)["complete"] is True
        assert target_relay_info("peer_review", review)["complete"] is True

    def test_relay_source_cross_validation(self):
        make_report()
        report_svc = QualityReportService()
        report_svc.run_action(40, "签收简报")
        meeting = meeting_rows_for(40)[0]

        # 来源与办理资格可用于跨模块校验
        checked = check_entity_status(
            "review_meeting", meeting["id"], ["待签认"], "简报复盘会议"
        )
        assert checked["id"] == meeting["id"]
        info = target_relay_info("review_meeting", meeting)
        assert info["qualification"]["来源模块"] == "quality_report"
        assert info["qualification"]["来源ID"] == 40


class TestRelayRoutes:
    """路由层：发起人查进度、续办，以及详情/列表带出派发信息。"""

    def setup_method(self):
        clear_all()

    def test_dispatch_status_and_retry_route(self, monkeypatch):
        make_report()
        service = QualityReportService()
        flow = service.relay
        original = flow.record_receipt
        calls = {"count": 0}

        def flaky(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("会议待办写入失败")
            return original(*args, **kwargs)

        monkeypatch.setattr(flow, "record_receipt", flaky)
        service.run_action(40, "签收简报")

        info = get_dispatch_status("quality_report", 40)
        assert info["retryable"] is True
        assert info["accepted"] is False

        result = retry_dispatch("quality_report", 40)
        assert result.ok is True
        assert info["target_id"] is not None

        info = get_dispatch_status("quality_report", 40)
        assert info["accepted"] is True
        assert info["retryable"] is False
        assert len(meeting_rows_for(40)) == 1

    def test_retry_route_is_idempotent_when_complete(self):
        make_report()
        QualityReportService().run_action(40, "签收简报")

        result = retry_dispatch("quality_report", 40)
        assert result.ok is False
        assert "已完整受理" in result.message
        assert len(meeting_rows_for(40)) == 1

    def test_entry_detail_carries_dispatch_and_relay(self):
        make_report()
        QualityReportService().run_action(40, "签收简报")

        report_detail = read_entry("quality_report", 40)
        assert report_detail["dispatch"]["accepted"] is True
        assert "待分发" not in str(report_detail.get("status"))

        meeting_id = meeting_rows_for(40)[0]["id"]
        meeting_detail = read_entry("review_meeting", meeting_id)
        assert meeting_detail["relay_source"] == "quality_report:40"
        assert meeting_detail["relay_complete"] is True
        assert "_relay_state" not in meeting_detail

    def test_target_action_blocked_through_route_until_resubmitted(self, monkeypatch):
        make_prep()
        service = LessonPrepService()
        original = service.relay.record_receipt
        calls = {"count": 0}

        def flaky(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("评议表写入失败")
            return original(*args, **kwargs)

        monkeypatch.setattr(service.relay, "record_receipt", flaky)
        service.run_action(12, "签阅记录")

        review_id = review_rows_for(12)[0]["id"]
        blocked = route_action(
            "peer_review", review_id, ActionPayload(action="汇总评议")
        )
        assert blocked.ok is False
        assert "尚未完整受理" in blocked.message

        resumed = route_action(
            "lesson_prep", 12, ActionPayload(action="签阅记录")
        )
        assert resumed.ok is True
        assert len(review_rows_for(12)) == 1

        accepted = route_action(
            "peer_review", review_id, ActionPayload(action="汇总评议")
        )
        assert accepted.ok is True
        assert store.find("peer_review", review_id)["status"] == "已汇总"
