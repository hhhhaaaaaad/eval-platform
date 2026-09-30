"""确定性的本地假 embedding 服务 —— 用来在**没有真实 API key** 的情况下打通向量路径。

## 它解决什么问题

评测平台的检索维度此前只在 **BM25 词法路径**上验证过：embedding key 是占位值，
`MemoryEmbeddingClient.embed()` 一律返回空数组，`MemoryRetriever` 于是走
`hasEmbedding=false` 的降级分支。结果是**向量检索、向量同步屏障、hnsw 模式
这三个东西从未真正执行过**——而它们都有各自的失败模式（Qdrant 写入失败、
`vector_sync_pending_count` 不归零、建集合维度不符）。

把 `MEMORY_EMBEDDING_BASE_URL` 指向本服务，就能让整条向量链路真跑起来。

## 它衡量什么、不衡量什么

**衡量**：向量链路的管道是否通 —— 向量能否写入 Qdrant、异步同步能否收敛到 0、
检索能否按向量召回、hnsw 模式与 exact 模式是否都能跑完。

**不衡量**：语义检索质量。这里的向量是**字符 bigram 的哈希投影**
（`hashing trick`），相似文本得到相似向量，但它建模的是**字面重叠**而非语义。
所以用本服务跑出的 Recall **不能**当作「语义检索效果」的结论——
引用这些数字时必须同时说明用的是假 embedding。

## 为什么用 bigram 哈希而不是随机向量

随机向量下任何两条文本的余弦相似度都接近 0，检索退化成随机返回——
那样连「向量召回是否真的在工作」都判断不了（随机返回也可能碰巧命中）。
bigram 哈希让「字面重合度高的文本」相似度更高，于是：

- 用与语料**完全相同**的文本去查，必然召回该条（相似度 1.0）——可用来断言链路通；
- 用改写过的文本去查，召回率介于随机与理想之间——符合「字面重叠」的预期。

两者都能与「链路断了」区分开。

## 用法

    cd E:\\java\\eval-platform\\backend
    ./.venv/Scripts/python.exe scripts/fake_embedding_server.py --port 8799

然后让 AgentWrite 指向它（eval profile）：

    MEMORY_EMBEDDING_BASE_URL=http://127.0.0.1:8799
    MEMORY_EMBEDDING_API_KEY=fake-key-not-validated

本服务**不校验 api-key**（它没有鉴权语义，只是个本地替身）。但这不代表线上也该这样——
真实客户端仍必须带 key，这里只是不需要。

## 请求/响应契约

与 OpenAI `POST /v1/embeddings` 兼容（AgentWrite 的 `MemoryEmbeddingClient`
按该形状解析）：请求 `{"input": ["文本"], "model": "..."}`，
响应 `{"data": [{"embedding": [...], "index": 0}], ...}`。
**只读 `data[0].embedding`**，因此其余字段给出最简即可。
"""

from __future__ import annotations

import argparse
import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

#: 与 AgentWrite 的 `memory.qdrant.vector-size` 必须一致：不一致时 Qdrant
#: 会以维度不符拒绝写入，而那是个足够醒目的错误，不需要在这里再加校验。
DEFAULT_DIM = 1024

#: 单个字符（unigram）与相邻两字符（bigram）各占一半权重。
#: 只用 bigram 时单字查询几乎全零（中文尤其明显）；只用 unigram 时又丢掉了词序信息。
_UNIGRAM_WEIGHT = 0.5
_BIGRAM_WEIGHT = 1.0


def embed_text(text: str, dim: int) -> list[float]:
    """把文本投影成 ``dim`` 维单位向量。

    纯标准库实现（`hash()` 换成 `zlib.crc32` 以保证**跨进程稳定**——
    Python 的 `hash()` 对 str 带随机盐，重启后会得到完全不同的向量，
    那会让「同一段文本两次跑出的检索结果不一致」，正是本项目最不能出的问题）。
    """
    import zlib

    vector = [0.0] * dim
    stripped = (text or "").strip()
    if not stripped:
        return vector

    def bump(token: str, weight: float) -> None:
        # crc32 而非内置 hash：见 docstring（内置 hash 有进程级随机盐）
        index = zlib.crc32(token.encode("utf-8")) % dim
        vector[index] += weight

    for char in stripped:
        bump(char, _UNIGRAM_WEIGHT)
    for i in range(len(stripped) - 1):
        bump(stripped[i : i + 2], _BIGRAM_WEIGHT)

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


class _Handler(BaseHTTPRequestHandler):
    """最小 OpenAI 兼容 embedding 端点。"""

    dim: ClassVar[int] = DEFAULT_DIM
    #: 调用统计（类属性），由子类/实例共享——用于回答「这个替身真的被调到了吗」。
    #: 显式标注 ClassVar 而不是让 ruff 把可变字面量当成实例字段的默认值。
    stats: ClassVar[dict[str, int]] = {"requests": 0, "texts": 0}

    def _read_body(self) -> bytes:
        """读请求体，**同时支持 Content-Length 与 chunked**。

        只读 Content-Length 是不够的：Spring 的 `RestClient` 在默认的
        `JdkClientHttpRequestFactory` 下会对这个请求使用 chunked 编码，
        此时没有 Content-Length，只按长度读会拿到空 body，
        然后以「input 字段缺失」400 回绝——一个**看起来像调用方出错、
        实际是服务端替身少实现了一种传输编码**的假故障。
        （写这个替身时正好踩了：AgentWrite 侧报 `Embedding API 调用失败`，
        差点被误判成「AgentWrite 的 embedding 客户端有问题」。）
        """
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks: list[bytes] = []
            while True:
                size_line = self.rfile.readline().strip()
                if not size_line:
                    break
                try:
                    size = int(size_line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()  # 结尾 CRLF
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # 每块尾部的 CRLF
            return b"".join(chunks)

        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # do_POST / do_GET 的驼峰命名是 BaseHTTPRequestHandler 的接口约定，不是笔误。
    def do_POST(self) -> None:
        raw = self._read_body()
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            # 把原始 body 带进错误信息：替身自己解析失败时，最需要看到的就是原样收到了什么
            self._send(400, {"error": {"message": f"请求体不是合法 JSON: {raw[:200]!r}"}})
            return

        inputs = body.get("input")
        if isinstance(inputs, str):
            inputs = [inputs]
        if not isinstance(inputs, list):
            self._send(400, {"error": {"message": "input 必须是字符串或字符串数组"}})
            return

        texts = [item if isinstance(item, str) else str(item) for item in inputs]
        type(self).stats["requests"] += 1
        type(self).stats["texts"] += len(texts)

        data = [
            {"object": "embedding", "index": index, "embedding": embed_text(text, self.dim)}
            for index, text in enumerate(texts)
        ]
        self._send(
            200,
            {
                "object": "list",
                "data": data,
                "model": body.get("model") or "fake-embedding",
                # usage 字段真实 API 会返回，这里是替身，给个诚实的 0。
                # 它不被 AgentWrite 读取（只读 data[0].embedding），留着只为形状完整。
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            },
        )

    def do_GET(self) -> None:
        """健康检查与调用统计：用来确认「这个替身真的被调到了」。"""
        self._send(200, {"status": "ok", "dim": self.dim, **type(self).stats})

    def _send(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    # 形参名沿用基类的 `format`/`*args` 签名（因此这里用 fmt 只是本地命名，不改基类契约）；
    # 覆盖它是为了静音逐请求日志——本服务只关心计数，否则日志会淹没控制台。
    def log_message(self, fmt: str, *args: object) -> None:
        """默认实现会为每个请求打一行日志；本服务只关心统计，故静音。"""


def main() -> int:
    parser = argparse.ArgumentParser(description="确定性本地假 embedding 服务（测试替身）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM)
    args = parser.parse_args()

    _Handler.dim = args.dim
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"假 embedding 服务已启动: http://{args.host}:{args.port}/v1/embeddings  (dim={args.dim})")
    print("它建模的是字面重叠而非语义：用它跑出的 Recall 不能当作语义检索效果。")
    print(f"调用统计: http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n收到中断，累计处理 {_Handler.stats['requests']} 次请求 / "
              f"{_Handler.stats['texts']} 条文本")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
