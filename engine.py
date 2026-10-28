"""
翻译引擎 — 多后端可切换，纯标准库 + requests，不依赖 Hermes。

后端类型（按 api_style 区分）：
  anthropic : Anthropic 兼容端点（MiniMax CN 站实测视觉可用，走 type=image）
  openai    : OpenAI 兼容端点（走 image_url）

两种输出模式：
  lines  : 纯文本，每行一条译文
  boxes  : 结构化，每条带相对坐标（0-1000 归一化），用于像素级原位贴字
"""

import base64
import hashlib
import json
import re
import threading
import time

try:
    import requests
except ImportError:
    requests = None


class TranslationError(Exception):
    pass


# ═══════════════════════════════════════════════════════════════
#  提示词
# ═══════════════════════════════════════════════════════════════

LANG_NAMES = {
    "auto": "（自动判断语种的）",
    "en": "英文",
    "zh": "中文",
    "ja": "日文",
    "ko": "韩文",
    "fr": "法文",
    "de": "德文",
    "es": "西班牙文",
    "ru": "俄文",
}


def lang_name(code):
    return LANG_NAMES.get(code, code)


def build_lines_prompt(source, target):
    """整图翻译 —— 只要译文，不要坐标。"""
    return (
        "你是屏幕翻译引擎。请识别图片中所有%s文字，逐行翻译成%s。\n"
        "严格按下面的格式输出，每行一条，不要编号，不要解释，不要合并行：\n"
        "译文1\n译文2\n译文3\n"
        "如果图片里没有可翻译的文字，只输出一行：<空>\n"
        % (lang_name(source), lang_name(target))
    )


def build_boxes_prompt(source, target):
    """
    结构化输出 —— 译文 + 位置，用于像素级原位贴字。

    坐标用 0-1000 归一化（相对图片宽高），避免模型输出像素数不准确。

    ⚠️ 两个致命坑（都实测踩过）：

    1. **规则 4 的「只识别 X 原文」必须填源语言，不能填目标语言。**
       早先 %s 顺序错位传成了 target，默认 target=zh 时提示词变成
       「只识别中文原文」—— 英文界面里当然没有中文原文，模型就干净地
       返回 [] 或只挑出零星几块。表现是「同一张图每次返回 0~5 块」，
       一直被误当成模型能力上限。修好后同一张英文界面稳定返回全部行。

    2. **必须让模型忽略图里已有的译文**，否则第二轮会把上一轮贴上去的
       译文当成原文再翻一遍，产生幻觉行（实测出现过）。

    source="auto" 时不能写「只识别（自动判断语种的）原文」这种病句，
    改为明确说明「识别所有可翻译的原语言文字」。
    """
    tname = lang_name(target)
    if source == "auto":
        src_rule = "识别图中所有**可翻译的原语言**文字（不要只挑某一种语言）"
    else:
        src_rule = "只需识别 %s 原文" % lang_name(source)
    return (
        "你是屏幕翻译引擎。识别图片中所有%s文字，翻译成%s，"
        "并给出每条文字在图中的位置。\n"
        "严格输出 JSON 数组，不要任何额外文字、不要 markdown 代码块。\n"
        "格式（每个字段都必须出现，且 w 是宽度、h 是高度，不要写重复的键）：\n"
        '[{"t":"原文","x":10,"y":20,"w":30,"h":8,"zh":"译文"}]\n'
        "字段说明：\n"
        "  t  = 原文（照抄图片里的文字）\n"
        "  zh = 译文\n"
        "  x  = 该行文字**左边**距离图片左边缘的距离，0-1000 归一化\n"
        "  y  = 该行文字**上边**距离图片上边缘的距离，0-1000 归一化\n"
        "  w  = 该行文字的**宽度**，0-1000 归一化\n"
        "  h  = 该行文字的**高度**，0-1000 归一化（通常比 w 小很多）\n"
        "要求：\n"
        "  1. 一行文字一个对象，不要合并相邻行\n"
        "  2. 按从上到下、从左到右的顺序排列\n"
        "  3. 必须把图里**每一行**可翻译的文字都列出来，不要漏\n"
        "  4. %s。如果图里已经出现%s文字（可能是之前翻译过的），"
        "那是译文不是原文，跳过它们，不要重复翻译\n"
        "  5. 坐标尽量准确，我会用它在屏幕上原位贴字\n"
        "  6. 最多输出 40 条\n"
        "只有图片里确实没有任何可翻译文字时，才输出：[]\n"
        % (lang_name(source), tname, src_rule, tname)
    )


# ═══════════════════════════════════════════════════════════════
#  解析
# ═══════════════════════════════════════════════════════════════

_NUM_QUOTE_FIX = re.compile(r'"(x|y|w|h)"\s*:\s*"?(-?\d+(?:\.\d+)?)"(?=\s*[,}])')
_QUOTE_BEFORE_KEY = re.compile(r'"\s*,\s*"')
_STRAY_QUOTE_AFTER_NUM = re.compile(r'("(?:x|y|w|h)"\s*:\s*-?\d+(?:\.\d+)?)"(\s*[,\}])')


def _repair_json(text):
    """
    修补模型产出的小型 JSON 语法错误。

    ⚠️ 这是「漏行」的真正根因（实测确认，不是模型能力问题）：
       模型把 "x":39,"y":52 写成 "x":39","y":52 —— 数字后面多一个引号。
       整段因此不是合法 JSON，标准 json.loads 直接失败；
       退回的 `\\{[^{}]*\\}` 逐对象兜底又很脆（正则遇到嵌套/转义就漏），
       最终表现就是「同一张图每次返回 0~5 块」「只识别到 1/5 行」。
       而原始响应里其实 5 行全都在。

    处理三类实测见过的破坏：
      1. `"x":39"`        → 数字尾随多余引号
      2. `",` + `"y"`     → 值结束引号后又接逗号引号（同上，另一种断法）
      3. 数值被写成 `"39"` → 字符串数字（下游 float() 能转，不必改）
    """
    if not text:
        return text
    # 数字后面紧跟的孤立引号（后面必须是 , 或 } 才算）
    text = _STRAY_QUOTE_AFTER_NUM.sub(r"\1\2", text)
    # "x":39" 这种：引号在数字后、逗号前
    text = _NUM_QUOTE_FIX.sub(r'"\1":\2"', text)
    text = _NUM_QUOTE_FIX.sub(r'"\1":\2', text)
    # 引号紧贴逗号再接键名  "39","y" → 39,"y"
    text = _QUOTE_BEFORE_KEY.sub('", "', text)
    return text


def parse_json_boxes(raw):
    """
    从模型输出里抽出 JSON 数组，容忍 markdown 包裹、前后废话和轻微语法损坏。

    解析顺序（逐级降级，每级都记录救回了多少条）：
      1. 原样 json.loads
      2. _repair_json 修补后 json.loads
      3. 逐对象 `\\{[^{}]*\\}` 兜底
      4. 逐对象 + 修补
    """
    if not raw:
        return []
    text = raw.strip()

    # 去掉 ```json ... ``` 包裹
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()

    i = text.find("[")
    j = text.rfind("]")
    if i < 0 or j <= i:
        return []
    body = text[i:j + 1]

    arr = None
    for attempt in (body, _repair_json(body)):
        try:
            arr = json.loads(attempt, object_pairs_hook=_no_dup)
            if isinstance(arr, list) and arr:
                break
        except Exception:
            arr = None

    if not isinstance(arr, list) or not arr:
        # 逐对象兜底（原样 + 修补各扫一遍，取条数多的）
        cand = []
        for chunk in (body, _repair_json(body)):
            got = []
            for mm in re.finditer(r"\{[^{}]*\}", chunk):
                for variant in (mm.group(0), _repair_json(mm.group(0))):
                    try:
                        got.append(json.loads(variant, object_pairs_hook=_no_dup))
                        break
                    except Exception:
                        continue
            if len(got) > len(cand):
                cand = got
        arr = cand
    if not isinstance(arr, list):
        return []

    out = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        zh = str(item.get("zh") or item.get("text") or "").strip()
        src = str(item.get("t") or item.get("src") or "").strip()
        if not zh:
            continue
        try:
            x = float(item.get("x", 0))
            y = float(item.get("y", 0))
            w = float(item.get("w", 0))
            h = float(item.get("h", 0))
        except (TypeError, ValueError):
            continue
        out.append({"t": src, "zh": zh, "x": x, "y": y, "w": w, "h": h})
    return out


def _no_dup(pairs):
    """
    object_pairs_hook —— 处理模型写出重复键的情况。

    模型有时把 h 误写成第二个 w（{"w":167,"w":32}）。
    Python 的 json 默认后者覆盖前者 → 宽度和高度被搞反 → 该行被丢弃。
    这里保留第一个 w 作为宽度，把重复出现的第二个 w 当作高度。
    """
    d = {}
    for k, v in pairs:
        if k in d:
            # 重复键：如果是 w 重复而缺 h，就把这次的值当作 h
            if k == "w" and "h" not in d:
                d["h"] = v
            continue
        d[k] = v
    return d


def parse_lines(raw):
    """整图翻译 → 逐行列表。"""
    if not raw:
        return []
    lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
    if len(lines) == 1 and lines[0] in ("<空>", "空", "[]"):
        return []
    return lines


# ═══════════════════════════════════════════════════════════════
#  翻译器
# ═══════════════════════════════════════════════════════════════

class LLMTranslator:
    """调用 LLM 做视觉翻译。"""

    def __init__(self, api_key, base_url, model, api_style="anthropic",
                 timeout=120, max_retries=3):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.api_style = api_style
        self.timeout = timeout
        self.max_retries = max_retries
        self._cache = {}
        self._lock = threading.Lock()

    # ── URL / 头 ──
    def _endpoint(self):
        if self.api_style == "anthropic":
            if self.base_url.endswith("/messages"):
                return self.base_url
            if self.base_url.endswith("/v1"):
                return self.base_url + "/messages"
            return self.base_url + "/v1/messages"
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/v1/chat/completions"

    def _headers(self):
        if self.api_style == "anthropic":
            return {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            }
        return {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.api_key,
        }

    # ── 缓存 ──
    def cache_get(self, key):
        with self._lock:
            return self._cache.get(key)

    def cache_put(self, key, value):
        with self._lock:
            self._cache[key] = value

    def clear_cache(self):
        with self._lock:
            self._cache.clear()

    # ── 对外接口 ──
    def translate_image_lines(self, img_b64, source="auto", target="zh"):
        """整图翻译，返回译文行列表。"""
        prompt = build_lines_prompt(source, target)
        raw = self._call(prompt, img_b64, tag=self._img_tag(img_b64, "lines", source, target))
        return parse_lines(raw)

    def translate_image_boxes(self, img_b64, source="auto", target="zh", retries=2):
        """
        像素级：返回 [{t, zh, x, y, w, h}]，坐标 0-1000 归一化。

        实测模型漏行严重且不稳定：同一张 5 行文字的图，多次调用返回
        1~5 块不等（出现过只返回 1 块）。这是模型侧问题，不是渲染问题。

        ⚠️ 早先的重试有缺陷：`if best: break` —— 只要拿到**任意**块就停，
           返回 1 块时根本不重试。正确做法是按**块数**判断：块数偏少
           （< MIN_BOXES）就继续重试，最后取块数最多的结果。

        代价：块数少时会多打几次 API（每张图每次变化最多 retries+1 次）。
        但漏行会让整行没有译文 —— 用户看到的是「翻译漏了」，比多花
        一点额度严重得多。缓存仍按图片内容哈希，同一帧不会重复计费。
        """
        MIN_BOXES = 3     # 少于这个块数认为可能漏行，值得重试
        prompt = build_boxes_prompt(source, target)
        tag = self._img_tag(img_b64, "boxes", source, target)
        best = []
        for attempt in range(retries + 1):
            raw = self._call(prompt, img_b64, tag=tag)
            boxes = parse_json_boxes(raw)
            if len(boxes) > len(best):
                best = boxes
            if len(best) >= MIN_BOXES:
                break
            # 块数偏少：清缓存强制重新请求（否则下一次直接命中同一份结果）
            with self._lock:
                self._cache.pop(tag, None)
            if attempt < retries:
                time.sleep(1.0)
        return best

    @staticmethod
    def _img_tag(img_b64, mode, source, target):
        """
        图片缓存 key —— 必须用图片**内容**而非 base64 头部。

        坑：base64 前 120 字符是 JPEG 文件头，对所有图片都一样，
        用它做 key 会让不同截图命中同一份缓存（表现为"改了内容却不重译"）。
        这里用完整 b64 的哈希。
        """
        h = hashlib.sha256(img_b64.encode("utf-8")).hexdigest()[:32]
        return "%s:%s:%s:%s" % (mode, source, target, h)

    def translate_text(self, texts, source="auto", target="zh"):
        """纯文本批量翻译（不带图）。"""
        if not texts:
            return []
        joined = "\n".join("[%d] %s" % (i + 1, t) for i, t in enumerate(texts))
        prompt = (
            "请把下面每条%s文本翻译成%s，**保持编号不变**，每条单独一行，"
            "格式严格为 `[编号] 译文`。不要解释。\n\n%s"
            % (lang_name(source), lang_name(target), joined)
        )
        raw = self._call(prompt, None, tag="txt:" + joined[:120])
        return parse_numbered(raw, len(texts))

    # ── 底层调用 ──
    def _call(self, prompt, image_b64, tag=None):
        if not self.api_key:
            raise TranslationError("未配置 API Key")
        if requests is None:
            raise TranslationError("缺少 requests 依赖")

        if tag:
            hit = self.cache_get(tag)
            if hit is not None:
                return hit

        payload = self._build_payload(prompt, image_b64)
        url = self._endpoint()
        last_err = None

        for attempt in range(self.max_retries):
            try:
                resp = requests.post(url, headers=self._headers(),
                                     data=json.dumps(payload), timeout=self.timeout)
                if resp.status_code == 200:
                    out = self._parse(resp.json())
                    if out is not None:
                        if tag:
                            self.cache_put(tag, out)
                        return out
                    last_err = "响应内容为空"
                elif resp.status_code == 429:
                    wait = 10 * (attempt + 1)
                    last_err = "限流(429)，%d 秒后重试" % wait
                    time.sleep(wait)
                    continue
                elif resp.status_code in (401, 403):
                    raise TranslationError("鉴权失败(%d)：检查 API Key" % resp.status_code)
                else:
                    last_err = "HTTP %d: %s" % (resp.status_code, resp.text[:180])
            except requests.exceptions.Timeout:
                last_err = "请求超时"
            except TranslationError:
                raise
            except Exception as e:
                last_err = "%s: %s" % (type(e).__name__, e)

            if attempt < self.max_retries - 1:
                time.sleep(2 * (attempt + 1))

        raise TranslationError(last_err or "未知错误")

    def _build_payload(self, prompt, image_b64):
        if self.api_style == "anthropic":
            content = []
            if image_b64:
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
                })
            content.append({"type": "text", "text": prompt})
            return {"model": self.model, "max_tokens": 8192,
                    "messages": [{"role": "user", "content": content}]}

        content = []
        if image_b64:
            content.append({"type": "text", "text": prompt})
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64," + image_b64}})
        else:
            content.append({"type": "text", "text": prompt})
        return {"model": self.model, "max_tokens": 8192,
                "messages": [{"role": "user", "content": content}]}

    def _parse(self, data):
        try:
            if self.api_style == "anthropic":
                for blk in data.get("content", []):
                    if blk.get("type") == "text":
                        return strip_think(blk.get("text", "")).strip()
                return None
            msg = data["choices"][0]["message"]["content"]
            return strip_think(msg).strip()
        except Exception:
            return None


def strip_think(text):
    """去掉 <think>...</think> 噪音。"""
    if not text:
        return ""
    if "<think>" in text and "</think>" in text:
        i = text.index("<think>")
        j = text.index("</think>") + len("</think>")
        text = text[:i] + text[j:]
    return text


def parse_numbered(raw, count):
    """把 '[3] xxx' 形式的响应解析成 list。"""
    if not raw:
        return [""] * count
    out = {}
    cur_idx = None
    buf = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = None
        if line.startswith("["):
            end = line.find("]")
            if end > 0 and line[1:end].strip().isdigit():
                m = int(line[1:end].strip())
        if m is not None:
            if cur_idx is not None:
                out[cur_idx] = " ".join(buf).strip()
            cur_idx = m
            buf = [line[end + 1:].strip()]
        elif cur_idx is not None:
            buf.append(line)
    if cur_idx is not None:
        out[cur_idx] = " ".join(buf).strip()

    if not out:
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        return lines[:count] + [""] * max(0, count - len(lines))

    return [out.get(i, "") for i in range(1, count + 1)]
