# 架构与审查面板

工作台的“架构与审查”页面采用等宽字符终端树图、彩色字符框线与独立审查栏。
框线由实际 `┌─┐│└┘` 等 Unicode 字符生成，布局仿照用户提供的终端参考图；
HTML 交互面板与纯文本导出使用同一份 118 列字符布局。
本版本用于项目架构与任务证据检查，不更改模型配置或启动任务。

```powershell
python -m streamlit run streamlit_app.py --server.address 127.0.0.1
```

在工作台导航中选择“架构与审查”，或访问 `/architecture`。

## 使用

1. 选择项目总览、已有任务或发布工作区。
2. 点击节点查看职责、实际节点 ID、源码文件及行号、已记录的状态和证据。
   Tab 和 Enter 也可切换与选择节点。
3. 查看最近事件，识别执行失败、开始记录、完成记录与缓存复用。
4. 用“刷新快照”更新本地证据，用“下载审查快照 JSON”保存审查材料。
5. 用“下载字符树 TXT”保存可复制的字符画；窄屏可在字符画区域横向滚动。

节点定义从 `pipeline_graph/book.py` 的实际常量提取。任务数据来自现有
ApplicationService 与 SQLite 任务登记，节点事件来自工作区的
`.pipeline_graph/state.json`、`events.jsonl`。知识库规模通过只读 SQLite
计数查询取得；评测结果来自 `work/kb-core-optimization/benchmark.json`。

图中的连接表示模块分组，具体运行依赖仍由所选任务的 recipe 决定，
不能把总览当成该任务的精确 DAG。没有执行记录的节点显示“未记录”。
执行成功和缓存复用均不等于出版质量通过；历史发布报告与知识库评测
明确显示其快照时间，不自动宣称当前来源仍有效。

## 边界

- 数据收集只读取有界 JSON 与事件尾部；不读取环境密钥。
- 展示字段使用白名单，嵌入数据转义，动态详情使用 textContent。
- 发布工作区必须位于允许的输出目录；外部路径和逃逸链接被拒绝。
- 页面刷新不运行模型请求、完整语料哈希或数据库 integrity 检查。
- HTML 画布在 iframe 内维护样式；现有任务、产物和设置页面不受影响。
- 较新 Streamlit 使用 `st.iframe` 自动适配内容高度；旧受支持版本保留
  components.html 兼容路径。

设计约定见仓库根目录 `DESIGN.md`。离线快照示例位于本机
`work/kb-core-optimization/architecture.html`，它表示生成时的证据。
