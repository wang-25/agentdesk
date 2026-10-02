# -*- coding: utf-8 -*-
"""检索后端对比的可执行性（M5d 的准备）。

这一组用例守两件事，都不需要 API Key、零成本：

  ① **配了 Key 之后的第一次体验必须是可执行的**。
     `DASHSCOPE_API_KEY` 一配上，embedding 后端就从 local（512 维）自动切成
     dashscope（1024 维），而磁盘上的索引还是旧的 —— 这时 `VectorStore.load`
     会拒绝加载。拒绝是对的（总不能拿两套向量空间硬算），但**报错必须告诉人
     下一步敲什么**：正在配环境的那个时刻，最不需要的就是再去翻文档找命令。

  ② **对比脚本本身在没有 Key 时也要能跑通**（`--local-only`），
     并且对比与门禁跑的是**同一份评测实现**（`evaluate(store=...)`），
     而不是在脚本里另抄一遍召回率算法 —— 两处实现必然漂移。
"""

import importlib.util
import json

import pytest

from app.llm import PROJECT_ROOT
from app.rag.embedder import Embedder
from app.rag.loader import Chunk
from app.rag.store import VectorStore

_spec = importlib.util.spec_from_file_location(
    "compare_embedders_under_test",
    PROJECT_ROOT / "scripts" / "compare_embedders.py")
assert _spec is not None and _spec.loader is not None
ce = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ce)


def _tiny_store(tmp_path, name="index"):
    """建一个 3 块的本地索引（零成本、不联网）。"""
    directory = tmp_path / name
    chunks = [
        Chunk(doc_id="disk-full", title="磁盘写满", text="磁盘写满 No space left on device 清理",
              source="disk.md", index=0, chunk_id="c0"),
        Chunk(doc_id="nginx-502", title="Nginx 502", text="nginx 502 Bad Gateway upstream 超时",
              source="nginx.md", index=0, chunk_id="c1"),
        Chunk(doc_id="oom", title="内存不足", text="OOMKilled 容器被杀 退出码 137",
              source="oom.md", index=0, chunk_id="c2"),
    ]
    store = VectorStore(Embedder(backend="local")).build(chunks)
    store.save(directory)
    return directory


# ============================================================
# 一、后端切换：报错必须可执行
# ============================================================
def test_mismatch_error_tells_you_the_exact_rebuild_command(tmp_path):
    """★ 配了 Key 之后最可能撞上的那条报错，必须自带修复命令。"""
    directory = _tiny_store(tmp_path)
    payload = json.loads((directory / "chunks.json").read_text(encoding="utf-8"))
    # 假装这份索引是 dashscope（1024 维）建的，而当前后端是 local
    payload["embedder"] = {"backend": "dashscope", "model": "text-embedding-v3",
                           "dim": 1024}
    (directory / "chunks.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError) as e:
        VectorStore.load(directory, embedder=Embedder(backend="local"))

    msg = str(e.value)
    assert "app.rag.pipeline build" in msg, "必须给出重建索引的**确切命令**"
    assert "DASHSCOPE_API_KEY" in msg, "要点明最常见的触发方式，否则会被当成索引坏了"
    assert "索引里是" in msg and "当前是" in msg, "要能看出是哪两个对不上"


def test_mismatch_error_offers_the_no_key_fallback(tmp_path):
    """反方向的修复路径也要给：暂时不想配 Key，就清空它退回 local。"""
    directory = _tiny_store(tmp_path)
    payload = json.loads((directory / "chunks.json").read_text(encoding="utf-8"))
    payload["embedder"] = {"backend": "dashscope", "model": "x", "dim": 1024}
    (directory / "chunks.json").write_text(json.dumps(payload, ensure_ascii=False),
                                           encoding="utf-8")
    with pytest.raises(RuntimeError) as e:
        VectorStore.load(directory, embedder=Embedder(backend="local"))
    assert "清空" in str(e.value)


def test_matching_backend_loads_fine(tmp_path):
    directory = _tiny_store(tmp_path)
    store = VectorStore.load(directory, embedder=Embedder(backend="local"))
    assert len(store.chunks) == 3 and store.bm25 is not None


# ============================================================
# 二、evaluate 接受注入的 store（对比与门禁同一份实现）
# ============================================================
def test_evaluate_accepts_an_injected_store(tmp_path):
    store = VectorStore(Embedder(backend="local")).build([
        Chunk(doc_id="disk-full", title="磁盘写满", text="磁盘写满 清理 空间",
              source="a.md", index=0, chunk_id="c0"),
    ])
    from app.rag import pipeline

    report = pipeline.evaluate(top_k=3, modes=("bm25",), verbose=False, store=store)
    assert report["total"] >= 1
    assert set(report["modes"]) == {"bm25"}
    assert "latency_ms" in report["modes"]["bm25"]


def test_evaluate_without_store_still_uses_the_cached_one(monkeypatch):
    """不传 store 时行为不变（门禁脚本依赖这条）。

    用一个最小假 store 断言"确实走了 load_store()"，
    而不是拿一个 dict 冒充（那只会测出 `evaluate` 会崩）。
    """
    from app.rag import pipeline

    calls = {"n": 0}

    class FakeEmbedder:
        @staticmethod
        def describe():
            return {"backend": "fake", "model": "f", "dim": 1}

    class FakeStore:
        embedder = FakeEmbedder()

        @staticmethod
        def search(query, top_k=3, mode="vector"):
            return []

    def fake_load_store(*args, **kwargs):
        calls["n"] += 1
        return FakeStore()

    monkeypatch.setattr(pipeline, "load_store", fake_load_store)
    report = pipeline.evaluate(top_k=1, modes=("vector",), verbose=False)
    assert calls["n"] == 1, "没传 store 时必须回落到 load_store()"
    assert report["embedder"]["backend"] == "fake"
    assert report["modes"]["vector"]["recall"] == 0.0


# ============================================================
# 三、对比脚本：没有 Key 也能跑通，有 Key 才建语义索引
# ============================================================
def test_build_refuses_without_a_key_with_an_actionable_message(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(SystemExit) as e:
        ce.build_semantic_index(verbose=False)
    msg = str(e.value)
    assert "DASHSCOPE_API_KEY" in msg and "--local-only" in msg, \
        "拒绝时要说清两条路：配 Key，或者先用 --local-only 验流程"


def test_has_key_reads_the_environment(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert ce.has_key() is False
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    assert ce.has_key() is True


def test_force_local_env_removes_the_key(monkeypatch):
    """评测**本地索引**时必须把 Key 摘掉 —— 否则 auto 模式会选 dashscope，
    载入本地索引直接报维度不匹配（这就是"对比"最容易被自己绊倒的地方）。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    ce._force_local_env()
    assert ce.has_key() is False


def test_store_for_forces_the_requested_backend(tmp_path):
    directory = _tiny_store(tmp_path)
    store = ce._store_for(directory, "local")
    assert store.embedder.backend == "local"


def test_semantic_index_lives_in_its_own_directory():
    """★ 对比**绝不能**覆盖正在用的索引。

    一份索引只能服务一个后端（向量空间不同），所以语义索引必须另建一份；
    否则一次对比就把可用状态弄没了 —— 那是"为了做实验把生产搞坏"的典型。
    """
    assert ce.SEMANTIC_INDEX != ce.LOCAL_INDEX
    assert ce.SEMANTIC_INDEX.parent == ce.LOCAL_INDEX.parent


# ============================================================
# 四、报告：两种后端都要能落到表里
# ============================================================
def _fake_report(name, recall, semantic_recall):
    return {
        "backend_name": name,
        "index_dir": f"data/{name}",
        "embedder": {"backend": "x", "model": f"m-{name}", "dim": 512},
        "modes": {
            mode: {"recall": recall,
                   "by_type": {"lexical": {"recall": 1.0},
                               "semantic": {"recall": semantic_recall},
                               "trap": {"recall": 1.0}},
                   "latency_ms": {"p50": 0.3, "p95": 0.5},
                   "misses": []}
            for mode in ce.MODES
        },
    }


def test_write_report_covers_every_backend_and_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "REPORT_DIR", tmp_path)
    reports = [_fake_report("local", 0.95, 0.83), _fake_report("dashscope", 0.97, 0.97)]
    path = ce.write_report(reports)
    text = path.read_text(encoding="utf-8")
    for name in ("local", "dashscope"):
        for mode in ce.MODES:
            assert name in text and mode in text
    assert "语义型" in text and "P50(ms)" in text
    assert path.suffix == ".md"


def test_print_report_shows_the_delta_between_two_backends(capsys):
    ce.print_report([_fake_report("本地哈希", 0.95, 0.83),
                     _fake_report("真语义", 0.97, 0.97)])
    out = capsys.readouterr().out
    assert "结论" in out
    assert "本地哈希" in out and "真语义" in out
    assert "pp" in out, "要给出百分点差值，而不是只列两行数字"


def test_print_report_with_one_backend_says_what_is_missing(capsys):
    ce.print_report([_fake_report("本地哈希", 0.95, 0.83)])
    out = capsys.readouterr().out
    assert "对比需要两份" in out


def test_print_report_states_the_judgement_rule(capsys):
    """★ 把"什么算赢"写在输出里 —— 否则数据出来以后仍会变成各说各话。"""
    ce.print_report([_fake_report("a", 0.9, 0.8), _fake_report("b", 0.9, 0.8)])
    out = capsys.readouterr().out
    assert "语义型那一列" in out
    assert "没赢" in out and "不引入" in out
