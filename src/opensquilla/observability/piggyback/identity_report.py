"""Self-contained Session projection of a graph reconstructed from IDs only."""
# ruff: noqa: E501 -- literal HTML/CSS templates, not execution logic.

from __future__ import annotations

from html import escape

from .identity import kind, references


def html_report(graph):
    def tag(value):
        if value is None:
            return "—"
        safe = escape(value, quote=True)
        return f'<a href="#{safe}" title="{safe}">{escape(value.split(":")[-1][:12])}</a>'

    sessions = []
    nodes = graph["nodes"]
    for session_id, session in graph["sessions"].items():
        sections = [f"<h2>Session <code>{escape(session_id)}</code></h2>"]
        for branch_id, branch in session["branches"].items():
            sections.append(f"<h3>Branch {tag(branch_id)}</h3>")
            sections.append(
                "<p>用户输入接受顺序："
                + " → ".join(tag(turn) for turn in branch["user_turn_ids"])
                + "</p>"
            )
            for run_id in branch["run_ids"]:
                report = graph["scopes"][run_id]
                status = "完整" if report["complete"] else "等待 / 不完整"
                activation = nodes.get(nodes[run_id]["activation_id"], {})
                group = nodes.get(activation.get("input_group_id"), {})
                turns = (
                    ", ".join(tag(turn) for turn in group.get("turn_ids", ()))
                    or "系统触发 / 未解析"
                )
                sections.append(
                    f'<div class="run"><h3>{escape(status)} · Run {tag(run_id)}</h3><p>触发输入：{turns}</p>'
                )
                if report["reasons"]:
                    sections.append(
                        '<p class="warning">' + escape(", ".join(report["reasons"])) + "</p>"
                    )
                positions = [
                    (call, graph["positions"][call])
                    for call in graph["topological_order"]
                    if call in graph["positions"] and graph["positions"][call]["run_id"] == run_id
                ]
                sections.append(
                    '<div class="scroll"><table><thead><tr><th>Session</th><th>User Turn</th><th>Run</th><th>Iteration</th><th>Operation</th><th>Call</th><th>Context</th></tr></thead><tbody>'
                )
                for call, position in positions:
                    row = nodes[call]
                    values = [
                        position["session_id"],
                        position["owner_user_turn_id"],
                        position["run_id"],
                        position["iteration_id"],
                        position["operation_id"],
                        call,
                        row["context_id"],
                    ]
                    sections.append(
                        "<tr>"
                        + "".join("<td>" + tag(value) + "</td>" for value in values)
                        + "</tr>"
                    )
                sections.append("</tbody></table></div>")
                if positions:
                    sections.append(
                        "<details><summary>展开顺序、重试、并行分支和状态事实</summary>"
                    )
                    for call, position in positions:
                        row = nodes[call]
                        relation = graph.get("call_relations", {}).get(call, {})
                        sections.append(
                            "<p>Call "
                            + tag(call)
                            + " · Previous "
                            + tag(row["previous_id"])
                            + " · Retry "
                            + tag(row["retry_id"])
                            + " · Fallback "
                            + tag(row["fallback_id"])
                            + "<br>Lane "
                            + tag(position["lane_id"])
                            + " · Attempt "
                            + tag(position["attempt_id"])
                            + " · 定位状态："
                            + escape(relation.get("location_status", "未单独校验"))
                            + "<br>全部激活输入："
                            + ", ".join(tag(t) for t in position["input_turn_ids"])
                            + "</p>"
                        )
                        for fact in graph.get("facts", {}).values():
                            if fact["subject_id"] == call:
                                sections.append(
                                    "<p>"
                                    + escape(fact["dimension"])
                                    + "："
                                    + escape(fact["outcome"])
                                    + " · 事实引用 "
                                    + tag(fact["id"])
                                    + "</p>"
                                )
                    sections.append("</details>")
                if not positions:
                    sections.append("<p>这个执行范围没有物理 LLM Call。</p>")
                sections.append("</div>")
        sessions.extend(sections)
    declaration_rows = []
    for identity, row in nodes.items():
        links = "<br>".join(escape(field) + ": " + tag(target) for field, target in references(row))
        declaration_rows.append(
            f'<tr id="{escape(identity, quote=True)}"><td>{escape(kind(identity))}</td><td><code>{escape(identity)}</code></td><td>{links}</td></tr>'
        )
    return (
        """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>ID-only Agent trace</title>
<style>body{font:15px/1.65 system-ui,sans-serif;margin:0;background:#f5f7fa;color:#142239}main{max-width:1320px;margin:auto;padding:36px}h1{font-size:30px;margin-bottom:6px}h2{font-size:21px;margin-top:32px}h3{font-size:17px}code{font:12px ui-monospace,monospace;overflow-wrap:anywhere}a{color:#205ac1;text-decoration:none}a:hover{text-decoration:underline}p{color:#536078}.run{padding:18px 22px;margin:14px 0;background:white;border:1px solid #dce3ed;border-radius:12px}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:10px;vertical-align:top;border-bottom:1px solid #e5eaf1}th{background:#edf1f7;white-space:nowrap}.warning{color:#9b5815}tr:target{background:#fff1bd}summary{cursor:pointer;font-weight:600}input{width:min(600px,90%);padding:10px;border:1px solid #c4cdda;border-radius:6px}</style><main>
<h1>Agent trace · 仅通过 ID 拼接关系</h1><p>默认显示 7 个定位字段，复杂关系按需展开。User Turn 是展示锚点，多输入时保留完整输入列表。表格使用确定性的合法拓扑顺序；并行 Lane 之间不宣称唯一时间顺序。</p>
<p>“完整”仅表示这个已闭合执行范围的 ID 声明收齐、引用有效且没有已知缺口。正文与环境不在此报告内；开放 Session 不声明永久完整。</p>"""
        + "".join(sessions)
        + """<details id="inventory"><summary>展开完整声明和兼容引用锚点</summary><p>事实使用统一格式；迁移期保留旧 ID，供来源引用及闭合清单校验。点击 ID 可定位到声明。</p><input id="filter" placeholder="输入 ID 或节点类型过滤"><div class="scroll"><table><thead><tr><th>类型</th><th>唯一 ID</th><th>引用（保持数组顺序）</th></tr></thead><tbody id="declarations">"""
        + "".join(declaration_rows)
        + """</tbody></table></div></details></main><script>document.getElementById('filter').addEventListener('input',event=>{const value=event.target.value.toLowerCase();for(const row of document.getElementById('declarations').rows)row.hidden=!row.textContent.toLowerCase().includes(value)});document.addEventListener('click',event=>{const a=event.target.closest('a[href^="#"]');if(a)document.getElementById('inventory').open=true});</script></html>"""
    )
