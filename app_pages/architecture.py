from pathlib import Path
import json
import streamlit as st
import streamlit.components.v1 as components
from app_pages._shared import application_service, job_label
from architecture_dashboard import collect_snapshot, discover_workspaces
from architecture_ascii import render_ascii, render_dashboard

st.header("架构与审查")
st.caption("检查项目模块与已记录的任务证据；刷新时采集快照。")
repo = Path(__file__).resolve().parents[1]
service = application_service()
jobs = service.list_jobs(limit=100)
mode = st.selectbox("查看范围", ["项目总览", "已有任务", "发布工作区"])
workspace = None
allowed_root = None
job_data = None
if mode == "已有任务":
    if jobs:
        selected = st.selectbox("选择任务", [job.id for job in jobs], format_func=lambda value: job_label(value, jobs))
        job = service.get_job(selected)
        workspace = Path(job.spec.output_dir)
        allowed_root = service.settings.jobs_root
        job_data = {key: getattr(job, key) for key in ("id", "status", "updated_at", "source_mode")}
    else:
        st.info("还没有任务，先展示项目架构。")
elif mode == "发布工作区":
    workspaces = discover_workspaces(repo)
    if workspaces:
        workspace = st.selectbox("选择工作区（最多 100 个）", workspaces, format_func=lambda value: value.name)
    else:
        st.info("尚未发现发布工作区，先展示项目架构。")
if st.button("刷新快照", icon=":material/refresh:"):
    st.rerun()
try:
    snapshot = collect_snapshot(repo, workspace=workspace, allowed_root=allowed_root, job=job_data)
except (OSError, ValueError) as exc:
    st.error(f"无法读取该工作区：{exc}")
    snapshot = collect_snapshot(repo)
metrics = st.columns(4)
metrics[0].metric("架构节点", len(snapshot["nodes"]))
metrics[1].metric("任务状态", snapshot["job"].get("status", "未记录"))
metrics[2].metric("发布验证", "有历史报告 · 需复核" if snapshot["review"] else "未记录")
kb = snapshot["knowledge"]
metrics[3].metric("索引块数", kb.get("chunks", "未记录"))
if kb:
    st.caption(f"知识库快照：{kb.get('workspaces', '未记录')} 个工作区 · {kb.get('snapshot_at', '未记录')} · 未执行完整性或来源新鲜度检查")
benchmark = snapshot.get("benchmark", {})
if benchmark:
    st.caption(f"最近知识库评测快照：{benchmark.get('snapshot_at')} · 评测通过：{benchmark.get('passed', '未记录')} · 结果等价：{benchmark.get('equivalent', '未记录')} · 不代表当前任务出版通过")
dashboard_html = render_dashboard(snapshot)
if hasattr(st, "iframe"):
    st.iframe(dashboard_html, height="content", tab_index=0)
else:
    # Compatibility with the supported Streamlit 1.48+ versions.
    components.html(dashboard_html, height=1220, scrolling=True)
st.download_button("下载审查快照 JSON", json.dumps(snapshot, ensure_ascii=False, indent=2), file_name="architecture-review.json", mime="application/json")
st.download_button("下载字符树 TXT", render_ascii(snapshot), file_name="architecture-tree.txt", mime="text/plain; charset=utf-8")
