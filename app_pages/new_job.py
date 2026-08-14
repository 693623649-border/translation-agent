from __future__ import annotations

from pathlib import Path

import streamlit as st

from app_pages._shared import (
    application_service,
    build_spec,
    credential_inputs,
    load_profiles_for_ui,
    profile_default_index,
    profile_label,
    profiles_for_stage,
)
from frontend_runtime import validate_upload_size
from product_paths import default_source_placeholder, recipe_paths


st.header("创建翻译任务")
st.caption("每个任务使用独立 UUID 工作区；重复运行会沿用 Graph 检查点。")

service = application_service()
config_path, profiles, profile_error = load_profiles_for_ui()
if profile_error:
    st.error(f"模型配置不可用：{profile_error}", icon=":material/error:")
    st.stop()
assert profiles is not None

ocr_profiles = profiles_for_stage(profiles, "ocr")
text_profiles = profiles_for_stage(profiles, "translation")
if not text_profiles:
    st.error("配置中至少需要一个文本模型 Profile。", icon=":material/error:")
    st.stop()

recipes = list(recipe_paths())
recipe_options: list[Path | None] = [None, *recipes]

with st.container(border=True):
    st.subheader("来源")
    source_mode_label = st.segmented_control(
        "文档入口",
        ["扫描 PDF", "文字 PDF", "EPUB"],
        default="文字 PDF",
        key="new_source_mode",
    )
    source_mode = {
        "扫描 PDF": "scanned-pdf",
        "文字 PDF": "text-pdf",
        "EPUB": "epub",
    }[source_mode_label or "文字 PDF"]
    source_origin = st.segmented_control(
        "文件来源",
        ["上传", "工作区路径"],
        default="上传",
        key="new_source_origin",
    )
    uploaded = None
    source_path = ""
    if source_origin == "工作区路径":
        source_path = st.text_input(
            "工作区内文件路径",
            placeholder=(
                str(default_source_placeholder(source_mode))
            ),
            help="路径必须位于设置页列出的允许根目录内。",
        )
    else:
        uploaded = st.file_uploader(
            "上传原始文档",
            type=["epub"] if source_mode == "epub" else ["pdf"],
            help=(
                f"上限 {service.settings.upload_limit_bytes / 1024 / 1024:.0f} MB；"
                "文件将保存到任务专属目录。"
            ),
        )

with st.form("new_translation_job"):
    with st.container(border=True):
        st.subheader("出版目标")
        format_options = (
            ["EPUB", "Word"]
            if source_mode == "epub"
            else ["EPUB", "Word", "知识库", "参考 PDF"]
        )
        selected_formats = st.pills(
            "输出格式",
            format_options,
            default=format_options,
            selection_mode="multi",
            key=f"new_formats_{source_mode}",
        )
        translate = st.toggle("翻译为简体中文", value=True)
        verify = st.toggle(
            "执行发布质量门",
            value=source_mode != "epub",
            disabled=source_mode == "epub",
            key=f"new_verify_{source_mode}",
            help="只有 release report 明确通过的文件才会显示为正式产物。",
        )
        if source_mode == "epub":
            st.warning(
                "EPUB 原生发布验证尚未接入。任务可以完成语义翻译和 EPUB/Word 草稿，"
                "但不会标记为正式发布产物。",
                icon=":material/warning:",
            )
        title = st.text_input("书名", placeholder="默认使用源文件名")
        author = st.text_input("作者", placeholder="可选")

    with st.expander("模型与 Graph 设置", icon=":material/tune:"):
        st.caption(f"Profile：{config_path}")
        ocr_profile = None
        if source_mode == "scanned-pdf":
            if not ocr_profiles:
                st.error("扫描 PDF 需要 OCR Profile。")
            else:
                ocr_profile = st.selectbox(
                    "OCR Profile",
                    ocr_profiles,
                    index=profile_default_index(ocr_profiles, profiles.ocr_profile),
                    format_func=profile_label,
                )
        toc_profile = None
        if source_mode != "epub":
            toc_profile = st.selectbox(
                "目录 Profile",
                text_profiles,
                index=profile_default_index(text_profiles, profiles.toc_profile),
                format_func=profile_label,
            )
        proofread_profile = None
        if source_mode == "scanned-pdf":
            proofread_profile = st.selectbox(
                "校勘 Profile",
                text_profiles,
                index=profile_default_index(
                    text_profiles,
                    profiles.proofread_profile or profiles.translation_profile,
                ),
                format_func=profile_label,
            )
        translation_profile = st.selectbox(
            "翻译 Profile",
            text_profiles,
            index=profile_default_index(
                text_profiles, profiles.translation_profile
            ),
            format_func=profile_label,
        )
        recipe = st.selectbox(
            "Graph Recipe",
            recipe_options,
            format_func=lambda value: "自动" if value is None else value.stem,
            help="Recipe 只定义拓扑；模型和密钥仍来自 Profile。",
        )
        include_proofread = st.toggle(
            "OCR 后执行校勘",
            value=False,
            disabled=source_mode != "scanned-pdf",
        )
        toc_source = st.segmented_control(
            "目录来源",
            ["pipeline", "outline"],
            default="pipeline",
        )
        text_pdf_reflow = st.toggle(
            "重排文字 PDF 的视觉换行",
            value=True,
            disabled=source_mode != "text-pdf",
        )
        concurrency = st.number_input(
            "翻译并发",
            min_value=1,
            max_value=64,
            value=int(translation_profile.concurrency),
        )
        force_all = st.checkbox("忽略 Graph 缓存重新执行", value=False)

    credential_names = {
        toc_profile.credential_env if toc_profile is not None else None,
        translation_profile.credential_env if translate else None,
        ocr_profile.credential_env if ocr_profile is not None else None,
        (
            proofread_profile.credential_env
            if include_proofread and proofread_profile is not None
            else None
        ),
    }
    with st.expander("本次任务凭据", icon=":material/key:"):
        st.caption("密钥不会进入 run-spec、SQLite、日志或命令行。")
        credentials = credential_inputs(
            credential_names,
            key_prefix="new_job_credential",
        )

    with st.container(horizontal=True, horizontal_alignment="right"):
        preview_clicked = st.form_submit_button(
            "预览计划", icon=":material/account_tree:"
        )
        submit_clicked = st.form_submit_button(
            "创建并运行",
            type="primary",
            icon=":material/play_arrow:",
        )

format_targets = {
    "EPUB": "publication.epub",
    "Word": "publication.docx",
    "知识库": "publication.knowledge_base",
    "参考 PDF": "publication.reference_pdf",
}
direct_targets = tuple(format_targets[name] for name in (selected_formats or ()))
if source_mode != "epub" and verify:
    targets = (
        ("publication.word_report",)
        if set(direct_targets) == {"publication.docx"}
        else ("publication.report",)
    )
else:
    targets = direct_targets
options = {
    "ocr_profile": ocr_profile.name if ocr_profile is not None else None,
    "toc_profile": toc_profile.name if toc_profile is not None else None,
    "proofread_profile": (
        proofread_profile.name if proofread_profile is not None else None
    ),
    "translation_profile": translation_profile.name,
    "translation_concurrency": int(concurrency),
    "include_proofread": bool(include_proofread),
    "toc_source": toc_source or "pipeline",
    "text_pdf_reflow": bool(text_pdf_reflow),
    "force_all": bool(force_all),
    "generate_epub": "publication.epub" in direct_targets,
    "generate_docx": "publication.docx" in direct_targets,
    "generate_knowledge_base": "publication.knowledge_base" in direct_targets,
    "generate_reference_pdf": "publication.reference_pdf" in direct_targets,
}
source_value = source_path.strip() if source_origin == "工作区路径" else None
spec = build_spec(
    source=source_value,
    source_mode=source_mode,
    config=config_path,
    recipe=recipe,
    title=title,
    author=author,
    targets=targets,
    translate=translate,
    verify=verify,
    options=options,
)

if preview_clicked or submit_clicked:
    if not direct_targets:
        st.error("至少选择一个出版格式。", icon=":material/error:")
    elif (
        source_mode != "epub"
        and verify
        and set(direct_targets)
        not in (
            {"publication.docx"},
            {
                "publication.epub",
                "publication.docx",
                "publication.knowledge_base",
                "publication.reference_pdf",
            },
        )
    ):
        st.error(
            "当前发布门支持“仅 Word”或四种完整产物。若只需部分格式，请关闭发布质量门，"
            "它们将作为草稿生成。",
            icon=":material/error:",
        )
    elif source_origin == "上传" and uploaded is None:
        st.error("请选择要上传的文件。", icon=":material/error:")
    elif source_origin == "工作区路径" and not source_path.strip():
        st.error("请输入工作区内的文件路径。", icon=":material/error:")
    else:
        try:
            if preview_clicked:
                plan = (
                    service.preview_upload_plan(spec, filename=uploaded.name)
                    if uploaded is not None
                    else service.preview_plan(spec)
                )
                st.session_state["new_job_plan"] = plan
            else:
                if uploaded is not None:
                    validate_upload_size(
                        int(uploaded.size),
                        limit=service.settings.upload_limit_bytes,
                    )
                job = (
                    service.submit_upload(
                        spec,
                        filename=uploaded.name,
                        content=uploaded.getbuffer(),
                        credentials=credentials,
                    )
                    if uploaded is not None
                    else service.submit_path(spec, credentials=credentials)
                )
                st.session_state["selected_job_id"] = job.id
                st.success(
                    f"任务 {job.id[:8]} 已进入后台运行。",
                    icon=":material/check_circle:",
                )
        except (OSError, RuntimeError, ValueError) as exc:
            st.error(str(exc), icon=":material/error:")

plan_value = st.session_state.get("new_job_plan")
if plan_value:
    with st.container(border=True):
        st.subheader("DAG 计划")
        for index, node in enumerate(plan_value, start=1):
            st.code(f"{index:02d}  {node}", language="text")
