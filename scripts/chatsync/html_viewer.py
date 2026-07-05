#!/usr/bin/env python3
"""
html_viewer.py — build a single-file, offline, searchable HTML timeline that
merges chat history from all agents.

The generated `viewer.html` inlines a compact JSON payload (one entry per
session, with trimmed message text) and a small vanilla-JS app: no network,
no build step, works by double-click. Light theme.
"""

from __future__ import annotations

import html
import json
import os
from typing import Dict, List, Optional

from .canonical import (
    BLOCK_REASONING,
    BLOCK_TOOL_RESULT,
    BLOCK_TOOL_USE,
    CanonicalSession,
)
from . import utils

# Per-message text cap in the HTML payload, to keep the file a sane size.
# Full-fidelity text always lives in the per-session .json / .md files; the
# HTML is a fast browse/search index, so we trim aggressively here.
_MSG_CAP = 2000
_MAX_MSGS = 120

AGENT_COLORS = {
    "claude": "#d97757",
    "workbuddy": "#4f7cff",
    "codex": "#10a37f",
    "cursor": "#6b7280",
    "antigravity": "#a855f7",
}


def _session_payload(sess: CanonicalSession) -> Dict[str, object]:
    msgs: List[Dict[str, str]] = []
    for m in sess.messages:
        text = m.text or ""
        extra = []
        for b in m.blocks:
            if b.kind == BLOCK_TOOL_USE:
                extra.append("🔧 " + b.text)
            elif b.kind == BLOCK_TOOL_RESULT:
                extra.append("📤 " + b.text)
            elif b.kind == BLOCK_REASONING:
                extra.append("💭 " + b.text)
        joined = "\n".join([text] + extra).strip()
        if not joined:
            continue
        msgs.append({"role": m.role, "t": utils.truncate(joined, _MSG_CAP)})
        if len(msgs) >= _MAX_MSGS:
            break
    return {
        "agent": sess.source_agent,
        "title": sess.title or sess.derive_title(),
        "project": sess.project or "",
        "created": sess.created_at or "",
        "updated": sess.updated_at or "",
        "id": sess.session_id,
        "msgs": msgs,
    }


def build_html(
    sessions: List[CanonicalSession],
    out_dir: str,
    dry_run: bool = False,
    max_sessions: int = 0,
) -> Optional[str]:
    payload = [_session_payload(s) for s in sessions if s.non_empty_messages()]
    # newest first
    payload.sort(key=lambda e: e.get("created", ""), reverse=True)
    truncated = 0
    if max_sessions and len(payload) > max_sessions:
        truncated = len(payload) - max_sessions
        payload = payload[:max_sessions]

    data_json = json.dumps(payload, ensure_ascii=False)
    colors_json = json.dumps(AGENT_COLORS, ensure_ascii=False)
    total_msgs = sum(len(e["msgs"]) for e in payload)

    doc = _TEMPLATE.replace("__DATA__", data_json)
    doc = doc.replace("__COLORS__", colors_json)
    doc = doc.replace("__SESSION_COUNT__", str(len(payload)))
    doc = doc.replace("__MSG_COUNT__", str(total_msgs))
    note = f" · 已限制显示最新 {len(payload)} 个（另有 {truncated} 个仅在归档文件中）" if truncated else ""
    doc = doc.replace("__TRUNC_NOTE__", note)

    if dry_run:
        return None
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "viewer.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return os.path.abspath(path)


# NOTE: braces in the <style>/<script> are doubled where literal, because we
# use str.replace (not .format) so raw JS/CSS braces are fine as-is.
_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chat Archive · 跨产品对话时间线</title>
<style>
  :root {
    --bg: #f7f8fa; --panel: #ffffff; --border: #e5e7eb; --text: #1f2328;
    --muted: #6b7280; --accent: #4f7cff; --hover: #f0f3f9;
    --user-bg: #eef4ff; --assistant-bg: #f7f8fa; --tool-bg: #f3f4f6;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
      "PingFang SC", "Microsoft YaHei", sans-serif;
    background: var(--bg); color: var(--text); height: 100vh; overflow: hidden;
  }
  .app { display: flex; height: 100vh; }
  /* Sidebar */
  .sidebar {
    width: 380px; min-width: 380px; background: var(--panel);
    border-right: 1px solid var(--border); display: flex; flex-direction: column;
  }
  .head { padding: 16px 16px 10px; border-bottom: 1px solid var(--border); }
  .head h1 { font-size: 16px; margin: 0 0 4px; }
  .stats { font-size: 12px; color: var(--muted); }
  .controls { padding: 10px 16px; border-bottom: 1px solid var(--border); }
  .search {
    width: 100%; padding: 8px 10px; border: 1px solid var(--border);
    border-radius: 8px; font-size: 13px; outline: none;
  }
  .search:focus { border-color: var(--accent); }
  .filters { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .chip {
    font-size: 11px; padding: 3px 9px; border-radius: 999px; cursor: pointer;
    border: 1px solid var(--border); background: #fff; user-select: none;
    display: flex; align-items: center; gap: 5px;
  }
  .chip.off { opacity: .38; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .list { flex: 1; overflow-y: auto; }
  .item {
    padding: 11px 16px; border-bottom: 1px solid var(--border); cursor: pointer;
  }
  .item:hover { background: var(--hover); }
  .item.active { background: #e8efff; }
  .item .t { font-size: 13px; font-weight: 600; line-height: 1.35;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden; }
  .item .meta { font-size: 11px; color: var(--muted); margin-top: 5px;
    display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
  .badge { font-size: 10px; padding: 1px 7px; border-radius: 4px; color: #fff; }
  /* Detail */
  .detail { flex: 1; overflow-y: auto; padding: 0; background: var(--bg); }
  .detail-head {
    position: sticky; top: 0; background: var(--panel);
    border-bottom: 1px solid var(--border); padding: 16px 28px; z-index: 5;
  }
  .detail-head h2 { margin: 0 0 6px; font-size: 18px; }
  .detail-head .meta { font-size: 12px; color: var(--muted); }
  .detail-head code { background: #eef0f3; padding: 1px 5px; border-radius: 4px; }
  .msgs { padding: 20px 28px; max-width: 900px; }
  .msg { margin-bottom: 16px; }
  .msg .role { font-size: 11px; font-weight: 700; color: var(--muted);
    text-transform: uppercase; letter-spacing: .04em; margin-bottom: 5px; }
  .bubble {
    padding: 11px 14px; border-radius: 10px; font-size: 13.5px; line-height: 1.6;
    white-space: pre-wrap; word-break: break-word; border: 1px solid var(--border);
  }
  .msg.user .bubble { background: var(--user-bg); }
  .msg.assistant .bubble { background: var(--assistant-bg); }
  .msg.tool .bubble, .msg.system .bubble { background: var(--tool-bg);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
    color: #475569; }
  .empty { display: flex; align-items: center; justify-content: center;
    height: 100%; color: var(--muted); font-size: 14px; }
  mark { background: #ffe58a; padding: 0 1px; border-radius: 2px; }
  .list::-webkit-scrollbar, .detail::-webkit-scrollbar { width: 9px; }
  .list::-webkit-scrollbar-thumb, .detail::-webkit-scrollbar-thumb {
    background: #d1d5db; border-radius: 5px; }
</style>
</head>
<body>
<div class="app">
  <div class="sidebar">
    <div class="head">
      <h1>💬 跨产品对话时间线</h1>
      <div class="stats">__SESSION_COUNT__ 个会话 · __MSG_COUNT__ 条消息 · 只读归档__TRUNC_NOTE__</div>
    </div>
    <div class="controls">
      <input id="search" class="search" placeholder="🔍 全文搜索标题与内容…" autocomplete="off">
      <div id="filters" class="filters"></div>
    </div>
    <div id="list" class="list"></div>
  </div>
  <div id="detail" class="detail">
    <div class="empty">← 从左侧选择一个会话查看</div>
  </div>
</div>
<script>
const DATA = __DATA__;
const COLORS = __COLORS__;
let activeAgents = new Set(Object.keys(COLORS));
let query = "";
let activeIdx = -1;

const esc = (s) => (s||"").replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function color(a){ return COLORS[a] || "#888"; }
function fmtDate(s){ return s ? s.slice(0,16).replace('T',' ') : ""; }
function projLabel(p){ if(!p) return ""; const x=p.replace(/\/+$/,'').split('/'); return x[x.length-1]||p; }

function highlight(text, q){
  if(!q) return esc(text);
  const t = esc(text); const lq = q.toLowerCase();
  let out=""; let i=0; const lt=t.toLowerCase();
  while(true){ const idx=lt.indexOf(lq,i); if(idx<0){ out+=t.slice(i); break; }
    out+=t.slice(i,idx)+"<mark>"+t.slice(idx,idx+q.length)+"</mark>"; i=idx+q.length; }
  return out;
}

function matches(s, q){
  if(!activeAgents.has(s.agent)) return false;
  if(!q) return true;
  const lq=q.toLowerCase();
  if((s.title||"").toLowerCase().includes(lq)) return true;
  if((s.project||"").toLowerCase().includes(lq)) return true;
  return s.msgs.some(m => (m.t||"").toLowerCase().includes(lq));
}

function renderFilters(){
  const counts={}; DATA.forEach(s=>counts[s.agent]=(counts[s.agent]||0)+1);
  const box=document.getElementById('filters'); box.innerHTML="";
  Object.keys(COLORS).forEach(a=>{
    if(!counts[a]) return;
    const c=document.createElement('div');
    c.className='chip'+(activeAgents.has(a)?'':' off');
    c.innerHTML='<span class="dot" style="background:'+color(a)+'"></span>'+a+' ('+counts[a]+')';
    c.onclick=()=>{ if(activeAgents.has(a)) activeAgents.delete(a); else activeAgents.add(a); renderFilters(); renderList(); };
    box.appendChild(c);
  });
}

function renderList(){
  const list=document.getElementById('list'); list.innerHTML="";
  const filtered = DATA.map((s,i)=>({s,i})).filter(o=>matches(o.s,query));
  if(!filtered.length){ list.innerHTML='<div style="padding:20px;color:#888;font-size:13px">无匹配结果</div>'; return; }
  filtered.forEach(({s,i})=>{
    const el=document.createElement('div');
    el.className='item'+(i===activeIdx?' active':'');
    el.innerHTML='<div class="t">'+highlight(s.title||'(无标题)',query)+'</div>'+
      '<div class="meta"><span class="badge" style="background:'+color(s.agent)+'">'+s.agent+'</span>'+
      (s.project?'<span>📁 '+esc(projLabel(s.project))+'</span>':'')+
      '<span>'+fmtDate(s.created)+'</span><span>· '+s.msgs.length+' 条</span></div>';
    el.onclick=()=>{ activeIdx=i; renderList(); renderDetail(s); };
    list.appendChild(el);
  });
}

function renderDetail(s){
  const d=document.getElementById('detail');
  let h='<div class="detail-head"><h2>'+esc(s.title||'(无标题)')+'</h2>'+
    '<div class="meta"><span class="badge" style="background:'+color(s.agent)+'">'+s.agent+'</span> '+
    (s.project?'&nbsp;<code>'+esc(s.project)+'</code>':'')+'&nbsp; · &nbsp;'+fmtDate(s.created)+
    '&nbsp; · &nbsp;'+s.msgs.length+' 条消息</div></div><div class="msgs">';
  s.msgs.forEach(m=>{
    h+='<div class="msg '+m.role+'"><div class="role">'+m.role+'</div>'+
       '<div class="bubble">'+highlight(m.t,query)+'</div></div>';
  });
  h+='</div>';
  d.innerHTML=h; d.scrollTop=0;
}

document.getElementById('search').addEventListener('input',(e)=>{
  query=e.target.value.trim(); renderList();
});

renderFilters(); renderList();
</script>
</body>
</html>
"""
