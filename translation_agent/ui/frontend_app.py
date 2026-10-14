"""Backward-compatible Streamlit entry point.

``streamlit_app.py`` is canonical; importing it keeps existing launch commands
and bookmarks working while sharing the same ``st.navigation`` application.
"""


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


from pathlib import Path
import runpy


# A normal import would be cached after Streamlit's first rerun and leave the
# compatibility entry point blank.  Execute the canonical script every time.
runpy.run_path(str(Path(__file__).with_name("streamlit_app.py")))
