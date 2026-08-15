from __future__ import annotations

import streamlit as st

from app_pages._shared import application_service, job_label
from application_service import REVIEW_ACCEPT_REASON


def _bounded(value: object, *, limit: int = 500) -> str:
    """Bound browser payloads; review evidence itself stays server-side."""

    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…"


st.header("人工复核")
st.caption(
    "复核决定绑定当前语义重建哈希与问题 ID；结构性或未知阻断项不能在此豁免。"
)

service = application_service()
jobs = service.list_jobs(limit=200)
if not jobs:
    st.info("还没有可复核的任务。请先创建并运行一个任务。")
    st.stop()

job_ids = [job.id for job in jobs]
selected = st.session_state.get("selected_job_id")
index = job_ids.index(selected) if selected in job_ids else 0
job_id = st.selectbox(
    "选择任务",
    job_ids,
    index=index,
    format_func=lambda value: job_label(value, jobs),
    key="review_job_id",
)
st.session_state["selected_job_id"] = job_id

# Normal page loads use the read-only status contract.  The mutating refresh is
# an explicit user action because it rewrites the derived review audit.
report = service.review_status(job_id)
with st.container(horizontal=True):
    if st.button("刷新页面", icon=":material/refresh:"):
        st.rerun()
    refresh_audit = st.button(
        "初始化/刷新派生复核审计",
        icon=":material/sync:",
    )
if refresh_audit:
    report = service.review_report(job_id)
    if report.get("action") == "viewed":
        st.success("派生复核审计已按当前重建与决策日志刷新。")

flash = st.session_state.pop("review_flash", None)
if flash:
    st.success(flash, icon=":material/check_circle:")

status = str(report.get("status") or "blocked")
summary = report.get("summary") or {}
overview = st.columns(4)
overview[0].metric("复核状态", status)
overview[1].metric("问题数", summary.get("issue_count", "—"))
overview[2].metric("仍阻断", summary.get("blocking_issue_count", "—"))
overview[3].metric("已解决", summary.get("resolved_issue_count", "—"))

error = report.get("error")
if error:
    st.error(
        _bounded(error.get("message") or "复核报告不可用", limit=800),
        icon=":material/error:",
    )
    st.caption("任务需要先完成语义重建，或修复审计文件后再进行人工复核。")
    st.stop()

reconstruction = report.get("reconstruction") or {}
policy = report.get("review_policy") or {}
issue_set = report.get("issue_set") or {}
decision_log = report.get("decision_log") or {}
with st.container(border=True):
    st.subheader("绑定信息")
    st.caption(f"重建文件：{_bounded(reconstruction.get('path', '—'), limit=320)}")
    st.code(str(reconstruction.get("sha256") or "—"), language="text")
    details = st.columns(3)
    details[0].metric("策略版本", policy.get("version", "—"))
    details[1].metric("问题集", issue_set.get("count", "—"))
    details[2].metric("决策记录", decision_log.get("record_count", "—"))
    st.caption(
        f"策略指纹：{policy.get('fingerprint', '—')} · "
        f"问题集 SHA-256：{issue_set.get('sha256', '—')}"
    )

issues = report.get("issues") or []
if not issues:
    st.success("当前语义重建没有需要人工处理的阻断项。", icon=":material/check:")
    st.stop()

st.subheader("阻断项")
for issue in issues:
    issue_id = str(issue.get("issue_id") or "")
    resolution = str(issue.get("resolution") or "unresolved")
    allowed = tuple(str(value) for value in (issue.get("allowed_decisions") or []))
    with st.container(border=True):
        heading = st.container(horizontal=True, vertical_alignment="center")
        heading.subheader(str(issue.get("code") or "未知问题"))
        heading.badge(
            resolution,
            color="green" if not issue.get("release_blocked", True) else "red",
        )
        st.caption(_bounded(issue.get("message") or "无问题说明"))
        st.code(issue_id, language="text")
        st.caption(
            f"主体：{_bounded(issue.get('subject_id', '—'), limit=200)} · "
            f"单元：{_bounded(issue.get('unit_id') or '文档级', limit=200)} · "
            f"证据 SHA-256：{issue.get('evidence_sha256') or '—'}"
        )

        effective = issue.get("effective_decision")
        if isinstance(effective, dict):
            st.success(
                f"已记录 {effective.get('decision', '—')} · "
                f"复核人 {_bounded(effective.get('reviewer', '—'), limit=200)} · "
                f"理由 {_bounded(effective.get('reason', '—'), limit=100)}",
                icon=":material/check_circle:",
            )
            continue

        if not issue.get("reviewable", False) or not allowed:
            st.warning(
                "当前复核策略不允许豁免此项；请修复源文档或语义结构后重新运行。",
                icon=":material/block:",
            )
            continue

        st.caption(f"策略允许的决定：{', '.join(allowed)}")
        if resolution == "unresolved" and "accepted" in allowed:
            with st.form(f"review_accept_{issue_id}"):
                reviewer = st.text_input(
                    "复核人",
                    key=f"reviewer_{issue_id}",
                    placeholder="姓名或团队账号",
                    max_chars=200,
                )
                st.text_input(
                    "固定理由",
                    value=REVIEW_ACCEPT_REASON,
                    disabled=True,
                    key=f"reason_{issue_id}",
                )
                confirmed = st.checkbox(
                    "我已核对该项，确认按当前文本接受，并理解决定绑定当前重建版本。",
                    key=f"confirm_{issue_id}",
                )
                submitted = st.form_submit_button(
                    "确认接受当前文本",
                    type="primary",
                    icon=":material/how_to_reg:",
                )
            if submitted:
                if not reviewer or not reviewer.strip():
                    st.error("请填写复核人。", icon=":material/error:")
                elif not confirmed:
                    st.error("提交前必须勾选确认。", icon=":material/error:")
                else:
                    updated = service.accept_review_issue_as_text(
                        job_id,
                        issue_id=issue_id,
                        reviewer=reviewer.strip(),
                    )
                    if updated.get("action") == "decision-recorded":
                        st.session_state["review_flash"] = (
                            f"已记录问题 {issue_id[:18]}… 的复核决定。"
                        )
                        st.rerun()
                    else:
                        failure = updated.get("error") or {}
                        st.error(
                            _bounded(
                                failure.get("message") or "复核决定未写入",
                                limit=800,
                            ),
                            icon=":material/error:",
                        )
        else:
            st.info("此项没有当前页面可执行的待定操作。")
