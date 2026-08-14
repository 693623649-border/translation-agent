"""Backward-compatible Streamlit entry point.

``streamlit_app.py`` is canonical; importing it keeps existing launch commands
and bookmarks working while sharing the same ``st.navigation`` application.
"""

from pathlib import Path
import runpy


# A normal import would be cached after Streamlit's first rerun and leave the
# compatibility entry point blank.  Execute the canonical script every time.
runpy.run_path(str(Path(__file__).with_name("streamlit_app.py")))
