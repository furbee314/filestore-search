"""Mock OpenAI-compatible LLM server for testing filestore-search without a
real LLM service.

It implements POST /v1/chat/completions and returns deterministic canned
responses based on the system prompt:
  - the "rewrite" system prompt -> a JSON rewrite with filters derived from
    simple keyword matching (enough to exercise the pipeline, including an
    any_of OR group for 'X or Y' requests)
  - the "answer" system prompt  -> a short natural-language answer quoting
    the top files from the user message

Run:  python3 mock_llm.py --port 8901
Then: FILESTORE_SEARCH_LLM_BASE=http://127.0.0.1:8901/v1 python3 -m cli llm-test
"""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def rewrite_reply(user_msg):
    m = user_msg.lower()
    out = {"query": user_msg.strip(), "any_of": None, "category": None,
           "platform": None, "version": None, "since": None}
    # category
    for kw, cat in (("rpm", "linux-rpm"), ("deb", "linux-deb"), ("msi", "windows-msi"),
                    ("iso", "iso"), ("install media", "iso"),
                    ("firmware", "firmware"), ("bios", "firmware"), ("ipmi", "firmware"),
                    ("driver", "driver"), ("executable", "windows-exe")):
        if kw in m:
            out["category"] = cat
            break
    # platform
    for kw, plat in (("ubuntu", "ubuntu"), ("debian", "debian"), ("rhel", "rhel"),
                     ("red hat", "rhel"), ("windows", "windows"),
                     ("win 10", "windows"), ("win 11", "windows")):
        if kw in m:
            out["platform"] = plat
            break
    # version: last dotted number
    v = re.findall(r"\b(\d+(?:\.\d+)+[a-z0-9]*)\b", m)
    if v:
        out["version"] = v[-1]
    # since: an explicit ISO date the user typed, verbatim. (In practice the
    # real model is told to leave 'since' null for anything relative; the
    # server resolves time windows from the system clock and discards any
    # date the model invents. The mock echoes user-typed dates so the
    # pipeline still exercises a well-behaved LLM.)
    sd = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", m)
    if sd:
        out["since"] = sd[0]
    # 'X or Y' / 'X or Y or Z': move the alternatives into any_of and out of
    # the AND query (a result must match one of them, not all of them).
    # The alternatives are the single word immediately before "or" and the
    # single word immediately after it.
    m_or = re.search(r"\s+(?:or|/)\s+", m)
    if m_or:
        words = lambda s: [w for w in re.findall(r"[a-z0-9.\-]+", s)
                           if w not in ("for", "my", "the", "a", "an",
                                        "with", "find", "please", "need",
                                        "want", "me", "or")]
        left, right = words(m[:m_or.start()]), words(m[m_or.end():])
        alts = []
        if left:
            alts.append(left[-1])
        if right:
            alts.append(right[0])
        out["any_of"] = alts or None
    # tighten query: drop filler AND recency words (recency is handled by
    # the store's ordering / since filter, not by full-text matching), and
    # drop the any_of alternatives (they live in their own OR group)
    q = re.sub(r"\b(for|my|the|a|an|please|find|me|or|need|want|get|install|"
               r"latest|newest|recent|recently|new)\b", " ", m)
    if out["any_of"]:
        for alt in out["any_of"]:
            q = re.sub(r"\b" + re.escape(alt.lower()) + r"\b", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    out["query"] = q or user_msg.strip()
    return json.dumps(out)


def answer_reply(user_msg):
    # pull the file list out of the user message
    i = user_msg.find("Matching files")
    j = user_msg.find("}", user_msg.rfind("{")) if i != -1 else -1
    files = []
    if i != -1:
        try:
            files = json.loads(user_msg[i + len("Matching files (JSON):\n"):].strip())
        except Exception:
            files = []
    q = user_msg.split("User question:", 1)[-1].split("\n", 1)[0].strip() \
        if "User question:" in user_msg else "?"
    if not files:
        return (f"I could not find a file matching '{q}'. "
                "Try a vendor, product model, or version number.")
    top = files[0]
    others = ""
    if len(files) > 1:
        others = " Also available: " + ", ".join(f["name"] for f in files[1:4]) + "."
    return (f"For '{q}' the best match is {top['name']} "
            f"(v{top.get('version') or 'unknown'}, {top.get('category')}). "
            f"Download it from: {top['path']}.{others}")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if not self.path.rstrip("/").endswith("chat/completions"):
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        sysp = next((x["content"] for x in body["messages"]
                     if x["role"] == "system"), "")
        userp = next((x["content"] for x in body["messages"]
                      if x["role"] == "user"), "")
        if "convert natural language" in sysp:
            content = rewrite_reply(userp)
        else:
            content = answer_reply(userp)
        resp = {"choices": [{"message": {"role": "assistant", "content": content}}],
                "model": body.get("model", "mock")}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8901)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), H)
    print(f"mock LLM listening on http://127.0.0.1:{args.port}/v1")
    srv.serve_forever()


if __name__ == "__main__":
    main()
