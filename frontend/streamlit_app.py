"""Canonical Streamlit entry point for the local translation workbench."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from orchestration.product_contracts import APP_VERSION


st.set_page_config(
    page_title="翻译出版工作台",
    page_icon=":material/translate:",
    layout="wide",
)

st.session_state.setdefault("selected_job_id", None)
st.session_state.setdefault("frontend_config_path", "")
st.session_state.setdefault("frontend_recipe_path", "")

pages = [
    st.Page(str(ROOT / "app_pages" / "architecture.py"), title="架构与审查", icon=":material/account_tree:"),
    st.Page(
        str(ROOT / "app_pages" / "new_job.py"),
        title="新任务",
        icon=":material/add_circle:",
        default=True,
    ),
    st.Page(
        str(ROOT / "app_pages" / "jobs.py"),
        title="任务状态",
        icon=":material/pending_actions:",
    ),
    st.Page(
        str(ROOT / "app_pages" / "artifacts.py"),
        title="产物",
        icon=":material/folder_open:",
    ),
    st.Page(
        str(ROOT / "app_pages" / "settings.py"),
        title="设置",
        icon=":material/settings:",
    ),
]

page = st.navigation(pages, position="top")
st.title(":material/translate: 翻译出版工作台")
st.caption(f"本地优先的文档语义翻译与出版流水线 · v{APP_VERSION}")
page.run()
