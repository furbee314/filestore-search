"""Central configuration for the offline file store search.

Everything can be overridden with environment variables so the same
installation works under nginx+gunicorn, systemd, or plain `python -m`.

Key environment variables:
  FILESTORE_SEARCH_CONFIG   path to a YAML/JSON config file (optional)
  FILESTORE_SEARCH_ROOT     directory that contains the config + db + data
  FILESTORE_SEARCH_DATA     directory of files to index (default: $ROOT/data)
  FILESTORE_SEARCH_DB       path to SQLite db (default: $ROOT/search.db)
  FILESTORE_SEARCH_URL      public base URL the web UI uses to build download links
  FILESTORE_SEARCH_LLM_BASE base URL of the local LLM (OpenAI-compatible /v1/chat/completions).
                            Default: Ollama on this machine (CPU), http://127.0.0.1:11434/v1
  FILESTORE_SEARCH_LLM_MODEL model name to request (default: qwen2.5:3b-instruct,
                            a small instruct model sized for CPU inference)
  FILESTORE_SEARCH_LLM_TIMEOUT seconds, default 60 (CPU inference is slow;
                            raise further if your CPU is weaker)
  FILESTORE_SEARCH_LLM_MAX_CTX model context window in tokens (default 4096).
                            Smaller (e.g. 2048) = lower RAM usage on CPU.
  FILESTORE_SEARCH_LLM_DISABLE=1  fully disable the LLM (pure FTS mode)
"""
import os
import re
import json

ROOT = os.environ.get("FILESTORE_SEARCH_ROOT") or os.path.dirname(os.path.abspath(__file__))

def _env_bool(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Config(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)

    def __setattr__(self, k, v):
        self[k] = v


def load_config():
    root = ROOT
    data_dir = os.environ.get("FILESTORE_SEARCH_DATA") or os.path.join(root, "data")
    db_path = os.environ.get("FILESTORE_SEARCH_DB") or os.path.join(root, "search.db")
    config_path = os.environ.get("FILESTORE_SEARCH_CONFIG") or os.path.join(root, "search.json")

    file_cfg = {}
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                file_cfg = json.load(f)
        except Exception as e:
            raise SystemExit(f"could not parse {config_path}: {e}")

    def pick(key, env_name, default):
        if os.environ.get(env_name) is not None:
            return os.environ[env_name]
        return file_cfg.get(key, default)

    cfg = Config(
        root=root,
        data_dir=os.path.abspath(os.path.expanduser(pick("data_dir", "FILESTORE_SEARCH_DATA", data_dir))),
        db_path=os.path.abspath(os.path.expanduser(pick("db_path", "FILESTORE_SEARCH_DB", db_path))),
        public_url=pick("public_url", "FILESTORE_SEARCH_URL", ""),
        # Default to Ollama serving a small instruct model on CPU. A 3B
        # 4-bit model fits in ~2GB RAM and is fast enough on any modern
        # CPU for the two jobs this app uses it for (query rewrite + short
        # answers). See README "Local LLM on CPU" for sizing options.
        llm_base=pick("llm_base", "FILESTORE_SEARCH_LLM_BASE", "http://127.0.0.1:11434/v1"),
        llm_model=pick("llm_model", "FILESTORE_SEARCH_LLM_MODEL", "qwen2.5:3b-instruct"),
        # API key is optional: most local servers (Ollama, vLLM, llama.cpp,
        # LM Studio) don't need one; text-generation-webui does.
        llm_api_key=pick("llm_api_key", "FILESTORE_SEARCH_LLM_API_KEY", ""),
        llm_timeout=float(pick("llm_timeout", "FILESTORE_SEARCH_LLM_TIMEOUT", 60)),
        llm_max_ctx=int(pick("llm_max_ctx", "FILESTORE_SEARCH_LLM_MAX_CTX", 4096)),
        llm_disable=_env_bool("FILESTORE_SEARCH_LLM_DISABLE", bool(file_cfg.get("llm_disable", False))),
        max_results=int(pick("max_results", "FILESTORE_SEARCH_MAX_RESULTS", 25)),
        # files to skip (dotfiles, temp files)
        ignored_names=tuple(file_cfg.get("ignored_names", [".*", "~$", ".tmp"])),
        # suffixes to skip (case-insensitive): checksum sidecars live next to
        # the files they describe and would otherwise pollute the index
        ignored_suffixes=tuple(file_cfg.get(
            "ignored_suffixes", [".sha1", ".sha128", ".sha256", ".sha512", ".md5"])),
    )
    return cfg
