"""tabmanage.py — 多标签页会话协调与 tabhost.html 服务。

功能：
- 维护会话文件：data/gui_tab_session.json（owner_pid、owner_base、tabs 列表、创建时间）。
- register_tab(title, url, base_url)：首进程成为 owner 并建会话；后续进程探测 owner 存活则作为 guest 加入。
- _owner_alive(base_url)：GET {base_url}/tabhost，HTTP 200 视为存活。
- guest_session_ok(title, owner_base)：检查会话是否有效且自身标签仍在。
- reset_session()：owner 退出时清除会话，供 guest 接管重建。
- tabs_payload()：返回 {ok, tabs[], position} 供前端渲染标签栏。
- handle_tabs_post(body)：处理标签栏位置持久化（top/bottom）。
- tab_host_html()：读取 ui/tabhost.html（开发环境/冻结 exe 均支持），缺失时返回中文兜底页。

所有函数均为防御式：不抛异常，静默回退，便于多进程并发安全。
"""

import json
import os
import sys
import threading
import time
from typing import Any


# 会话文件路径：data/gui_tab_session.json（相对于 pdfmanage.app_base_dir()）
def _tab_session_path() -> str:
    """返回会话文件绝对路径（懒加载 pdfmanage 避免顶层导入开销）。"""
    try:
        from pdfmanage import app_base_dir

        base = str(app_base_dir())
    except Exception:
        # 极端兜底：用当前工作目录
        base = os.getcwd()
    return os.path.join(base, "data", "gui_tab_session.json")


TAB_SESSION_PATH = _tab_session_path()

# 会话锁（可重入，跨线程/进程安全——文件层面靠原子写保证）
_TAB_LOCK = threading.RLock()


def _read_session() -> dict | None:
    """读取会话 JSON；文件不存在/损坏/非 dict 均返回 None。"""
    try:
        with open(TAB_SESSION_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _write_session(obj: dict) -> None:
    """原子写会话：同目录 tempfile + os.replace，持锁。"""
    directory = os.path.dirname(os.path.abspath(TAB_SESSION_PATH))
    try:
        os.makedirs(directory, exist_ok=True)
    except Exception:
        pass
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tab_session-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, TAB_SESSION_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        # 静默失败：调用方不依赖写入是否成功（会话文件丢失仅影响标签合并，不阻塞主流程）
        pass


def _owner_alive(base_url: str) -> bool:
    """探测 owner 服务是否存活：GET {base_url}/tabhost，超时 1.2s，200 即 True。"""
    try:
        import urllib.request
        import urllib.error

        req = urllib.request.Request(
            base_url.rstrip("/") + "/tabhost",
            method="GET",
            headers={"Connection": "close"},
        )
        with urllib.request.urlopen(req, timeout=1.2) as resp:
            return resp.status == 200
    except Exception:
        return False


def _append_tab(session: dict, title: str, url: str) -> None:
    """去重合并标签：同 title 保留最新 url，否则追加。就地修改 session。"""
    tabs = session.get("tabs", [])
    if not isinstance(tabs, list):
        tabs = []
    found = False
    for t in tabs:
        if isinstance(t, dict) and t.get("title") == title:
            t["url"] = url
            found = True
            break
    if not found:
        tabs.append({"title": title, "url": url})
    session["tabs"] = tabs


def register_tab(title: str, url: str, base_url: str) -> str:
    """注册标签页，返回 "owner" 或 "guest"。

    - 读取会话；若存在且 owner 探测存活 → 去重/更新自身标签，写回，返回 "guest"。
    - 首次探测失败会短暂等待后重试一次（防启动竞态误判 owner 死亡误抢占），
      仍失败才新建会话（owner_pid=当前进程、owner_base=base_url、tabs=[{title,url}]），返回 "owner"。
    """
    with _TAB_LOCK:
        session = _read_session()
        if session:
            owner_base = session.get("owner_base", "")
            alive = _owner_alive(owner_base)
            if not alive:
                # 首次探测失败：短暂等待后重试一次，避免启动竞态误判 owner 死亡
                try:
                    time.sleep(0.4)
                except Exception:
                    pass
                alive = _owner_alive(owner_base)
            if alive:
                # owner 存活：去重/更新自身标签
                _append_tab(session, title, url)
                _write_session(session)
                return "guest"
            # owner 确认已死：接管
            print("检测到原窗口 owner 无响应，接管为窗口 owner")
        # 无会话或 owner 确认已死：成为新 owner
        new_session = {
            "owner_pid": os.getpid(),
            "owner_base": base_url,
            "created": time.time(),
            "tabs": [{"title": title, "url": url}],
        }
        _write_session(new_session)
        return "owner"


def reset_session() -> None:
    """删除会话文件（owner 退出时调用，让 guest 能接管）。"""
    try:
        if os.path.exists(TAB_SESSION_PATH):
            os.unlink(TAB_SESSION_PATH)
    except Exception:
        pass


def guest_session_ok(title: str, owner_base: str | None = None) -> bool:
    """检查 guest 会话是否仍有效：会话存在、owner 探测存活、自身标题在 tabs 中。

    owner_base 缺省为 None → 以会话内记录的 owner_base 为准探测（调用方通常
    不知道 owner 基址，会话文件才是权威来源）；显式传入时要求与会话一致
    （rebase 守卫：仅当调用方明确瞄准某 owner 会话时才做一致性校验），
    不一致视为会话失效。
    """
    with _TAB_LOCK:
        session = _read_session()
        if not session:
            return False
        sess_owner = session.get("owner_base") or ""
        if owner_base is not None and owner_base != sess_owner:
            return False
        if not sess_owner:
            return False
        if not _owner_alive(sess_owner):
            return False
        tabs = session.get("tabs", [])
        if not isinstance(tabs, list):
            return False
        for t in tabs:
            if isinstance(t, dict) and t.get("title") == title:
                return True
        return False


def tabs_payload() -> dict:
    """返回标签栏渲染所需 payload：{ok: True, tabs: [...], position: "top"|"bottom"}。

    position 从 config.json 读取（键 tabs_position，缺省 "top"）。
    """
    try:
        from configmanage import get_config

        cfg = get_config(show_dialogs=False) or {}
        position = cfg.get("tabs_position", "top")
        if position not in ("top", "bottom"):
            position = "top"
    except Exception:
        position = "top"
    session = _read_session()
    tabs = session.get("tabs", []) if session else []
    if not isinstance(tabs, list):
        tabs = []
    # 仅保留合法条目
    clean_tabs = [{"title": t.get("title", ""), "url": t.get("url", "")} for t in tabs if isinstance(t, dict)]
    return {"ok": True, "tabs": clean_tabs, "position": position}


def handle_tabs_post(body: dict) -> tuple[bool, str]:
    """处理 /api/tabs POST：仅支持 position 字段（top/bottom）。

    成功 → (True, "")；失败 → (False, 中文错误信息)。
    """
    if not isinstance(body, dict):
        return False, "请求体必须为 JSON 对象"
    position = body.get("position")
    if position not in ("top", "bottom"):
        return False, "tabs_position 仅支持 top / bottom"
    try:
        from configmanage import update_config

        cfg = update_config("tabs_position", position)
        # update_config 返回新配置；若写入失败内部会 fallback 但不抛异常，这里简略校验
        if cfg.get("tabs_position") != position:
            return False, "保存配置失败"
    except Exception:
        return False, "保存配置失败"
    return True, ""


def tab_host_html() -> bytes:
    """读取 ui/tabhost.html 并返回 UTF-8 字节；缺失时返回中文兜底页（避免白屏）。

    开发环境：repo 根目录下的 ui/tabhost.html
    冻结 exe：sys._MEIPASS/ui/tabhost.html（pack.ps1 已 --add-data "ui;ui"）
    """
    # 1) 开发环境：相对当前文件目录
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base_dir, "ui", "tabhost.html"),
    ]
    # 2) 冻结 exe：_MEIPASS
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.insert(0, os.path.join(meipass, "ui", "tabhost.html"))

    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().encode("utf-8")
        except Exception:
            continue
    # 兜底页：最小可用 HTML，含中文提示，防止标签窗口白屏
    fallback = (
        "<!DOCTYPE html>\n"
        "<html lang=\"zh-CN\">\n"
        "<head><meta charset=\"utf-8\"><title>标签页宿主</title></head>\n"
        "<body style=\"font-family:sans-serif;padding:2em;color:#333;\">\n"
        "界面加载失败：缺少 ui/tabhost.html\n"
        "</body></html>"
    )
    return fallback.encode("utf-8")