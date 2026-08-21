"""跨源同事件归并:extract 出 cluster_key,runtime 按 cluster 去重(幂等)。"""
import io

# 1) extract.py:cluster_key
p = "services/intelligence-ingest/src/intelligence_ingest/extract.py"
src = io.open(p, encoding="utf-8").read()
if "def event_cluster_key" not in src:
    anchor = "def extract_event(doc: Document) -> dict[str, Any] | None:"
    helper = '''def event_cluster_key(title: str, event_type: str, assets: list) -> str:
    """跨源同事件归并键:规范化标题 + 事件类型 + 主资产。

    只归并「规范化后完全相同标题」的转载/通稿——不同标题不强行合并,
    宁可重复观察也不误并(与方向推断同一保守原则)。
    """
    import re

    normalized = re.sub(r"[^\\w\\u4e00-\\u9fff]+", "", (title or "").lower())
    primary = sorted(str(a) for a in assets)[0] if assets else ""
    return "clu-" + sha256(
        f"{event_type}|{primary}|{normalized}".encode()).hexdigest()[:16]


def extract_event(doc: Document) -> dict[str, Any] | None:'''
    assert anchor in src
    src = src.replace(anchor, helper, 1)
    # 事件字典加 cluster_key
    src = src.replace(
        '''    return {
        "event_id": f"evt-{digest}",
        "document_id": doc.document_id,''',
        '''    return {
        "event_id": f"evt-{digest}",
        "cluster_key": event_cluster_key(doc.title, event_type, doc.assets),
        "document_id": doc.document_id,''')
    io.open(p, "w", encoding="utf-8", newline="\n").write(src)
    print("extract: cluster_key added")

# 2) runtime intelligence.py:按 cluster 去重
p = "packages/dsh-runtime/src/dsh_runtime/intelligence.py"
src = io.open(p, encoding="utf-8").read()
if "cluster|" not in src:
    old = '''        dedupe_key = hashlib.sha256(
            f"{spec.source_id}|{symbol}|{title}|{source_url}|{published_at}".encode("utf-8")
        ).hexdigest()'''
    new = '''        # 跨源归并:同 cluster_key(转载/通稿)只形成一条决策;
        # 无 cluster 的外部源退回逐源去重。
        cluster_key = str(raw.get("cluster_key") or "")
        if cluster_key:
            dedupe_key = hashlib.sha256(
                f"cluster|{cluster_key}|{self.market}".encode("utf-8")
            ).hexdigest()
        else:
            dedupe_key = hashlib.sha256(
                f"{spec.source_id}|{symbol}|{title}|{source_url}|{published_at}".encode("utf-8")
            ).hexdigest()'''
    assert old in src
    src = src.replace(old, new)
    io.open(p, "w", encoding="utf-8", newline="\n").write(src)
    print("runtime: cluster dedupe wired")
