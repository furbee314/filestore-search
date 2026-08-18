"""Client for a locally-hosted LLM service (OpenAI-compatible chat endpoint).

The LLM is used for two things:
  1. rewrite_query()  - turn natural language into a tightened FTS5 query
                        plus optional structured filters (category/platform).
  2. answer()         - generate a natural-language answer over the top search
                        results, so a user can ask "which firmware do I need
                        for my Dell R740 with 4410 CPU?" and get a concise
                        answer with file links.

Everything degrades gracefully: if the LLM is unreachable or disabled, the
caller falls back to the deterministic FTS search.

CPU sizing: the default target is Ollama serving qwen2.5:3b-instruct on a
plain CPU box (no GPU). The context window (llm_max_ctx, default 4096) is
forwarded to Ollama as options.num_ctx so the KV cache stays small; lower it
(e.g. 2048) on machines with less RAM.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from config import load_config

SYSTEM_REWRITE = """You convert natural language requests into a structured
search over a software / firmware / driver file store.

Return ONLY a JSON object with these keys (no prose, no markdown fences):
{
  "query": "the keyword query to run full-text search; include product, vendor,
            version and keywords; omit fill words like 'the'/'please'/'find'",
  "category": one of [linux-rpm, linux-deb, linux-source, windows-msi,
            windows-exe, windows-driver, windows-other, firmware, driver,
            iso, generic, null],
  "platform": one of [rhel, sles, opensuse, ubuntu, debian, linux, windows,
            firmware, unknown, null],
  "version": "a specific version if the user named one, else null",
  "since": "ISO date (YYYY-MM-DD) for 'files from <date> onward' requests,
            e.g. 'since last week', 'updated in March'; null when the user
            has no time window"
}

Rules:
- If the user clearly wants a specific OS platform, set platform.
- Set category when the user mentions rpm/deb/windows/firmware/driver/etc.
- Put concrete identifiers (product model numbers, versions, part numbers)
  verbatim into "query".
- Never invent product names that are not in the request.
- Recency words ("latest", "newest", "recent", "added/updated since <date>")
  are handled by the store's sort/ordering: keep "query" to the actual
  file/product keywords only and do NOT put words like "latest" or "newest"
  into it. If the user names an explicit time window, set "since".
- The output must be one single valid JSON object: every key and every
  string value in double quotes; null (lowercase, unquoted) for missing.
"""

SYSTEM_ANSWER = """You are a helpful assistant helping a user find software,
firmware, or drivers in an internal file store. You are given the user's
question and a JSON list of matching files (each with name, path, category,
platform, version, vendor, size, description).

Rules:
- Answer concisely and in plain language.
- Name the most relevant file(s) by their exact "name" and give their "path"
  so the user can download them.
- If multiple files are plausible (e.g. different architectures or versions),
  say so and note the difference.
- If no file matches the request, say that clearly and suggest what to check.
- Do not invent files that are not in the provided list.
- Do not use markdown headings; keep it short.
"""


# values the rewrite prompt asks the model to pick from; anything outside
# the whitelist (small models occasionally invent values like "linux-driver")
# is discarded so it can't zero out the result set.
_VALID_CATEGORIES = frozenset(
    {"linux-rpm", "linux-deb", "linux-source", "windows-msi", "windows-exe",
     "windows-driver", "windows-other", "firmware", "driver", "iso",
     "generic"})
_VALID_PLATFORMS = frozenset(
    {"rhel", "sles", "opensuse", "ubuntu", "debian", "linux", "windows",
     "firmware", "unknown"})


class LLMClient:
    def __init__(self, cfg=None, base_url=None, model=None, timeout=None,
                 api_key=None, enabled=None, max_ctx=None):
        cfg = cfg or load_config()
        self.base_url = (base_url or cfg["llm_base"]).rstrip("/")
        self.model = model or cfg["llm_model"]
        self.timeout = timeout or cfg["llm_timeout"]
        self.api_key = api_key or cfg.get("llm_api_key")
        self.enabled = (not cfg["llm_disable"]) if enabled is None else enabled
        self.max_ctx = int(max_ctx or cfg.get("llm_max_ctx", 4096))
        # Ollama detection by its default port: the OpenAI-compatible
        # endpoint accepts options.num_ctx (KV-cache / context sizing),
        # other backends (vLLM, text-generation-webui) don't take that key,
        # so only send it when we actually talk to Ollama.
        p = urllib.parse.urlsplit(self.base_url)
        self._is_ollama = (p.port == 11434)
        self._last_ok = None
        self._last_ok_time = 0

    # -- low level ---------------------------------------------------------
    def _post(self, payload, timeout=None):
        url = self.base_url + "/chat/completions"
        if self._is_ollama:
            # keep the context window explicit so CPU RAM usage is bounded
            payload = dict(payload)
            payload["options"] = {"num_ctx": self.max_ctx}
        data = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                body = r.read().decode("utf-8", "replace")
            obj = json.loads(body)
            return obj["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"LLM HTTP {e.code}: {e.read()[:300]!r}")
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError,
                OSError) as e:
            raise RuntimeError(f"LLM unreachable: {e}")

    def _chat(self, system, user, temperature=0.2, max_tokens=512,
              timeout=None):
        if not self.enabled:
            raise RuntimeError("LLM disabled")
        return self._post({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }, timeout=timeout)

    # -- high level --------------------------------------------------------
    def rewrite_query(self, question, timeout=None):
        """Return a dict: {query, category, platform, version, since} or None."""
        content = self._chat(SYSTEM_REWRITE, question, temperature=0.0,
                             max_tokens=200, timeout=timeout)
        obj = _extract_json(content)
        if not isinstance(obj, dict):
            return None
        out = {
            "query": str(obj.get("query") or question).strip(),
            "category": obj.get("category") or None,
            "platform": obj.get("platform") or None,
            "version": obj.get("version") or None,
            "since": obj.get("since") or None,
        }
        # guard against hallucinated filter values (weak models do this)
        if out["category"] and out["category"] not in _VALID_CATEGORIES:
            out["category"] = None
        if out["platform"] and out["platform"] not in _VALID_PLATFORMS:
            out["platform"] = None
        return out if out["query"] else None

    def answer(self, question, results, timeout=None):
        """Return a short natural-language answer referencing the results."""
        # keep the prompt short (smaller prompt = faster CPU inference and
        # less RAM); 8 top files is plenty for a concise answer
        slim = [{k: r.get(k) for k in
                 ("name", "path", "category", "platform", "arch", "version",
                  "vendor", "size", "description")}
                for r in results[:8]]
        user = (f"User question: {question}\n\n"
                f"Matching files (JSON):\n{json.dumps(slim, indent=2)}")
        return self._chat(SYSTEM_ANSWER, user, temperature=0.2,
                          max_tokens=512, timeout=timeout)

    def ping(self):
        try:
            self._chat("Reply with the single word: ok", "ping",
                       max_tokens=8, timeout=10)
            self._last_ok = True
            return True
        except Exception:
            self._last_ok = False
            return False


def _extract_json(text):
    """Pull the first JSON object out of an LLM reply (handles fences/prose).

    Small CPU models (3B) frequently emit JSON with unquoted keys or values
    (e.g. ``"category": firmware`` or ``{ query: ... }``). After a strict
    parse fails we run a conservative repair pass that only quotes bare
    word tokens next to a key or value position, so valid JSON is never
    mangled and malformed output from weak models still gets through.
    """
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = t[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    import re
    cand = re.sub(r",\s*([}\]])", r"\1", candidate)      # trailing commas
    cand = cand.replace("'", '"')                         # single quotes
    # quote bare keys:  { query: ...  /  , category:
    cand = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*:)",
                  r'\1"\2"\3', cand)
    # quote bare values (single words; leave null/true/false alone)
    def _qval(m):
        word = m.group(2)
        if word in ("null", "true", "false"):
            return m.group(0)
        return m.group(1) + '"' + word + '"' + m.group(3)
    cand = re.sub(r"(:\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*[,}\]])", _qval, cand)
    try:
        return json.loads(cand)
    except json.JSONDecodeError:
        return None


# convenience
def make_client(cfg=None):
    return LLMClient(cfg=cfg)
