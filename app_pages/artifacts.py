from __future__ import annotations

from pathlib import Path

import streamlit as st

from app_pages._shared import application_service, job_label


st.header("任务产物")
st.caption("只有发布报告明确通过且内容哈希匹配的文件才标记为正式产物。")

service = application_service()
jobs = service.list_jobs(limit=200)
if not jobs:
    st.info("还没有可查看的任务。")
    st.stop()

job_ids = [job.id for job in jobs]
selected = st.session_state.get("selected_job_id")
index = job_ids.index(selected) if selected in job_ids else 0
job_id = st.selectbox(
    "选择任务",
    job_ids,
    index=index,
    format_func=lambda value: job_label(value, jobs),
)
st.session_state["selected_job_id"] = job_id
records = service.artifacts(job_id)
if not records:
    st.info("任务尚未生成 Graph 可识别的产物。")
    st.stop()

for record in records:
    path = Path(record.path)
    with st.container(border=True):
        heading = st.container(horizontal=True, vertical_alignment="center")
        heading.subheader(record.name)
        color = {
            "released": "green",
            "blocked": "red",
            "draft": "orange",
        }[record.status]
        heading.badge(record.status, color=color)
        st.caption(
            f"{record.kind} · {path.stat().st_size / 1024 / 1024:.2f} MB · "
            f"SHA-256 {record.sha256[:12] if record.sha256 else '—'}"
        )
        if record.status == "released" and path.stat().st_size <= 64 * 1024 * 1024:
            st.download_button(
                "下载正式产物",
                data=path.read_bytes(),
                file_name=path.name,
                mime=record.media_type or "application/octet-stream",
                key=f"download_{job_id}_{record.kind}_{record.name}",
                icon=":material/download:",
            )
        elif record.status == "released":
            st.info(f"文件较大，请从本机工作区读取：{path}")
        else:
            st.caption("草稿或阻断产物仅供检查，不提供正式下载。")
