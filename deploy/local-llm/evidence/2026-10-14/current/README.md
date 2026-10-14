# Hy-MT2 实时翻译证据

采集时间：2026-10-14T00:39:16.800813+08:00。人工验收：**待用户判断**。

模型：`Intel/Hy-MT2-30B-A3B-int4-AutoRound`；提示词版本：`book-translation-v12-paragraph-context-v3`。

以下为小样本诊断材料，源文来自测试样例，英文段落为本次编写的测试文本。原始响应未经改译；流水线产物可能经过繁简转换。机器检查仅观察格式和语言残留，不证明含义、术语、风格或整书质量。完整请求、响应、重试与哈希见 [review.json](review.json)。

## ja-title

原文：

```text
『世界の終りとハードボイルド・ワンダーランド』は村上春樹の長編小説である。「壁」という概念が作品の中心にあり、読者は二つの物語を行き来しながら読み進める。彼は計算機の部屋で、古いデータの符号を整理している。
```

第 1 次原始响应：

```text
『世界的尽头与冷酷仙境』是村上春树创作的长篇小说。『墙』这一概念贯穿全篇，读者需要在两个故事之间来回切换才能理解剧情。他在计算机房里整理着那些陈旧数据的编码。
```

严格标点门：`{'passed': False, 'error': 'Translation changed source quotation glyphs, order, or counts.'}`。

当前本地门：`{'passed': False, 'error': 'Translation changed source quotation glyphs, order, or counts.'}`。

第 2 次原始响应：

```text
『世界的尽头与冷酷仙境』是村上春树创作的长篇小说。作品中以“墙”这一概念为核心，读者需要在两个故事之间来回切换阅读。他正在计算机机房里整理那些陈旧数据的编码。
```

严格标点门：`{'passed': False, 'error': 'Translation changed source quotation glyphs, order, or counts.'}`。

当前本地门：`{'passed': False, 'error': 'Translation changed source quotation glyphs, order, or counts.'}`。

流水线错误：`Translation failed after two requests: Translation changed source quotation glyphs, order, or counts. Expected quote sequence: '『』「」'; received: '『』'; expected punctuation: ('『', '・', '』', '。', '「', '」', '、', '。', '、', '。'); received: ('『', '』', '。', '“', '”', '，', '。', '。').`。人工验收：待用户判断。

## ja-punctuation

原文：

```text
「本当ですか」と彼は言った。『秘密や社会――その光と影』（2015年刊）は、彼の代表作であり…多くの読者（特に若い世代）に愛されている。これは事実である。
```

第 1 次原始响应：

```text
「真的吗？」他说道。『秘密与社会——其光明与阴影』（2015年出版）是他的代表作……深受众多读者（尤其是年轻一代）的喜爱。这是事实。
```

严格标点门：`{'passed': False, 'error': 'Translation changed source punctuation glyphs, order, or counts.'}`。

当前本地门：`{'passed': True, 'error': None}`。

流水线最终产物：

```text
「真的吗？」他说道。『秘密与社会——其光明与阴影』（2015年出版）是他的代表作……深受众多读者（尤其是年轻一代）的喜爱。这是事实。
```

流水线错误：`None`。人工验收：待用户判断。

## en-prose

原文：

```text
The author distinguishes a description of social institutions from a moral judgment. A change in terminology alone does not establish that the argument has changed. The reader must examine the reasons given in the following paragraph.
```

第 1 次原始响应：

```text
作者将对社会制度的描述与道德评判区分开来。仅仅改变术语并不能说明论证立场发生了转变。读者应当仔细审视接下来段落中所给出的理由。
```

严格标点门：`{'passed': False, 'error': 'Translation changed source punctuation glyphs, order, or counts.'}`。

当前本地门：`{'passed': True, 'error': None}`。

流水线最终产物：

```text
作者将对社会制度的描述与道德评判区分开来。仅仅改变术语并不能说明论证立场发生了转变。读者应当仔细审视接下来段落中所给出的理由。
```

流水线错误：`None`。人工验收：待用户判断。
