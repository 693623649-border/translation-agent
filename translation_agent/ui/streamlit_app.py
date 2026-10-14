"""Canonical Streamlit entry point for the local translation workbench."""

from __future__ import annotations


def _ensure_checkout_importable() -> None:
    """Let `python path/to/entry.py` import this package from a checkout."""
    import sys
    from pathlib import Path

    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "translation_agent" / "__init__.py"
        ).is_file():
            root = str(candidate)
            if root not in sys.path:
                sys.path.insert(0, root)
            return


_ensure_checkout_importable()


import streamlit as st

from translation_agent.runtime.product_contracts import APP_VERSION


st.set_page_config(
    page_title="翻译出版工作台",
    page_icon=":material/translate:",
    layout="wide",
)

st.session_state.setdefault("selected_job_id", None)
st.session_state.setdefault("frontend_config_path", "")
st.session_state.setdefault("frontend_recipe_path", "")

pages = [
    st.Page(
        "pages/new_job.py",
        title="新任务",
        icon=":material/add_circle:",
        default=True,
    ),
    st.Page(
        "pages/jobs.py",
        title="任务状态",
        icon=":material/pending_actions:",
    ),
    st.Page(
        "pages/review.py",
        title="人工复核",
        icon=":material/fact_check:",
    ),
    st.Page(
        "pages/artifacts.py",
        title="产物",
        icon=":material/folder_open:",
    ),
    st.Page(
        "pages/settings.py",
        title="设置",
        icon=":material/settings:",
    ),
]

page = st.navigation(pages, position="top")
st.title(":material/translate: 翻译出版工作台")
st.caption(f"本地优先的文档语义翻译与出版流水线 · v{APP_VERSION}")
page.run()
