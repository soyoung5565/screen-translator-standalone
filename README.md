# 屏幕翻译器 · 独立版

Windows 屏幕实时翻译工具。**完全独立运行，不依赖 Hermes、不依赖任何 Agent 框架、不依赖 OCR 引擎。**

直接双击 `启动.bat` 即可。

---

## 它是什么

拖出一个屏幕区域 → 把截图交给多模态大模型 → 模型返回**译文 + 位置坐标** → 译文**贴在原文原位**（像素级覆盖）。

像手机浏览器自带翻译一样，但跑在 Windows 桌面上。

两种模式：

| 模式 | 说明 |
|---|---|
| **像素级（原位贴字）** ⭐ 默认 | 模型识别每行文字并给出坐标，译文逐条贴在原位，原文半透可见便于校对 |
| 整图翻译 | 模型只返回译文文本，整段显示在区域上 |

---

## 特点

**无 OCR** — 文字识别和定位全部由多模态大模型完成，不装任何 OCR 引擎、不下载模型。

**无 Hermes 依赖** — 只需一个 API Key。原始版本唯一依赖是「从 `~/.hermes/config.yaml` 读 key」，现已改为读自己的 `.env` / 环境变量。

**防自反馈幻觉** — 译文层用 `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` 标记为「不参与屏幕捕获」，截图里永远只有原始界面，模型不会把上一轮的译文当成原文再翻一遍。

---

## 安装

```bash
# 需要 uv（已有）或 pip
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
```

---

## 配置 API Key

三选一：

```bash
# 方式一：命令行写入（推荐）
python app.py --set-key sk-cp-xxxx

# 方式二：环境变量
set SCREEN_TRANS_API_KEY=sk-cp-xxxx

# 方式三：手动编辑本目录 .env
SCREEN_TRANS_API_KEY=sk-cp-xxxx
```

其他可选配置（同名环境变量，或写进 `config.json`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SCREEN_TRANS_BASE_URL` | `https://api.minimax.cn/anthropic` | API 端点 |
| `SCREEN_TRANS_MODEL` | `MiniMax-M3` | 模型名 |
| `SCREEN_TRANS_STYLE` | `anthropic` | 协议风格（`anthropic` / `openai`） |
| `SCREEN_TRANS_SOURCE` | `auto` | 源语言 |
| `SCREEN_TRANS_TARGET` | `zh` | 目标语言 |

> **视觉输入目前只在 Anthropic 兼容端点实测通过**（`/anthropic/v1/messages` + `X-Api-Key`）。
> MiniMax 的 OpenAI 兼容端点虽然能返回 200，但图片走的是 `image_url`，需要自己验证是否真的看得到图。

---

## 使用

| 操作 | 快捷键 |
|---|---|
| 开始 / 停止翻译 | `Ctrl+Alt+S` |
| 隐藏 / 显示区域框 | `Ctrl+Alt+H` |
| 退出 | `Ctrl+Alt+Q` |

> 全局热键（`RegisterHotKey`），面板没焦点时也有效 —— 打游戏/看视频时照样能控制。
> 加 `Ctrl+Alt` 前缀是为了不和游戏自身的 `Ctrl+S` 冲突。
> 若某个热键被别的程序占用，启动时会打印出来，此时用面板内按钮或 `Ctrl+S/H/Q`（需面板有焦点）。

- 拖动青色框内部 = 移动区域
- 拖动角上圆点 = 缩放区域

---

## 自测

```bash
# 命令行翻译链路
python app.py --cli "Hello world"

# 打包后自检（资源 / 配置 / 解析器 / 视觉链路 / 墨迹校正）
python app.py --selftest
# exe 版：屏幕翻译器.exe --selftest
#   注意 exe 是 console=False，没有 stdout —— 结果写到 exe 同目录 selftest.log

# 墨迹校正对抗测试（不依赖模型，确定性）
python test_ink_refine.py

# 解析器容错回归（用实测抓到的坏 JSON，不构造样例）
python test_parse_repair.py

# 渲染保真度（真实 Qt 覆盖层 + 确定性坏坐标回归）
python test_render_fidelity.py

# 贴字尺寸/偏移
python test_overlay_metrics.py

# 全局热键
python test_hotkeys.py
```

> **打包验收必做**：源码测试全绿 ≠ exe 里是同一份代码。PyInstaller 归档是
> 压缩的，外面 grep 字符串不可信；时间戳只证明"打了包"，不证明"打进的是新代码"。
> 唯一可信判据是让 exe 自己跑 `--selftest` 并把结果落盘。

---

## 技术要点

**坐标用 0-1000 归一化** — 让模型输出相对坐标而不是像素数，避免它猜不准绝对位置。截图缩放不影响定位精度。

**墨迹校正（关键）** — 模型给的 `w/h/x/y` 系统性不准：h 常只有真实的 40~60%，w 偏窄，导致遮罩盖不住英文、字号忽大忽小。所以拿到坐标后再用**截图实测的墨迹包围盒**反推真实框：按行统计墨迹 → 找连续行带 → 取带内左右极值。窗口尺度用同图中位数高度，绝不用模型给的 h（h 本身就是最大误差源）。

**变化检测** — 每 0.4s 抓一次屏，MD5 比对，内容没变就不请求 API。既更快又不浪费额度。

**缓存 key 用图片内容哈希** — 不能用 base64 前 N 字符（JPEG 文件头对所有图片都一样），否则不同截图会命中同一份缓存，表现为「改了内容却不重译」。

**解析器容错** — 模型有时把 `h` 误写成第二个 `w`（`{"w":167,"w":32}`），JSON 默认后者覆盖前者 → 宽高颠倒 → 整行被丢弃。用 `object_pairs_hook` 救回。

**块数偏少就重试** — 同一张 5 行的图，模型有时只返回 1 块。旧逻辑「拿到任意块就 break」等于不重试；现在按块数判断（< 3 块继续重试），取块数最多的结果。

**DPI 感知必须在进程启动时设置** — 150% 缩放的屏幕上，不开 per-monitor 感知的话，Qt 坐标、win32 坐标、mss 物理坐标三者不一致，截图和贴字全部错位。

**配置路径区分源码/打包** — 单文件 exe 的 `__file__` 在临时解包目录，`.env` 必须挂到 `sys.executable` 所在目录，否则 `--set-key` 写的 key 下次启动就没了。

---

## 已知限制

- **模型漏行** — 复杂排版下可能少识别一两行。已加「块数偏少就重试、取块数最多的结果」，但仍是模型侧问题
- **模型坐标有误差** — 已用截图实测的墨迹包围盒校正（`refine_blocks_with_ink`），遮罩/字号/位置基本对齐到像素；纯图标、无文字的块无法校正
- **译文比原文长** → 向右溢出原框（横向文本的固有难点）
- **视频/游戏等动态内容** 变化检测会持续触发翻译，吃额度
- **API 成本** — 每次内容变化 = 一次视觉模型调用（块数少时最多 3 次）

---

## 依赖

```
PyQt5   GUI
mss     屏幕捕获（比 pyautogui 快 10 倍）
Pillow  图像处理
requests HTTP
```

总大小约 60 MB（含 venv）。
