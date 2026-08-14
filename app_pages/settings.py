from __future__ import annotations

import streamlit as st

from app_pages._shared import application_service, configured_profile_path
from product_contracts import APP_VERSION, CONTRACT_SCHEMA_VERSION


st.header("本地运行设置")
st.caption("设置只控制 Web UI 产品壳；模型密钥不会保存在这里。")

service = application_service()

with st.form("frontend_settings"):
    profile_path = st.text_input(
        "Profile 配置文件",
        value=str(configured_profile_path()),
        help="必须位于允许的工作区根目录内，并使用 .toml 扩展名。",
    )
    saved = st.form_submit_button("保存当前会话设置", icon=":material/save:")
if saved:
    try:
        resolved = service.source_policy.resolve_file(profile_path, suffixes={".toml"})
        st.session_state["frontend_config_path"] = str(resolved)
        st.success("设置已保存到当前浏览器会话。", icon=":material/check_circle:")
    except (OSError, ValueError) as exc:
        st.error(str(exc), icon=":material/error:")

with st.container(border=True):
    st.subheader("安全边界")
    st.write("允许读取的根目录")
    for root in service.settings.source_roots:
        st.code(str(root), language="text")
    st.write("任务运行目录")
    st.code(str(service.settings.jobs_root), language="text")
    st.write("任务数据库")
    st.code(str(service.settings.database), language="text")
    st.metric(
        "上传上限",
        f"{service.settings.upload_limit_bytes / 1024 / 1024:.0f} MB",
    )

with st.container(border=True):
    st.subheader("运行契约")
    st.write(f"应用版本：`{APP_VERSION}`")
    st.write(f"RunSpec / ArtifactRecord schema：`v{CONTRACT_SCHEMA_VERSION}`")
    st.caption("任务注册表使用 SQLite WAL；每个任务拥有独立 UUID 工作区。")
