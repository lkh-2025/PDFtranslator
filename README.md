# PDFTranslator · 英文 PDF 转中文（本地离线）

把一篇英文 PDF 读懂、翻成中文、再按中文版式重新排成一份新文档：解析章节与段落、抽取图表与公式、
统一术语、逐段重写译文、中文模板排版，最后通读自检。**全程在本机完成，运行期不联网。**

## 它做什么，不做什么

| 会做 | 不会做 |
| --- | --- |
| 判断单栏 / 双栏，识别标题层级与正文段落 | 扫描件（没有文字层）不能翻，没有内置 OCR |
| 图表、公式、算法整块按原版截图保留，图注与表题翻成中文 | 图内文字保持英文，不做图内 OCR |
| 用术语表统一全文译名，逐段重写并带上下文 | 不保证译文与原文逐句对齐（是「重写」不是「替换」） |
| 按中文模板排版：两端对齐、行首禁则、标题层级 | 不保留原始页数与分页 |
| 自检越界、重叠、缺字形、术语缺失、数字丢失 | 参考文献、作者、DOI 仍保留英文（出版惯例） |

## 环境要求

- Windows 10/11（其他平台未验证），Python **3.10+**
- 依赖：PyMuPDF ≥ 1.24、requests ≥ 2.31；本地 GGUF 推理需要 `llama-cpp-python`；界面拖拽可选 `tkinterdnd2`
- 模型：Qwen2.5-7B-Instruct 的 **GGUF Q4_K_M** 分片，约 4.36 GB，加载后常驻内存约 7–8 GB

## 安装（三步）

```powershell
# 1) 基础依赖（PyMuPDF / requests）
python -m pip install -r requirements.txt

# 2) 安装本工程与本地推理后端（装完后任意目录可用 pdf-translator）
python -m pip install -e ".[offline]"

# 3) 确认版本
pdf-translator --version
```

`.[offline]` 会装上 `llama-cpp-python`（GGUF 推理后端）。只用在线 API 或模拟通道时可以不加，
但本地「自然档」必须装它。需要界面拖拽时再补 `python -m pip install -e ".[ui]"`。

不安装也能用：在项目根目录 `python -m pdf_translator …`，或双击 `scripts\run_ui.bat` 开界面
（这两条路仍需要 `llama-cpp-python` 已装在当前 Python 里）。

## 准备模型

模型权重约 4.36 GB，不随仓库分发，需要单独下载 **Q4_K_M** 量化的两个分片。

### 从哪里下载

任选一个源，内容和文件名一致：

- Hugging Face（官方）：https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF
- ModelScope（国内推荐）：https://modelscope.cn/models/Qwen/Qwen2.5-7B-Instruct-GGUF
- HF 镜像：https://hf-mirror.com/Qwen/Qwen2.5-7B-Instruct-GGUF

浏览器直接下载这两个文件：

1. https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf?download=true
2. https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf?download=true

命令行下载（Hugging Face）：

1. `python -m pip install -U huggingface_hub`
2. `hf download Qwen/Qwen2.5-7B-Instruct-GGUF --include "qwen2.5-7b-instruct-q4_k_m-*" --local-dir D:\PDFTranslator\models\llm-gguf`

`hf` 命令较旧时改用 `huggingface-cli download`，参数相同。国内直连 Hugging Face 慢时，先执行：

1. `$env:HF_ENDPOINT = "https://hf-mirror.com"`

再执行上面的 `hf download`。

命令行下载（ModelScope）：

1. `python -m pip install -U modelscope`
2. `modelscope download --model Qwen/Qwen2.5-7B-Instruct-GGUF --include "qwen2.5-7b-instruct-q4_k_m-*" --local_dir D:\PDFTranslator\models\llm-gguf`

不要 `git clone` 整个仓库：里面还有 fp16、q2、q3、q5、q6、q8 等十多个量化版本，全量约 50 GB。只要 `q4_k_m` 这两个分片。

### 放到哪里

模型目录的查找顺序（命令行与界面共用，见 `src/pdf_translator/paths.py`）：

1. 环境变量 `PDF_TRANSLATOR_MODEL_DIR`
2. `%APPDATA%\PDFTranslator\config.json` 里的 `"model_dir"`
3. 默认：有 D 盘时 `D:\PDFTranslator\models`，否则 `%LOCALAPPDATA%\PDFTranslator\models`

在模型目录下建一个 `llm-gguf\`，放入这两个分片（文件名保持原样，不要求改名）：

```
llm-gguf\qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf   3,993,201,344 字节
llm-gguf\qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf     689,872,288 字节
```

放好后先自检一次（只读磁盘，不翻译）：

```powershell
pdf-translator --check-models
```

正常输出会列出两个分片与合计大小，最后一行是 `加载检查：llama.cpp 可加载`。
退出码：`0` 可用、`3` 缺文件、`4` 加载失败。

换目录时二选一：

```powershell
$env:PDF_TRANSLATOR_MODEL_DIR = "E:\PDFTranslator\models"
```

```powershell
'{"model_dir": "E:\\PDFTranslator\\models"}' | Set-Content "$env:APPDATA\PDFTranslator\config.json" -Encoding UTF8
```

## 用界面

```powershell
pdf-translator-ui
```

也可以双击 `scripts\run_ui.bat`，或 `python -m ui.app`。

用法：拖入 PDF → 点「开始翻译」→ 完成后自动打开产物目录。右侧「使用说明」（或按 `F1`）有同样一份
分段说明，不用翻文档。

界面与命令行共用同一套五阶段引擎：同样的阶段缓存、全文术语表、上下文翻译与自检规则；
中断后重开界面，已完成的段落会直接复用。

界面只有一条链路：**版式固定为中文重排**。翻译服务可选「自然档」（本地离线，默认）、
「在线 API」（填接口地址与 Key）与「模拟」（占位译文，只用于试跑界面，别拿它当真结果）。
选择的翻译服务会记在 `config\ui_settings.json`，下次启动沿用；「完成后自动打开」只在本次运行有效。

## 用命令行

```powershell
# 最常用：整篇翻译，产物默认写到 out\results\<原文件名>_zh.pdf
pdf-translator 你的论文.pdf --provider offline

# 指定输出位置
pdf-translator 你的论文.pdf --provider offline -o out\results\demo_zh.pdf

# 只重排版（改过样式、不动译文）
pdf-translator 你的论文.pdf --provider offline --stage layout
```

> `--provider` 的默认值是 `mock`（占位译文）。**真翻译必须写 `--provider offline`**，
> 这是最常踩的一个坑。

只跑某一个阶段时，其余阶段读工作区里已有的 JSON；上游 JSON 不存在会提示先跑对应阶段。

## 五个阶段

整条链路拆成五步，每步只读写工作区里的 JSON，可单独重跑：

| 阶段 | 产物 | 做什么 |
| --- | --- | --- |
| ① `structure` | `structure.json` | 单/双栏判定、标题分级、正文段落、图表位置；公式与算法整块、行列不可靠的宽表另存 200 dpi 截图 |
| ② `glossary` | `glossary.json` | 全文术语表：命中 `config\glossary.tsv` 的给中文译名，其余标「保留原文」 |
| ③ `translate` | `translation.json` | 逐段翻译，每段带术语提示与相邻段落语境；按内容指纹缓存，结构重跑不重译 |
| ④ `layout` | `layout.json` | 中文模板排版：两端对齐、行首禁则、标题层级、图注与图表同页 |
| ⑤ `check` | `selfcheck.json` | 通读自检：越界、重叠、缺字形、行首标点、提示词回吐、术语一致性与数字丢失 |

```powershell
# 全流程
pdf-translator 论文.pdf --provider offline --stage all

# 只补跑术语（改完 glossary.tsv 之后）
pdf-translator 论文.pdf --provider offline --stage glossary
```

## 产物在哪

- 译文：`out\results\<原文件名>_zh.pdf`（可用 `-o` 另存）
- 阶段产物：`out\reports\<slug>\`（上面五个 JSON，可单独重跑）
- 自检报告：`out\reports\<slug>\selfcheck.json`（命令行会打印路径）

想把一轮产物集中到别处（只读环境、临时试验）时用环境变量覆盖根目录：

```powershell
$env:PDF_TRANSLATOR_OUT_DIR = 'D:\work\run1'
```

## 自检与退出码

阶段⑤默认开启，检查越界、文字重叠、缺字形、行首禁则、提示词回吐、漏译、空页与孤立标题；
不通过时命令行退出码为 `1`，界面会弹窗列出问题项。加 `--no-selfcheck` 可完全跳过阶段⑤。
想单独复核一份已经生成的文件：

```powershell
python tools\check_reflow.py 原文.pdf out\results\demo_zh.pdf
```

## 参数速查

| 参数 | 说明 |
| --- | --- |
| `-o, --output` | 输出 PDF 路径 |
| `--provider offline\|openai\|mock` | 翻译通道；默认 `mock`，真翻译用 `offline` |
| `--tier llm` | 离线档位，当前只有 `llm`（GGUF int4） |
| `--stage structure\|glossary\|translate\|layout\|check\|all` | 跑到哪个阶段为止 / 只跑哪个阶段 |
| `--layout reflow` | 版式固定为中文重排，保留参数只为兼容旧脚本 |
| `--font 字体文件` | 排版用中文字体，默认按 宋体 → 思源宋体 → 微软雅黑 回退 |
| `--check-models` | 列出模型并试加载一次（不翻译） |
| `--no-selfcheck` | 跳过产物自检 |
| `--api-key / --base-url / --model` | 走 OpenAI 兼容接口时的配置（Key 也可用 `PDF_TRANSLATOR_API_KEY`） |
| `--version` | 版本号 |

## 术语表怎么加词

`config\glossary.tsv`，一行一条，`原文<TAB>译法`：

```
token pruning	令牌剪枝
Vision Transformer	Vision Transformer
Northwind Analytics	
```

- 第三行这种「译法留空」表示**保留英文原文**，适合方法名、品牌名。
- 匹配不区分大小写、长词优先，加词不需要改代码，也不必重启（每轮从磁盘读）。

## 常见问题

**跑一篇要多久？**
第③阶段按段落串行生成，本地 GGUF Q4_K_M 实测每段约 40–60 秒；一篇 10 页论文走完五个阶段
通常 20–40 分钟（含术语、排版、自检）。改样式只想重排时用 `--stage layout`，跳过翻译。

**中断了要重头跑吗？**
不用。第③阶段按内容指纹缓存，重跑 `--stage translate` 只会补没译完的段落。

**提示「未找到语言模型」？**
把两个 GGUF 分片放进提示的目录（默认 `D:\PDFTranslator\models\llm-gguf`），再 `--check-models`。

**PowerShell 里中文是乱码？**
控制台编码问题，不是程序问题。加 `$env:PYTHONIOENCODING='utf-8'`，或用 `python -X utf8 -m pdf_translator …`。

**翻译后参考文献还是英文？**
刻意保留：参考文献、作者/单位、DOI/arXiv 与图内小字号标注不翻，避免姓名与编号被误译。

**扫描件能翻吗？**
不能。PDF 必须带文字层；扫描件请先自己做 OCR。

**能翻日文、法文 PDF 吗？**
不能。这是「英 → 简中」单向工具。

## 目录结构

```
PDFtranslator/
├─ src/
│  ├─ pdf_translator/          解析 / 术语 / 翻译 / 排版 / 自检 / 命令行入口
│  │  ├─ __main__.py           命令行
│  │  ├─ paths.py             模型与产物目录解析
│  │  ├─ textfilter.py        段落分类与标点规则（reflow 共用）
│  │  ├─ providers/           offline（GGUF）/ openai / mock 与后处理
│  │  └─ reflow/              五阶段：structure → glossary → translate → layout → check
│  └─ ui/                      Tkinter 单窗口界面与主题
├─ tools/                      自检、结构统计、预览、术语审计、界面截图
├─ tests/                      不依赖大模型的最小回归测试
├─ config/                     glossary.tsv、terms_cache.json、ui_settings.json
├─ docs/                       项目日志
├─ out/                        results（译文）、reports（阶段 JSON 与自检）
└─ scripts/run_ui.bat          免安装启动界面
```

## 工具脚本

| 脚本 | 用途 |
| --- | --- |
| `python tools\check_reflow.py 原文 译文` | 版式与内容自检（与阶段⑤同一套规则） |
| `python tools\structure_report.py 原文` | 只跑阶段①，打印章节/段落/图表/公式统计 |
| `python tools\reflow_preview.py 甲.pdf 乙.pdf preview.png` | 生成左右并排的预览图，方便对比两份产物或原稿 |
| `python tools\term_audit.py --source … --translated …` | 专名保留审计 |
| `python tools\shot_ui.py` | 把界面截图存到 `docs\images\`（开发用） |

脚本会自动把 `src/` 加进 `sys.path`，在项目根目录直接运行即可。

## 回归测试

不使用本地大模型，用占位翻译器跑通五阶段，验证结构、术语、翻译、排版、自检的产物链路：

```powershell
python -m unittest discover -s tests -v
```

## 已知取舍

- 图内文字仍是英文（不做图内 OCR）；参考文献与作者保留英文。
- 行内公式退化为 Unicode 近似，展示公式与算法整块按原版截图贴入。
- 排版尺寸：A4、左右 64pt / 上下 66pt、正文 10.5pt 行距 1.62；截图不放大到超过原始版面宽度。
- 单篇论文实测页数会变化（11→16、11→16、10→13 页），这是「重排」而非「套版」的正常结果。

## 许可与致谢

- 本项目采用 **AGPL-3.0**（见 `LICENSE`）。
- 核心依赖 PyMuPDF 同为 AGPL-3.0 或 Artifex 商业授权，对外分发前请确认合规路径。
- 语言模型 Qwen2.5-7B-Instruct（Apache-2.0），本机以 GGUF Q4_K_M 运行。
- 其他依赖：llama-cpp-python（MIT）、requests（Apache-2.0）、tkinterdnd2（MIT）。
