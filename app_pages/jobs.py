from __future__ import annotations

import streamlit as st

from app_pages._shared import (
    application_service,
    credential_inputs,
    job_label,
    load_profiles_for_ui,
)


st.header("任务状态")
st.caption("后台任务可在浏览器关闭后继续；失败或取消的任务可沿用缓存恢复。")

service = application_service()
jobs = service.list_jobs(limit=200)
if not jobs:
    st.info("还没有任务。请先在“新任务”页面创建一个任务。")
    st.stop()

filter_value = st.segmented_control(
    "状态筛选",
    ["全部", "运行中", "已完成", "需处理"],
    default="全部",
)
if filter_value == "运行中":
    visible = [job for job in jobs if job.status in {"queued", "running"}]
elif filter_value == "已完成":
    visible = [job for job in jobs if job.status == "succeeded"]
elif filter_value == "需处理":
    visible = [
        job for job in jobs if job.status in {"failed", "cancelled", "interrupted"}
    ]
else:
    visible = jobs
if not visible:
    st.info("当前筛选条件下没有任务。")
    st.stop()

job_ids = [job.id for job in visible]
selected = st.session_state.get("selected_job_id")
index = job_ids.index(selected) if selected in job_ids else 0
job_id = st.selectbox(
    "选择任务",
    job_ids,
    index=index,
    format_func=lambda value: job_label(value, visible),
)
st.session_state["selected_job_id"] = job_id
job = service.get_job(job_id)

metrics = st.columns(4)
metrics[0].metric("状态", job.status)
metrics[1].metric("入口", job.source_mode)
metrics[2].metric("任务 ID", job.id[:8])
metrics[3].metric("退出码", "—" if job.exit_code is None else job.exit_code)
st.caption(f"更新时间：{job.updated_at} · 工作区：{job.workspace}")

with st.container(horizontal=True):
    refresh = st.button("刷新", icon=":material/refresh:")
    cancel = st.button(
        "取消任务",
        icon=":material/cancel:",
        disabled=job.status not in {"queued", "running", "cancel_requested"},
    )
if refresh:
    st.rerun()
if cancel:
    service.cancel(job.id)
    st.rerun()

if job.status in {"failed", "cancelled", "interrupted"}:
    _config, profiles, _error = load_profiles_for_ui()
    credential_names: set[str | None] = set()
    if profiles is not None:
        for profile in profiles.profiles.values():
            credential_names.add(profile.credential_env)
    with st.expander("恢复任务", icon=":material/restart_alt:"):
        st.caption("仅需重新提供本次运行所需密钥；已完成节点将从 Graph 缓存恢复。")
        resume_credentials = credential_inputs(
            credential_names,
            key_prefix=f"resume_{job.id}",
        )
        if st.button(
            "恢复运行",
            type="primary",
            icon=":material/play_arrow:",
        ):
            try:
                service.resume(job.id, credentials=resume_credentials)
                st.rerun()
            except (OSError, RuntimeError, ValueError) as exc:
                st.error(str(exc), icon=":material/error:")

if job.error:
    st.error(job.error, icon=":material/error:")

with st.container(border=True):
    st.subheader("最近日志")
    log = service.log(job.id)
    st.code(log or "等待任务输出…", language="text")

with st.expander("运行规格与事件", icon=":material/data_object:"):
    st.json(job.spec.to_dict())
    st.json(service.registry.events(job.id, limit=100))
