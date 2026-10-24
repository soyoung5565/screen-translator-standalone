"""
配置加载 — 完全独立于 Hermes。

优先级：
  1. 环境变量（SCREEN_TRANS_ prefixed）
  2. 本目录 config.json
  3. 附近目录的 .env（手动放置）
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
ENV_PATH = os.path.join(HERE, ".env")


def _load_dotenv(path):
    """极简 .env 解析：不覆盖已存在的环境变量。"""
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

    dotenv = _load_dotenv(ENV_PATH)

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
