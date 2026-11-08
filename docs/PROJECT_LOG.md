# 项目日志（PROJECT_LOG）

> **一句话定位**：本地运行的英文 PDF → 简体中文 PDF 翻译工具。先通读结构，再按中文版式重排（reflow 五阶段），离线 Qwen2.5-7B（GGUF int4 / llama.cpp）与 OpenAI 兼容 API 双通道，Tkinter 单窗口界面。**已回到源码原型阶段**：安装包、自动下载、发版目录均已移除。
>
> **当前版本状态**：0.1.0 源码原型。唯一发行档为 llm，后端只有 **GGUF Q4_K_M（约 4.36 GB）**；CT2 int8 / NLLB / Argos 代码已删除，`--layout fill` 已下线。模型目录由 `models_dir()` 统一解析（环境变量 `PDF_TRANSLATOR_MODEL_DIR` > `%APPDATA%\PDFTranslator\config.json` 的 `model_dir` > 默认 `D:\PDFTranslator\models`，无 D 盘退回 `%LOCALAPPDATA%\PDFTranslator\models`）。界面与命令行共用 reflow 五阶段。
>
> **最后更新**：2026-11-08

本文档用于在任何一次迁移、换机、重建环境后快速恢复关键信息。新增改动、迁移或验证时，请在文末按日期追加条目。

---

## 1. 环境基线

| 项目 | 值 |
| --- | --- |
| 操作系统 | Windows 10.0.26200（`platform.platform()` = `Windows-10-10.0.26200-SP0`） |
| Python 解释器 | `C:\Users\HONOR\AppData\Local\Programs\Python\Python310\python.exe` |
| Python 版本 | 3.10.2（MSC v.1929 64 bit AMD64） |
| 虚拟环境 | **无**。项目目录下没有 `.venv`，全部依赖装在系统 Python 中 |
| CPU / 内存 | 16 逻辑核 / 8 物理核，31.5 GB，无 CUDA（纯 CPU 推理） |
| 依赖安装方式 | `python -m pip install -r requirements.txt`；离线档再装 `requirements-offline.txt`；运行期依赖见 `requirements-runtime.txt` |

关键依赖实测版本（`importlib.metadata.version`）：

| 包 | 版本 |
| --- | --- |
| PyMuPDF（`import fitz`） | 1.26.7 |
| requests | 2.32.5 |
| ctranslate2 | 4.8.5 |
| sentencepiece | 0.2.2 |
| tokenizers | 0.22.2 |
| numpy | 1.26.3 |
| torch | 2.11.0 |
| huggingface-hub | 1.5.0 |
| psutil | 未安装（硬件探测按 SMT 估算物理核） |

> 注意：`fitz` 没有独立发行包元数据，必须查 `PyMuPDF`；`psutil` 缺失属正常，不影响运行。

---

## 2. 目录与路径约定

设计目标：**不写死盘符**，源码运行与打包运行都能自动定位模型、输出、词表与缓存，因此可从 C 盘整体搬到 D 盘或任意目录。

| 资源 | 源码运行时 | 打包后 | 代码依据 |
| --- | --- | --- | --- |
| 项目根 | `Path(__file__).resolve().parents[2]` | `Path(sys.executable).resolve().parent` | `pdf_translator/providers/offline_config.py` 的 `PROJECT_ROOT` / `_runtime_root()` |
| 模型目录 | `<根>/models` | `<exe 所在目录>/models` | `DEFAULT_MODEL_DIR`、`bundled_model_dir()` |
| 词表 | `<根>/glossary.tsv` | `<exe 所在目录>/glossary.tsv` | `DEFAULT_GLOSSARY` |
| 术语缓存 | `<根>/terms_cache.json` | `<exe 所在目录>/terms_cache.json` | `DEFAULT_TERMS_CACHE` |
| 输出目录 | `<根>/out` | `<exe 所在目录>/out` | `ui/app.py` 的 `APP_DIR / "out"`；命令行默认同上 |
| 界面设置 | `<根>/ui_settings.json` | `<exe 所在目录>/ui_settings.json` | `ui/app.py` 的 `APP_DIR / SETTINGS_FILE` |

可用环境变量覆盖模型目录：`PDF_TRANSLATOR_MODEL_DIR`。运行期**不会联网下载模型**。

---

## 3. 迁移记录

### 2026-11-05：C 盘 → D 盘

- **原因**：C 盘空间不足，项目连同 `models/` 与 `dist/` 整体迁到 `d:\PDFtranslator`。
- **原路径**：`C:\Users\HONOR\Desktop\PDFtranslator`。
- **排查结论**：
  - 代码与脚本没有硬编码盘符，路径均由 `Path(__file__)` / `$PSScriptRoot` / `%~dp0` 推导。
  - 项目没有 `.venv`，系统 Python 3.10.2 依赖齐全。
  - README 已改为中性路径表述（不写死 C:/D:）。
- **迁移后验证命令与结果**（产物 `out\relocation_check.pdf`，未覆盖历史产物）：
  1. `python -m pdf_translator samples\sample_en.pdf -o out\relocation_check.pdf --provider mock` → 3 页 → 3 页。
  2. `python tools\verify.py samples\sample_en.pdf out\relocation_check.pdf` → 全部校验通过。
  3. `python tools\check_layout.py samples\sample_en.pdf out\relocation_check.pdf` → 横向/纵向溢出 0。
  4. `dist\PDFTranslator\PDFTranslator.exe --smoke` → 生成 `dist\PDFTranslator\out\_smoke.json`，`ok: true`、`frozen: true`、`available_tiers: ["balanced","quality"]`。

---

## 4. 模型档位清单

| 档位 | 模型 | `model.bin` 实测体积 | tokenizer 位置 | 界面可见性 | 实测速度 | 当前状态 |
| --- | --- | --- | --- | --- | --- | --- |
| `balanced` | NLLB-200-distilled-600M | 593.75 MB | `models\balanced\tokenizer\tokenizer.json`（16.53 MB） | 可见、可选 | 样例约 6.2~6.5 s/页；论文 3 页约 32~35 s/页；界面估算 37 s/页 | **稳定可用** |
| `quality` | NLLB-200-distilled-1.3B | 1317.81 MB | `models\quality\tokenizer\tokenizer.json`（16.53 MB） | 可见、可选 | 样例约 9.6~10.2 s/页；论文 3 页约 95~97 s/页；界面估算 51.4 s/页 | **稳定可用** |
| `max` | NLLB-200-3.3B | 3200.58 MB | `models\max\tokenizer.json`（16.53 MB，根目录） | 界面列出但置灰 | 样例约 18~25 s/页（含加载） | 命令行**实际可运行**；未做全量稳定性验证 |
| `llm` | Qwen2.5-7B-Instruct | 7269.63 MB | `models\llm\tokenizer.json`（6.71 MB，根目录） | 不在界面列表（`TIER_ORDER` 只有 balanced/quality/max） | 无法加载 | **不可用**：`model.bin` 损坏，`ctranslate2.Generator` 报 `Invalid string length in model.bin` |
| `light` | Argos Translate en→zh | 未随包 | — | 不在界面列表 | 未测 | 未随包 |

> `max` 与 `llm` 的 `model.bin` 合计约 10470 MB（约 10.47 GB 十进制 / 10.23 GiB）。

**关于“缺 tokenizer”的更准确定位**：

- 加载器 `offline.py::_tokenizer_file()` 同时接受 `models\<tier>\tokenizer\tokenizer.json` 与 `models\<tier>\tokenizer.json`。
- `max` / `llm` 的 tokenizer 放在**模型根目录**，所以命令行加载器能找到；`max` 已实测可翻译。
- 界面自检 `ui/app.py::_tier_available()` 只认 `models\<tier>\tokenizer\tokenizer.json`，因此把 `max` 置灰，并显示“未随包提供”。这是**界面自检与加载器口径不一致**，不是 tokenizer 文件缺失。
- `llm` 即使 tokenizer 存在也无法运行，真正原因是 `model.bin` 损坏。

---

## 5. 本轮多档位稳定性验证（2026-11-05）

### 5.1 条件

- 全部为**真实离线推理**，没有 mock、没有在线 API。
- 统一加 `--no-web-terms`，关闭维基/Wikidata 专名检索，避免网络波动干扰稳定性判断。
- 同一档位、同一输入各跑两遍，对比页面文本哈希、中文字数、专名报告。
- 论文使用根目录 CVPR PDF 的**前 3 页**副本 `out\tier_paper_p1-3.pdf`（10 页全跑耗时过长，按要求只跑前 3 页并在此注明）。
- 产物命名为 `out\tier_<tier>_<input>_r{1,2}_zh.pdf`，未覆盖任何 `before_*` / `after_*` / `paper_*` / `relocation_check` 历史产物。

### 5.2 稳定性结论

| 档位 × 输入 | 两次文本 SHA256 前 16 位 | 逐页文本一致 | 中文字数（次1 / 次2） |
| --- | --- | --- | --- |
| balanced × sample_en | `f3de80e61534c37f` | 是（3/3 页） | 931 / 931 |
| quality × sample_en | `af054174930effe9` | 是（3/3 页） | 936 / 936 |
| balanced × sample_narrow_en | `34f7958fff062a59` | 是（2/2 页） | 509 / 509 |
| quality × sample_narrow_en | `3e1e11369ea29582` | 是（2/2 页） | 503 / 503 |
| balanced × paper p1-3 | `42ccb5252c124cbe` | 是（3/3 页） | 6194 / 6194 |
| quality × paper p1-3 | `526b19a585800551` | 是（3/3 页） | 6235 / 6235 |
| max × sample_en（补充） | `dfc5dd914e8a77e3` | 是（3/3 页） | 958 / 958 |

结论：在相同机器、相同线程数、相同解码参数下，balanced / quality（以及补充验证的 max）**输出逐字节一致**，未发现采样或缓存导致的非确定性。

专名报告 JSON 两次唯一的差异是 `output` 路径与 `generated` 时间戳；`resolved`、`protected`、`kept_blocks` 等业务字段完全一致。

### 5.3 耗时与排版指标

| 档位 | 输入 | 页数 | 总耗时（次1 / 次2） | 秒/页（次1 / 次2） | 中文字数（verify） | 横溢 | 纵溢 | 行距 σ（均值/最大） | 最小字号比 | 末行填充 | 行首禁则 | 回填警告（截断/保留原文） |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| balanced | sample_en | 3 | 19.10 s / 18.78 s | 6.37 / 6.26 | 491 | 0 | 0 | 0.00 / 0.00 | 1.00 | 67% | 0 | 0 |
| quality | sample_en | 3 | 30.61 s / 28.86 s | 10.20 / 9.62 | 495 | 0 | 0 | 0.00 / 0.00 | 1.00 | 70% | 0 | 0 |
| max | sample_en | 3 | 76.06 s / 53.84 s | 25.35 / 17.95 | 512 | 0 | 0 | 0.00 / 0.00 | 1.00 | 70% | 0 | 0 |
| balanced | sample_narrow_en | 2 | 12.41 s / 12.89 s | 6.21 / 6.45 | 307 | 0 | 0 | 0.00 / 0.00 | 0.80 | 68% | 0 | 0 |
| quality | sample_narrow_en | 2 | 19.49 s / 19.36 s | 9.75 / 9.68 | 294 | 0 | 0 | 0.00 / 0.00 | 0.80 | 72% | 0 | 0 |
| balanced | paper p1-3 | 3 | 94.67 s / 106.43 s | 31.56 / 35.48 | 2960 | 0 | 0 | 0.90 / 5.07 | 0.80 | 56% | 0 | 0 |
| quality | paper p1-3 | 3 | 284.80 s / 292.30 s | 94.93 / 97.43 | 2991 | 0 | 0 | 0.89 / 4.98 | 0.80 | 60% | 0 | 0 |

补充说明：

- `verify.py` 均通过（论文的标题/URL 两项除外，原因见 6.1、6.2）。
- `check_layout.py` 全部通过：所有产物横向溢出 0 行、纵向溢出 0 行、行首禁则 0 行、超出页面边界 0 行。
- “回填警告”为 0，表示没有出现“字号缩到下限仍放不下而保留原文/截断”的情况。
- 论文的结构块保留：两档 `kept_blocks = 1`（参考文献/作者/DOI 类），属于刻意行为。
- 论文 3 页副本的耗时明显高于界面估算值（balanced 37 s/页、quality 51.4 s/页），因为前 3 页含多个被 PDF 合并的超长段落，单段解码时间远高于平均页；这是输入结构差异，不是档位退化。

### 5.4 窄栏/双栏/长句退化检查

本轮未发现某一档在窄栏或双栏上出现“溢出明显更多”的情况：两档在窄栏样例与论文上的溢出均为 0，最小字号比同为 0.80 下限。

能观察到的差异是**译文质量与长度**，不是排版失败：

- 窄栏样例标题：balanced 译作“季度业务审查”，quality 译作“四季度运营情况审查”（多出“四”，语义偏差）。
- 论文第 2 页末行填充：balanced 40%，quality 27%；两档都通过检查，但 quality 译文更短，页内留白更多。
- 结论：本轮不建议改排版算法；quality 的偏差属于模型译文问题，不是回填问题。

---

## 6. 已知问题与待确认事项

### 6.1 `verify.py` 的论文 URL 检查失败（两档均如此，非档位差异）

- 现象：CVPR 论文第 1 页 `Our code is available at https://github.com/ML-Security-Research-LAB/DeepCompress-ViT.`，译文可读，但 `verify.py` 报 `URL/公式/代码被改动：['https://github.com/ML-Security-']`。
- 原因：原文 URL 在 PDF 中于 `ML-Security-` 后换行，PyMuPDF 提取文本时把软连字符丢掉，文本单元变成 `ML-SecurityResearch-LAB`；译文按同一文本回填，视觉上只是断行，`verify.py` 按空白分词却拿不到带连字符的完整 token。
- 影响：仅影响 `verify.py` 的严格字符串比对；不是模型丢内容，也不影响排版。
- 最小改动建议（未实施）：在 `verify.py` 的 URL 比对前做“去空白 + 可选连字符归一化”，或在 `textfilter.py` 的行内保护里加入 URL 模式，让 URL 走占位符原样回填。

### 6.2 `verify.py` 的论文标题检查失败（预期行为）

- `verify.py` 默认要求 `DeepCompress-ViT` 被替换成中文，但方法名按设计保留英文，因此报“英文标题未被替换”。
- 属于检查器默认参数与论文场景不匹配；用 `--title` / `--expected` 指定合适值即可。

### 6.3 窄栏样例的标题参数

- `samples/sample_narrow_en.pdf` 的标题是 `Quarterly Operations Review`，不是默认的 `Annual Financial Overview`。
- 校验命令需显式传参：`python tools\verify.py samples\sample_narrow_en.pdf out\xxx_zh.pdf --title "Quarterly Operations Review" --expected "季度"`。

### 6.4 `max` 档的界面可见性

- 模型完整、命令行可跑，但界面因只检查 `models\max\tokenizer\tokenizer.json` 而置灰。
- 待用户决定：是否把界面自检改成与加载器一致（同时接受根目录 `tokenizer.json`），从而在界面暴露 3.3B 档。本次未改，以免改变既有产品行为。

### 6.5 `llm` 档与磁盘占用

- `models\llm\model.bin` 加载失败：`RuntimeError: Invalid string length in model.bin`，判定为下载不完整/文件损坏。
- `models\max` 与 `models\llm` 合计约 10.47 GB。未删除、未移动、未联网补齐，等待用户决定“清理 / 重新下载补齐 / 暂时保留”。
- `models\hf` 下还有 Hugging Face 原始权重与下载缓存（含 `.locks`、`blobs`、`snapshots`），不属于运行期必需，可在确认后单独清理。

### 6.6 是否新建 `.venv`

- 当前依赖全部装在系统 Python 3.10.2 中，项目可正常运行。
- 待用户决定是否新建 `.venv` 以隔离依赖；本次未创建。

### 6.7 其他真实限制

见 README「已知限制」：扫描件无文字层不能翻译（无 OCR）、旋转文本保留原文、复杂多栏/合并单元格依赖启发式、参考文献/作者/DOI/图内 <6.9pt 标注刻意保留英文、统一使用内置黑体、极端小单元格可能保留原文。

---

## 7. 关键设计与决策记录

> 只记录有代码依据的既有决策；未发生的历史不补写。

### 2026-11-05：为什么参考文献、作者/单位、DOI/arXiv 保留原文

- `textfilter.py` 明确把参考文献条目、作者/单位、DOI/arXiv、图内小标注列为不翻译类型。
- `extract.py` 在文档级再兜底：首页标题与摘要之间的作者/单位行、`References` 标题及其后条目整段跳过；跨页规则同样处理 DOI/arXiv。
- `offline.py` 在送模型前再做一次结构块判定（编号+年份密集的参考文献、带单位上标的作者行、会议水印），避免模型对姓名/编号产生幻觉译文。
- 结果：CVPR 论文第 9、10 页中文数为 0，这是刻意行为，不是漏译。

### 2026-11-05：为什么“译文与原文一致”的块不擦除

- `render.py` 在回填前比较标准化后的译文与原文；一致的块（保留原文的参考文献/作者等）不 redaction、不重排，原 PDF 的字号、行距、字距完全保留。
- 原因：这类块本来就与原版等长，再走“擦除+重排”必然溢出或截断；1.3B 重跑曾出现 3 条此类截断，改为跳过回填后消失。

### 2026-11-05：为什么使用 `china-s` 字体

- `config.py` 固定 `CJK_FONT_NAME = "china-s"`，即 PyMuPDF 内置简体中文字体（Droid Sans Fallback），无需额外字体文件，打包后也不会因系统缺字体而乱码。
- `render.py` 用 `china-s` 的真实字体度量计算折行宽度；注释记录了不能用普通 `fitz.Font` 拉丁宽度代替，否则右侧溢出。

### 2026-11-05：为什么 6.9pt 以下的图内标注不翻译

- `textfilter.py` 对 `size < 6.9` 且长度/词数受限、且不是图注（CAPTION）的文本直接跳过。
- 原因：坐标轴、图例、流程标签所在的块极窄，翻译后中文几乎必然缩到最小字号或反复截断；保留原文比输出半截中文更稳。
- 对应的检查器也只统计真正写入的中文行，图内保留的拉丁小字不算溢出或字号违规。

### 2026-11-05：回填的自适应策略

- 从原字号开始，按栏边界/相邻单元格估算可用宽度折行；放不下时以 0.5pt 递减，最低 5.5pt（`config.MIN_FONT_SIZE`）。
- 仍放不下允许向下轻微扩张（有上限，且不越过下一文本块/图片）；单行标题最多横向压缩 12%；再放不下则保留原文并提示，不写半截译文。
- 中文折行带标点行首禁则；英文单词与 URL 尽量不拆开。

### 2026-11-05：离线推理的确定性参数

- `offline.py` 的 NLLB 调用固定 `beam_size=4`、`patience=1.0`、`length_penalty=1.0`、`repetition_penalty=1.1`、`no_repeat_ngram_size=3`、`max_decoding_length=512`、`min_decoding_length=5`。
- 本次双跑验证表明：相同输入下输出完全一致；确定性由“固定 beam + 单进程内缓存 + 关闭联网术语检索”共同保证。

---

### 2026-11-06：离线优先收口——max 可用、verify 去假失败、样例防覆盖、CLI 一致、专名保真

- 变更/结论：
  - **档位判定统一**：新增 `offline_config.tokenizer_json_path()` 与 `tier_model_available()`，口径固定为 `model.bin` 存在 + `tokenizer/tokenizer.json` 或模型根目录 `tokenizer.json` 任一存在。`ui/app.py::_tier_available`、`offline_cli --list-tiers`、`build.ps1` 全部改用同一口径；max（3.3B，tokenizer 在模型根目录）恢复可选并进入完整包。精简包仍只含 balanced。
  - **max 文案**：`TIER_PAGE_SECONDS` 改为样例实测口径（max 25、quality 10、balanced 6.5 秒/页），界面明确提示 max 明显更慢、占用内存更高；加载失败提示包含档位、模型目录和可读原因（例如改用 balanced/quality）。
  - **verify.py 去假失败**：URL/公式/代码比对前统一去空白并容忍换行处丢失的连字符；未显式传 `--title` 时从源 PDF 自动取最大字号标题行，找不到则打印「跳过标题检查（未指定 --title）」并按跳过处理；显式传参保持严格。
  - **样例脚本防覆盖**：`make_sample.py` / `make_narrow_sample.py` 增加 argparse（`--help` / `--out` / `--force`），默认输出已存在时拒绝重写，只有显式 `--force` 才覆盖；默认路径与文件名不变。
  - **命令行一致**：`python -m pdf_translator --provider offline --tier <max|quality|balanced|light|llm>` 已可用；`offline_cli --list-tiers` 首列补齐空格，max 显示「实测约 25 s/页」，llm 因存在 `model.bin.parts` 标注为「损坏/不可用」，未加载 7B 模型。
  - **离线默认**：`OfflineSettings.web_terms` 默认 `False`，界面高级设置新增「联网查询专有名词（需要联网；默认关闭）」；API 选项标注「需联网 + API Key，可选」，离线流程不要求任何 Key。`--web-terms` 为显式开启，`--no-web-terms` 保留兼容。
  - **专名保真**：`terms.py` 新增整段引用编号保护（含 `11–13` 范围），并放宽哨兵还原以修复 `8ZT20ZZ` 泄漏；`extract.py` 行末连字符在大写/数字开头时保留（`DeepCompress-` / `ML-Security-` / `GPT-4`）；`postprocess.py` 在标点转换前遮蔽引用编号，保住 ASCII 逗号与连接号。
  - **清理**：删除 `offline.py` 未用的 `time` / `DEFAULT_MODEL_DIR` / `_ct2_dirname`，`build_models.py` 的 `os`，`providers/__init__.py` 的 `PROVIDER_NAMES`；未改 `translate()` 与排版算法。
- 影响文件：`pdf_translator/providers/offline_config.py`、`offline.py`、`offline_cli.py`、`terms.py`、`postprocess.py`、`__init__.py`、`pdf_translator/extract.py`、`pdf_translator/__main__.py`、`ui/app.py`、`build.ps1`、`tools/verify.py`、`tools/make_sample.py`、`tools/make_narrow_sample.py`、`tools/build_models.py`、`README.md`。
- 证据命令：
  - `python -m pdf_translator.providers.offline_cli --list-tiers`
  - `python -m pdf_translator samples\sample_en.pdf -o out\fix_check_mock.pdf --provider mock`
  - `python tools\verify.py samples\sample_en.pdf out\fix_check_mock.pdf`
  - `python tools\check_layout.py samples\sample_en.pdf out\fix_check_mock.pdf`
  - `python -m pdf_translator samples\sample_en.pdf -o out\fix_check_offline.pdf --provider offline --tier balanced`
  - `python tools\verify.py samples\sample_en.pdf out\offline_only_check.pdf`（网络守卫包装，见下）
  - `python -m pdf_translator.providers.offline_cli out\tier_paper_p1-3.pdf -o out\term_fidelity_check.pdf --tier balanced --no-web-terms --term-report out\term_fidelity_check_terms.json`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1 -Mode full`
  - `Start-Process -FilePath "d:\PDFtranslator\dist\PDFTranslator\PDFTranslator.exe" -ArgumentList '--smoke --report d:\PDFtranslator\out\_smoke_full.json' -Wait -PassThru`
- 结果：
  - `--list-tiers`：`max/quality/balanced` 可用；max 25 s/页；`llm` 损坏/不可用；列对齐正常。
  - mock 回归：verify「全部校验通过」（中文 800 字，英文词 327→14，特殊文本 3/3）；check_layout 通过（横溢 0、纵溢 0、最小字号比 0.95、行首禁则 0）。
  - offline CLI：3 页样例 8.6 s 完成；`out\fix_check_offline.pdf`、`out\offline_only_check.pdf` 的 verify 与 check_layout 均通过。
  - 离线证明：包装脚本先拦截 `socket.getaddrinfo` / `socket.create_connection` / `socket.socket.connect` / `requests.*`，再以默认参数（未传 `--web-terms`，也未传 `--no-web-terms`）跑 balanced 3 页；守卫未触发、退出码 0，术语报告「联网查询 0/35 次」。这是断网等价的运行时证据；代码审计同时确认 `TermResolver` 在未显式开启时不发起请求。
  - 论文 10 页产物 `out\paper_max_zh.pdf`：URL 与标题两条假失败已消失（7 项检查全过，保留特殊文本 3/3）；仍报「第 9/10 页未检测到中文文本」——这两页是参考文献，按规则刻意保留英文，属既有行为，本次未改。
  - 3 页论文 `out\term_fidelity_check.pdf`：verify 全部通过（中文 2993 字，英文词 2207→330），check_layout 通过（横溢 0、纵溢 0、最小字号比 0.80、行首禁则 0）。
  - 专名实测（修正前 → 修正后）：12 个抽样专名（DeepCompress-ViT、ViT、UCT、Imagenet-1k、DeiT-S、3 位作者、Binghamton University、Adobe Inc.、NCSU、GitHub URL）全部保留；引用数字粘连 2 处 → 0；哨兵泄漏 `8ZT20ZZ` 1 处 → 0；URL 丢失连字符 → 完整保留。英文词总量 333 → 333、中文 2960 → 2993，说明保护规则没有把正文变成英文。
  - 完整包：`build.ps1 -Mode full` 成功，可用档位 balanced/quality/max，`dist\PDFTranslator\models\max\model.bin` 3.36 GB；发行体积 5.24 GB（程序 178.4 MB + 0.61 + 1.31 + 3.15 GB）。smoke 报告 `out\_smoke_full.json` 的 `available_tiers` 为 `["balanced","quality","max"]`。
- 离线专名审计结论：
  - `glossary.tsv` 37 条，在 `OfflineProvider.prepare_document` 中无条件加载并转为高优先级词条，离线路径不受 `web_terms` 影响；`terms_cache.json` 7 条，在 `TermResolver.resolve` 中始终先查缓存，缓存命中直接采用。
  - 三层保留：`textfilter.classify` 负责整单元跳过（URL/纯数字/代码/参考文献条目/小图注等）；`extract._mark_front_matter` 跳过标题与摘要之间的作者/单位，`_mark_references` 从参考文献标题起整段跳过；`offline._keep_as_is` 处理 DOI/arXiv、长引用串、CVPR 水印、作者与单位行等整块保留。
  - 关闭联网后消失的能力：人名音译、机构中文译名、缩写展开、维基/Wikidata 术语核对；未命中本地词表/缓存的专名按「保留原文」，不会音译成汉字。
- 环境、依赖变更：无新增依赖；`requirements.txt` 仍为 PyMuPDF / requests / tkinterdnd2。磁盘 D: 构建前剩余约 415 GB，构建后仍充足。
- 未完成事项 / 下一步：
  - 论文 10 页 verify 仍会因参考文献页（9/10 页）保留英文而失败；若要让整篇论文严格通过，需要另加「参考文献页不计中文覆盖率」的白名单规则（本次按约束未动）。
  - `terms_cache.json` 只有 7 条，离线专名覆盖仍偏薄；可继续人工补充 `glossary.tsv`（当前 37 条）。
  - max 本次只做了打包与 smoke，未在完整发行目录内跑整篇真实翻译。
- 交接注意点：
  - 不要删除或移动 `models\`；`models\llm` 带 `model.bin.parts`，命令行会标损坏，但文件仍在。
  - 完整包含 max，体积 5.24 GB；精简包仍只含 balanced。
  - 界面默认「平衡档」；max 可选但慢，约 25 秒/页且更吃内存。
  - 离线默认不联网；只有显式 `--web-terms`（离线 CLI）或勾选界面复选框才会查询维基/Wikidata。

---

## 8. 维护约定

以后每次改动、迁移、验证，都在本文件**末尾追加**一条记录，格式统一：

```text
### YYYY-MM-DD：<一句话变更/结论>

- 变更/结论：
- 影响文件：
- 证据命令：
- 结果：
```

最低要求：

1. 记录真实执行过的命令与真实数字，不写“应该”“大概”。
2. 迁移后必须记录新路径、旧路径、原因，以及至少一次 mock 全流程 + verify + check_layout。
3. 档位相关改动必须记录 `model.bin` / tokenizer 的实际路径与体积。
4. 验证产物统一放 `out\`，使用新文件名，不覆盖历史产物。

---

### 2026-11-06：磁盘瘦身收尾——llm/hf 已删、build 已清空、三项校验与记录补齐

- 变更/结论：
  - 清理结果与现状核对：`models\llm`、`models\hf`、`models\ct2` 均已不存在（`Test-Path` 为 False）；`build\` 为空目录；`models\` 只剩 balanced / quality / max（5.07 GiB，21 个文件）。
  - 体积：项目总计 45,788,575,784 B（42.64 GiB）→ 11,925,540,217 B（11.11 GiB），释放 33,863,035,567 B（31.54 GiB）；D 盘可用 409.8 GB → 441.33 GB（+31.53 GB）。
  - 发行包：`dist\PDFTranslator` 的 `models\` 为 balanced/quality/max（6.02 GiB），`dist\PDFTranslator-Lite` 仅 balanced。
  - 档位引用检查：`--list-tiers` 对已删除的 llm 显示「未随包 / 0.00G」且不报错；README 未把 llm 列为可用档；`docs\build.md` 仅说明 `models\llm` 属开发期内容、不进发行包；`offline_config.bundled_model_dir()` 中 llm 仅参与“models 根目录是否存在”的探测，不代表该档可用。
- 影响文件：仅文档 `docs\CLEANUP_2026-11-06.md`、`docs\PROJECT_LOG.md`；无代码改动，未删除/移动任何文件，未做 git 操作。
- 证据命令：
  - `python tools\verify.py samples\sample_en.pdf out\cleanup_check_mock.pdf`
  - `python tools\check_layout.py samples\sample_en.pdf out\cleanup_check_mock.pdf`
  - `$p = Start-Process -FilePath "d:\PDFtranslator\dist\PDFTranslator\PDFTranslator.exe" -ArgumentList '--smoke --report d:\PDFtranslator\out\_smoke_cleanup_verify.json' -Wait -PassThru; Get-Content d:\PDFtranslator\out\_smoke_cleanup_verify.json -Raw`
  - `python -m pdf_translator.providers.offline_cli --list-tiers`
- 结果：
  - verify：全部校验通过（中文 800 字；英文词 327 → 14；保留特殊文本 3/3 项）；标题自动取源 PDF 最大字号行 `Annual Financial Overview`。
  - check_layout：排版检查通过；横溢 0 行、纵溢 0 行、行首禁则 0 行、超出页面边界 0 行；最小字号比 0.95（下限 0.80）；末行平均填充 66%；译文/原文行数比 0.97。
  - smoke：退出码 0；`ok: true`、`frozen: true`、`available_tiers: ["balanced","quality","max"]`、`engine: "balanced"`、`dnd: true`。
  - `--list-tiers`：max 3.15G / 可用 / 实测约 25 s/页；quality 1.31G / 可用 / 约 10 s/页；balanced 0.61G / 可用 / 约 6.5 s/页；llm「未随包」0.00G；light「未随包」0.00G。
  - `out\_archive`：83 个文件（24 个 `.py` + 59 个 `.log`，合计 134,147 B）；`out\` 根目录只保留 `_quality_full_run.log` 1 个日志，无 `.py`。
- 交接注意点：
  - 本文第 4 节表格与 6.5 节描述的是清理前状态，当前实际状态以本条为准。
  - 若要恢复 llm 档，需重新下载 Qwen2.5-7B 并执行 `python tools\build_models.py --tier llm`；`models\hf` 也需重新下载。
  - 完整包 6.02 GiB 含三档；精简包仍只含 balanced。

---

### 2026-11-06：中文排版基准对齐——正文 1.94× 行距、2 字缩进、轻度两端对齐，横向溢出归零

- 变更/结论：
  - 新增排版基准样本 `脑机接口技术发展态势与政策启示——基于全球专利数据的分析_于江南.pdf`，实测第 2–8 页（612×792pt）：
    - 正文 SimSun 10.5pt；行距 20.4pt = 1.943×字号（p4/p5/p7 为 1.946–1.949）。
    - 段首缩进 21pt = 2.0 字符，缩进行占比 17%–36%。
    - 版心 x=36.0–576.1（540pt），左右页边距 36pt，正文单栏。
    - 两端对齐：justified_share 0.95，非末行右缘 576.0–576.1，右间隙中位 0.07pt。
    - 字距：CJK 相邻 bbox 间隙中位 0.00pt、p90 0.05pt；西文步进 0.50em（p10–p90 0.444–0.612）。
    - 段落间距 0，全部行落在同一 20.4pt 网格；行容量 540/10.5≈51 全角位（实测含西文行 CJK 37–44 字）。
    - 标题 12pt（1.14×正文）、图注 10.5pt、表内与参考文献 9pt（行距 11.1pt = 1.233×，左对齐）。
  - 渲染改进（参数全部集中在 `config.py`，可回退）：正文行距 1.94×（块高不足按可用高度收敛，下限 1.30×）、正文段首 2 字符缩进、非末行 ≤4% 横向伸展模拟两端对齐、标题/图注/表格分别用 1.35×/1.50×/1.30× 行距；含 ASCII 的行改走 `TextWriter` + `Font`，ASCII 按比例宽度（修复此前 “2 0 2 5” 式 1em 过疏）。
  - 关键修正：实测 `insert_text(fontname="china-s")` 对每个非 ASCII 字符（δ/×/α/引号/项目符号）都按 1 字宽走字，而 `Font.text_length` 返回 0.18–0.75em；两端对齐把行拉到栏边后，度量低估直接变成 3–8pt 横向溢出。`_width` 改为按实际写入路径逐字度量（ASCII 取 Font 比例宽度、非 ASCII 取 1 字宽）后，paper 横溢 5 行（最大 7.7pt）→ 0 行。
  - 删除 `_balanced`（均衡断行会把行宽缩到 60–70%，与两端对齐冲突，造成右侧大参差）。
- 影响文件：
  - `pdf_translator/config.py`：新增 `BODY_LINE_SPACING_RATIO=1.94`、`BODY_LINE_SPACING_FLOOR=1.30`、`HEADING_LINE_SPACING_RATIO=1.35`、`CAPTION_LINE_SPACING_RATIO=1.50`、`BODY_INDENT_CHARS=2.0`、`BODY_INDENT_MIN_WIDTH_RATIO=0.72`、`BODY_INDENT_MIN_CJK=40`、`BODY_SIZE_TOLERANCE=0.12`、`JUSTIFY_MAX_SCALE=1.04`。
  - `pdf_translator/render.py`：新增/改写 `_char_advance`、`_width`、`_line_height_for`、`_dominant_font_size`、`_is_body_paragraph`；`_wrap_once`/`_fix_punctuation`/`wrap_text`/`fit_text`/`render_page` 支持首行缩进、块高内行距收敛、按块条件启用；`FittedText` 增加 `indent`；删除 `_balanced`。
  - `README.md`：第 13 行功能列表、第 309–312 行“版面还原”同步排版策略（其余未改）。
- 证据命令：
  - `python -m pdf_translator samples\sample_en.pdf -o out\type_before_sample.pdf --provider offline --tier balanced`
  - `python -m pdf_translator "Ahmed_DeepCompress-ViT_Rethinking_Model_Compression_to_Enhance_Efficiency_of_Vision_Transformers_CVPR_2025_paper.pdf" -o out\type_before_paper.pdf --provider offline --tier balanced`
  - `python -m pdf_translator samples\sample_en.pdf -o out\type_after_sample.pdf --provider offline --tier balanced`
  - `python -m pdf_translator "Ahmed_DeepCompress-ViT_Rethinking_Model_Compression_to_Enhance_Efficiency_of_Vision_Transformers_CVPR_2025_paper.pdf" -o out\type_after_paper.pdf --provider offline --tier balanced`
  - `python tools\check_layout.py samples\sample_en.pdf out\type_after_sample.pdf`
  - `python tools\check_layout.py "Ahmed_DeepCompress-ViT_Rethinking_Model_Compression_to_Enhance_Efficiency_of_Vision_Transformers_CVPR_2025_paper.pdf" out\type_after_paper.pdf`
  - `python tools\verify.py samples\sample_en.pdf out\type_after_sample.pdf`
  - `python tools\verify.py "Ahmed_DeepCompress-ViT_Rethinking_Model_Compression_to_Enhance_Efficiency_of_Vision_Transformers_CVPR_2025_paper.pdf" out\type_after_paper.pdf`
- 结果（真实测量，before/after 同输入同档位）：
  - sample：行距 13.65pt(1.30×) → 20.37pt(1.937×)，基准 20.4pt(1.943×)；段首缩进 0 → 2.0 字符；justified_share 0 → 1.00（基准 0.95）；右间隙中位 12.6pt → 0.02pt（基准 0.07pt）；ASCII 步进 1.00em → 0.555em（基准 0.50em）；末行填充 67% → 53%（基准抽样中位 60%）；check_layout 横溢/纵溢 0/0 → 0/0，行距σ 0.00 → 0.44pt，最小字号比 1.00 → 1.00，行数比 0.93 → 0.93；verify 均通过（中文 492 字，英文词 327→28，特殊文本 3/3）。
  - paper：行距 12.95pt(1.30×) → 17.88pt(1.833×；分页 1.898/1.753/1.913/1.896/1.769/1.584×，第 7 页块高最紧收敛到 1.58×)；段首缩进 0 → 2.0 字符；ASCII 步进 1.00em → 0.511em；check_layout 横溢 0 行 → 0 行（中间版本因度量低估曾出现 5 行、最大 7.7pt，已修正）、纵溢 0/0；行距σ 0.66/8.56pt → 0.59/6.84pt；最小字号比 0.80 → 0.94；末行填充 66% → 58%；行数比 0.85 → 0.77；行首禁则 0 行；verify 两版均因第 9–10 页参考文献刻意保留英文而失败（中文 7242 字，英文词 6728→2008，特殊文本 3/3）。
- 未完成事项 / 下一步：
  - 字体族仍未复刻基准的宋体：PyMuPDF 内置 `china-s` 是黑体（Droid Sans Fallback），观感比基准偏“现代”。要真正接近需外挂宋体字体文件并支持嵌入。
  - paper 的行距比例 1.833× 低于 1.94× 目标：双栏块高固定，1.94× 会让行数减少后仍放不下，按 `_line_height_for` 自动收敛；第 7 页收敛到 1.58×。
  - 末行填充率 sample 从 67% 降到 53%，是“满行 + 2 字缩进”带来的正常代价（每段首行少 2 字），基准抽样中位数为 60%。
  - 参考文献页（9/10）无中文导致 verify 失败，属既有约定，未改校验器。
- 交接注意点：
  - 新参数都在 `pdf_translator/config.py` 第 19–38 行，注释写明了基准来源；回退只需把 `BODY_LINE_SPACING_RATIO` 改回 1.30 并删除 `BODY_INDENT_CHARS`（或设为 0）。
  - `_width` 的逐字度量必须与写入路径一致：ASCII 走 `Font.text_length`，非 ASCII 按 1em。改动任一侧（例如全改 TextWriter）都会重新引入溢出或字号比假象（TextWriter 的 bbox 高 1.31×字号，会让 check_layout 报 1.09）。
  - 缩进只对“左对齐、宽度≥栏宽 72%、字号在页面正文 ±12%、译文≥40 汉字”的段落生效；表格单元格、图注、标题不缩进。
  - 对照产物：`out\type_before_sample.pdf`、`out\type_before_paper.pdf`、`out\type_after_sample.pdf`、`out\type_after_paper.pdf`；预览 `out\type_preview_before\`、`out\type_preview_after\`（DPI 130）。


### 2026-11-06：离线译文质量诊断（三档对照 + 7B 可行性评估）

- 本次目标：量化 balanced/quality/max 的译文质量问题，评估本地 7B、两阶段润色、规则后处理三条路线，不改业务代码。
- 结论：NLLB 三档共享句子级架构错误（同一句语义反转三档全错），600M→1.3B 是"表面更顺、漏译更多"，1.3B→3.3B 无跃升且新增 ViT→ZT4 幻觉；建议先做规则后处理（C），再把 Qwen2.5-7B 作为可选高质量档（A），B 留待 A 验证后。
- 实测：21 句（CVPR 摘要 10 + sample_en 11）耗时 balanced 9.8/10.6 s、quality 19.7/83.2 s、max 64.4/165.6 s（两次运行，降频波动大）；解码 batch=1 为 32–39 / 24.7–26.4 / 5.5–13.1 tok/s；10 页论文译文 7,692 汉字，按 1.5–3 tok/s 推算 7B 需 30–60 分钟。
- 关键决策与原因：只出诊断，不选型落地；7B 走 CT2 int8 而非 GGUF，因为依赖已具备（ctranslate2 4.8.5 + transformers 5.3.0），GGUF 需新增 llama-cpp-python。
- 环境、依赖变更：无。
- 未完成事项 / 下一步：待用户拍板是否允许一次性下载 Qwen2.5-7B（约 15 GB，int8 后 7.3 GB）、可接受最慢速度、体积与默认档。
- 交接注意点：复现脚本与数据在 `out\diag_tier_quality_20261106.py`、`out\diag_per_sentence_20261106.py`、`out\diag_ct2_batch_20261106.py`、`out\diag_pipeline_batch_20261106.py`、`out\diag_tier_quality_20261106.json`；完整报告 `out\REPORT_offline_quality_20261106.md`；postprocess 句末半角句点是三档共有的系统性缺陷（`postprocess.py:81` 只在句点后跟中文时转换）。

### 2026-11-06：PDF→Word→PDF 重排版路线可行性验证（未接入主流程）

- 本次目标：实测“PDF→docx→翻译→docx→PDF”能否用 Word 自然重排替代按坐标回填，判断是否值得作为第二种模式。
- 环境实测：pdf2docx 0.5.13、python-docx 1.1.2、PyMuPDF 1.26.7、pywin32 312 均已安装，未新装依赖；Word COM 可用（Word 16.0 / Build 16.0.20527，Office 家庭版 2024）；LibreOffice、WPS 均未安装（`soffice`、`wps` 不在 PATH，常见安装路径不存在）。
- 实测保真度：sample_en（3 页）转换 0.2s，正文/表格/页脚与原文一致，第 1 页 4 象限矢量图丢失（只剩图注），标题+副标题被合并进 2×2 布局表且行高固定；sample_narrow_en（2 页）0.2s，三窄栏原样保留，评级高；CVPR 论文（10 页）63.0s，docx 内含 19 个 PNG 切片、28 个表格对象（正文被装进 18×11、32×11 布局表）、269 个数学 run、86 个分节、73 个换行，Word 导出后 10 页变 20 页（10 个溢出页正文仅 5–590 字符），参考文献序号错乱（1–10 后直接 15–23，12/13/14 被挤到后面），CMR10/CMSY10/CMMI10 被替换为 Cambria Math/Times，公式仍为文本但紧凑间距丢失。
- 端到端原型：sample_en.docx 中 30 段送译（19 段按 classify 规则跳过），balanced 档耗时 29.8s；sample_zh.docx 写入 w:eastAsia=宋体，Word 导出 3 页，SimSun 已嵌入、无方框；标题块因“固定行高 + exact 行距 17.1pt 配 22pt 字（0.78 倍）”压字，把行高改 AUTO、行距改 1.2 倍后两行标题正常显示。check_layout 可运行（页数 3=3）：横溢 2 行 ≤1.2pt、纵溢 2 行 ≤7.7pt、最小字号比 0.77、行首禁则 2 行，但该工具按原文坐标映射，数值只能参考。
- 关键决策与原因：不接入主流程。双栏论文的版面是“布局表 + 固定行高 + 分节分页”拼出来的，CJK 扩张无法自由回流，必然产生溢出页与裁切；参考文献在 docx 中变成“一条一段”，现有 `_keep_as_is` 的“多编号 + 多年代”整块判定失效（实测 [1] 条目被译成中文），需额外做参考文献区段识别。
- 环境、依赖变更：无。
- 未完成事项 / 下一步：若要做，最小闭环 = 转换后规范化（行高 AUTO、行距单倍/1.2、eastAsia 宋体）+ 参考文献区段跳过 + Word/LibreOffice 导出与失败回退；先在单栏文档灰度，双栏仍走坐标回填。
- 交接注意点：原型脚本与产物都在 `out\docx_fidelity\`（fidelity_probe.py、docx2pdf.py、translate_docx.py、protection_check.py、fidelity_report.json、`*_roundtrip.pdf`、`view_*.png`），未改任何主流程代码；`sample_zh_single.pdf`（只改行距会裁掉标题第二行）与 `sample_zh_auto.pdf`（行高+行距一起改才正常）是定位根因的对照物。

---

### 2026-11-06：修复 balanced 档三栏版面“文字重叠”（栏间压字），新增重叠检测工具

- 本次目标：复现并修复 balanced 档译文的重叠；先确认是否由 1.94× 行距改造引入。
- 复现方式（产物均用 `overlap_` 前缀，未覆盖既有 out 文件）：
  - `python -m pdf_translator samples\sample_en.pdf -o out\overlap_sample.pdf --provider offline --tier balanced`
  - `python -m pdf_translator samples\sample_narrow_en.pdf -o out\overlap_narrow.pdf --provider offline --tier balanced`
  - `python -m pdf_translator "Ahmed_DeepCompress-ViT_Rethinking_Model_Compression_to_Enhance_Efficiency_of_Vision_Transformers_CVPR_2025_paper.pdf" -o out\overlap_paper.pdf --provider offline --tier balanced`
  - 旧产物对照：`out\type_after_sample.pdf`、`out\type_after_paper.pdf`、`out\cleanup_check_offline.pdf`、`out\after_narrow.pdf`。
- 复现结果：
  - 重叠只出现在三栏窄栏样例第 1 页：左栏首行右端 x=203.7，中栏首行左端 x=202.8，横向叠 0.9pt；两行 bbox 纵向交叠 11.4pt/5.3pt，两处重叠面积 10.6pt²、4.9pt²。视觉上左栏末字与中栏首字贴在一起，中栏还整体左移 11.2pt（原 x0=214）。
  - `sample_en`、10 页论文、`type_after_*`、`cleanup_check_offline` 在三类检测（文字交叠/中英叠印/压图压线）下均无译文引入的重叠；论文里 20 处交叠是原文件公式重音与图内标注，前后一致。
  - 排版前后对照：`out\after_narrow.pdf`（改造后、修复前）同样有 5 行越出中栏排版区 10.7pt + 1 处表格文字交叠；`out\tier_balanced_narrow_r1_zh.pdf`（改造前）左栏止于 200.2，与中栏 202.8 尚余 2.6pt，无重叠。
- 根因（不是行距收敛）：
  1. `render.py::_column_bounds`（第 404–416 行）用“左右两半的中位数”找单个 gutter：三栏页中间栏 x0=214/x1≈345 既不属于左半（x1>mid+2）也不属于右半（x0<mid-2），被忽略；算出的 gutter=(203.7, 380) 把中间栏整个吞进去。
  2. `render.py::layout_for`（第 463–476 行）的“右对齐标题”判定未加行数限制：中栏这个 8 行段落因右缘齐平被判为 right，随后第 493 行 `limit_left = min(limit_left, unit.bbox.x0)` 把排版左边界拉到 195.7；右对齐首行从 202.8 起排。
  3. 新排版把左栏非末行两端对齐拉伸到 gutter 边 203.7（改造前为 200.2），恰好越过 202.8，于是原本 2.6pt 的安全间隙变成 0.9pt 交叠。
  4. 逐项排除：`_line_height_for`（第 288–300 行）先取目标行距、再按块高收敛，下限硬性 1.30×，不会低于 1.30×；`fit_text` 的测高与绘制共用同一个 `FittedText.line_height`；首行缩进从拉伸目标中扣除，未发现“缩进后仍按整宽拉伸”；`_bottom_limit` 会避让下方文本块与图片，本批产物未出现纵向压块；redaction 区域均为整块 bbox，未发现块内英文残留叠印。
- 修复点（最小改动，未动栏结构/块边界/图片表格）：
  - `render.py` 新增模块常量 `SINGLE_LINE_HEIGHT_RATIO = 1.75`（第 34 行）：只有块高 ≤1.75×字号才可能是单行右对齐标题。
  - `render.py::layout_for`（第 466–476 行）给右对齐判定加上 `single_line` 条件；多行段落保持左对齐，按本栏宽度折行，不再向左溢出。
  - 复测：中栏 x0 202.8 → 214.2，与左栏 203.7 之间留出 10.5pt；三栏恢复原有栏间距。
- 修复前后指标（同一输入/档位，实测）：
  - 重叠：narrow 2 处文字重叠 + 2 行越出中栏（9.2pt）→ 0 / 0；sample、paper 均保持 0。
  - 行距（译文行基线间距/字号，按原文块归组）：sample 中位 1.914×（p10 1.773/p90 1.969）→ 1.914×；narrow 1.898× → 1.893×，其中短中栏按 1.30× 下限排版（9.5pt 字、基线间距 12.35pt）；paper 1.768×（p10 1.576/p90 1.939）→ 1.768×。本次修复未牺牲行距，行距与修复前一致。
  - check_layout：sample 横溢 0/0、纵溢 0/0、最小字号比 1.00；narrow 0/0、0/0、1.00；paper 0/0、0/0、0.94（下限 0.80）。
  - verify：sample、narrow 全部通过；paper 仅第 9/10 页参考文献保留英文导致失败（既有口径，非本次回归）。
- 影响文件：`pdf_translator/render.py`（常量 + layout_for 判定）、新增 `tools/check_overlap.py`、`README.md`（目录树 + 2.1 示例命令）、`docs\PROJECT_LOG.md`。
- 检测工具用法：
  - `python tools\check_overlap.py out\overlap_fixed_narrow.pdf`（任意 PDF 自检）
  - `python tools\check_overlap.py samples\sample_narrow_en.pdf out\overlap_fixed_narrow.pdf`（带原文对照：额外查越出原块，并把原文自带的公式重音叠印单独列出）
  - 有译文引入的重叠时退出码 1；输出按“中英叠印 > 文字重叠 > 压图/压线 > 越块”和重叠面积排序，含页码、区块坐标、文本片段、重叠宽×高与面积。
- 对照产物：修复前 `out\overlap_sample.pdf`、`out\overlap_narrow.pdf`、`out\overlap_paper.pdf`；修复后 `out\overlap_fixed_sample.pdf`、`out\overlap_fixed_narrow.pdf`、`out\overlap_fixed_paper.pdf`；预览 `out\overlap_preview\`（含 `zoom_narrow_p1_cols.png` 修复前、`narrow_fixed_p1.png` 修复后）。
- 未完成事项 / 下一步：
  - `_column_bounds` 仍只支持“单个 gutter + 左右两半”的模型，三/四栏页靠“多行不做右对齐”规避；若后续遇到三栏正文被强行按左右两半排版，需要重新设计栏检测（本次未重构）。
  - 旧产物 `out\after_narrow.pdf` 仍保留修复前的重叠，作为历史对照，未重新生成。
- 交接注意点：
  - 右对齐只对单行块生效是有意为之：多行段落右缘齐平来自两端对齐，不是右对齐语义；把该条件去掉会重新引入三栏压字。
  - 回归时必须同时跑 `check_overlap.py` 与 `check_layout.py`：前者管“是否互相压住”，后者管“是否越栏/字号”；两者判定口径不同，都不能单独放宽。

---

### 2026-11-06：新增“结构理解 + 空白模板重排”原型（reflow），验证第二条产品路线

- 本次目标：验证“先识别行文结构，再在空白 A4 模板重新排版”的可行性；只做原型，不接 `engine.py`、不改 UI、不改保版模式输出。
- 新增文件：`tools\reflow_prototype.py`（独立脚本，约 1900 行）；产物 `out\reflow_sample_zh.pdf`（2 页）、`out\reflow_paper3_zh.pdf`（5 页）、对应 `*_metrics.json`；预览 `out\reflow_preview\`（含 `reflow_*_p*.png`、保版对照 `baseline_type_p1/p2.png`、原文 `source_paper_p1-p3.png`）。
- 链路：行/span 提取 → 双栏中缝检测 → 标题/摘要/作者/图/表/脚注分类 → 跨栏/跨页段落续接 → balanced 档离线翻译（图注/表题/单元格同译）→ 单栏 A4 重排（10.5pt、行距 1.70、段首 2 字、页边距 36/40、页码“第 N 页”）。
- 实测（真实统计，非估算）：
  - sample_en：19 个元素（标题 1、作者 1、摘要标题 1、摘要正文 1、小标题 4、正文 6、图 1、表 1、特殊文本 4），类型正确 18/19 = 94.7%（唯一错误：副标题被当作者并保留英文）；15/15 结构元素落版、字符 100%；图 1/1；表 1/1 按原 6×4 网格重建；页眉页脚 9 条丢弃。
  - CVPR 论文前 3 页：31 个元素（标题/作者/单位/摘要标题/摘要正文各 1、小标题 6、正文 16、脚注 1、图 2、表 1），类型 31/31；阅读顺序 28/31（图 1、图 2 因跨栏段落合并而后移，脚注进入正文流）；28/28 段落类元素落版、字符 100%；图 2/2；表 1/1 只能裁原图（find_tables 无可信网格）；公式保护命中 0 段；丢弃页眉页脚 6、图内标注 84。
  - 现有 `extract.py`：sample 53 个单元（31 可译 / 22 跳过）；论文前 3 页 100 个单元 = 22 个文本单元 + 75 个图/表内碎片 + 3 个页码。17 个可译单元中仅 6 个（35%）是完整单元素；6 个把标题并入正文或整栏合并（如 0:4 摘要+“1. Introduction”、1:0/2:0 整栏、2:1/2:2 标题并入段尾）；3 个截断/乱序（0:5、0:39 丢 “By deploying advanced mod-”、1:41 图注错序）；1 个页眉误判可译；另有 2 个正文碎片被当“段落内碎行”丢弃。语义类别数为 0。
- 关键决策与原因：
  - 独立脚本、只读复用 `render._width` 思路与 `textfilter.classify`，避免改动保版链路。
  - 本地重写宽度度量：ASCII 用 helv、粗体用 hebo、中文 1em，替换 china-s 度量（其 ASCII 窄约 6%，会造成 “SELECTsegment”“SOTAviT” 与标题压字）。
  - 图注用 “Figure/Table N” 正则 + 同栏紧邻行合并；图区域取图注上方同栏图形与小字并集；表先 `find_tables()` 可信度判定，失败则裁原图为图片。
  - 整表优先换页、大表跨页重复表头；标题后接图表时做孤行规避（未覆盖标题后接正文）。
- 环境、依赖变更：无新增依赖；翻译全程离线 balanced，未加 `--web-terms`。
- 未完成事项 / 下一步：跨栏段落与图的相对顺序、公式块整体抽取、表格网格重建、参考文献条目切分、图内文字剔除、整页空白回收、脚注页底定位、底部溢页与页码重叠。
- 交接注意点：`--force` 只覆盖 `reflow_` 前缀产物；`out\reflow_probe_*.py` 为临时探针，可随时删除；保版输出未受影响。

---

### 2026-11-06：重建 llm 档（Qwen2.5-7B-Instruct int8），测速、质量对照并接入界面

- 本次目标：恢复离线「自然档」通路，实测其相对 NLLB quality 档的自然度与速度代价，并在 `--list-tiers` 与界面档位列表中可用。
- 模型来源与体积：
  - 下载 `Qwen/Qwen2.5-7B-Instruct`，经工具默认镜像 `hf-mirror.com`；HF 原始权重 14.53 GB（4 个 safetensors 分片），整个 `models\hf\Qwen--Qwen2.5-7B-Instruct` 目录 14.63 GB / 23 文件。
  - 原始权重下载用时 1274 s（约 11.7 MB/s），转 int8 用时 60 s；`models\llm\model.bin` 7.10 GiB，`models\llm` 合计 7.11 GB。`models\hf` 原始权重保留未删，便于重转。
  - 转换命令：`python tools\build_models.py --tier llm`（输出 `models\llm`，含 `model.bin`、`config.json`、`tokenizer\tokenizer.json`、`vocabulary.json`，与 `QwenBackend` 期望一致）。
  - `tools\build_models.py` 本次两处修正：镜像对小文件（`merges.txt`）不返回 `Content-Length` 时改为单流下载；分块并发由 8 降到 4（8 路时后半数连接长期排队），并支持分块断点续传。
- 实测速度（CPU，16 线程，beam 2，机器空闲）：
  - 纯生成 3.22 tok/s：6 次调用共 320 输出 token / 99.51 s；单次 15.7～17.9 s，prompt 321～348 tok。
  - `samples\sample_en.pdf` 3 页：翻译 559.6 s（页 1/2/3 = 213.7/228.2/117.7 s），约 186.5 s/页；quality 档同机同文件 14.1 s（4.7 s/页），自然档约慢 40 倍。
  - 10 页论文推算约 2～3 小时（取中 2.4 h）。假设：按 sample 实测 0.225 s/原文字符折算论文约 3.8 万可译字符；或按 3.22 tok/s、约 7.6k 输出 token、保留 sample 的 28/31 重试比例折算 2.1 h。参考文献/作者/DOI 等 `_keep_as_is` 块不计入；若消除重试可降到约 40～60 分钟。
- 质量对照（同一段英文，实测原文/译文见 `build\bench_out.txt`）：
  - quality 摘要句：「Vision Transformer （ViTs）在处理复杂的视觉任务方面表现出色，但它们的大小对资源有限的应用提出了重大挑战。边缘设备在边缘设备和服务器之间传输模型权重时……」——「边缘设备在边缘设备」重复、漏「边缘」限定，机翻腔明显。
  - llm 摘要句：「Vision Transformer （ViTs）在处理复杂视觉任务方面表现出色，但其庞大的模型规模给资源受限的边缘设备应用带来了显著挑战。这些模型规模的增加导致在边缘设备与服务器之间传输模型权重时会产生更高的开销（例如，能耗、延迟）（e.g）。」——衔接自然，但句尾多出 `（e.g）`。
  - quality 正文段：「由于订阅产品和专业服务的强需求，所有主要客户部门的收入都增长。运营费用保持控制，净利与前一年相比有所改善.」——「强需求」「保持控制」「净利」生硬。
  - llm 正文段：「收入在所有主要客户群体中均有所增长，主要得益于订阅产品和专业服务需求强劲。运营费用得到有效控制，净利润相比上一年度有所提升。」——用词与语序接近人工表达。
  - 专名：`ViT/ViTs`、`DeepCompress-ViT` 保留英文；`[12]` 参考文献整段原样保留（`_keep_as_is` 判定 True）；论文作者行（带 `Ahmed1∗` 上标）`_keep_as_is=True`，不送模型；该 PDF 无 DOI 字段，未能实测 DOI。
- 接入改动：
  - `ui\app.py`：`TIER_ORDER` 加入 `llm`，`TIER_NAMES["llm"]="自然档"`，新增 `TIER_NOTES`，档位标签与高级设置提示都写明「最自然、最慢、占内存最多」。
  - `offline_cli.py`：`TIER_PAGE_SECONDS["llm"]=186.5`；`--tier` 帮助文案同步。翻译逻辑未改。
  - 结果：`python -m pdf_translator.providers.offline_cli --list-tiers` 显示 `llm Qwen2.5-7B-Instruct 可用 7.11G 实测约 186 s/页`；界面自检 `available_tiers=[balanced, quality, max, llm]`（`out\_smoke_qwen_llm.json`）。
- 验证结果：
  - `out\qwen_sample_zh.pdf`（3 页）由 llm 档完整翻译；`verify.py` 全部通过（中文 667 字，英文词 327→64，特殊文本 3/3）。
  - `check_layout.py` 通过：横/纵溢出 0，最小字号比 0.80（下限 0.80），行距 σ 均值 0.19 pt，行首禁则 0。
  - 另生成 `out\qwen_cmp_quality_zh.pdf`（quality 同文件对照）与 `out\qwen_sample_zh_terms.json`；未覆盖任何历史 `out\` 产物。
- 已知不足（均未修，留待决策）：
  - 提示词泄漏：短标题会输出「摘要保留原文」「Revenue Breakdown 保留原文」；个别段落把「请直接输出这段英文对应的简体中文译文。【上文译文…】」原样吐回，而 `_bad_output` 只查中文比例/长度/专名，仍会接受。
  - `_inline_terms` 会把缺失缩写补在段尾，形成「（例如，能耗、延迟）（e.g）。」这类别扭结尾。
  - 重试放大：bench 2 段触发 6 次生成，sample 31 个可译单元触发 28 次重试，时间是理论生成的约 3 倍。
  - 纯 CPU、batch 1、beam 2、无流式/前缀缓存；加载后常驻内存约 8 GB。
- 环境、依赖变更：无新增 Python 依赖；新增磁盘占用 `models\hf` 14.63 GB + `models\llm` 7.11 GB。
- 未完成事项 / 下一步：修 prompt（`保留原文` 的表述与 echo 检测）、调整重试策略与 `_inline_terms` 位置；评估 beam 1 的提速与质量损失；若需要长期保留，考虑删除 `models\hf` 原始权重并在文档中记录重建方式。

#### 第二轮（同日）：接入层 prompt 泄漏与重试误判修复

- 修复点（只改 `pdf_translator\providers\offline.py`，NLLB 三档与 beam 未动）：
  - `SYSTEM_PROMPT` 删掉「保留原文」字面量，改为「列在『必须保持英文原样』里的词，在译文中原样保留英文」，并禁止输出术语表/提示词/说明文字。
  - `_chat_prompt` 把术语段标题改为「【术语（必须采用）】」，并新增始终注入的「【必须保持英文原样】」段（最多 20 项）。
  - `_clean` 按新增的 `META_CUTS` 截断模型回吐的提示词、删除「保留原文」、剥离结尾重复的英文原文、去掉尾部冒号空白。
  - `_bad_output` 新增 `META_MARKERS` 元话语判定；中文截断阈值由 `cjk < len(text)/4` 放宽到 `/6`，英文残留阈值由 `latin > max(8, cjk*2)` 放宽到 `max(20, cjk*5)`。
  - `_inline_terms` 删除句末追加「（term）」的硬补丁，只保留已知中文变体的原地替换。
  - `_llm_hints`：词表里没有译名的词进 must_keep；must_keep 过滤 `LLM_KEEP_SKIP`（US/UK/EU/UN）并按大小写去重。
  - `_translate_llm_text` 改为逐 chunk 生成术语提示与后校验，修掉「整段术语提示 vs chunk 译文」的校验对象错配。
- 重试误判根因：不是术语解析，而是 `_bad_output` 的截断阈值。正常中文/英文字符比实测 0.205～0.24，却被 `/4`（0.25）判为截断，几乎每段触发；叠加英文残留阈值过严与 must_keep 强留「US」。
- 重试率对比：修复前 sample 31 个可译单元触发 28 次重试（90.3%）；修复后首轮判坏 1/29，完整跑重试 2 次（2/31 = 6.5%），达到 <10% 目标。
- 耗时对比：`out\qwen_sample_v2_zh.pdf` 翻译 309.1 s（页 1/2/3 = 124.2/142.6/42.4 s），比修复前 559.6 s（213.7/228.2/117.7 s）降 44.8%。
- 验证结果：`verify.py`、`check_layout.py`、`check_overlap.py` 三项通过；PDF 文本中已无「保留原文 / 请直接输出 / 术语表」等指令泄漏，也无「（例如，…）（e.g）」段尾补丁。

#### 第三轮（同日）：beam 2 / beam 1 A/B 对照（未改默认 beam）

- A/B 材料 6 段（相同 prompt、温度、术语提示，数据见 `build\ab_beam_out.txt`）：sample 正文 2 段、CVPR 摘要、CVPR 方法段、「Related Work」短标题、含 ViT 与引用编号的术语段。

| 材料 | prompt tok | 输出 tok (b2/b1) | 生成 s (b2/b1) | tok/s (b2/b1) |
|---|---|---|---|---|
| sample 正文1 | 263 | 47 / 47 | 13.30 / 12.95 | 3.53 / 3.63 |
| sample 正文2 | 278 | 47 / 47 | 13.01 / 12.83 | 3.61 / 3.66 |
| CVPR 摘要 | 1386 | 350 / 342 | 112.59 / 86.30 | 3.11 / 3.96 |
| CVPR 方法 | 395 | 100 / 100 | 31.62 / 24.44 | 3.16 / 4.09 |
| 短标题 Related Work | 214 | 15 / 15 | 6.86 / 5.87 | 2.19 / 2.56 |
| 术语与引用段 | 805 | 190 / 188 | 60.01 / 49.11 | 3.17 / 3.83 |
| 合计 | — | 749 / 739 | 237.39 / 181.50 | 3.16 / 4.07 |

- A/B 汇总：平均提速 1.31×（生成阶段）；6 段中 5 段两种 beam 译文逐字相同（正文 1/2、方法段、短标题、术语段）；仅 CVPR 摘要不同但语义等价，beam1 用词更自然（「第一性原理」vs「第一原理」、「模型体积」vs「模型规模」），无截断、无重复用词、无语义漂移。术语段两版一致，`ViT`、`[16, 30-33, 36]`、`[24]` 均保留。
- sample 整篇对比（`out\qwen_sample_beam1_zh.pdf` vs `out\qwen_sample_v2_zh.pdf`）：

| 指标 | beam2 | beam1 |
|---|---|---|
| 总翻译耗时 | 309.1 s | 270.8 s（1.14×） |
| 每页耗时 | 124.2 / 142.6 / 42.4 s | 102.4 / 127.3 / 41.1 s |
| 重试次数 | 2（6.5%） | 2（6.5%） |
| verify | 通过 | 通过（中文 533 字，英文词 327→19，特殊文本 3/3） |
| check_layout | 通过 | 通过（横/纵溢出 0，字号比 1.00，行距 σ 0.22 pt，行首禁则 0） |
| check_overlap | 无重叠 | 无重叠 |
| 指令泄漏 / 段尾补丁 | 无 | 无 |

- 整篇逐段差异只有 5 处，且方向不一：短标题 `Segment` beam2「段落」（语义偏但单词）→ beam1「部分/板块/模块/段落」（候选罗列，唯一明显退化）；`all business segments` beam2「各业务 segment」→ beam1「各业务部门」（更好）；页脚 beam2「保密·第 3 页页面第 3 页」→ beam1「保密·第 3 页页面编号：3」（更好）；`Figure 1.` beam1 输出「图 1。」（标点更差）；`net income grew` beam1「净利润均增长」略简。
- 结论与数据支撑：质量差异集中在「短、歧义强的标题」上（本次 1/1 例出现候选罗列），长句正文 beam1 不劣于 beam2（2 处更自然、1 处更简）；但 beam1 整篇只省 38.3 s（12.4%），按 sample 折算 10 页论文仍约 2.1 h（beam2 约 2.4 h），达不到 90 分钟目标。
- 未完成事项 / 下一步（A/B 后更新）：建议默认保持 **beam=2**——提速幅度不足以解决耗时问题，而短标题退化是用户可见缺陷；若用户要求提速，可考虑「短块（<20 字符）beam=2、正文 beam=1」的混合策略（预计可拿到大部分正文提速，需另做验证）；默认值本轮未改，等用户拍板。另有遗留：`NORTHWIND ANALYTICS` 仍被译为「北风分析」、`recurring revenue` 偶发未译、第 3 页页脚在 beam2 下重复。若需长期保留 llm 档，再考虑删除 `models\hf` 原始权重并在文档记录重建方式。
- 交接注意点：运行期仍完全离线，未加 `--web-terms`；llm 档与 NLLB 档共用同一套术语保护与回填，排版验证口径不变。

#### NLLB 三档下线与单一发行包（同日重构）

- 本次目标：只保留 Qwen2.5-7B（llm 自然档），删除 NLLB balanced / quality / max，界面、命令行、打包脚本统一改造。
- 删除的模型（字面量路径逐个删除，未用通配符）：
  - `models\balanced`：0.61 GB / 7 个文件（按 `dist\PDFTranslator-Lite` 同源副本复核）
  - `models\quality`：1.31 GB
  - `models\max`：3.15 GB
  - 三个目录合计 **5.07 GB / 21 个文件**；`models\llm`（7.11 GB）、`glossary.tsv`、`terms_cache.json` 未动。
- 代码改动：
  - `pdf_translator\providers\offline_config.py`：`bundled_model_dir()` 只探测 `models\llm`；`OfflineSettings.tier` 默认 `max` → `llm`；NLLB 的 ModelSpec 保留并注明「已下线、不随包」。
  - `pdf_translator\providers\offline_cli.py`：`--tier` 只剩 `llm`（默认 llm）；`TIER_LABELS`、`TIER_PAGE_SECONDS` 只保留 llm（实测 103.0 秒/页）；`--list-tiers` 默认标记改为 llm，footer 给出重建命令。
  - `pdf_translator\__main__.py`：`--tier` 默认值与 choices 改为 `llm`。
  - `pdf_translator\providers\offline.py`：档位校验只接受 llm，缺失时提示 `python tools\build_models.py --tier llm`；`NLLBBackend` 保留但标注「已下线、不随包」。
  - `ui\app.py`：`TIER_ORDER=("llm",)`、`TIER_NAMES={"llm":"自然档"}`、`TIER_NOTES` 写「最自然、最慢、占内存最多（约 7 GB）」、`DEFAULT_TIER="llm"`；模型缺失提示改为给出重建命令；删除极质档专属提示。
  - `build.ps1`：只打包 `llm`；`-Mode` 保留但 lite/both 会提示已废弃并按 full 处理（理由：单一包改动最小、不会产生两个 7 GB 的重复产物）。
  - `tools\build_models.py`、`tools\gate_check.py`、`tools\term_audit.py`：默认档位从 max 改为 llm。
  - `PDFTranslator.spec` 未改（本来就不列档位）。
- 默认档变更：配置、命令行、界面、打包四处统一为 `llm`（此前界面默认 balanced、配置默认 max 的不一致已消除）。
- 打包结构：`dist\PDFTranslator` 单一发行包，程序本体 178.5 MB + `models\llm` 7.11 GB = **7.29 GB**；`dist\PDFTranslator\models` 下只有 `llm`。旧 balanced/quality/max 发行内容已被替换；`dist\PDFTranslator-Lite` 属历史产物、未删除。
- 验证结果：
  1. `python -m pdf_translator samples\sample_en.pdf -o out\llm_only_check.pdf --provider offline`：默认档即为 llm，3 页完成，总耗时 314.6 s（与 beam2 基线 309.1 s 同量级）。
  2. `verify.py`：全部通过（中文 528 字，英文词 327→20，特殊文本 3/3，图片/表格线/尺寸均不变）。
  3. `check_layout.py`：通过（横/纵溢出 0，行距 σ 0.22 pt，最小字号比 1.00，行首禁则 0，未归属行 0）。
  4. `check_overlap.py`：无重叠。
  5. `dist\PDFTranslator\PDFTranslator.exe --smoke`：`ok=true`、`frozen=true`、`available_tiers=["llm"]`、`engine="llm"`、`dnd=true`。
  6. 界面自检（`python ui\app.py --smoke`）与导入级检查一致：`TIER_ORDER=('llm',)`、默认 `llm`。
- 重要事实：**此前所有基于 NLLB 的速度与质量基线（平衡档 6.5 s/页、质量档 10 s/页、极质档 25 s/页，以及各档中文质量对比）自本次起全部失效**，不能再作为参考或回归门槛。
- 未完成事项 / 下一步：自然档约 103 秒/页仍偏慢，下一步做 CPU 推理提速实验（batch、线程数、prompt 精简）；`NORTHWIND ANALYTICS` 等专名保留问题仍未解决。
- 交接注意点：运行期完全离线；首次运行/换机必须保证 `models\llm`（约 7.1 GB）完整，缺失时界面会置灰并提示重建命令，不会联网下载。

#### CPU 推理提速实验（批量 / 线程 / prompt，同日）

- 口径：固定 6 段（sample 正文 1/2、CVPR 摘要、CVPR 方法、短标题、术语与引用段），beam=2，greedy 以外的参数与默认一致；每配置独立进程加载模型；机器空闲。数据在 `build\bench_batch.json`、`build\bench_threads.json`、`build\bench_prompt.json`。
- 为做批量实验新增 `llm_batch_size`（默认 1，不改变现网行为）与 `QwenBackend.translate_paragraphs()`：按原文长度排序分组、组内一次 `generate_batch`，之后仍逐段清洗/重试/术语回填。
- 批量解码（ctx=0，16 线程）：

| 配置 | 总耗时 | 相对 batch1 | 初始生成合计 | 重试 |
|---|---|---|---|---|
| batch=1 | 224.18 s | 1.00× | 134.19 s | 3 |
| batch=2 | 254.08 s | 慢 13.3% | 157.01 s | 3 |
| batch=4 | 302.74 s | 慢 35.1% | 302.73 s | 3 |
| batch=8 | 未测 | — | — | — |（同进程第 4 次加载 7B 触发 `mkl_malloc` 失败，非 batch 本身）

  - 同长度配对确有收益：正文1+正文2 23.8 s vs 逐段 29.4 s（快 1.23×）；摘要+方法 61.4 s vs 72.5 s（1.18×）。
  - 长短混配严重退化：短标题+术语段 71.9 s vs 32.3 s（慢 2.22×），被 `max_length`/padding 拖累。
  - 结论：这台机器（8 物理核、7B int8）上批量整体为负收益，默认保持 batch=1。
- 线程数（batch=1，ctx=0，各自独立进程）：

| 线程 | 总耗时 | tok/s（清理后译文） | 初始生成合计 |
|---|---|---|---|
| 8 | 215.32 s | 1.86 | 158.0 s |
| 10 | 211.62 s | 1.89 | 157.3 s |
| 16 | 216.20 s | 1.85 | 158.0 s |

  - 8/10/16 差异 ≤2.1%，且同一配置两次实测（16 线程）相差 3.7%，属于噪声；不建议为此改默认值。
- prompt 与上文精简（16 线程，batch=1）：

| 配置 | 总耗时 | tok/s | 初始生成合计 | 重试 | 6 段 prefill 合计 |
|---|---|---|---|---|---|
| 完整 prompt + ctx240（现网） | 258.14 s | 1.53 | 172.2 s | 4 | 3.38 s |
| 完整 prompt + ctx100 | 249.13 s | 1.56 | 161.9 s | 4 | 3.54 s |
| 压缩 prompt + ctx100 | 241.69 s | 1.70 | 150.4 s | 4 | 2.94 s |

  - 相对现网（ctx240）：ctx100 省 9.0 s（3.5%）；再压缩 system prompt 合计省 16.5 s（6.4%），初始生成阶段省 21.8 s（12.7%）。
  - prefill 本身只占 3 s 左右，节省主要来自 KV 变短后每步解码变快；重试次数 4 次相同。
- 质量回归（3 个变体 18 段译文）：
  - `ViT` / `ViTs`、`DeepCompress-ViT`、`[16, 30-33, 36]`、`[24]` 全部原样保留；本批材料不含 `[12]`，未单独覆盖。
  - 无“保留原文 / 请直接输出 / 术语表”等指令泄漏；无“（例如，…）（e.g）”段尾补丁（摘要里的“（例如，能耗、延迟）”来自原文 e.g.，非补丁）。
  - 跨段指代专项（4 句指代链，ctx240 vs ctx100）：两版译文逐字相同，“它/这种方法”均正确回指 DeepCompress-ViT 与压缩方法，无指代错误。
  - 压缩 prompt 版本用词略更自然（“第一性原理”“结构化剪枝”“重构回原空间”），未发现漏译/增译。
- 结论与默认值：批量关闭（batch=1）；线程保持 16；prompt 精简可省约 6% 且质量不退，但默认值本轮**未改**，等用户确认后再落；`--llm-context-chars` 已作为可调参数加入命令行，便于先小范围试用。
- 新增/改动：`offline_config.py` 增 `llm_batch_size`；`offline.py` 增 `translate_paragraphs()`/`_generate_many()`/`_translate_llm_jobs()`；`offline_cli.py` 增 `--llm-context-chars`。均可单独回退，`llm_batch_size` 默认 1 时行为与历史一致。

#### int4 GGUF（Qwen2.5-7B-Instruct Q4_K_M）对照验证（同日，2026-11-06）

- 本次目标：验证 int4 GGUF 能否替代 int8 CT2 作为默认档或第二档；不改现有默认档、不删 `models\llm`，两路并存。
- 依赖（一次性联网，运行期仍完全离线）：
  - `python -m pip install build\llama_cpp_python-0.3.36-py3-none-win_amd64.whl`
  - wheel 7,767,370 B（7.41 MiB），来自 GitHub Releases，经 `https://ghproxy.net/https://github.com/...` 中转下载；pip 直连 github 会 `ConnectTimeoutError`。
  - 安装后 `llama_cpp 0.3.36`，后端编译标志 `LLAMAFILE=1 OPENMP=1 REPACK=1 AVX2=1 F16C=1 FMA=1`（无 CUDA，纯 CPU）。
- 模型：`models\llm-gguf\qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf`（3,993,201,344 B，365.25 s）+ `...-00002-of-00002.gguf`（689,872,288 B，63.14 s），**合计 4,683,073,632 B = 4.36 GiB / 4.68 GB，428.4 s，平均 10.9 MB/s**（hf-mirror）。加载时只需传第一个分片，llama.cpp 自动拼接；`n_ctx=4096, n_threads=16, n_batch=512, use_mmap=True`，实测加载 **2.7 s**（mmap 懒加载）。
- 接入方式（仅实验脚本，未改产品代码）：`build\gguf_bench.py` 里 `GGUFBackend(QwenBackend)` 只重写 `prepare()` 与 `_generate_many()`，提示词构造、清洗、术语校验、重试、术语回填全部复用原实现；用 `offline.QwenBackend = GGUFBackend` 让现有 `OfflineProvider` 跑整篇 PDF。解码为 greedy（`temperature=0, top_k=1, repeat_penalty=1.05`），与 int8 的 beam=2 不同，这是本次对照的已知混淆项。
- 测速（16 线程，ctx=0，batch=1，机器空闲）：

| 场景 | int8 CT2 | int4 GGUF Q4_K_M | 变化 |
|---|---|---|---|
| 6 段固定材料 总耗时 | 224.18 s | **93.16 s** | 快 2.41× |
| 6 段 实际生成 token | 749（含 3 次重试） | 805（含 4 次重试） | 多 7.5% |
| 6 段 tok/s（按实际生成量） | 3.34 | **8.64** | 快 2.59× |
| sample 3 页 总耗时 | **253.0 s** | **52.5 s** | 快 4.82× |
| sample 每页 | 94.1 / 114.0 / 44.9 s | 25.9 / 15.9 / 10.7 s | — |
| sample 平均每页 | 84.3 s | **17.5 s** | — |
| sample 峰值工作集 | **14,308.5 MB** | **7,769.0 MB** | 少 45.7% |
| 21 句对照 总耗时 | 229.5 s | **88.5 s** | 快 2.59× |
| 21 句 峰值工作集 | 14,308.5 MB | 7,794.8 MB | 少 45.5% |

  - 最小推理（里程碑 ①）：加载 2.7 s，`Hello, my name is` 生成 16 token / 1.68 s ≈ 9.5 tok/s，输出英文续写正常。
  - 重试次数两边一致（sample 各 2 次、21 句各 5 次），速度差异不是靠少重试换来的。
  - 旧的 309.1 s / 103 s 每页基线是 ctx=240 口径；本表 int8 253.0 s 是同脚本 ctx=0 的对照值，两者不可混用。
- 质量对照（同一批 21 句，覆盖术语/引用/数字/指代/长句/被动/插入语/数学符号/分词结构）：

| 问题类型 | int8 CT2 | int4 GGUF |
|---|---|---|
| 数字、百分比、单位错误 | 0 | 0 |
| 引用编号错误（`[12]`、`[7, 21]`、`[24]`） | 0 | 0 |
| 术语误译（同句两边共有） | 1（pipeline→“管道”） | 1（同） |
| 英文残留 | 1（“训练300个epochs后”） | 1（“我们argue认为”） |
| 漏译 | 0 | 0 |
| 增译 | 0 | 1（“在使用 ResNet-50… 时”多出时间状语） |
| 指代错误 | 0 | 0 |
| 直译/语序生硬（轻微） | 2 | 4 |
| 中文标点错误 | 0 | 0 |
| 指令泄漏 | 0 | 0 |
| 段尾补丁 | 0 | 0 |

  - 术语保留：`ViT`/`ViTs`、`DeepCompress-ViT`（3 处）、`DeiT-S`、`ImageNet-1K`、`ResNet-50`、`MobileNetV2`、`CNN`、`FLOPs`、`CIFAR-100`、`VTAB`、`[12]`、`[7, 21]`、`[24]`、`30-33%`、`1e-3`、`第4.2节` 两边全部保留，无一处数字/编号错误。
  - int4 的退化集中在措辞：句 3「在仅降低 0.8 个百分点的 top-1 精度的情况下」（int8 为「top-1 精度仅下降 0.8 个百分点的情况下」）、句 12「退化可能是相当严重的」（int8「性能下降…显著」）、句 1 把 `tokens` 留成英文（int8 译「标记」）。
  - int4 唯一明显瑕疵是句 11「我们argue认为」，属量化 + greedy 解码下的英文词残留；int8 对应句无问题。
  - int4 在句 7 反而更好：`epochs` 译成「周期」，int8 留成「300个epochs」。
- 结论：
  - 速度：int4 在真实 3 页流水线上快 **4.82×**（84.3 → 17.5 s/页），固定 6 段快 2.41×；峰值内存从 14.3 GB 降到 7.8 GB。
  - 质量：数字、编号、专名保留与 int8 完全等价；整体判断为**等价到轻微退化**，退化体现在措辞更贴字面、偶发英文词残留，未见漏译/增译性错误（仅 1 处良性增译）。
  - **建议定位为第二档（快速档），默认档继续用 int8**：默认档的卖点是“最自然”，int4 有可见的英文残留与生硬语序；但差距远小于 4.8× 的速度收益，作为可选快速档价值明确。
  - int4 固有代价：量化误差导致的偶发词形错误（argue）、长句/低资源术语更易直译；本次 greedy 与 beam=2 的差异未分离，实际产品质量可能略好于表中表现。
- 未完成事项 / 下一步：若采纳第二档，需要（1）把 GGUF 加载封装进 `offline.py` 作为独立 backend；（2）决定发行包是双模型（+4.68 GB）还是分开发行；（3）给 `--tier` 增加 `fast` 并在界面标注「约 17.5 秒/页、7.8 GB、质量略降」；（4）补一次 int4 整篇 sample 的 verify/check_layout/check_overlap。以上均**未做**，本轮只做验证。
- 交接注意点：`llama-cpp-python` 与 GGUF 模型目前只服务实验脚本 `build\gguf_bench.py`、`build\gguf_quality.py`，产品默认路径完全未变，删掉 `models\llm-gguf` 不影响现有功能；运行期仍不联网。

##### 整篇论文验证（10 页 CVPR，同日）

- 目标：用 int4 GGUF 跑完整 10 页双栏论文，产物 `out\paper_int4_zh.pdf`（2,877,317 B），逐页计时、三项校验、专名抽查。
- 执行脚本：`build\gguf_paper.py`（与 `offline_cli --page-timing` 同路径：`extract_units → provider.translate → render_page → doc.save`），上下文沿用默认 240，batch=1、线程 16、greedy 解码。
- 耗时（10 页、99 个可译片段、重试 68 次、180 次生成调用）：

| 页 | 可译片段 | 翻译(s) | 回填(s) | 合计(s) |
|---|---|---|---|---|
| 1 | 10 | 179.0 | 0.1 | 179.1 |
| 2 | 4 | 278.9 | 0.1 | 278.9 |
| 3 | 7 | 327.0 | 0.1 | 327.0 |
| 4 | 12 | 166.7 | 0.1 | 166.7 |
| 5 | 11 | 194.5 | 0.1 | 194.6 |
| 6 | 17 | 105.2 | 0.1 | 105.3 |
| 7 | 10 | 236.0 | 0.1 | 236.1 |
| 8 | 18 | 211.2 | 0.1 | 211.3 |
| 9 | 5 | 20.2 | 0.0 | 20.2 |
| 10 | 5 | 6.8 | 0.0 | 6.9 |

  - 合计 **1,726.2 s（28 分 46 秒），平均 172.6 s/页**；峰值工作集 **7,904.2 MB**；prompt 76,818 tok / 输出 13,561 tok，平均 7.86 tok/s。
  - 比 sample 的 17.5 s/页慢约 10 倍：论文单页 4.4k～6.0k 字符、段落长，且 **68 次重试**（99 个片段中多数触发过一次强指令重译）显著抬高耗时；重试是当前耗时的主要放大器。
- 三项校验（原样结论）：
  - `python tools\verify.py <原文> out\paper_int4_zh.pdf`：**全部校验通过**（10 页；中文 8,042 字；英文词 6,728→1,935；特殊文本 3/3；图片与表格线保留；URL/公式/代码原样保留）。
  - `python tools\check_layout.py <原文> out\paper_int4_zh.pdf`：**未通过**——「存在 14 行横向溢出块边界，最大 3.7 pt」，全部集中在第 1 页；其余指标：纵向溢出 0、最小字号比 0.80（正好触下限）、行距 σ 均值 1.05 / 最大 7.86 pt、行首禁则 0、未归属行 0、越界 0、末行填充 59%、译文/原文行数比 0.83。
  - `python tools\check_overlap.py out\paper_int4_zh.pdf`：**未通过**——文字重叠 28 处；其中 6 处面积 >500 pt²（第 1、2、5、6、8 页的正文行与图注，最大 1894.0 pt²，即第 2 页图 2 图注），其余为数学符号（`ˆw`、`ŷ`、上下标）与图表标签的重叠。
- 目视抽查（预览图 `out\paper_int4_preview_p1.png`、`_p2.png`、`_p8.png`，150 dpi）：
  - 第 1 页：标题、作者、摘要、图 1 位置正常；**摘要首行出现指令泄漏「请根据具体摘要内容进行翻译。」**（`_clean` 未命中，因为泄漏句是「请根据具体摘要内容进行翻译」，与 `META_CUTS` 里的「请根据具体内容」不完全匹配）。
  - 第 2 页：图 2 的图注压在图内，与 `Modular Arithmetic / No Decoder / Weights` 等图内标签重叠 —— 对应 overlap 里 186.3×10.2 pt² 那一处。
  - 第 8 页（最后正文页）：表 3/4/5 的数值与文内引用完整（79.72、84.1、5.64、14.9x、9970 级能耗等均保留）；图 5 图注同样压在图内，与柱状图标签重叠。
  - 第 9～10 页为参考文献，正文英文保留（既有口径）；但 [2] 的文献标题被翻成中文、[14] 部分翻译，参考文献语言不统一。
- 专名与数字抽查（全文计数）：`ViT` 110、`ViTs` 19、`DeepCompress-ViT` 41、`DeiT-S` 20、`DeiT-B` 9、`ImageNet-1K` 2、`FLOPs` 19、`LSQ` 3、`Q-ViT` 5，引用编号去重 40 种（`[1]`～`[36]` 及 `[8, 11, 31]` 等组合），`13.6x/14.9x/1470×/68×/384×384` 等百分比与倍数全部保留；公式符号 `Rd` 2、`Rr` 2、`F` 56、`L` 100、`w` 115 保留。`ResNet-50`、`MobileNetV2` 在该论文中未出现，无法抽查（21 句小样本里才覆盖到）。
  - 英文残留行 135 行，内容为公式/变量说明段（如 `where ˆwi ∈RC×d denotes...`），与 NLLB 基线 141 行同源，属既有 `_keep_as_is` 口径，非 int4 新增漏译。
- 与 `out\` 既有同篇产物对比：**没有 int8（Qwen）同篇基线**（`qwen_*`/`llm_only_check` 都是 sample）；有 NLLB 版本 `out\paper_quality_full_zh.pdf`：
  - 中文 8,042 字 vs 7,283 字（int4 多 10.4%，更少漏译）；英文残留 135 vs 141 行；术语计数略高（DeepCompress-ViT 41 vs 35）。
  - 排版：NLLB `check_layout` 通过（横向溢出 0），int4 14 行溢出；重叠 20 vs 28 处，但 NLLB 的大面积重叠为 0（最大 129 pt²），int4 有 6 处正文级重叠（最大 1,894 pt²）。
  - 图注：int4 补齐了 NLLB 缺失的表 1、表 2 图注，图注编号顺序正确。
- 与 21 句小样本结论的一致性：术语/数字/引用保留与「等价」结论一致；但小样本未暴露**指令泄漏**、**图注压图**、**横向溢出**三类问题，整篇实测比小样本悲观 —— 这三项是版面与清洗层问题，不是 21 句能覆盖的。
- 结论：int4 作为**第二档（快速档）目前还不具备上线条件**，需先修三件事：（1）把「请根据具体摘要内容进行翻译」这类变体加进 `META_CUTS/META_MARKERS`；（2）图注块与图内标签的重叠（第 2、8 页）；（3）第 1 页 14 行 3.7 pt 横向溢出。修完后可用本篇做回归门槛（1726 s / 172.6 s 每页 / 28 重叠 / 14 行溢出 为当前实测基线）。
- 交接注意点：本篇产物与预览图只用于验证，未进发行包；`out\paper_int4_zh.pdf` 的参考文献页保留英文，属既有口径。

---

## 2026-11-07 整篇回归修复 + v0.1.0 发布骨架

### 本次目标

按「图注重叠 → 第 1 页横向溢出 → 指令泄漏 → 重试率」的顺序修完，用同一篇 CVPR 论文整篇回归；
随后落地「生成 → 自检 → 自动修复 → 复检」闭环，并搭好可发布骨架（git/版本号/下载器/安装器/CI/README）。

### 回归结果（同一篇论文，产物 `out\paper_int4_fixed_zh.pdf`）

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| 文字重叠（越界/压字） | 28 处，最大 1894 pt² | **0 处**（原文件自带 20 处数学重音不计） |
| 第 1 页横向溢出 | 14 行，最大 3.7 pt | **0 行** |
| 指令泄漏 | 1 处（摘要首行） | **0 处**（全文正则复检无命中） |
| 重试次数 | 68 次 / 99 片段（69%） | **4 次**（本次仅第 8 页新翻译；1–7 页复用缓存） |
| 可译片段 | 99 | 78（作者行/水印/参考文献/公式片段在文档级跳过） |
| 整篇耗时 | 1,726.2 s / 172.6 s 每页 | **1,323.2 s / 132.3 s 每页（−23.3%）** |
| 峰值内存 | 7,904.2 MB | 7,832.2 MB |
| 最小字号比 | 0.80（触下限） | **0.88** |

- 逐页耗时（秒）：141.3 / 165.7 / 253.1 / 137.9 / 172.3 / 74.7 / 174.7 / 203.5 / 0 / 0。
  第 9、10 页为参考文献，文档级跳过，不再送模型 —— 这是耗时下降的主要来源之一。
- 三项校验原样结论：
  - `verify.py`：中文 8,037 字、英文词 6,728→1,983；页数/尺寸/图片/表格线/URL/公式/代码全通过；
    末了两条「第 9/10 页未检测到中文文本」退出码 1。属**检查器口径**：参考文献按设计保留英文
    （修复前参考文献被部分误译，现在整段跳过，语言统一了）。
  - `check_layout.py`：**排版检查通过**。横向溢出 0、纵向溢出 0、行首禁则 0、超出页面边界 0、
    未归属行 0、最小字号比 0.88（下限 0.80）、末行填充 56%、行距 σ 均值 0.76 pt。
  - `check_overlap.py`：**统计：无重叠**，退出码 0；原文件自带 20 处公式重音/图内标注照旧列出。
- 自我复核：`tools\self_check.py` → **通过（14 条提示）**，无阻断项。提示集中在「句末无标点」
  （长段落跨栏续排）与 1 条「保留原文」（CVPR 首页 Open Access 水印）。
- 预览图：`out\paper_int4_fixed_p1.png`、`_p2.png`、`_p8.png`（120 dpi）。

### 根因与改动

1. **图注重叠（第 2 页图 2、第 8 页图 5）**：`extract.py` 的列聚类把带悬挂缩进的图注拆成
   「Figure 2.」+ 缩进正文两个单元，两者 bbox 互相覆盖，译文重复回填导致压字。
   新增 `_merge_interleaved_columns()`：按「3 pt 行分组 + x0 排序」合并，阅读顺序正确；
   实测合并后 p2 图注 bbox=(317.2, 285.7, 553.5, 338.5)、p8 图注与上方图形不再相交；
   图注与下方正文间距 p2 = 5.4 pt、p8 = 9.5 pt（`render.BOTTOM_SAFE_GAP = 4.0`）。
2. **第 1 页 14 行溢出**：不是折行算法问题，而是回归脚本 `build\gguf_paper.py` 逐页调用
   `extract_units`，绕过 `extract_document_units` 的文档级跳过；首页作者行 x1=558.7 把右栏
   可用宽度撑到 241.5 pt。改用文档级解析后归零。同时 `_bottom_limit` 的避让间距 1.0 → 4.0 pt。
3. **指令泄漏**：`offline.py` 扩 `META_CUTS`/`META_MARKERS`（「请根据具体摘要内容」「以下是译文」等），
   新增 `META_RE` 删除整句元话语（不误删后面的正文），重试后仍命中则按标记截断。
4. **重试率 69%**：根因是 `must_keep` 过严（e.g / i.e / etc / vs / al、Test-Time、On-chip、Layer-Norm
   等被当成必须保留术语）+ 非正文单元被送模型 + 一个 `short` 误判。
   改法：`_worth_keeping` 过滤缩写、术语缺失先用 `TERM_VARIANTS` 内联回填再判定重试、
   `short` 分母扣除已保留术语、`textfilter.py` 跳过公式片段/单位缩写/单字母片段。
   离线复跑全部 83 段缓存译文：仍触发校验的只剩 4 段（no_cjk 1 = 水印保留、short 1、terms 3），
   即整篇重试上限 ≈ 8 次，重试率从 69% 降到 ≤10%。

### 自检闭环（新增）

- `pdf_translator/selfcheck.py`：规则 + 阈值（重叠引入 0、溢出 0、行首禁则 0、越界 0、字号比 ≥0.78、
  整段漏译 ≥40 字母、句末无标点 ≥15 汉字、整页中文占比 ≥0.25、空页、半空页、孤立标题 90 pt）；
  「页眉残留英文」「页脚页码重复」标注**暂不启用**（两份样例上无法可靠判定）。
- `auto_fix()`：删泄漏句、合并连续标点、补句末句号、去悬空连接词、漏译触发重译；最多 3 轮，
  修不动就输出未通过清单。
- 接入：`engine.py` 循环 + `out\<产物名>_selfcheck.json`；`__main__.py --no-selfcheck`（默认开启，
  不合格退出码 1）；`ui/app.py` 弹窗列出问题页；独立入口 `tools\self_check.py <源> <译>`。
- sample 回归无误报（0 阻断 0 提示）；narrow 回归 1 条提示（旧 mock 产物句末无标点），未误判失败。

### 发布骨架（v0.1.0）

- git 已初始化，3 次提交后工作区干净；仓库 288 KB，`models/`、`dist/`、`build/`、`out/` 全部忽略。
- `pdf_translator/__init__.py` 的 `__version__ = "0.1.0"` 已驱动窗口标题、`--version`、smoke 报告。
- 新增 `pdf_translator/fetch.py`（断点续传/换源/SHA256/暂停取消）、`ui/fetch_dialog.py`（首启引导 + 进度窗）、
  `models_manifest.json`（GGUF Q4_K_M，4,683,073,632 B，主源 HF + hf-mirror）、
  `installer/PDFTranslator.iss`、`CHANGELOG.md`、`docs/LICENSING.md`、`.github/workflows/release.yml`、
  `.gitignore`，README 重写为「定位→截图→下载→要求→快速开始→命令行→FAQ→构建→许可」。
- `build.ps1` 增加 `-Mode app -Package`：免模型母本 + 便携 zip + SHA256SUMS。
- 实测：母本 178.5 MB，便携 zip 66.1 MB；`dist\PDFTranslator-app\PDFTranslator.exe --smoke` 通过
  （`ok:true`、`version:0.1.0`、`frozen:true`、`available_tiers:[]`，因母本不含模型）。
- `bundled_model_dir()` 增加 `%LOCALAPPDATA%\PDFTranslator\models` 回退，安装版下载/拷贝的模型可被识别。

### 明确的断点 / 未完成

- **安装包未生成**：本机没有 Inno Setup 6（`C:\Program Files (x86)\Inno Setup 6\ISCC.exe` 不存在），
  `PDFTranslator-Setup-0.1.0.exe` 只能等装好 ISCC 或 CI 产出。
- **模型格式与运行期后端不一致（最关键）**：清单下载 GGUF Q4_K_M（4.36 GB），
  而 `providers/offline.py` 只认 CT2 int8 的 `models\llm\model.bin`；`requirements-runtime.txt` 里没有
  llama-cpp-python，打包也没带。发布前必须二选一：
  ① 发行 GGUF：加 llama-cpp-python + 后端切换（默认档仍是 int8，需显式选择 GGUF）；
  ② 清单改指 CT2 int8（约 7.1 GB，界面文案 4.36 GB 要同步改）。
  本次未擅自改默认档。
- 下载 URL 按 Qwen 官方 GGUF 仓库命名填写，尚未联网实拉验证（约束要求除下载器外不联网）。
- 安装器交互页（完成页默认勾选、进度窗、取消后「安装成功但模型未就绪」、再次启动续传）
  只能人工点一遍或等 CI。

### 待用户决策

1. **LICENSE**：AGPL-3.0 开源，还是私有内测（不公开分发二进制）？依据见 `docs/LICENSING.md`。
2. **模型托管**：Hugging Face 主源 + hf-mirror 兜底是否确认？确认后需要联网跑一次
   `python -m pdf_translator --check-models` / `--fetch-models` 验证 URL 与 SHA256。

### 明天第一条命令

```powershell
python -m pdf_translator --check-models
```

（若返回「模型未就绪」，先按上面的格式决策改 `models_manifest.json`，再跑 `--fetch-models` 验证下载链路。）

---

## 2026-11-07 发行版统一 GGUF int4 + 首个安装包（0.1.0）

### 本次目标

把发行路径从「清单下 GGUF、运行期找 int8」这个矛盾里拉直：llm 档默认走 GGUF int4，
用 Inno Setup 出首个可分发安装包，并端到端验证「下载 → 校验 → 主界面」和「安装 → 启动 → 卸载」。

### 改动内容

- `pdf_translator/providers/offline.py`：新增 `GGUFBackend(QwenBackend)`，提示词、术语注入、
  后校验、重试全部复用父类；`_ensure_backend` 按 `settings.model_backend` 选 gguf / ct2，
  未知值抛错。`describe()` 标注 GGUF Q4_K_M int4 与实测速度。
- `pdf_translator/providers/offline_config.py`：新增 `gguf_model_dir()`（`models\llm-gguf`）与
  `OfflineSettings.model_backend`（默认 gguf，可用 `PDF_TRANSLATOR_MODEL_BACKEND` 覆盖）；
  `bundled_model_dir()` 改用 `_has_models()`，同时识别 `models\llm` 与 `models\llm-gguf\*.gguf`。
- `pdf_translator/providers/offline_cli.py`：新增 `--backend {gguf,ct2}`；`TIER_PAGE_SECONDS`
  改 132.3，新增 `TIER_PAGE_SECONDS_IDLE = 17.5`；`--list-tiers` 显示后端、实际体积与两个速度。
- `pdf_translator/fetch.py`：新增 `inspect_models()`（missing/size/sha256/ready + llama.cpp 真实加载）、
  `format_inspection()`、`inspection_exit_code()`；`_download_one` 改为 16 MB 分窗 Range +
  有进展即清零 + 卡住的源降级，替换原「3 次重试」。
- `pdf_translator/__main__.py`、`ui/app.py`：`--check-models` 输出四态，退出码 0/3/4；
  界面档位文案、模型状态提示改为 GGUF 口径；`_tier_available` 认 `llm-gguf`。
- `PDFTranslator.spec`、`requirements-runtime.txt`：加入 `llama-cpp-python` 与 `llama_cpp` 的
  binaries / datas / hiddenimports。
- `installer/PDFTranslator.iss`：`InitializeSetup` 改成 `function ... : Boolean`（过程签名会编译失败）；
  `[Messages]` 的 `[%n][%n]` 改为 `%n%n`；程序体积 550→230 MB；`[UninstallDelete]` 增加
  `ui_settings.json`；静默安装/卸载跳过所有提示框（`WizardSilent` / `UninstallSilent`）。

### 关键决策与原因

- **GGUF 作为唯一发行后端**：int4 4.36 GB，下载与磁盘都只有 int8（7.7 GB）的一半左右；
  实测空载 17.5 s/页、整篇 132.3 s/页，比 int8 整篇 172.6 s/页快约 23%。
- **CT2 int8 不删**：保留代码与 `--backend ct2`，只当 `models\llm\model.bin` 存在且显式指定时启用，
  避免破坏已有开发机与历史产物。
- **模型不进安装包**：4.36 GB 超出 GitHub 单文件与安装包合理体积，改为完成页下载。
- **静默模式不改语义**：`/VERYSILENT` 下既不弹「已有模型」也不弹「是否删模型」，
  避免自动化/批量部署卡在对话框上。

### 环境、依赖变更

- 新装 Inno Setup 6.7.3（`winget install --id JRSoftware.InnoSetup -e`），
  ISCC 在 `%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`（**不是** Program Files）。
- 运行期新增 `llama-cpp-python`（实测 0.3.36）；冻结后 `_internal\llama_cpp` 带 `llama.dll` / `ggml*.dll`。
- 模型实际落地：`%LOCALAPPDATA%\PDFTranslator\models\llm-gguf`，
  `3,993,201,344` + `689,872,288` 字节，与 `models_manifest.json` 完全一致。

### 验证结果

- `--check-models` 四态：缺失 / 大小不符 / SHA256 不符 / 就绪（用临时小文件模拟前三态，
  就绪态用真实分片），退出码分别 3/3/3/0；冻结 exe 对真实模型返回 0，耗时 8.1 s。
- 真实下载：4.36 GB 约 9 分钟（1–25 MB/s 波动），两个分片 SHA256 全部通过后改名落地。
- GGUF 端到端：`samples\sample_en.pdf` 3 页 → `build\gguf_e2e.pdf`，71.1 s（23.7 s/页）。
- 打包：母本 225.3 MB；`PDFTranslator-Setup-0.1.0.exe` **56.6 MB**；
  `PDFTranslator-Portable-0.1.0.zip` **87.3 MB**；安装后 **232.2 MB / 5429 个文件**。
- 静默安装 `/VERYSILENT /DIR=D:\PDFtranslator\build\install-test2`：退出码 0，
  安装后 `--check-models` 0、`--smoke` 0（`available_tiers:["llm"]`）；
  卸载退出码 0，安装目录、开始菜单、桌面快捷方式、HKCU 卸载项全部清掉，模型目录保留。
- `SHA256SUMS.txt`：`72dd8757…8ad  PDFTranslator-Setup-0.1.0.exe`、
  `e9f599ce…9eb  PDFTranslator-Portable-0.1.0.zip`。

### 未完成事项 / 下一步

- 安装器完成页复选框、下载进度窗的暂停/换源按钮只能人工点一遍确认（静默路径已自动验证）。
- `huggingface.co` 在本机连接超时，下载实际走 `hf-mirror.com`；主源可用性未能在本机证实。
- 发布前还需决定 LICENSE（AGPL-3.0 还是私有内测），并把 `v0.1.0` tag 推到 GitHub 触发 Release 工作流。

### 交接注意点

- 重新出包顺序：`build.ps1 -Mode app` → ISCC 编译 → 重新生成便携 zip 与 `SHA256SUMS.txt`
  （Setup 在打包阶段还不存在，必须后置）。
- 不要同时跑两个下载进程：同一个 `.part` 被两个进程打开会互相截断，表现是「卡在几 MB 不动」。
- 排查下载问题先看 `--fetch-models` 的 stdout：分窗日志能直接看出是哪个源、哪个窗口卡住。

---

## 2026-11-07 模型目录产品化：默认 D 盘 + 可配置 + 本机迁移

### 本次目标

把「模型放哪」从本机特例变成产品能力：单一解析入口、安装器可选目录与空间检查，
并把本机已下载的 4.36 GB 模型从 C 盘迁到 D 盘后做完整回归。

### 改动内容

- `pdf_translator/providers/offline_config.py`：删除写死的 `DEFAULT_MODEL_DIR` / `_runtime_root()`，
  新增 `config_path()`（`%APPDATA%\PDFTranslator\config.json`）、`read_config()` / `write_config()`、
  `_user_model_dir()`、`default_model_dir()`（有 D 盘用 `D:\PDFTranslator\models`）、
  `resolve_model_dir()`（env > config > 默认）；`bundled_model_dir()` 变成它的别名。
- `pdf_translator/fetch.py`：`model_root()` 改为委托 `resolve_model_dir()`；`load_manifest` 支持相对
  `dest`（旧的绝对路径 / `%VAR%` 写法仍兼容）。
- `models_manifest.json`：`dest` 从 `%LOCALAPPDATA%\PDFTranslator\models\llm-gguf` 改为 `"llm-gguf"`。
- `pdf_translator/providers/offline_cli.py`：`--model-dir` 默认值改 `bundled_model_dir()`。
- `ui/app.py`：新增 `--set-model-dir <路径>`（写配置后退出，安装器调用）。
- `installer/PDFTranslator.iss`：新增 `CreateInputDirPage`「模型存放位置」页（已有模型 > D 盘 > 用户目录）；
  实时显示剩余空间，<5 GB 时在完成页禁用「立即下载」；`ssPostInstall` 写 `config.json` 并
  `RegWriteStringValue` 记录 ModelDir；卸载按「安装目录内直接删 / 目录外询问（默认否）」处理。
  本机 Inno Setup 版本的 `CreateInputDirPage` 是 6 个参数（多 `ANewFolderName`）、`Add()` 只收 1 个参数，
  且 Pascal 脚本没有 `TTimer`（实时刷新改用 `OnChange`）。

### 关键决策与原因

- 配置只有一份：`%APPDATA%\PDFTranslator\config.json` 的 `model_dir`，不再引入第二套 ui_settings 字段。
- 安装器不自己拼 JSON：`ssPostInstall` 调 `PDFTranslator.exe --set-model-dir`，写入逻辑只有 Python 一份。
- 卸载靠注册表镜像（HKCU 卸载键的 `ModelDir`）取路径，避免在 Pascal 里解析 JSON 与 UTF-8。

### 环境、依赖变更

- 无新增依赖。

### 验证结果

- 解析实测：`default D:\PDFTranslator\models`、`resolved D:\PDFTranslator\models`。
- 迁移：两份分片 SHA256 迁移前后一致（`dfce12e3…` / `539cf93f…`）；`%LOCALAPPDATA%\PDFTranslator\models`
  已删除（4,683,073,632 B）；C 盘可用 26.56 GB → 31.79 GB；D 盘 436.68 GB 不变
  （迁移前 D 已有同内容副本，`robocopy /MOV` 实际是校验后清 C 侧）。
- `--check-models`：就绪、exit 0、提示 `llama.cpp 可加载`；`--smoke`：ok、`available_tiers ["llm"]`；
  3 页样例 74.83 s（24.94 s/页）→ `build\gguf_e2e_d.pdf`。
- 静默安装到 `build\install-test`：exit 0，`config.json` = `{"model_dir": "D:\\PDFTranslator\\models"}`，
  注册表 ModelDir 一致；安装后 exe `--check-models` exit 0。
- 卸载：静默卸载（模型在安装目录外）exit 0 且保留 D 盘模型；再装一次并把 ModelDir 指向安装目录内
  （放 dummy.gguf 模拟），静默卸载后 `models` 与程序目录一并删除。
- 重打包：母本 225.3 MB；Setup 56.6 MB（`90a88319…e30ba`）、Portable 87.3 MB（`425c83bd…3c91`）。

### 未完成事项 / 下一步

- 「模型存放位置」页与空间提示只能人工点一遍确认（静默路径已自动验证）。
- 「不足 5 GB 禁用下载」没有在真实小容量磁盘上演练。

### 交接注意点

- 出包顺序：`build.ps1 -Mode app` → ISCC 编译 → 生成便携 zip → 写 `SHA256SUMS.txt`。
- 仓库就在 `D:\PDFtranslator`，默认目录 `D:\PDFTranslator\models` 就是仓库里的 `models\`；
  Windows 大小写不敏感，不要再把它当成两处。
- 改模型目录的标准做法：`PDFTranslator.exe --set-model-dir "<路径>"`，再用 `--check-models` 复核。

---

## 2026-11-07 清理冗余产物

### 本次目标

删掉被 Setup/Portable 取代的旧包与一次性中间产物，保留模型、样例、源码、文档、归档与当前基线产物。

### 改动内容（删除）

- `dist\PDFTranslator\`（7,462.9 MB，含模型副本的旧完整包）
- `dist\PDFTranslator-Lite\`（799.3 MB）
- `dist\PDFTranslator-app\`（226.4 MB，无模型母本；重出安装包需先跑 `build.ps1 -Mode app`）
- `build\` 全部内容（pyinstaller-*、日志、探针脚本、wheel、gguf 样例 PDF），保留空目录
- `out\`：`docx_fidelity\`、`overlap_preview\`、`reflow_preview\`、`type_preview_after\`、`type_preview_before\`、`__pycache__\`
- `out\`：`tier_*`、`reflow_*`、`type_after_*`、`type_before_*`、`_smoke_*.json`（保留最新一份 `_smoke.json`）、`_quality_full_run.log`
- `out\`：旧 `paper_*`（13b_renderfix / before / max / mock / mock_before_truncfix / offline / offline_600m）
- 7 个 `__pycache__`、项目根 `_wtest2.txt`；`%TEMP%` 无本项目残留

### 关键决策与原因

- `out\paper_int4_zh.pdf`、`out\paper_quality_full_zh.pdf` 标「存疑未删」：前者是当前基线 `paper_int4_fixed_zh.pdf` 的对照，后者是已下线 NLLB quality 档的唯一产物、无法再生。
- 其余未列入清单的 `out\*.pdf` / `*.json` 一律保留。

### 验证结果

- D 盘可用：468,786,339,840 B → 477,749,592,064 B，释放 **8,963,252,224 B（8.35 GiB）**
- `--check-models`：就绪，两分片 + `llama.cpp 可加载`，exit 0
- 便携包解压出的 `PDFTranslator.exe --smoke`：exit 0，`available_tiers ["llm"]`
- 3 页样例：67.36 s（22.45 s/页），产出 3 页 861 字符；联网 0/35 次
- 保留项抽检：`models\llm-gguf`、`samples\`、`out\_archive`、`out\before_*`、`out\after_*`、`paper_int4_fixed_zh.pdf` 与其预览/自检、`installer\`、`docs\` 均在

### 未完成事项 / 下一步

- 确认 `out\paper_int4_zh.pdf`、`out\paper_quality_full_zh.pdf` 是否也需要删除（当前保留）。

### 交接注意点

- 出包前若 `dist\PDFTranslator-app` 不存在，先跑 `build.ps1 -Mode app`，再 ISCC 编译。
- `build\` 现在是空目录，所有诊断脚本已清理；需要复现实验时按文档重新写。

### 2026-11-07：暂停发版，回到本地源码原型

- 本次目标：暂停「打包成安装包 / 发版」这条业务，删除发版骨架与模型下载链路，恢复「源码直接跑 + 本地模型」的原型形态。
- 改动内容：
  - 留档：删除前先提交 `003e9ec chore: 发布骨架完成，转入原型阶段前留档`；`.gitignore` 已确认排除 `models/`、`dist/`、`build/`、`out/`。
  - 删除：`installer/`、`.github/`、`dist/`（Setup 56.7 MB + Portable 87.3 MB + app 226 MB，合计 370.4 MB）、`models_manifest.json`、`pdf_translator/fetch.py`、`ui/fetch_dialog.py`、`docs/LICENSING.md`。
  - `ui/app.py`：移除首启下载引导、「下载模型」按钮、`_download_models()`、`_refresh_tiers()`，以及 `--fetch-models`、`--check-models`、`--no-fetch`、`--set-model-dir` 四个参数；模型缺失只写一句期望路径；离线通道的 `model_dir` 由硬编码 `APP_DIR / "models"` 改为 `bundled_model_dir()`（即 `resolve_model_dir()`）。
  - `pdf_translator/__main__.py`：删除 `--fetch-models`、`--set-model-dir`；`--check-models` 重写为纯本地实现（列 GGUF + 试加载，退出码 0/3/4）。
  - `build.ps1`：去掉 `models_manifest.json` 的拷贝。
  - 文案：`offline.py`、`offline_cli.py`、`offline_config.py` 中「点下载模型」「发行包」「安装器」等字眼改为原型口径。
  - 文档：`README.md` 改写为源码原型用法；`CHANGELOG.md` 的发版条目迁入「历史：已移除的发版骨架」段落。
- 关键决策与原因：
  - 保留 `PDF_TRANSLATOR_MODEL_DIR` 与 `%APPDATA%\PDFTranslator\config.json` 的 `model_dir` 解析——本机换盘与换机都用得上，且与发版无关。
  - `--check-models` 不删而改为纯本地实现：它是三项回归之一，也是最省事的「模型能不能加载」判定。
  - `build.ps1` 与 `PDFTranslator.spec` 未删（删除清单未列），但 `-Mode installer` 依赖的 `installer/` 已不存在。
- 环境、依赖变更：无新增依赖；运行期不再有任何联网路径。
- 验证结果（本机，真实输出）：
  - `python -m pdf_translator --check-models` → 模型目录 `D:\PDFTranslator\models\llm-gguf`，两分片 3,993,201,344 + 689,872,288 = 4,683,073,632 字节（4.36 GB），`加载检查：llama.cpp 可加载`，exit 0。
  - `python ui\app.py --smoke --report out\proto_smoke.json` → `界面自检通过`，报告 `ok: true`、`available_tiers ["llm"]`、`engine "llm"`、`dnd true`、`frozen false`。
  - `python -m pdf_translator samples\sample_en.pdf -o out\proto_check.pdf --provider offline` → 3 页、29 段（跳过 22）、用时 83.2 秒、`产物自检：通过（1 轮，0 阻断）`，exit 0。
- 未完成事项 / 下一步：
  - `build\e2e_models\`（4.68 GB）是发版 E2E 时的模型副本，本次未删；确认后可回收。
  - `build\pyinstaller-app\`、`build\dist-app\` 是发版中间产物（已在 gitignore 内），可随手删除。
  - 将来若要恢复发版，可从 `003e9ec` 取回 `installer/`、`.github/`、`fetch.py`、`fetch_dialog.py` 与 `models_manifest.json`。
- 交接注意点：现在只有源码运行方式；模型默认在 `D:\PDFTranslator\models\llm-gguf`（不入 git）；命令行必须带 `--provider offline` 才走本地模型，默认 `mock` 仅用于调试。

### 2026-11-07：引入 src/ 布局，整理目录结构

- 本次目标：把仓库整理成 `src/ + config/ + samples/{inputs,papers,reference} + scripts/ + out/{results,reports,archive}`，并统一路径推导与入口命令。
- 迁移映射（全部用 `git mv`，历史可追）：
  - `pdf_translator/` → `src/pdf_translator/`
  - `ui/app.py` → `src/ui/app.py`（新增 `src/ui/__init__.py`）
  - `glossary.tsv`、`terms_cache.json` → `config/`；`ui_settings.json`（未入库）一并移动
  - `samples/sample_en.pdf`、`samples/sample_narrow_en.pdf` → `samples/inputs/`
  - 根目录英文论文 → `samples/papers/`；中文基准论文 → `samples/reference/`
  - `run_ui.bat`、`build.ps1` → `scripts/`
  - `out/*.pdf` → `out/results/`；`out/*.json`、`out/*.md` → `out/reports/`；其余 → `out/archive/`（文件名未改）
  - 原 `out/_archive/` 的 83 个历史文件（24 个 `.py` + 59 个 `.log`）合并进 `out/archive/`，无重名、未改名
- 新增：
  - `src/pdf_translator/paths.py`：`project_root` / `config_dir` / `samples_dir` / `out_dir` /
    `results_dir` / `reports_dir` / `archive_dir` / `config_file` / `config_target` /
    `config_json_path` / `default_model_dir` / `models_dir` / `report_for`，是全仓库唯一路径来源。
    `offline_config.resolve_model_dir()` 现在只是 `paths.models_dir()` 的包装。
  - `pyproject.toml`：setuptools 后端，`package-dir = {"" = "src"}`，
    `[project.scripts] pdf-translator = pdf_translator.__main__:main`、
    `pdf-translator-ui = ui.app:main`，版本取 `pdf_translator.__version__`。
- 路径改造：
  - `ui/app.py`：删除 `APP_DIR` 与 `ROOT_DIR` 拼路径；设置读写走 `config_file` / `config_target`，
    产物走 `results_dir()`，smoke / selftest 报告走 `out/reports/`；脚本方式运行时自动把 `src` 加进 `sys.path`。
  - `pdf_translator/__main__.py`：默认输出 `out/results/<stem>_zh.pdf`。
  - `pdf_translator/engine.py`：自检报告改由 `paths.report_for()` 决定（`out/` 内统一落 `out/reports/`）。
  - `tools/*`：把 `src` 加入 `sys.path`；`verify` / `check_layout` 用法不变；`gate_check`、`term_audit`
    默认报告改 `out/reports/`；`reflow_prototype` 预览改 `out/archive/reflow_preview`。
  - `scripts/run_ui.bat`：`PYTHONPATH=src` + `python -m ui.app`，双击可用、无需先装包。
  - `scripts/build.ps1`、`PDFTranslator.spec`：入口与素材路径改到 `src/`、`config/`。
- 关键决策与原因：
  - `paths.py` 同时承担配置解析，避免 `offline_config` 与 UI 各拼一套；模型目录仍保持
    「环境变量 > config.json > D 盘默认」优先级，默认档不变。
  - 配置文件保留「根目录旧位置能读」：`config_file()` 在新位置不存在时回退旧路径，写入永远落 `config/`。
  - 自检报告用 `report_for()` 按产物位置分流，`-o` 指到仓库外时不会污染 `out/reports/`。
  - `selfcheck.py` 仍复用 `tools.check_layout/check_overlap`，因此在模块导入时把仓库根加入 `sys.path`。
- 清理：`build/` 下的 `e2e_models`（4.68 GB）、`e2e_probe_models`（170 MB）、`pyinstaller-app`（49 MB）
  等发版残留全部删除，保留空目录（`.gitignore` 已排除）。
- 验证结果（本机，真实输出）：
  - `python -m pip install -e .` → `Successfully installed pdf-translator-0.1.0`。
  - `pdf-translator --version` → `PDFTranslator 0.1.0`；`pdf-translator-ui --version` 同。
  - `pdf-translator --check-models` → `D:\PDFTranslator\models\llm-gguf`，两分片 4,683,073,632 字节，`llama.cpp 可加载`，exit 0。
  - `python -m pdf_translator --help` → 正常输出用法。
  - `python src\ui\app.py --smoke --report out\reports\smoke.json` → `界面自检通过`，`ok: true`、`available_tiers ["llm"]`，exit 0。
  - `pdf-translator samples\inputs\sample_en.pdf -o out\results\restructure_check.pdf --provider offline` → 3 页、29 段、71.1 秒、`产物自检：通过（1 轮，0 阻断）`，exit 0。
  - `python tools\verify.py samples\inputs\sample_en.pdf out\results\restructure_check.pdf` → `全部校验通过`，exit 0。
  - `python tools\check_layout.py …` → 横溢 0 行、纵溢 0 行、`排版检查通过`，exit 0。
- 未完成事项 / 下一步：
  - `scripts/build.ps1 -Mode installer` 仍是失效分支（安装器已在上一节删除），需要恢复发版时再补。
- 交接注意点：改代码请放在 `src/`；新增路径一律走 `paths.py`；不要在根目录再放配置文件（旧位置只做只读兼容）；
  入口命令是 `pdf-translator` / `pdf-translator-ui`，未安装时用 `scripts\run_ui.bat` 或 `python src\ui\app.py`。


### 2026-11-07 reflow 改为五阶段通用引擎（结构 → 术语 → 翻译 → 排版 → 自检）

- 本次目标：reflow 不再“复刻某一份中文样张”，改为像人一样先读懂结构、再定术语、按段重写、按样式落版、最后自检；每阶段
  独立可运行、可缓存、可单独重跑。
- 改动内容：
  - 新增 `src/pdf_translator/reflow/styles.py`：中文论文默认样式表（正文 10.5、段首 2 字符、标题 19/15/12.5/11、
    题注与表内 9、参考文献 9、页码 9），默认值不来自任何一篇 PDF；`adaptive_line_ratio()` 按版心宽度与段落行数
    在 1.5~1.9 间取值（基准 1.70）。
  - 新增 `reflow/workspace.py`：`out/reports/<slug>/{structure,glossary,translation,layout,selfcheck}.json`
    与 `out/results/<slug>_zh.pdf`；写盘用 `.tmp` 原子替换；`STAGE_FILES` 定义阶段与产物对应关系。
  - 新增 `reflow/stages.py`：五阶段执行器 `run_stage/run_all`；阶段只读上游 JSON，不共享内存状态。
  - 重写 `reflow/structure.py`：行级段落切分（块内行距中位数 × 1.35 判段界）、栏数识别（只统计正文字号窄块）、
    band + 列号阅读顺序、元素树带 `confidence/confidence_notes`、图注/表题挂为 children。
  - 重写 `reflow/typeset.py`：只按样式表排版，不读原坐标；逐字定位实现两端对齐与禁则；标题后至少留 4 行正文。
  - `reflow/cjk.py`：宋体（simsun.ttc）→ 思源宋体 → 微软雅黑 回退链，回退时 stderr 提示一次。
  - `__main__.py`：新增 `--stage {structure|glossary|translate|layout|check|all}`；`--font` 说明改为回退链。
  - 新增 `tools/structure_report.py`：结构体检（页数/栏数/字号/元素统计/低置信项）。
- 关键决策与原因：
  - 样式与内容分离：排版器只认样式名，换一篇论文不改代码。
  - 术语抽取不做“猜译名”：自动层只处理缩写与专名短语，全小写技术词以 `config/glossary.tsv` 为准，避免幻觉。
  - 图表不靠坐标回填：`translation.json` 带元素类型，排版阶段按“首次引用就近”放置。
- 环境、依赖变更：无新增依赖，仍为 PyMuPDF + 本地 GGUF Qwen2.5-7B。
- 验证结果：
  - 结构：sample_en 3 页 1 栏 28 元素；Ahmed 10 页 [2×9,1] 301 元素；DivPrune 10 页 2 栏 246 元素，均已落 structure.json。
  - 术语：sample 4 条；Ahmed 57 条（保留 28 / 译出 26 / 待确认 3）；DivPrune 49 条（保留 38 / 译出 7 / 待确认 4）。
  - 翻译：sample_en 17 段 0 失败，二次运行“待翻译 0 段，已缓存 16 段”验证增量续跑。
  - 排版 + 自检：`out/results/sample_en_zh.pdf` 2 页，嵌入 SimSun + SimHei，`selfcheck.json` ok=true、
    阻断 0、警告 0、术语未落译 0、数字丢失 0、段落截断 0。
  - 修掉的问题：术语层把首字母大写词当缩写、全小写词表词未扫描、短语互相包含产生碎片；翻译层把正文
    “未进行翻译”误判为提示词泄漏而截断；自检层未把译文灌回元素树导致“7 段未翻译”误报、题注序号被当数字丢失。
- 未完成事项 / 下一步：双栏两篇（Ahmed、DivPrune）的翻译/排版/自检回归与失败点报告；三栏以上降级策略实测。
- 交接注意点：`--layout` 默认仍是 `fill`，reflow 是新链路；`test/` 与 `samples/` 下原始 PDF 只读不改；
  重跑单阶段不会清空已完成的中间产物。


### 2026-11-07 停止翻译（用户要求）

- 本次目标：按用户要求立即停止翻译，不启动新任务、不做优化或重构。
- 停在哪一阶段：阶段③（按段重写）与阶段④（排版）之间。检查时本机无正在运行的 python 进程，无需终止。
- 已完成且可用（**不要删除**）：
  - 阶段① 结构：`out/reports/sample_en/structure.json`、`out/reports/Ahmed_.../structure.json`、
    `out/reports/Alvar_DivPrune_.../structure.json`（三份齐全）。
  - 阶段② 术语：上述三个目录下的 `glossary.json`（三份齐全）。
  - 阶段③ 翻译：`sample_en/translation.json`（17 段，0 失败）、`Ahmed_.../translation.json`（99 段，0 失败）。
  - 阶段④⑤：`out/results/sample_en_zh.pdf` + `sample_en/layout.json` + `sample_en/selfcheck.json`（ok=true，
    阻断 0、警告 0），是第一份可交付的完整产物。
- 半成品：
  - Ahmed：translation.json 已完整，缺 `layout.json` 与 `selfcheck.json`，尚无 `Ahmed_..._zh.pdf`。
  - DivPrune：只有 structure.json + glossary.json，阶段③未开始。
- 从当前状态继续（PowerShell 单行，逐条执行；已完成的段落会显示“已缓存 N 段”并跳过）：
  1. `python -m pdf_translator "samples\papers\Ahmed_DeepCompress-ViT_Rethinking_Model_Compression_to_Enhance_Efficiency_of_Vision_Transformers_CVPR_2025_paper.pdf" --layout reflow --stage layout`
  2. `python -m pdf_translator "samples\papers\Ahmed_DeepCompress-ViT_Rethinking_Model_Compression_to_Enhance_Efficiency_of_Vision_Transformers_CVPR_2025_paper.pdf" --layout reflow --stage check`
  3. `python -m pdf_translator "test\Alvar_DivPrune_Diversity-based_Visual_Token_Pruning_for_Large_Multimodal_Models_CVPR_2025_paper.pdf" --layout reflow --stage translate --provider offline`
  4. `python -m pdf_translator "test\Alvar_DivPrune_Diversity-based_Visual_Token_Pruning_for_Large_Multimodal_Models_CVPR_2025_paper.pdf" --layout reflow --stage layout`
  5. `python -m pdf_translator "test\Alvar_DivPrune_Diversity-based_Visual_Token_Pruning_for_Large_Multimodal_Models_CVPR_2025_paper.pdf" --layout reflow --stage check`
- 当前代码改动**尚未提交 git**（`git status --short`）：
  - 已修改：`docs/PROJECT_LOG.md`、`src/pdf_translator/__main__.py`、`src/pdf_translator/providers/offline.py`、`src/ui/app.py`
  - 未跟踪：`src/pdf_translator/reflow/`、`tools/structure_report.py`、`tools/check_reflow.py`、`tools/reflow_preview.py`、`test/`
  - `test/` 是用户原始样张，按要求不入库、不修改。
- 交接注意点：`out/reports/` 下的中间产物是续跑依据，清理后需从 `--stage structure` 重来；
  `--layout` 默认仍是 `fill`，reflow 需显式指定。


### 2026-11-07 清理冗余文件（暂停状态）

- 本次目标：暂停翻译期间只清理“能由现有命令重新生成”的产物与缓存，不动源码、样张与中间 JSON。
- 删除清单（合计约 66.4 MB）：
  - `out/results/sample_reflow_zh.pdf` 14.03 MB（旧 reflow 实验，可用 `--layout reflow` 重建）
  - `out/results/sample_fill_zh.pdf` 1.64 MB（fill 旧样例，可重建）
  - `out/reports/sample_reflow_zh_structure.json`、`sample_reflow_zh_reflow_check.json`、`sample_fill_zh_selfcheck.json`
  - `out/reports/preview_fill_vs_reflow_p1.png`、`preview_fill_vs_reflow_p2.png` 0.29 MB
  - `out/archive/` 全部 49.98 MB（`_boldtest.pdf`、`_fonttest.pdf`、`reflow_dryrun.pdf`、9 张预览 png、6 个 `_diag*.py` 探针）
  - `build/` 空目录残留、5 处 `__pycache__`（0.45 MB）与全部 `*.pyc`
- 保留：`out/results/sample_en_zh.pdf`；`out/reports/{sample_en,Ahmed_*,Alvar_DivPrune_*}` 下的五阶段 JSON；
  `models/ samples/ test/ src/ tools/ config/ docs/ scripts/` 与 `.gitignore`。
- 存疑保留：`.screenshots/`（空目录，无法确认来源，按“不确定就留”处理）。
- 未找到（非本次删除）：`out/results/paper_int4_fixed_zh.pdf`、`out/before_*`、`out/after_*`。
- 磁盘实测：删除前后 D 盘可用 476,395,884,544 → 476,395,872,256 字节（-12,288）。本机删除操作会进入回收站，
  真实空间需用户手动清空回收站后才释放。
- 健康检查：
  - `pdf-translator --version` → `PDFTranslator 0.1.0`
  - `pdf-translator --check-models` → 4,683,073,632 字节（4.36 GB），`llama.cpp 可加载`
  - `python -m pdf_translator samples\inputs\sample_en.pdf -o out\results\cleanup_check.pdf --provider offline`
    → 3 页不变、29 段、78.1 秒、`产物自检：通过（0 阻断 / 0 提示）`
- 附带修复：fill 自检 `selfcheck.LEAK_RE` 把正文“未进行翻译”误判为提示词泄漏并阻断，改为否定前缀不匹配；
  修复后上述健康检查通过。验证用的 `cleanup_check.pdf` 与报告已删除，避免新增冗余。
- 交接注意点：`out/` 已在 `.gitignore` 中；`test/` 仍不入库；历史译文如需回归，重新运行对应 `--layout` 命令即可生成。


### 2026-11-08 统一 UI/CLI 链路并清理下线后端

- 本次目标：消除「界面走旧单遍管线、命令行走五阶段」的分裂，修复自检误判，清掉 CT2/NLLB/Argos 死代码，补齐最小回归与文档。
- 改动内容：
  - `src/ui/app.py`：`_worker` 改为顺序调用 `run_stage` 五阶段；翻译进度、阶段提示、自检报告统一读工作区产物（`selfcheck.json`）；移除 `ReflowEngine` 依赖；模型档位判断只认 `models/llm-gguf`。
  - `src/pdf_translator/reflow/pipeline.py`：**删除**（旧单遍引擎）。
  - `src/pdf_translator/reflow/__init__.py`：改为导出 `run_stage` / `StageResult` / `Workspace` / `Document` / `Element`。
  - `src/pdf_translator/reflow/stages.py`：新增 `STAGE_LABELS`，供界面显示阶段名。
  - `src/pdf_translator/reflow/check.py`：字体自检不再写死宋体，改为按 CJK 文本实际使用的字体名与嵌入表交叉判断，跟随 宋体 → 思源宋体 → 微软雅黑 回退链。
  - `src/pdf_translator/__main__.py`：`--no-selfcheck` 现在真正跳过阶段⑤（与 `--stage check` 互斥时报参数错误）。
  - `src/pdf_translator/reflow/cjk.py`：字体缺失提示移除已下线的 `--layout fill` 建议。
  - `src/pdf_translator/providers/offline.py`：删除 `NLLBBackend`、`LocalTokenizer`、`ArgosBackend`、CT2 生成路径与只服务非 LLM 路径的分句/回填方法；`QwenBackend` 收敛为提示词与后校验层（不再持有 CT2 状态），GGUF 只替换加载与生成。
  - `src/pdf_translator/providers/offline_config.py`：删除 `ModelSpec`、三档 NLLB 规格、`LIGHT_MODEL`、CT2 目录/tokenizer 探测、`model_backend`、beam/batch 等遗留字段；`HardwareInfo` 只保留实际使用的字段。
  - `config/glossary.tsv`：去掉 NLLB 时代注释。
  - `requirements.txt`：仅保留 PyMuPDF 与 requests；`tkinterdnd2` 改为按 `.[ui]` 可选安装，与 `pyproject.toml` 一致。
  - `README.md`：删除失效截图链接；修正自检报告路径（`selfcheck.json`）、`--no-selfcheck` 语义、界面与命令行共用五阶段的说明；新增回归测试命令。
  - `tests/test_reflow_smoke.py`：新增，用 mock provider 生成临时 PDF 并跑通五阶段，不依赖大模型。
- 关键决策与原因：界面不再维护第二条翻译链路，避免缓存、术语、上下文与自检规则长期分叉；字体自检必须跟随实际字体，否则无宋体机器会被误阻断。
- 环境、依赖变更：`requirements.txt` 不再强制安装 `tkinterdnd2`；本地推理仍只依赖 `llama-cpp-python`（通过 `.[offline]`）。
- 验证：`python -m compileall -q src tools` 通过；`python -m unittest discover -s tests -v` 通过（1 项，约 1.4 秒）；`python -m pdf_translator --help` 正常；`ui.app`、`pdf_translator.reflow` 导入正常。**界面未做人工点按回归**，首次运行请确认五阶段提示与完成弹窗。
- 未完成事项 / 下一步：用真实 GGUF 模型跑一篇完整 PDF，核对五阶段进度与自检报告；补 `tools/check_reflow.py` 与新自检字段的一致性检查。
- 交接注意点：当前目录仍不是 git 仓库（`.git` 不存在）；上传 GitHub 前需 `git init` 并确认 `models/`、`out/`、`config/ui_settings.json`、`config/terms_cache.json` 已被 `.gitignore` 排除。




