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
| 开始 / 停止翻译 | `Ctrl+S` |
| 隐藏 / 显示区域框 | `Ctrl+H` |
| 退出 | `Ctrl+Q` |

- 拖动青色框内部 = 移动区域
- 拖动角上圆点 = 缩放区域

---

## 自测

```bash
# 命令行翻译链路
python app.py --cli "Hello world"

# 翻译管线（合成截图 → LLM → 坐标换算 → 贴字）
python test_e2e.py

# GUI 端到端 + 两轮迭代幻觉检查
python test_gui_e2e.py

# overlay 绘制位置验证
python verify_overlay.py
```

---

## 技术要点

**坐标用 0-1000 归一化** — 让模型输出相对坐标而不是像素数，避免它猜不准绝对位置。截图缩放不影响定位精度。

**变化检测** — 每 0.4s 抓一次屏，MD5 比对，内容没变就不请求 API。既更快又不浪费额度。

**缓存 key 用图片内容哈希** — 不能用 base64 前 N 字符（JPEG 文件头对所有图片都一样），否则不同截图会命中同一份缓存，表现为「改了内容却不重译」。

**解析器容错** — 模型有时把 `h` 误写成第二个 `w`（`{"w":167,"w":32}`），JSON 默认后者覆盖前者 → 宽高颠倒 → 整行被丢弃。用 `object_pairs_hook` 救回。

**DPI 感知必须在进程启动时设置** — 150% 缩放的屏幕上，不开 per-monitor 感知的话，Qt 坐标、win32 坐标、mss 物理坐标三者不一致，截图和贴字全部错位。

---

## 已知限制

- **模型坐标有误差** — 通常在 ±10px 内，长句/竖排/斜体可能偏得更多
- **译文比原文长** → 向右溢出原框（横向文本的固有难点）
- **模型偶发漏行** — 复杂排版下可能少识别一两行，隔一轮会自动补上
- **视频/游戏等动态内容** 变化检测会持续触发翻译，吃额度
- **API 成本** — 每次内容变化 = 一次视觉模型调用

---

## 依赖

```
PyQt5   GUI
mss     屏幕捕获（比 pyautogui 快 10 倍）
Pillow  图像处理
requests HTTP
```

总大小约 60 MB（含 venv）。
