import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from bondhype.deepseek import DeepSeekClient
from bondhype.llm import Completion, Usage


@pytest.fixture
def api():
    """A local stand-in for the DeepSeek endpoint; set `api.reply` to the JSON body to serve."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps(server.reply).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.reply = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.url = f"http://127.0.0.1:{server.server_port}"
    yield server
    server.shutdown()


def complete(api):
    client = DeepSeekClient("key", base_url=api.url)
    return client.complete(model="deepseek-flash", system="s", user="u")


def test_the_reply_text_comes_back_with_the_token_usage_the_api_reported(api):
    api.reply = {
        "choices": [{"message": {"content": "hello"}}],
        "usage": {
            "prompt_tokens": 500,
            "prompt_cache_hit_tokens": 350,
            "prompt_cache_miss_tokens": 150,
            "completion_tokens": 80,
        },
    }

    assert complete(api) == Completion(
        text="hello", usage=Usage(cache_hit_tokens=350, cache_miss_tokens=150, output_tokens=80)
    )


@pytest.mark.parametrize(
    ("usage", "output_tokens"),
    [({}, 0), ({"usage": None}, 0), ({"usage": {"completion_tokens": 7}}, 7)],
)
def test_missing_usage_counts_as_zero_instead_of_failing_the_call(api, usage, output_tokens):
    api.reply = {"choices": [{"message": {"content": "hello"}}]} | usage

    assert complete(api) == Completion(text="hello", usage=Usage(output_tokens=output_tokens))
