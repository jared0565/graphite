import json
from pathlib import Path

from graphite.export import html as html_export


def _export_graph(output_path: Path, *, node_name: str = "safe") -> None:
    html_export.to_html(
        graph_data={
            "nodes": [{"id": "node-1", "name": node_name, "kind": "file"}],
            "edges": [],
            "metadata": {"node_count": 1, "edge_count": 0},
        },
        clusters={"clusters": [], "count": 0},
        analysis={},
        manifest={"root": "<unsafe & repo>"},
        output_path=output_path,
    )


def test_html_escapes_untrusted_data_and_uses_safe_dom_rendering(tmp_path: Path) -> None:
    payload = "</script><script>globalThis.pwned=true</script>"
    output_path = tmp_path / "graph.html"

    _export_graph(output_path, node_name=payload)

    document = output_path.read_text(encoding="utf-8")
    assert payload not in document
    assert r"\u003c/script\u003e" in document
    assert "<title>Graphite — &lt;unsafe &amp; repo&gt;</title>" in document
    assert ".innerHTML" not in document
    assert ".textContent" in document


def test_html_is_published_atomically(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "graph.html"
    calls: list[tuple[Path, str]] = []

    monkeypatch.setattr(
        html_export,
        "atomic_write_text",
        lambda path, content: calls.append((path, content)),
    )

    _export_graph(output_path)

    assert len(calls) == 1
    target, document = calls[0]
    assert target == output_path
    assert document.startswith("<!DOCTYPE html>")
    assert document.endswith("</html>\n")


def test_html_preserves_template_tokens_inside_graph_data(tmp_path: Path) -> None:
    output_path = tmp_path / "graph.html"
    repository_label = "{{title}} / {{node_count}}"

    _export_graph(output_path, node_name=repository_label)

    document = output_path.read_text(encoding="utf-8")
    assert f'"name": "{repository_label}"' in document


def test_json_for_script_escapes_html_characters_and_round_trips() -> None:
    value = {"label": "repository &<>"}

    encoded = html_export._json_for_script(value)

    assert r"\u0026" in encoded
    assert r"\u003c" in encoded
    assert r"\u003e" in encoded
    assert json.loads(encoded) == value
