"""
配置加载 — 完全独立于 Hermes。

优先级：
  1. 环境变量（SCREEN_TRANS_ prefixed）
  2. 本目录 config.json
  3. 附近目录的 .env（手动放置）
"""

import json
import os
import sys


def _app_dir():
    """
    配置文件所在目录 —— 必须区分「源码运行」和「PyInstaller 打包运行」。

    ⚠️ 坑：单文件 exe 的 `__file__` 在 `sys._MEIPASS`（每次启动解包到新的临时目录，
    退出即删）。若把 .env / config.json 挂在 `__file__` 旁边：
      * 用户放在 exe 同目录的 .env 永远读不到；
      * `--set-key` 写进去的 key 下次启动就随临时目录消失。
    所以冻结状态下取 `sys.executable` 所在目录（exe 实际所在处）。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


HERE = _app_dir()
CONFIG_PATH = os.path.join(HERE, "config.json")
ENV_PATH = os.path.join(HERE, ".env")


def _env_candidates():
    """
    .env 的查找顺序（先到先得）：
      1. exe/脚本所在目录          —— 用户自己放的，最高优先
      2. 上一级目录                —— dist/屏幕翻译器.exe 能复用项目根的 .env
    存在的坑：单文件 exe 常被单独拷出去用，这时两份都找不到，只能靠环境变量。
    """
    dirs = [HERE, os.path.dirname(HERE)]
    out = []
    for d in dirs:
        p = os.path.join(d, ".env")
        if os.path.exists(p):
            out.append(p)
    return out


def _load_dotenv(path):
    """极简 .env 解析：不覆盖已存在的环境变量。支持行内注释与引号。"""
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            # 去掉行内注释：只认「空格 + #」，避免误伤 URL 里的 #
            for sep in (" #", "\t#"):
                idx = v.find(sep)
                if idx > 0:
                    v = v[:idx].strip()
                    break
            if k:
                env[k] = v
    return env


def load_config():
    """返回 dict，键：api_key / base_url / model / ocr_lang / interval / source_lang / target_lang"""
    file_cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        except Exception as e:
            print("[配置] config.json 解析失败：%s" % e)

    dotenv = {}
    # 合并所有候选 .env（近的优先，不覆盖已有键）
    for _p in _env_candidates():
        for k, v in _load_dotenv(_p).items():
            dotenv.setdefault(k, v)

    def pick(name, env_name, default=None):
        v = os.environ.get(env_name)
        if v:
            return v
        v = dotenv.get(env_name)
        if v:
            return v
        return file_cfg.get(name, default)

    cfg = {
        "api_key": pick("api_key", "SCREEN_TRANS_API_KEY", ""),
        "base_url": pick(
            "base_url",
            "SCREEN_TRANS_BASE_URL",
            "https://api.minimax.cn/anthropic",
        ),
        "model": pick("model", "SCREEN_TRANS_MODEL", "MiniMax-M3"),
        "api_style": pick("api_style", "SCREEN_TRANS_STYLE", "anthropic"),
        "interval": int(pick("interval", "SCREEN_TRANS_INTERVAL", 3)),
        "source_lang": pick("source_lang", "SCREEN_TRANS_SOURCE", "auto"),
        "target_lang": pick("target_lang", "SCREEN_TRANS_TARGET", "zh"),
    }
    return cfg


def save_config(cfg):
    """写回 config.json（不含明文 key，除非显式给了）。"""
    out = {k: v for k, v in cfg.items() if k != "api_key"}
    out["api_key"] = cfg.get("api_key", "")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
