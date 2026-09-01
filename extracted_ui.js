 抽出，便于 node --check）
            js_path = _ui_js_path()
            try:
                with open(js_path, "rb") as f:
                    data = f.read()
            except OSError:
                self._send(404, b"// ui/app.js missing", "application/javascript; charset=utf-8")
            else:
                self._send(200, data, "application/javascript; charset=utf-8")
            return
        if path == "/api/heartbeat":
            self._touch_heartbeat()
            self._send(204, b"", "text/plain")
            return
        if path == "/api/ping":
            # 轻量存活探测：仅返回 ok，不修改任何状态。供外部（如 GUI）
            # 发现并恢复已存活的矫正界面。
            self._send(200, self._json({"ok": True}), "application/json; charset=utf-8")
            return
        if path == "/api/pages":
            state = self.server.state
            # S5：跨线程读 pages 时加锁快照（与保存/暂存/完成写入并发）
            lock = state.get("pages_lock")
            if lock is not None:
                with lock:
                    pages_snapshot = dict(state["pages"])
            else:
                pages_snapshot = dict(state["pages"])
            pages_list = []
            # 各页原始宽高（PDF 不可用时为空 dict，前端回退 onload 测量）
            dims = _page_dims(state)
            for n in sorted(pages_snapshot):
                raw_html = pages_snapshot[n]
                # Add ptoe-marker class for marker spans so saved pages render
                # highlighted in the editor while leaving stored HTML unchanged.
                served_html = _ensure_marker_classes(raw_html)
                # 2026-08-15 修复：已保存/历史内容按原样 serve（normalize_headings=False）
                # ——其中可能含用户手动设置的标题，不能再归一为 <p>（否则「保存后重开，
                # 已设置的标题格式丢失」）；OCR 自动标题的归一只在写入历史时做一次。
                item = {
                    "page": n,
                    "text": _page_text(served_html, normalize_headings=False),
                }
                if n in dims:
                    item["w"], item["h"] = dims[n]
                pages_list.append(item)
            payload = {"pages": pages_list}
            self._send(200, self._json(payload), "application/json; charset=utf-8")
            return
        if path == "/api/format_rules":
            # 格式规则列表（config.json format_rules 键；缺失返回空列表）。
            # 读取时经 _validate_format_rules 迁移旧模型规则，前端始终拿到新格式。
            try:
                from configmanage import get_config

                cfg = get_config(show_dialogs=False) or {}
                rules = _validate_format_rules(cfg.get("format_rules") or [])
                self._send(
                    200,
                    self._json({"ok": True, "rules": rules}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/history":
            # 历史记录列表：文件名/路径分列显示（同名不同路径可区分），
            # 同一文件按时间倒序编号版本（v1=最新）
            items = _history_entries()
            by_pdf: dict[str, list[dict[str, Any]]] = {}
            for it in items:
                by_pdf.setdefault(it["pdf"], []).append(it)
            for group in by_pdf.values():
                group.sort(key=lambda x: x["updated"], reverse=True)
                for i, it in enumerate(group, start=1):
                    it["version"] = i
            # 2026-08-23：全局按修改时间倒序——最近修改/读取的文件排在历史记录第一位
            # （此前仅组内排序用于版本号，返回列表仍是文件名序 ≈ 哈希序，与新旧无关）
            items.sort(key=lambda x: x.get("updated") or "", reverse=True)
            # 为每条记录确定 display_name：优先使用版本文件中的 display_name，否则回退到 name
            for it in items:
                pid = it["id"]
                # 读取对应版本文件检查 display_name
                fp = _history_dir() / f"{pid}.json"
                disp = None
                if fp.is_file():
                    try:
                        data = json.loads(fp.read_text(encoding="utf-8"))
                        disp = data.get("display_name")
                    except Exception:
                        pass
                it["display_name"] = disp if disp else it["name"]
            self._send(
                200, self._json({"items": items}), "application/json; charset=utf-8"
            )
            return
        if path == "/api/history/export":
            # 把某一历史版本导出为独立 JSON 文件（含内嵌预览图），供其他电脑
            # 通过 /api/history/import 导入继续矫正（跨平台矫正活动）。
            # 载荷 = 版本原始内容 + images 键（本机共享 sidecar 合并；旧版本
            # 文件自带 images 键则直接用）。Content-Disposition 让浏览器下载。
            import urllib.parse as _up

            try:
                qs = _up.parse_qs(_up.urlsplit(self.path).query)
                pid = (qs.get("id") or [""])[0]
                fp = _history_dir() / f"{pid}.json" if pid else None
                if not pid or not (fp and fp.is_file()):
                    self._send(
                        404,
                        self._json(
                            {"ok": False, "error": f"history version not found: {pid}"}
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                data = json.loads(fp.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {"pages": {}}
                images = data.get("images") or _load_images_cache(_version_prefix(pid))
                data["images"] = images or {}
                body = self._json(data)
                self._send(
                    200,
                    body,
                    "application/json; charset=utf-8",
                    {"Content-Disposition": f'attachment; filename="{pid}.json"'},
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/proofread_settings":
            self._proofread_settings()
            return
        if path == "/api/shortcuts":
            self._shortcuts()
            return
        if path == "/api/config":
            # 字体/界面配置：GET 读取 config.json fonts + citationItalicEnabled
            from configmanage import get_config

            cfg = get_config(show_dialogs=False) or {}
            fonts = cfg.get("fonts") or {}
            if not isinstance(fonts, dict):
                fonts = {}
            self._send(
                200,
                self._json(
                    {
                        "ok": True,
                        "fonts": {
                            "body": fonts.get("body", "serif"),
                            "heading": fonts.get("heading", "sans-serif"),
                            "note": fonts.get("note", "serif"),
                            "citation": fonts.get("citation", "cursive"),
                        },
                        "citationItalicEnabled": bool(
                            cfg.get("citationItalicEnabled", False)
                        ),
                    }
                ),
                "application/json; charset=utf-8",
            )
            return
        if path == "/api/llm_status":
            # llama-server 运行状态探测（深度校对/句子校正用）
            try:
                import llamamanage
                from configmanage import get_config
                from llamamanage import _probe_server

                cfg = get_config(show_dialogs=False) or {}
                model_choices = cfg.get("model_choices") or {}
                pr_cfg = cfg.get("proofread") or {}
                model_key = str(
                    pr_cfg.get("llm_model") or cfg.get("selected_model") or ""
                )
                model_info = (
                    (model_choices or {}).get(model_key)
                    if isinstance(model_choices, dict)
                    else None
                )
                model_name = (
                    str((model_info or {}).get("name") or model_key)
                    if model_info
                    else None
                )
                probe = _probe_server(model_name) if model_name else "none"
                # 大模型加载耗时可达数分钟，此期间进程存活但 /health 仍 503：
                # 报「启动中」而非「未运行」，避免用户误判启动失败（2026-08-09）
                proc = getattr(llamamanage, "_server_process", None)
                loading = (
                    bool(proc is not None and proc.poll() is None) and probe == "none"
                )
                self._send(
                    200,
                    self._json(
                        {
                            "ok": True,
                            "running": probe != "none",
                            "mismatch": probe == "mismatch",
                            "loading": loading,
                            "model": model_key or None,
                        }
                    ),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        m = re.fullmatch(r"/preview/(\d+)", path)
        if m:
            data = _preview_bytes(self.server.state, int(m.group(1)))
            if data is None:
                self._send(404, b"no image", "text/plain")
                return
            self._send(200, data[1], data[0], {"Cache-Control": "max-age=3600"})
            return
        m = re.fullmatch(r"/full/(\d+)", path)
        if m:
            data = _full_bytes(self.server.state, int(m.group(1)))
            if data is None:
                self._send(404, b"no image", "text/plain")
                return
            self._send(200, data[1], data[0], {"Cache-Control": "max-age=3600"})
            return
        if path == "/help.md":
            try:
                help_path = Path(__file__).resolve().parent / "help.md"
                if help_path.is_file():
                    content = help_path.read_text(encoding="utf-8")
                    self._send(200, content.encode("utf-8"), "text/markdown; charset=utf-8")
                else:
                    self._send(404, b"help.md not found", "text/plain")
            except Exception as e:
                self._send(500, str(e).encode("utf-8"), "text/plain; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")

    # -- POST --

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/heartbeat":
            self._touch_heartbeat()
            self._send(204, b"", "text/plain")
            return
        if path == "/api/gone":
            # pagehide 信标（sendBeacon）：标签页被关闭/导航离开，开始倒计时
            st = self.server.state
            st["gone_at"] = st.get("gone_at") or time.monotonic()
            self._send(204, b"", "text/plain")
            return
        if path == "/api/history/delete":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                deleted = _delete_history(
                    list(body.get("ids") or []), bool(body.get("all"))
                )
                self._send(
                    200,
                    self._json({"ok": True, "deleted": deleted}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/history/export/bulk":
            # 多选导出：把多个历史版本打包为一个 ZIP（每成员为 {pid}.json，
            # 含内嵌预览图 sidecar 合并），供批量迁移/备份。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                ids = list(body.get("ids") or [])
                if not ids:
                    self._send(
                        400,
                        self._json({"ok": False, "error": "未选择要导出的历史版本"}),
                        "application/json; charset=utf-8",
                    )
                    return
                import io
                import zipfile

                buf = io.BytesIO()
                count = 0
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for pid in ids:
                        fp = _history_dir() / f"{pid}.json"
                        if not fp.is_file():
                            continue
                        try:
                            data = json.loads(fp.read_text(encoding="utf-8"))
                            if not isinstance(data, dict):
                                data = {"pages": {}}
                            images = data.get("images") or _load_images_cache(
                                _version_prefix(pid)
                            )
                            data["images"] = images or {}
                            zf.writestr(
                                f"{pid}.json",
                                json.dumps(data, ensure_ascii=False, indent=2),
                            )
                            count += 1
                        except Exception:  # noqa: BLE001
                            continue
                if count == 0:
                    self._send(
                        404,
                        self._json({"ok": False, "error": "没有可导出的历史版本"}),
                        "application/json; charset=utf-8",
                    )
                    return
                import time as _time

                stamp = _time.strftime("%Y%m%d%H%M%S")
                self._send(
                    200,
                    buf.getvalue(),
                    "application/zip",
                    {
                        "Content-Disposition": f'attachment; filename="ptoe_history_{stamp}.zip"'
                    },
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/history/import":
            # 把导出的历史版本 JSON 或 ZIP 导入本机（跨平台矫正活动）。
            # body 两种形态：
            #   {filename, content} —— 单 JSON（向后兼容）
            #   {filename, is_zip: true, content_b64} —— ZIP 包（多版本）
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                filename = str(body.get("filename") or "")
                is_zip = bool(body.get("is_zip"))
                if is_zip:
                    import base64
                    import io
                    import zipfile

                    b64 = str(body.get("content_b64") or "")
                    try:
                        zip_bytes = base64.b64decode(b64)
                    except Exception:
                        self._send(
                            400,
                            self._json(
                                {"ok": False, "error": "ZIP 内容 base64 解码失败"}
                            ),
                            "application/json; charset=utf-8",
                        )
                        return
                    ids = []
                    errors = []
                    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                        for name in zf.namelist():
                            if not name.lower().endswith(".json"):
                                continue
                            try:
                                member_content = json.loads(
                                    zf.read(name).decode("utf-8")
                                )
                                ok, msg, stem = _import_history(member_content, name)
                                if ok:
                                    ids.append(stem)
                                else:
                                    errors.append(f"{name}: {msg}")
                            except Exception as exc:  # noqa: BLE001
                                errors.append(f"{name}: {exc}")
                    if not ids:
                        err_msg = "导入失败：" + (
                            "；".join(errors) if errors else "ZIP 中无有效 JSON"
                        )
                        self._send(
                            400,
                            self._json({"ok": False, "error": err_msg}),
                            "application/json; charset=utf-8",
                        )
                        return
                    self._send(
                        200,
                        self._json({"ok": True, "ids": ids, "errors": errors or None}),
                        "application/json; charset=utf-8",
                    )
                    return
                # 原有单 JSON 路径
                content = body.get("content")
                ok, msg, stem = _import_history(content, filename)
                if not ok:
                    self._send(
                        400 if "缺少 pages" in msg else 500,
                        self._json({"ok": False, "error": msg}),
                        "application/json; charset=utf-8",
                    )
                    return
                self._send(
                    200,
                    self._json({"ok": True, "id": stem}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/history/load":
            # 把某一历史版本重新载入浏览器编辑器（再次矫正）：
            # 返回该版本的 pages（按页码排序，字段与 /api/convert 一致用 html）；
            # 同时把预览图来源切换为该版本所属 PDF，保证图与文对应。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                pid = str(body.get("id") or "")
                loaded = _load_history_version(pid)
                if loaded is None:
                    self._send(
                        404,
                        self._json(
                            {"ok": False, "error": f"history version not found: {pid}"}
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                out = [
                    # 与 /api/pages 一致：serve 时补回 ptoe-marker 显示类，
                    # 否则保存时被 sanitize 剥掉的 class 会让标记渲染成纯文本
                    # （旧历史版本磁盘载荷无 class，必须在此补齐）。
                    # 2026-08-15 修复：历史内容按原样返回（不再归一 <h1>-<h6>）——
                    # 其中可能含用户手动设置的标题，归一会导致「保存后重开，
                    # 已设置的标题格式丢失」；OCR 自动标题的归一只在写入历史时做一次。
                    # 2026-08-30：serve 前做杂符括号清理（token 级）——历史版本
                    # 可能保存过 \\〔^{x〕}\\ 之类大模型杂符包裹，须在界面可见/再次
                    # 矫正前清除（与 reocr 对比基准保持一致）。
                    {
                        "page": int(k),
                        "html": _ensure_marker_classes(
                            _clean_bracket_junk_html(str(v))
                        ),
                    }
                    for k, v in loaded["pages"].items()
                ]
                out.sort(key=lambda x: x["page"])
                pdf = loaded["pdf"]
                st = self.server.state
                # 把版本内容同步进服务端状态：刷新/再次载入/暂存/完成都以浏览器
                # 打开的内容为准（无文件模式下 state["pages"] 初始为空）
                # S5：与保存/暂存/完成写入并发时加锁
                lock = st.get("pages_lock")
                with lock if lock is not None else nullcontext():
                    for k, v in loaded["pages"].items():
                        try:
                            # 2026-08-15 修复：历史版本内容按原样同步（不再归一标题）——
                            # 用户手动设置的 <h1>-<h6> 必须保留，否则「保存后重开，
                            # 已设置的标题格式丢失」；OCR 自动标题的归一只在写入历史时做一次
                            # 2026-08-30：同步前做杂符括号清理（与 serve 路径一致）
                            st["pages"][int(k)] = sanitize_html(
                                _clean_bracket_junk_html(str(v))
                            )
                        except (TypeError, ValueError):
                            continue
                if pdf and Path(pdf).is_file() and st.get("pdf_path") != pdf:
                    st["pdf_path"] = pdf
                    st["preview_cache"] = OrderedDict()  # 换书后旧页码缓存作废
                    # 旧 PDF 句柄作废（下次按需重开）
                    try:
                        st["preview_doc"] = None
                    except Exception:
                        st["preview_doc"] = None
                # 记录 history_name 供后续暂存/保存使用（测试断言）
                st["history_name"] = Path(pdf).name if pdf else None
                # 从版本文件读取 display_name（重命名功能的结果），供 EPUB 导出 fallback 使用
                # 遵循 GET /api/history 同一模式：版本 JSON 中有则优先，无则回退到 name
                _disp = None
                vp = _history_dir() / f"{pid}.json"
                if vp.is_file():
                    try:
                        _ddata = json.loads(vp.read_text(encoding="utf-8"))
                        _disp = _ddata.get("display_name")
                    except Exception:
                        pass
                st["display_name"] = _disp if _disp else st.get("display_name")
                # 加载内嵌预览图供跨电脑时 fallback
                st["embedded_images"] = loaded.get("embedded_images") or {}
                # 返回页面数据（与 /api/convert 约定一致）+ 文字纠错状态
                self._send(
                    200,
                    self._json(
                        {
                            "ok": True,
                            "pages": out,
                            "pdf": pdf,
                            "proofread": loaded.get("proofread")
                            or {"errors": {}, "original": {}, "dismissed": {}},
                            "last_proofread_page": loaded.get("last_proofread_page"),
                        }
                    ),
                    "application/json; charset=utf-8",
                )

            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/history/rename":
            # 重命名历史记录条目：更新 display_name，应用到同一 PDF 组的所有版本文件
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                vid = str(body.get("id") or "").strip()
                new_name = str(body.get("newName") or "").strip()
                if not vid:
                    self._send(
                        400,
                        self._json({"ok": False, "error": "缺少版本 ID"}),
                        "application/json; charset=utf-8",
                    )
                    return
                if not new_name:
                    self._send(
                        400,
                        self._json({"ok": False, "error": "新名称不能为空"}),
                        "application/json; charset=utf-8",
                    )
                    return
                # 验证：拒绝包含路径分隔符的名称
                if "\\" in new_name or "/" in new_name or ":" in new_name:
                    self._send(
                        400,
                        self._json({"ok": False, "error": "名称含非法字符（含 \\ / :）"}),
                        "application/json; charset=utf-8",
                    )
                    return
                # 长度限制（最多 100 个字符，与文件名惯例一致）
                if len(new_name) > 100:
                    self._send(
                        400,
                        self._json({"ok": False, "error": "名称过长（最多 100 字符）"}),
                        "application/json; charset=utf-8",
                    )
                    return
                prefix = _version_prefix(vid)  # 由版本文件名 stem 算出的 book 前缀
                if not prefix:
                    self._send(
                        404,
                        self._json({"ok": False, "error": "无法定位所属 PDF 组"}),
                        "application/json; charset=utf-8",
                    )
                    return
                d = _history_dir()
                # 遍历该 PDF 组的所有版本文件（前缀匹配），写入 display_name
                # 注意用 Path.glob（返回 Path 对象）；glob.glob 返回 str 无 .read_text
                matched = False
                for fp in d.glob(f"{prefix}_*.json"):
                    try:
                        data = json.loads(fp.read_text(encoding="utf-8"))
                        # 保留原有字段，仅更新/写入 display_name
                        data["display_name"] = new_name
                        # 使用原子写入：先写临时文件再 os.replace
                        import tempfile, os as _os
                        fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".rename-", suffix=".tmp")
                        try:
                            with os.fdopen(fd, "w", encoding="utf-8") as f:
                                json.dump(data, f, ensure_ascii=False, indent=2)
                                f.flush()
                                os.fsync(f.fileno())
                            _os.replace(tmp, str(fp))
                        except Exception:
                            try:
                                _os.unlink(tmp)
                            except Exception:
                                pass
                        matched = True
                    except Exception:
                        continue
                # 更新内存中 _HISTORY_INDEX 以立即反映变更
                # 重新计算签名使下次 _history_entries 重读
                fps = sorted(fp for fp in d.glob("*.json") if not fp.name.endswith(".images.json"))
                sig = "|".join(
                    f"{fp.name}:{fp.stat().st_mtime_ns}:{fp.stat().st_size}" for fp in fps
                )
                # 若重命名的是当前编辑中的书（前缀一致），同步更新会话内
                # state.display_name，使随后的保存/暂存/完成沿用它而非回退到原名
                st = self.server.state
                if st.get("history_prefix") == prefix:
                    st["display_name"] = new_name
                # 只要取第一个版本的显示名作为组名变更的凭证
                first_items = _history_entries(prefix)
                first_display = first_items[0].get("display_name", first_items[0].get("name", "")) if first_items else new_name
                self._send(
                    200, self._json({"ok": True, "display_name": first_display}), "application/json; charset=utf-8"
                )
                return
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/convert":
            # 繁简转换（简→繁 / 繁→简）：只转换文本节点，标签/标记不变；
            # 无状态 —— 只返回转换结果，由浏览器更新界面（保存时才落盘）。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                mode = body.get("mode")
                convert_modes = globals().get("_CONVERT_MODES", {"t2s", "s2t"})
                if mode not in convert_modes:
                    self._send(
                        400,
                        self._json({"ok": False, "error": f"bad mode: {mode}"}),
                        "application/json; charset=utf-8",
                    )
                    return

                converted = []
                for item in body.get("pages") or []:
                    try:
                        n = int(item.get("page"))
                    except (TypeError, ValueError):
                        continue
                    html_text = sanitize_html(str(item.get("html") or ""))
                    converted.append(
                        {"page": n, "html": convert_text_html(html_text, mode)}
                    )

                self._send(
                    200,
                    self._json({"ok": True, "pages": converted}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/clean":
            # 文本智能清理（段落合并 / 段首 #/* 符号 / 中英文标点 / HTML 标签）：
            # 无状态 —— 只返回清理结果，由浏览器更新界面（保存时才落盘）。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                cleaned = []
                for item in body.get("pages") or []:
                    try:
                        n = int(item.get("page"))
                    except (TypeError, ValueError):
                        continue
                    cleaned.append(
                        {
                            "page": n,
                            "html": clean_page_html(str(item.get("html") or "")),
                        }
                    )
                self._send(
                    200,
                    self._json({"ok": True, "pages": cleaned}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/format_rules":
            # 整体保存格式规则（弹窗编辑后一次提交）
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                rules = _validate_format_rules(body.get("rules") or [])
                from configmanage import set_format_rules

                set_format_rules(rules)
                self._send(
                    200,
                    self._json({"ok": True, "rules": rules}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/format_rules/apply":
            # 应用格式规则到指定页 HTML（任务 B：服务端应用引擎）
            # body: {page:int, html:str, rule_id?:str, all?:bool, sel_start?:int, sel_end?:int}
            # 返回: {ok:bool, html:str} 或 {ok:false, error:str}
            try:
                if rulemanage is None:
                    self._send(
                        500,
                        self._json({"ok": False, "error": "rulemanage 模块未加载"}),
                        "application/json; charset=utf-8",
                    )
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))

                # 校验参数
                page = body.get("page")
                html_text = body.get("html")
                rule_id = body.get("rule_id")
                all_rules = bool(body.get("all"))
                sel_start = body.get("sel_start")
                sel_end = body.get("sel_end")

                if not isinstance(page, int) or page < 1:
                    self._send(
                        400,
                        self._json({"ok": False, "error": "page 必须为正整数"}),
                        "application/json; charset=utf-8",
                    )
                    return
                if not isinstance(html_text, str):
                    self._send(
                        400,
                        self._json({"ok": False, "error": "html 必须为字符串"}),
                        "application/json; charset=utf-8",
                    )
                    return

                # 读取规则（与 GET /api/format_rules 相同来源）
                from configmanage import get_config
                cfg = get_config(show_dialogs=False) or {}
                rules = cfg.get("format_rules") or []
                rules = _validate_format_rules(rules)

                # 调用 rulemanage 引擎
                new_html, err = rulemanage.apply_rules(
                    html_text,
                    rules,
                    rule_id=rule_id,
                    all_rules=all_rules,
                    sel_start=sel_start if isinstance(sel_start, int) else None,
                    sel_end=sel_end if isinstance(sel_end, int) else None,
                )
                if err:
                    self._send(
                        400,
                        self._json({"ok": False, "error": err}),
                        "application/json; charset=utf-8",
                    )
                    return

                # 结果过一遍 sanitize_html 再返回
                new_html = sanitize_html(new_html)
                self._send(
                    200,
                    self._json({"ok": True, "html": new_html}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": f"应用格式规则失败: {e}"}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/proofread_settings":
            self._proofread_settings()
            return
        if path == "/api/shortcuts":
            self._shortcuts()
            return
        if path == "/api/config":
            self._config()
            return
        if path == "/api/llm_start":
            # 启动 llama-server（默认附加 --mmproj 图像投影，供大模型重识别 OCR 使用；
            # 模型未配置 mmproj 时回退纯文本）。
            try:
                from configmanage import get_config
                from llamamanage import runserver

                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                cfg = get_config(show_dialogs=False) or {}
                model_choices = cfg.get("model_choices") or {}
                pr_cfg = cfg.get("proofread") or {}
                model_key = str(
                    body.get("model")
                    or pr_cfg.get("llm_model")
                    or cfg.get("selected_model")
                    or ""
                )
                if (
                    not isinstance(model_choices, dict)
                    or model_key not in model_choices
                ):
                    self._send(
                        400,
                        self._json(
                            {
                                "ok": False,
                                "error": (
                                    f"模型 '{model_key}' 未在配置中注册"
                                    f"（可用: {', '.join(sorted(model_choices)) if isinstance(model_choices, dict) else '无'}）"
                                ),
                            }
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                model_info = model_choices.get(model_key) or {}
                has_mmproj = bool(model_info.get("mmproj"))
                from llamamanage import _active_engine

                eng = _active_engine()
                eng_label = "vLLM-Omni" if eng == "vllm" else "llama-server"
                eng_port = (
                    (cfg.get("vllm_server_args") or {}).get("port") or "8000"
                    if eng == "vllm"
                    else (cfg.get("llama_server_args") or {}).get("port") or "8080"
                )
                # 快速切换支持（2026-08 修复）：若端口上运行的是其他模型，先停掉
                # 本进程管理的旧实例再启动新模型；仍被占用（外部进程）则给出明确提示，
                # 避免直接调 runserver 撞端口报出难懂的占用错误。
                from llamamanage import _probe_server as _probe_pre, stopserver

                pre_name = str(model_info.get("name") or model_key)
                if _probe_pre(pre_name) == "mismatch":
                    stopserver()  # 仅能停掉本进程启动的实例
                    time.sleep(1.0)
                    if _probe_pre(pre_name) == "mismatch":
                        self._send(
                            200,
                            self._json(
                                {
                                    "ok": False,
                                    "error": (
                                        f"端口 {eng_port} 被外部 {eng_label} 占用"
                                        "（非本程序启动），请手动关闭后重试"
                                    ),
                                }
                            ),
                            "application/json; charset=utf-8",
                        )
                        return
                # 矫正/重识别为单请求顺序处理（用户逐页点击，一次一个请求），并发
                # 槽位取 1 即可——llama-server 的 KV cache ≈ n_ctx × parallel，默认
                # 取 config 的 parallel(6)×max_tokens(8192) 会把 KV 预分配撑到数 GB，
                # 显著超过直接启动（--parallel 1）的显存占用（2026-09-01 修复）。
                running = bool(runserver(model_key, with_mmproj=has_mmproj, parallel=1))
                if running:
                    # Issue 1 fix: persist model choice so it survives UI restart
                    try:
                        from configmanage import set_proofread_param
                        set_proofread_param("llm_model", model_key)
                    except Exception:
                        pass  # Best-effort; don't fail startup if config write fails
                    message = f"{eng_label} 已就绪"
                else:
                    # 启动失败：区分「端口被其他模型占用」与「启动超时/失败」，给出可操作提示
                    from llamamanage import _probe_server

                    model_name = str(model_info.get("name") or model_key)
                    if _probe_server(model_name) == "mismatch":
                        message = (
                            f"端口 {eng_port} 已被其他 {eng_label} 占用（模型不符），"
                            f"请先停止旧服务（或手动关闭任务管理器中的 {eng_label}）后重试"
                        )
                    else:
                        message = f"{eng_label} 启动失败（请检查模型路径/服务日志）"
                self._send(
                    200,
                    self._json(
                        {
                            "ok": True,
                            "running": running,
                            "image_model": has_mmproj,
                            "message": message,
                        }
                    ),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/llm_stop":
            try:
                from llamamanage import _active_engine, _probe_server, stopserver

                eng_label = (
                    "vLLM-Omni" if _active_engine() == "vllm" else "llama-server"
                )
                stopserver()
                # stopserver 已兜底杀端口上的遗留实例；再探测确认端口真正释放
                # （杀不掉时提示手动关闭，避免界面误报已停止）
                if _probe_server(None) != "none":
                    self._send(
                        200,
                        self._json(
                            {
                                "ok": True,
                                "message": f"已停止 {eng_label}（端口仍有进程占用，请手动关闭）",
                            }
                        ),
                        "application/json; charset=utf-8",
                    )
                else:
                    self._send(
                        200,
                        self._json({"ok": True, "message": f"已停止 {eng_label}"}),
                        "application/json; charset=utf-8",
                    )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/proofread":
            # 文字纠错：剥标签取纯文本 → proofread_page → 错误列表。
            # 无状态 —— 只返回检测结果，由浏览器叠加标注（不入 undo 快照）。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                text = _proofread_plain_text(str(body.get("html") or ""))
                # 原有规则（半角转全角/引号配对/混淆表/词典）默认关闭，
                # 由 config.json proofread.enable_legacy_rules 开关控制（矫正界面设置）。
                legacy_rules = False
                try:
                    from configmanage import get_config as _get_cfg

                    _pr_cfg = (_get_cfg(show_dialogs=False) or {}).get(
                        "proofread"
                    ) or {}
                    legacy_rules = bool(_pr_cfg.get("enable_legacy_rules"))
                except Exception:
                    legacy_rules = False  # 配置读取失败 → 只跑三条新规则
                errors = proofread_page(text, enable_legacy_rules=legacy_rules)
                # optional LLM enhancement: client must opt-in via use_llm flag.
                # 模型与开关持久化在 config.json（/api/proofread_settings），前端不再用 localStorage
                # （随机端口下 localStorage 每次运行失效，2026-08-07 修复）。失败不再静默吞掉，
                # 通过 llm_error 字段上浮给前端（基础 errors 照常返回）。
                use_llm = bool(body.get("use_llm"))
                llm_model = str(body.get("llm_model") or "").strip() or None
                llm_used = False
                llm_error = None
                if use_llm:
                    from configmanage import get_config

                    cfg = get_config(show_dialogs=False) or {}
                    model_choices = cfg.get("model_choices") or {}
                    pr_cfg = cfg.get("proofread") or {}
                    default_model = pr_cfg.get("llm_model")
                    model_key = str(
                        llm_model or default_model or cfg.get("selected_model") or ""
                    )
                    if (
                        not isinstance(model_choices, dict)
                        or model_key not in model_choices
                    ):
                        llm_error = (
                            f"模型 '{model_key}' 未在配置中注册"
                            f"（可用: {', '.join(sorted(model_choices)) if isinstance(model_choices, dict) else '无'}）"
                        )
                    else:
                        llm_sugs, llm_err = _proofread_llm_enhance(
                            text, errors, model_key
                        )
                        if llm_err:
                            llm_error = llm_err
                        else:
                            llm_used = True
                            for s in llm_sugs:
                                # append non-overlapping suggestions（模型返回项可能缺字段：跳过非法项）
                                if (
                                    not isinstance(s, dict)
                                    or "start" not in s
                                    or "end" not in s
                                    or "wrong" not in s
                                ):
                                    continue
                                if not any(
                                    not (
                                        s["end"] <= e["start"] or s["start"] >= e["end"]
                                    )
                                    for e in errors
                                ):
                                    errors.append(s)
                self._send(
                    200,
                    self._json(
                        {
                            "ok": True,
                            "errors": errors,
                            "llm_used": llm_used,
                            "llm_error": llm_error,
                        }
                    ),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                import traceback

                tb = traceback.format_exc()
                # return traceback in response for local debugging; caller (UI) can show it
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e), "trace": tb}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/reocr":
            # 大模型重识别：对指定页重新 OCR，逐行逐字对比当前文本，差异以纠错标注返回。
            # 无状态 —— 只返回新 OCR 文本与 diff 标注，由浏览器叠加显示（不入 undo 快照）。
            tmp_path = None
            try:
                import llamamanage
                from configmanage import get_config

                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                state = self.server.state
                cfg = get_config(show_dialogs=False) or {}
                model_choices = cfg.get("model_choices") or {}
                pr_cfg = cfg.get("proofread") or {}
                model_key = str(
                    body.get("model")
                    or pr_cfg.get("llm_model")
                    or cfg.get("selected_model")
                    or ""
                )
                if (
                    not isinstance(model_choices, dict)
                    or model_key not in model_choices
                ):
                    self._send(
                        400,
                        self._json(
                            {
                                "ok": False,
                                "error": (
                                    f"模型 '{model_key}' 未在配置中注册"
                                    f"（可用: {', '.join(sorted(model_choices)) if isinstance(model_choices, dict) else '无'}）"
                                ),
                            }
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                # 2026-08-09：重识别前先探测服务端已加载模型，避免「所选模型与服务不符」时
                # 静默用错模型 OCR，或把 llama-server 的 400 原样透出（用户报「选择 qwen4 报 400」）。
                model_name = (model_choices.get(model_key) or {}).get(
                    "name"
                ) or model_key
                probe = llamamanage._probe_server(model_name)
                if probe == "none":
                    proc = getattr(llamamanage, "_server_process", None)
                    if proc is not None and proc.poll() is None:
                        self._send(
                            200,
                            self._json(
                                {
                                    "ok": False,
                                    "error": "模型服务正在加载中，请稍候片刻后重试。",
                                }
                            ),
                            "application/json; charset=utf-8",
                        )
                    else:
                        self._send(
                            200,
                            self._json(
                                {
                                    "ok": False,
                                    "error": "未检测到运行中的模型服务，请先点击「启动服务」加载所选模型。",
                                }
                            ),
                            "application/json; charset=utf-8",
                        )
                    return
                if probe == "mismatch":
                    self._send(
                        200,
                        self._json(
                            {
                                "ok": False,
                                "error": (
                                    f"当前服务加载的模型与所选模型 {model_key} 不符。"
                                    "请先点击「停止服务」，再点击「启动服务」加载所选模型后重试。"
                                ),
                            }
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                # 2026-09-01：所选模型是视觉模型（配置了 mmproj），但当前运行的
                # llama-server 若为纯文本模式（未加载 --mmproj），收图会报 500
                # 「peg-native format」。发送前先探测一次多模态能力，命中则给出
                # 明确指引，避免把难懂的 500 直接抛给用户。
                sel_has_mmproj = bool((model_choices.get(model_key) or {}).get("mmproj"))
                mmproj_ok = llamamanage._probe_mmproj()
                if sel_has_mmproj and mmproj_ok is False:
                    self._send(
                        200,
                        self._json(
                            {
                                "ok": False,
                                "error": (
                                    f"当前服务为纯文本模式（未加载 mmproj 视觉投影），"
                                    f"无法对模型 {model_key} 执行重识别。"
                                    "请先点击「停止服务」，再点击「启动服务」加载所选视觉模型后重试。"
                                ),
                            }
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                try:
                    page_no = int(body.get("page"))
                except (TypeError, ValueError):
                    self._send(
                        400,
                        self._json({"ok": False, "error": "page 参数无效"}),
                        "application/json; charset=utf-8",
                    )
                    return
                img = _reocr_image(state, page_no)
                if img is None:
                    self._send(
                        404,
                        self._json(
                            {"ok": False, "error": f"第 {page_no} 页图像不可用"}
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                content_type, img_bytes = img
                ocr_prompt = cfg.get("ocr_prompt") or llamamanage.OCR_PROMPT
                res = llamamanage._request_image_new(
                    ocr_prompt,
                    "",
                    model_key=model_key,
                    # 重识别为纯 OCR 任务：thinking=True 会触发 Qwen 隐藏思考链长生成，
                    # KV 缓存暴涨占满显存且拖慢识别 ~7 倍（2026-08 修复）
                    thinking=False,
                    timeout=llamamanage.REQUEST_TIMEOUT,
                    img_bytes=img_bytes,
                    # 2026-08-31：把真实 MIME 传给 _request_image_new——页图为 PNG 时
                    # 若数据 URI 错标 image/jpeg，llama-server 解图失败返回 500
                    # （曾致「重识别失败:500 Server Error」）
                    content_type=content_type,
                )
                # 2026-09-01：自动修复——若命中 peg-native format（多模态推理失败，常因
                # 纯文本模式服务误收图），且所选模型配置了 mmproj，先探测服务端多模态能力，
                # 仅当确认为纯文本模式（probe_mmproj=False）时才自动重启视觉服务并重试一次。
                # 若探测为视觉模式或探测不明，则判定为单页图片异常（过大/损坏/MIME异常），
                # 不重启服务，直接返回单页失败提示（含图片大小便于自查）。
                err_str = str(res.get("error") or "")
                auto_heal_attempted = False
                if (
                    res.get("error")
                    and sel_has_mmproj
                    and llamamanage._active_engine() == "llama"
                    and any(k in err_str for k in _LLM_PEG_MARKERS)
                ):
                    # 先探测：当前服务是否真正加载了 mmproj（视觉投影）
                    mmproj_probe = llamamanage._probe_mmproj()
                    if mmproj_probe is False:
                        # 确认为纯文本模式：执行自动重启并重试
                        try:
                            llamamanage.stopserver()
                            time.sleep(0.5)
                            ok = llamamanage.runserver(model_key, with_mmproj=True)
                            if ok:
                                auto_heal_attempted = True
                                res = llamamanage._request_image_new(
                                    ocr_prompt,
                                    "",
                                    model_key=model_key,
                                    thinking=False,
                                    timeout=llamamanage.REQUEST_TIMEOUT,
                                    img_bytes=img_bytes,
                                    content_type=content_type,
                                )
                                err_str = str(res.get("error") or "")
                        except Exception:
                            # 自动修复过程出错：静默忽略，走统一错误处理
                            pass
                    else:
                        # 探测为视觉模式 或 探测不明：判定为单页图片异常，不重启服务
                        # 先尝试按页降分辨率重试一次，再决定是否返回错误
                        img_size = len(img_bytes) if img_bytes else 0
                        retry_bytes = None
                        retry_ct = None
                        try:
                            # 以当前 _REOCR_MAX_SIDE 的一半为目标最大边，重新渲染更小的 JPEG
                            # 复用 preview_doc 与锁，避免重开 PDF
                            doc = _preview_doc(state)
                            lock = state.get("preview_doc_lock")
                            if (
                                doc is not None
                                and not getattr(doc, "is_closed", False)
                                and 1 <= page_no <= doc.page_count
                            ):
                                import fitz
                                with lock if lock is not None else nullcontext():
                                    r = doc[page_no - 1].rect
                                max_dim = max(r.width, r.height)
                                if max_dim > 0:
                                    # 目标最大边 = _REOCR_MAX_SIDE // 2 (约 780px)，足够 OCR 且 token 大幅减少
                                    target_side = _REOCR_MAX_SIDE // 2
                                    dpi = (target_side * 72.0) / max_dim
                                    quality = int(state.get("preview_quality", 70))
                                    with lock if lock is not None else nullcontext():
                                        pix = doc[page_no - 1].get_pixmap(
                                            matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0),
                                            alpha=False,
                                        )
                                        retry_bytes = pix.tobytes("jpeg", jpg_quality=quality)
                                        retry_ct = "image/jpeg"
                        except Exception:
                            # 任何渲染异常静默回退，走原错误分支
                            retry_bytes = None
                            retry_ct = None

                        if retry_bytes and len(retry_bytes) < img_size:
                            # 用降采样图再次请求
                            res2 = llamamanage._request_image_new(
                                ocr_prompt,
                                "",
                                model_key=model_key,
                                thinking=False,
                                timeout=llamamanage.REQUEST_TIMEOUT,
                                img_bytes=retry_bytes,
                                content_type=retry_ct,
                            )
                            if not res2.get("error"):
                                # 重试成功：用新结果继续走正常流程
                                res = res2
                                err_str = ""
                            else:
                                # 重试仍失败：更新错误信息并落入下方统一错误处理
                                err_str = str(res2.get("error") or err_str)
                        # 无重试或重试失败：构造友好提示并返回
                        friendly = _friendly_llm_error(err_str)
                        if retry_bytes:
                            friendly += f"（该页图片可能过大/损坏/MIME异常，原始 {img_size} 字节→降采样 {len(retry_bytes)} 字节重试仍失败，已跳过自动重启；可尝试对该页单独降低分辨率或检查原图）"
                        else:
                            friendly += f"（该页图片可能过大/损坏/MIME异常，大小 {img_size} 字节，已跳过自动重启；可尝试对该页单独降低分辨率或检查原图）"
                        self._send(
                            200,
                            self._json({"ok": False, "error": friendly}),
                            "application/json; charset=utf-8",
                        )
                        return
                if res.get("error"):
                    friendly = _friendly_llm_error(err_str)
                    # 若经历过自动修复尝试（上述分支已执行），在提示中追加说明
                    if auto_heal_attempted:
                        friendly += " （已尝试自动重启视觉服务仍失败，请手动点击「停止服务」再「启动服务」后重试）"
                    self._send(
                        200,
                        self._json(
                            {
                                "ok": False,
                                "error": friendly,
                            }
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                new_text = str(res.get("result") or "")
                # 2026-08-09：ULQ4/ULQ8 等 PaddleOCR 系模型输出带 bbox 坐标前缀与思考块，
                # 先剥离再繁简/标点归一，避免格式 token 被当成纠错项（用户报「ulq 输出含格式参数」）。
                if new_text:
                    from stringmanage import clean_bbox_text, strip_think_blocks, ttos

                    new_text = strip_think_blocks(new_text)
                    new_text = clean_bbox_text(new_text)
                    # 2026-08-08：重识别结果先繁转简再与当前文本对比（与 /api/proofread 的 ⑫b 一致）
                    # 2026-08-30：识别结果中的括号对（【x】/[x]/［x］ 等）在对比前统一
                    # 归一为 〔x〕（与 clean 流程一致），避免括号样式差异被当作纠错项；
                    # 括号内内容原样保留（此前 2026-08-28 是「不处理、保留原始输出」）
                    new_text = ttos(new_text)
                # 2026-08-23/28：模型可能把图片页脚的页码一并识别进来，先剥掉末尾页码
                # （第 N 页 / 字符+数字：页123·P123·No.123 / 括号包裹 / 独立成行裸数字），
                # 避免被当成正文差异标注；仅清理返回文字最末尾的页码，正文不受影响。
                # 须在英文标点归一（_full_punct）之前执行，否则 "No.123" 的 "." 被转成
                # "。" 后无法匹配页码样式。
                new_text = _strip_trailing_page_number(new_text)
                # 2026-08-09：再将英文标点归一为中文标点，避免半角/全角差异被当成纠错项
                new_text = _full_punct(new_text)
                # 2026-08-30：对比前做杂符括号清理 + 括号对统一（〔x〕）——
                # 部分大模型把原文 〔x〕 引注识别成 \\〔^{x〕}\\ 的杂符包裹格式
                # （\\ ^ { } 等无效字符夹着括号），须先折叠为 〔x〕 再与原文比较，
                # 否则杂符被逐字判为纠错项。括号对归一为逐字符 1:1 替换；
                # 杂符清理改变长度但只作用在模型返回文本侧（后文 current_text
                # 已由进入矫正界面时的清理保证无杂符，见 _page_text/initial_html）。
                new_text = _normalize_brackets(new_text)
                current_text = _proofread_plain_text(str(body.get("html") or ""))
                # 与 new_text 同样做半角→全角标点归一：否则相同内容因标点宽度差异
                # 被逐字判为差异，产生大量非预期位置的纠错标注（2026-08 修复）
                current_text = _full_punct(current_text)
                # 与 new_text 做同等括号归一：否则「原文是〔1〕、模型输出【1】」这种
                # 纯粹样式差异会被逐字判为差异（2026-08-30）
                current_text = _normalize_brackets(current_text)
                diff = diff_reocr_texts(current_text, new_text)
                self._send(
                    200,
                    self._json({"ok": True, "text": new_text, "diff": diff}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            finally:
                pass
            return
        if path == "/api/proofread_feedback":
            # 纠错反馈回写：accept → add_user_fix；ignore → ignore_word；支持批量 items。
            # 有状态持久化（写 data/proofread_dict.json），失败不阻塞前端 UI。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                fb_type = str(body.get("type") or "")
                if fb_type not in ("accept", "ignore"):
                    self._send(
                        400,
                        self._json({"ok": False, "error": f"bad type: {fb_type}"}),
                        "application/json; charset=utf-8",
                    )
                    return
                if dictionarymanage is None:
                    self._send(
                        503,
                        self._json(
                            {"ok": False, "error": "dictionarymanage not ready"}
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                # 批量 accept（proofreadApplyCurrent 一次发多条）
                if fb_type == "accept" and isinstance(body.get("items"), list):
                    for item in body["items"]:
                        w = str(item.get("wrong") or "")
                        f = str(item.get("fixed") or "")
                        if w and f and w != f:
                            dictionarymanage.add_user_fix(w, f)
                elif fb_type == "accept":
                    w = str(body.get("wrong") or "")
                    f = str(body.get("fixed") or "")
                    if w and f and w != f:
                        dictionarymanage.add_user_fix(w, f)
                else:  # ignore
                    w = str(body.get("wrong") or "")
                    if w:
                        dictionarymanage.ignore_word(w)
                self._send(
                    200,
                    self._json({"ok": True}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/export":
            # 导出 TXT / DOCX：浏览器把当前全部页面（含未保存修改）发来，
            # 服务端转纯文本后由用户弹窗（tkinter 保存对话框）选择保存位置。
            # body 可带 "path" 直接指定路径（测试/脚本用，跳过对话框）。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                fmt = str(body.get("format") or "")
                if fmt not in ("txt", "docx", "epub", "md"):
                    self._send(
                        400,
                        self._json({"ok": False, "error": f"bad format: {fmt}"}),
                        "application/json; charset=utf-8",
                    )
                    return
                items = sorted(
                    (x for x in (body.get("pages") or []) if isinstance(x, dict)),
                    key=lambda x: _safe_int(x.get("page")),
                )
                blocks: list[dict] = []
                for item in items:
                    # 应用加粗注释标签转换（注　　释：）
                    html_text = transform_note_labels(str(item.get("html") or ""))
                    blocks.extend(_html_to_rich_blocks(html_text))
                st = self.server.state
                explicit = body.get("path")
                used_dialog = False
                if explicit:
                    out_path = str(explicit)
                else:
                    # 保存对话框统一交给主线程弹出（tkinter 不能在 HTTP
                    # worker 线程可靠弹窗）；界面已关闭则直接放弃本次导出
                    try:
                        out_path, used_dialog = _ask_export_path(st, fmt)
                    except _ExportAborted:
                        self._send(
                            500,
                            self._json(
                                {"ok": False, "error": "矫正界面已关闭，导出取消"}
                            ),
                            "application/json; charset=utf-8",
                        )
                        return
                    if out_path is None and used_dialog:
                        self._send(
                            200,
                            self._json({"ok": False, "cancelled": True}),
                            "application/json; charset=utf-8",
                        )
                        return
                    if out_path is None:
                        # headless 兜底：当前目录 + 默认文件名（重名自动加序号）
                        # 默认名优先用重命名后的 display_name，回退 history_name（2026-08-30）
                        base = (
                            st.get("display_name")
                            or st.get("history_name")
                            or "矫正导出"
                        ).removesuffix(".pdf")
                        base = (base or "矫正导出").strip() or "矫正导出"
                        out_path = _default_export_path(f"{base}.{fmt}")
                out = Path(out_path)
                out.parent.mkdir(parents=True, exist_ok=True)
                if fmt == "txt":

                    def _txt_line(b: dict) -> str:
                        # 图片块以 [图片] 占位符表示；首行缩进（data-ind=first）
                        # 以全角空格前缀近似（纯文本唯一能承载的版式信息）
                        if b["kind"] == "img":
                            return "[图片]"
                        line = "".join(r["text"] for r in b["runs"])
                        ind = b.get("indent") or {}
                        if ind.get("ind") == "first":
                            n = int(ind.get("indv") or 2)
                            line = "\u3000" * max(1, min(8, n)) + line
                        return line

                    text = "\n\n".join(_txt_line(b) for b in blocks) + "\n"
                    out.write_text(text, encoding=_TXT_ENCODING)
                elif fmt == "md":
                    # Markdown 导出：与前端 htmlToMd 规则一致（见 _build_md）
                    _build_md(blocks, str(out))
                elif fmt == "epub":
                    # epub：标记→文章结构→XHTML→打包（临时目录隔离，完成后清理）
                    import shutil as _sh
                    import tempfile as _tf

                    tmp_dir = _tf.mkdtemp(prefix="ptoe_export_epub_")
                    try:
                        # 浏览器提交的 html 可能含 <div> 块（Chrome contenteditable
                        # 回车产生），apply_markers 只认 p/h1-6——先 sanitize 归一为
                        # <p>（保留对齐/注释/图片 class），与「完成并转换」路径一致；
                        # 否则产出 <p><div class="ptoe-align-center">…</div></p>
                        # 非法嵌套、对齐丢失（2026-08-15）
                        src_items = [
                            {
                                "page": p["page"],
                                "text": sanitize_html(str(p["html"] or "")),
                            }
                            for p in items
                        ]
                        articles = apply_markers(src_items)
                        title = st.get("display_name") or (st.get("history_name") or "矫正导出")
                        structured = {
                            "articles": articles,
                            "pages": src_items,
                            "body": "\n\n".join(
                                (p.get("text") or "").strip()
                                for p in src_items
                                if (p.get("text") or "").strip()
                            ),
                            "paragraphs": [
                                {"page": p["page"], "text": p["text"]}
                                for p in src_items
                                if (p.get("text") or "").strip()
                            ],
                            "meta": {
                                "title": title,
                                "author": "",
                                "language": "zh-CN",
                                "package_epub": True,
                                "epub_version": "3.0",
                            },
                        }
                        from htmlmanage import HTMLConverter

                        result = HTMLConverter(
                            output_dir=tmp_dir, epub_version="3.0"
                        ).convert_document(structured)
                        generated = result.get("epub")
                        if not generated or not Path(generated).is_file():
                            raise RuntimeError(
                                result.get("epub_error") or "EPUB 打包失败"
                            )
                        _sh.copy2(generated, str(out))
                    finally:
                        _sh.rmtree(tmp_dir, ignore_errors=True)
                else:
                    _build_docx(blocks, str(out))
                self._send(
                    200,
                    self._json(
                        {"ok": True, "path": str(out), "used_dialog": used_dialog}
                    ),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path not in ("/api/save", "/api/stage", "/api/finish"):
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8"))
            items = body.get("pages") or []
            state = self.server.state
            # 浏览器可能载入历史版本后新增了会话外页码（如无文件模式打开暂存），
            # 保存/暂存/完成一律按提交内容 upsert，而不是只更新已知页码。
            # S5：写入 pages 与「构建 ordered 快照」共用一把锁，保证与
            # /api/pages 读取、/api/history/load 写入互斥。
            saved = 0
            lock = state.get("pages_lock")
            if lock is not None:
                with lock:
                    for item in items:
                        try:
                            n = int(item.get("page"))
                        except (TypeError, ValueError):
                            continue
                        state["pages"][n] = sanitize_html(str(item.get("html") or ""))
                        saved += 1
                    pages_snapshot = dict(state["pages"])
            else:
                for item in items:
                    try:
                        n = int(item.get("page"))
                    except (TypeError, ValueError):
                        continue
                    state["pages"][n] = sanitize_html(str(item.get("html") or ""))
                    saved += 1
                pages_snapshot = dict(state["pages"])
            # 历史记录名（无文件模式打开历史版本后，保存/暂存/完成沿用该名称）
            if body.get("name"):
                state["history_name"] = str(body.get("name"))
            # 文字纠错状态（保存/暂存/完成时随历史缓存落盘）
            if body.get("proofread"):
                state["proofread"] = {
                    "errors": body["proofread"].get("errors") or {},
                    "original": body["proofread"].get("original") or {},
                    "dismissed": body["proofread"].get("dismissed") or {},
                }
            if body.get("last_proofread_page") is not None:
                try:
                    state["last_proofread_page"] = int(body["last_proofread_page"])
                except (TypeError, ValueError):
                    pass
            # 保存：不新建历史版本，直接覆盖当前缓存（同一份内容反复保存只更新
            # 同一个文件）；暂存/完成并转换仍各生成一个新历史版本（可随时恢复）
            # S4：写入失败返回 False → 前端报错提示（不静默丢数据）
            if path == "/api/save":
                ok = _overwrite_history(state)
            else:
                ok = _write_history_version(state)
            payload = {"ok": ok, "saved": saved}
            if not ok:
                payload["error"] = "历史缓存写入失败（磁盘错误或权限不足？）"
            if path == "/api/finish":
                # 完成并转换：不关闭服务，每次点击都重新转换（on_convert 回调），
                # 用户可留在页面继续修改后再次点击；浏览器关闭才结束等待。
                conv = None
                on_convert = state.get("on_convert")
                if on_convert:
                    ordered = [
                        {"page": n, "text": pages_snapshot[n]}
                        for n in sorted(pages_snapshot)
                    ]
                    # name：浏览器打开的历史记录名（无文件模式下用作 EPUB 标题）
                    with state["convert_lock"]:
                        try:
                            conv = on_convert(ordered, name=body.get("name") or None)
                        except Exception as e:  # noqa: BLE001 - 转换异常回给浏览器提示
                            conv = {"ok": False, "message": str(e)}
                payload["converted"] = conv
            elif path == "/api/stage":
                payload["staged"] = True
            self._send(200, self._json(payload), "application/json; charset=utf-8")
        except Exception as e:  # noqa: BLE001 - 界面出错要回给浏览器而不是崩溃
            self._send(
                500,
                self._json({"ok": False, "error": str(e)}),
                "application/json; charset=utf-8",
            )

    def log_message(self, fmt: str, *args: Any) -> None:
        # 静默访问日志，避免终端刷屏
        return


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def correct_pages(
    pages: list[dict[str, Any]],
    *,
    pdf_path: str | Path | None = None,
    img_dir: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    preview_dpi: int = 90,
    preview_quality: int = 70,
    idle_timeout: int = 600,
    on_convert: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    history: bool = True,
    preload_history: bool = True,
) -> list[dict[str, Any]]:
    """启动手动矫正界面并阻塞，直到浏览器被关闭（或 Ctrl+C）。

    返回与输入同构的校正后 pages 列表：[{'page': int, 'text': str}, ...]，
    按页码升序；text 为 sanitize_html 清洗后的 HTML 片段（含白名单标记）。
    用户按 Ctrl+C 中断时放弃本次矫正结果（保持原 text）继续流程。

    on_convert：可选回调，收到按页码排序的 pages 列表并返回结果 dict。
    点「完成并转换」时（可重复点击）在服务线程中串行调用，结果经
    /api/finish 响应回给浏览器（转换完成/未完成提示）；服务不因一次
    「完成并转换」而关闭，用户可留在页面继续修改后再次点击。
    阻塞结束条件只有：浏览器关闭超过 idle_timeout（自动继续）或 Ctrl+C。

    history：为 True 时按 pdf_path 把矫正内容缓存到本地（data/correction_history/），
    保存/暂存/完成时写入；下次对同一 PDF 运行 --correct 自动加载已修改内容，
    支持对已矫正内容再次手动矫正。

    preload_history：为 True（默认）时，启动界面时用同一 PDF 最新历史版本覆盖
    传入的初始文本（适用于直接矫正/无 OCR 的 correct 命令，便于对已修改内容
    再次矫正）；为 False 时完全使用传入的 pages（适用于 epub 流水线：重新识别
    后的新文本必须优先展示，不能被上一次暂存/保存的历史内容覆盖）。

    idle_timeout：浏览器（页面）被关闭后的等待秒数，超过即自动继续后续流程
    （保留最后一次保存/完成的内容）；默认 600 秒（10 分钟）。
    页面每 30s 发心跳，关闭标签页时发 pagehide 信标，据此监测。
    """
    ordered = sorted(
        (
            {"page": int(p["page"]), "text": str(p.get("text") or "")}
            for p in pages
            if "page" in p and "text" in p
        ),
        key=lambda x: x["page"],
    )
    # 历史缓存：同一 PDF 最新版本的矫正内容优先作为初始内容；
    # preload_history=False 时（重新识别后）不加载历史，避免旧暂存覆盖新识别文本。
    history_pages: dict[str, str] = _history_pages_for_init(
        str(pdf_path), history=history, preload_history=preload_history
    )
    if history_pages:
        loaded = sum(1 for p in ordered if str(p["page"]) in history_pages)
        if loaded:
            print(f"      已加载历史矫正记录（{loaded}/{len(ordered)} 页）")
    state: dict[str, Any] = {
        "pages": {
            # 2026-08-15：传入矫正界面的文本一律按正文展示——OCR 自动结构产生的
            # <h1>-<h6> 标题归一为 <p>（标题由用户在界面手动标记），保证界面所见
            # 与浏览器关闭后的返回结果一致。
            # 2026-08-15 修复：历史缓存内容按原样载入（normalize_headings=False）——
            # 其中可能含用户手动设置的标题，不能再归一为 <p>（否则「保存后重开，
            # 已设置的标题格式丢失」）；OCR 自动标题的归一只在写入历史时做一次
            # （_save_ocr_history），此处仅对无历史的原始 OCR 文本兜底归一。
            p["page"]: _page_text(
                str(history_pages.get(str(p["page"]), p["text"])),
                normalize_headings=str(p["page"]) not in history_pages,
            )
            for p in ordered
        },
        # S5：pages 读写共用锁（/api/pages、/api/history/load、保存/暂存/完成）
        "pages_lock": threading.Lock(),
        "finished": threading.Event(),
        # P2：预览 JPEG LRU 缓存（OrderedDict，上限 _PREVIEW_CACHE_MAX）；
        # preview_doc/preview_doc_lock 复用同一 fitz.Document，避免每页重开 PDF
        "preview_cache": OrderedDict(),
        "preview_doc": None,
        "preview_doc_lock": threading.Lock(),
        "pdf_path": str(pdf_path) if pdf_path else None,
        "img_dir": str(img_dir) if img_dir else None,
        "preview_dpi": preview_dpi,
        "preview_quality": preview_quality,
        # 浏览器存活监测（关闭浏览器后自动继续）
        "last_heartbeat": time.monotonic(),
        "gone_at": None,
        "idle_timeout": float(idle_timeout),
        "auto_finished": False,
        # 完成并转换（可重复）与本地历史缓存（同一 PDF 多版本）
        "on_convert": on_convert,
        "convert_lock": threading.Lock(),
        # 无文件会话（pdf_path 为 None）也允许暂存/保存：用会话前缀 manual_<随机>
        # 落盘历史缓存，之后可再次打开；名称默认「手动录入」。
        "history_prefix": (
            _history_prefix(str(pdf_path)) if pdf_path else f"manual_{uuid4().hex[:8]}"
        )
        if history
        else None,
        "history_name": None if pdf_path else "手动录入",
        "history_lock": threading.Lock(),
        # 导出保存对话框队列：tkinter 只能在主线程可靠弹窗——/api/export
        # handler 把请求入队，主循环 _drain_dialog_queue 弹框并回填结果
        "dlg_queue": [],
        "dlg_lock": threading.RLock(),
        # 文字纠错状态（保存/暂存/完成时随历史缓存落盘，加载时恢复）
        "proofread": {"errors": {}, "original": {}, "dismissed": {}},
        "last_proofread_page": None,
        # 预渲染页数上限：可经 config.json 顶层键 prerender_max_pages 覆盖
        "prerender_max_pages": _resolve_prerender_max(),
        "embedded_images": {},
    }
    server = ThreadingHTTPServer((host, port), _CorrectionHandler)
    server.daemon_threads = True
    server.state = state
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()
    # 后台渐进式预渲染预览图：用户在浏览器编辑期间把所有页渲染好，
    # 首次保存/暂存/完成时 _build_embedded_images 全命中缓存直接返回，
    # 不再阻塞按钮。每页单独持锁 + 50ms 间隔，不阻塞 UI 预览/请求线程。
    threading.Thread(
        target=_prerender_embedded_images, args=(state,), daemon=True
    ).start()
    # 多进程预热预览图磁盘缓存：大书（≥_WARM_MIN_PAGES）提前并行渲染到
    # <pdf目录>/preview_cache/<prefix>_<dpi>/，/preview 命中后免渲染锁竞争。
    # 同一 (pdf, dpi) 只预热一次；线程内自检页数阈值，小书/异常静默跳过。
    if pdf_path:
        _warm_key = (str(pdf_path), int(preview_dpi))
        with _preview_warm_started:
            _warm_new = _warm_key not in _preview_warmed_keys
            if _warm_new:
                _preview_warmed_keys.add(_warm_key)
        if _warm_new:
            threading.Thread(
                target=_warm_preview_cache, args=(state,), daemon=True
            ).start()
    url = f"http://{server.server_address[0]}:{server.server_address[1]}/"
    print(f"      矫正界面已启动: {url}（对比原图与识别文字，完成后点「完成并转换」）")
    # 记录服务信息 sidecar，供 GUI 配置中心发现并恢复已存活的矫正界面
    _write_server_info(server.server_address[1])
    if open_browser:
        webbrowser.open(url)
    try:
        # 浏览器关闭监测：页面每 30s 发心跳；关闭标签页时发 pagehide 信标。
        # 信标确认关闭或心跳失联超过 idle_timeout 秒后，自动继续后续流程。
        stale_since: float | None = None
        while not state["finished"].is_set():
            if state["finished"].wait(0.5):
                break  # 浏览器被判定关闭，自动继续（「完成并转换」不再关闭服务）
            # 导出保存对话框只能在主线程弹出（tkinter 线程安全），
            # 逐轮取走队列里的请求弹框，阻塞直到用户选择/取消
            _drain_dialog_queue(state)
            gone, stale_since = _browser_gone(state, stale_since=stale_since)
            if gone:
                state["auto_finished"] = True
                state["finished"].set()
                break
        if state.get("auto_finished"):
            idle = float(state.get("idle_timeout") or 600)
            secs = int(idle)
            print(
                f"      浏览器已关闭超过 {secs // 60} 分 {secs % 60} 秒，"
                "自动继续后续流程（未保存的修改已丢弃，保留已保存内容）"
            )
    except KeyboardInterrupt:
        print("\n      手动矫正被中断，放弃本次矫正结果，继续原流程")
    finally:
        # 清除服务信息 sidecar（仅当前进程 pid 匹配时删除）
        _clear_server_info()
        server.shutdown()
        server.server_close()
        serve_thread.join(timeout=5)
        # 唤醒可能阻塞在保存对话框上的 /api/export 请求（handler 返回 500）
        _abort_dialog_queue(state)
        # P2：关闭复用的 fitz.Document，释放文件句柄
        doc = state.get("preview_doc")
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
            state["preview_doc"] = None
    # S5：返回前对 pages 加锁快照
    lock = state.get("pages_lock")
    if lock is not None:
        with lock:
            out = [
                {"page": n, "text": state["pages"][n]} for n in sorted(state["pages"])
            ]
    else:
        out = [{"page": n, "text": state["pages"][n]} for n in sorted(state["pages"])]
    return out


# ---------------------------------------------------------------------------
# 内嵌 HTML 界面
#   虚拟列表（仅渲染视口附近行，DOM 与页数无关，支撑 1000+ 页）；
#   选中文字弹出快捷菜单（点击菜单按钮后菜单保持隐藏，不再自动弹出）；
#   可配置快捷键（每个操作绑定一个组合键，localStorage 持久化）；
#   标记按钮：全文 / 段落（插入到光标处；段落标记段首=与上一段合并、
#   段尾=与下一段合并）；
#   布局：左右两栏等高（CSS grid 拉伸），图片栏完整显示整张原图；
#   点「完成并转换」后弹出完成状态提示，并询问是否关闭当前页面。
# ---------------------------------------------------------------------------

_UI_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>矫正 - ptoe</title>
<style>
:root{--accent:#2f6fed;--border:#d8dee6;--bg:#f4f6f9;--editor-font-size:14px;}
*{box-sizing:border-box}
body{margin:0;font-family:"Microsoft YaHei",system-ui,sans-serif;background:var(--bg);color:#1c2733;}
#toolbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:4px;padding:5px 8px;background:#fff;border-bottom:1px solid var(--border);flex-wrap:wrap;font-size:12px;}
#toolbar .title{font-weight:700;margin-right:10px;}
#toolbar .spacer{flex:1;}
#toolbar .sep{width:1px;height:22px;background:var(--border);margin:0 4px;}
#toolbar label{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:#5a6b7c;}
#prCount{display:inline-flex;align-items:center;gap:2px;font-size:12px;color:#5a6b7c;white-space:nowrap;margin-left:4px;}
#prCountNum{color:#e02020;font-weight:700;}
#toolbar input[type=number]{width:64px;padding:3px 5px;border:1px solid var(--border);border-radius:4px;font:inherit;}
/* U1：按功能分组的浅色区块，替代细分隔线；主操作组不折行、右端常驻 */
#toolbar .tb-group{display:inline-flex;align-items:center;gap:3px;padding:2px 6px;background:#f4f6f9;border:1px solid #e4e9f0;border-radius:8px;white-space:nowrap;}
#toolbar .tb-group .tb-label{font-size:11px;color:#8a97a6;margin-right:2px;user-select:none;}
#toolbar .tb-main{flex-wrap:nowrap;background:transparent;border-color:transparent;margin-left:auto;}
#toolbar .ic-btn{width:26px;height:26px;padding:0;display:inline-flex;align-items:center;justify-content:center;}
/* 紧凑尺寸：工具栏内文字按钮/下拉/输入框缩小，配合 flex-wrap 保证最多两行 */
#toolbar button{padding:3px 8px;font-size:12px;}
#toolbar select{padding:3px 5px;font-size:12px;}
#toolbar .ic-btn:disabled{opacity:.4;cursor:default;}
button{font:inherit;padding:5px 11px;border:1px solid var(--border);border-radius:4px;background:#fff;cursor:pointer;}
button:hover{border-color:var(--accent);color:var(--accent);}
button.active{border-color:var(--accent);background:#eef3fb;color:var(--accent);}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent);}
button.primary:hover{background:#2256c2;color:#fff;}
button:disabled{opacity:.5;cursor:not-allowed;}
select{font:inherit;padding:5px 8px;border:1px solid var(--border);border-radius:4px;background:#fff;}
#status{font-size:12px;color:#5a6b7c;}
#pos{font-size:12px;color:#8a97a6;white-space:nowrap;}
/* U2：三色 toast 提示（成功/失败/警告），顶部居中，3s 自动消失 */
#toast{position:fixed;top:60px;left:50%;transform:translateX(-50%);z-index:90;display:flex;flex-direction:column;gap:6px;align-items:center;pointer-events:none;}
.toast{background:#1c2733;color:#fff;font-size:13px;line-height:1.5;padding:8px 16px;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.25);opacity:0;transform:translateY(-6px);transition:opacity .2s,transform .2s;max-width:70vw;}
.toast.show{opacity:1;transform:translateY(0);}
.toast.ok{background:#1a7f37;}
.toast.fail{background:#c0392b;}
.toast.warn{background:#b8860b;}
/* 预览图加载提示胶囊（底部居中）：加载中显示，完成后闪现「加载完成」 */
#loadHint{position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(8px);z-index:70;background:#334155;color:#fff;font-size:13px;padding:6px 14px;border-radius:16px;opacity:0;pointer-events:none;transition:opacity .18s ease,transform .18s ease;box-shadow:0 2px 10px rgba(0,0,0,.25);}
#loadHint.show{opacity:1;transform:translateX(-50%) translateY(0);}
#loadHint.done{background:#166534;}
/* 行级微光占位：预览图未就绪时填充图片区域，替代纯白 */
.ptoe-img-loading{background:linear-gradient(100deg,#eceff3 40%,#f7f9fb 50%,#eceff3 60%);background-size:200% 100%;animation:ptoeShimmer 1.1s linear infinite;}
@keyframes ptoeShimmer{to{background-position:-200% 0;}}
button.loading::after{content:'';display:inline-block;width:11px;height:11px;margin-left:8px;border:2px solid rgba(255,255,255,.45);border-top-color:#fff;border-radius:50%;animation:ptoe-spin .8s linear infinite;vertical-align:-2px;}
@keyframes ptoe-spin{to{transform:rotate(360deg);}}
/* U3：hintbar 可折叠（✕ 关闭，localStorage 记忆） */
#hintbar{padding:6px 14px;font-size:12px;color:#5a6b7c;background:#eef3fb;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;}
#hintbar .hint-text{flex:1;}
#hintClose{flex:none;width:22px;height:22px;padding:0;line-height:1;border:none;background:transparent;color:#8a97a6;font-size:14px;border-radius:4px;}
#hintClose:hover{background:#dfe7f3;color:#1c2733;border:none;}
#hintbar.hidden{display:none;}
#pages{position:relative;overflow-anchor:none;}
/* 宽度基准动态行高（2026-08）：行高由左侧图片按栏宽等比撑出（服务端 /api/pages
   下发各页原始宽高，前端预计算 heights[]，未挂载行前缀和也精确 → 跳转瞬时定位）。
   每页用各自真实宽高比，个别异常大小页面只影响自身行高，不干扰其他页。
   文字窗内容超出图片高度时在行内滚动（height:0+min-height:100% 不参与撑行）。 */
.page-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;align-items:stretch;background:#fff;border:1px solid var(--border);border-radius:8px;padding:12px;}
.page-head{grid-column:1 / -1;font-size:12px;color:#5a6b7c;border-bottom:1px dashed var(--border);padding-bottom:6px;}
.img-panel{position:relative;min-width:0;overflow:hidden;background:#fff;border:1px solid var(--border);border-radius:4px;padding:4px;}
.img-panel img{width:100%;height:auto;display:block;background:#fff;cursor:zoom-in;}
.badge{position:absolute;top:8px;left:8px;background:rgba(0,0,0,.55);color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;pointer-events:none;}
.editable{height:0;min-height:100%;overflow-y:auto;padding:10px 14px;border:1px solid var(--border);border-radius:4px;line-height:1.7;font-size:var(--editor-font-size);outline:none;}
.editable:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(47,111,237,.15);}
.editable h1{font-size:1.45em;} .editable h2{font-size:1.28em;} .editable h3{font-size:1.12em;}
.editable h4,.editable h5,.editable h6{font-size:1.02em;}
.ptoe-align-left{text-align:left;} .ptoe-align-center{text-align:center;} .ptoe-align-right{text-align:right;}
.ptoe-marker{background:#fff3bf;border:1px solid #e8c24a;border-radius:3px;padding:0 4px;font-size:12px;color:#8a6d00;cursor:help;user-select:all;}
.ptoe-search{background:#fff1a8;border-radius:2px;padding:0 2px;color:inherit;}
.editable mark.ptoe-search{background:#fff1a8;color:inherit;border-radius:2px;padding:0 2px;}
 .editable .ptoe-note{font-size:12px;color:#556677;}
.pop-btn:hover{background:#eef3fb;border-color:var(--accent);}
/* 全局延迟提示：悬停超过设定时间才显示（含快捷键），延迟可在「快捷键」设置中调整 */
#tip{position:fixed;z-index:80;display:none;background:#1c2733;color:#fff;font-size:12px;line-height:1.5;padding:5px 9px;border-radius:4px;max-width:320px;pointer-events:none;}
#tip .tip-key{color:#bcd0e5;margin-left:6px;white-space:nowrap;}
#popup{position:fixed;z-index:60;display:none;flex-wrap:wrap;gap:4px;max-width:360px;padding:6px 8px;background:#fff;border:1px solid var(--border);border-radius:8px;box-shadow:0 4px 18px rgba(0,0,0,.22);}
.pop-btn{min-width:34px;height:30px;padding:0 8px;font-size:13px;border:1px solid var(--border);background:#fff;color:#1c2733;border-radius:6px;cursor:pointer;line-height:1;}
/* 右键上下文菜单（2026-08-08）：浅色主题、圆角、分组分隔线、二级菜单向右展开（右缘不足向左） */
#contextMenu{position:fixed;z-index:80;min-width:172px;padding:5px;background:#fff;border:1px solid var(--border);border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.22);display:flex;flex-direction:column;gap:1px;user-select:none;}
#contextMenu[hidden]{display:none;} /* 必须显式覆盖：作者样式 #contextMenu{display:flex} 会压过 UA 的 [hidden]{display:none} */
.ctx-item{display:flex;align-items:center;justify-content:space-between;gap:10px;width:100%;padding:7px 12px;border:none;background:transparent;color:#1c2733;font-size:13px;text-align:left;border-radius:5px;cursor:pointer;white-space:nowrap;font-family:inherit;}
.ctx-item:hover{background:#eef3fb;color:var(--accent);}
.ctx-arrow{font-size:11px;color:#8a97a6;}
.ctx-sub{position:relative;}
.ctx-submenu{position:absolute;top:-5px;left:100%;margin-left:4px;min-width:150px;padding:5px;background:#fff;border:1px solid var(--border);border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.22);display:none;flex-direction:column;gap:1px;z-index:81;}
.ctx-sub.open > .ctx-submenu{display:flex;}
.ctx-submenu.ctx-left{left:auto;right:100%;margin-left:0;margin-right:4px;}
/* 视口下缘不足：二级菜单改为向上对齐（底边与父项底边齐平） */
.ctx-submenu.ctx-up{top:auto;bottom:-5px;}
/* hover 间隙桥：父项与二级菜单之间的 4px margin 用透明伪元素补上，
   鼠标横移过间隙不会触发 mouseleave（配合 JS 的 200/300ms hover-intent 延时） */
.ctx-submenu::before{content:'';position:absolute;top:0;bottom:0;left:-8px;width:8px;}
.ctx-submenu.ctx-left::before{left:auto;right:-8px;}
.ctx-sep{height:1px;background:var(--border);margin:4px 2px;}
.ctx-empty{padding:7px 12px;font-size:12px;color:#9aa7b4;white-space:nowrap;cursor:default;}
/* 格式刷：激活态高亮 + 激活时光标变复制样式 */
.pop-btn.active{background:#ffe9a8;outline:1px solid #d9a400;}
.pop-rule-wrap{position:relative;display:flex;flex-wrap:nowrap;gap:4px;align-items:center;min-width:224px;}
.pop-rule-sub{display:none;position:absolute;left:100%;top:-4px;margin-left:4px;min-width:120px;max-height:260px;overflow-y:auto;background:#fff;border:1px solid var(--border);border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,.15);z-index:61;padding:4px 0;}
.pop-rule-sub .ctx-item{display:block;width:100%;text-align:left;padding:4px 10px;border:none;background:none;cursor:pointer;font-size:13px;white-space:nowrap;}
.pop-rule-sub .ctx-item:hover{background:#f0f0f0;}
.pop-rule-sub .ctx-empty{padding:4px 10px;color:#999;font-size:12px;}
body.paint-mode{cursor:copy;}
/* 文字纠错：错误标注（删除线红色）+ 候选正确字（绿色）+ 确认悬浮窗 */
.ptoe-err{text-decoration:line-through;color:#c00;background:#ffe0e0;padding:0 3px;border-radius:3px;cursor:pointer;}
.ptoe-fix{color:#080;font-size:0.9em;}
#errPopup{position:fixed;z-index:65;display:none;background:#fff;border:1px solid #ccc;border-radius:6px;padding:4px;box-shadow:0 2px 8px rgba(0,0,0,.2);gap:6px;}
/* 图片设置弹窗：点击编辑区内的图片弹出，调整大小/位置/删除 */
#imgPopup{position:fixed;z-index:65;display:none;flex-direction:column;gap:6px;padding:10px;background:#fff;border:1px solid #ccc;border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.2);min-width:140px;}
.img-pop-btn{background:#f5f5f5;border:1px solid #ddd;border-radius:4px;padding:3px 10px;font-size:13px;cursor:pointer;}
.img-pop-btn:hover{background:#eef3fb;border-color:var(--accent);}
#errOk{background:#2e8b57;color:#fff;border:none;border-radius:4px;padding:2px 10px;cursor:pointer;font-size:14px;}
#errNo{background:#c0392b;color:#fff;border:none;border-radius:4px;padding:2px 10px;cursor:pointer;font-size:14px;}
/* 文字纠错下拉菜单 */
#proofreadMenu{position:fixed;z-index:70;background:#fff;border:1px solid #ddd;border-radius:6px;box-shadow:0 2px 10px rgba(0,0,0,.15);min-width:120px;padding:4px;display:none;}
#proofreadMenu button{display:block;width:100%;text-align:left;padding:6px 10px;border:none;background:none;cursor:pointer;border-radius:4px;font-size:13px;}
#proofreadMenu button:hover{background:#f0f0f0;}
/* 下拉指示符：小号低对比三角，提示「校」为下拉菜单；菜单展开时按钮高亮 */
#proofreadBtn .ptoe-caret{font-size:9px;color:#8a97a6;margin-left:3px;vertical-align:1px;}
#popup .sep{width:100%;height:0;border-top:1px solid var(--border);margin:2px 0;}
.ic-b{font-weight:700;} .ic-i{font-style:italic;font-family:Georgia,'Times New Roman',serif;} .ic-h{font-weight:700;} .ic-p{font-weight:600;} .ic-t{font-weight:600;} .ic-n{font-size:12px;color:#556677;}
/* 左侧预览图上的「图」按钮：把当前显示的图片插入右侧文字光标处 */
.img-insert{position:absolute;right:8px;bottom:8px;z-index:5;padding:3px 10px;font-size:13px;border:1px solid var(--border);background:#fff;color:#1c2733;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.18);cursor:pointer;}
.img-insert:hover{background:#eef3fb;border-color:var(--accent);}
.img-crop{position:absolute;right:8px;bottom:34px;z-index:5;padding:3px 10px;font-size:13px;border:1px solid var(--border);background:#fff;color:#1c2733;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.18);cursor:pointer;}
.img-crop:hover{background:#eef3fb;border-color:var(--accent);}
.crop-layer{position:absolute;inset:0;z-index:30;background:rgba(0,0,0,.18);border-radius:4px;touch-action:none;}
.crop-box{position:absolute;box-shadow:0 0 0 9999px rgba(0,0,0,.55);border:1px dashed #fff;cursor:move;touch-action:none;}
.crop-box .crop-handle{position:absolute;width:11px;height:11px;background:#fff;border:1px solid #2a5db0;border-radius:2px;}
.crop-box .crop-handle.tl{top:-6px;left:-6px;cursor:nwse-resize;}
.crop-box .crop-handle.tr{top:-6px;right:-6px;cursor:nesw-resize;}
.crop-box .crop-handle.bl{bottom:-6px;left:-6px;cursor:nesw-resize;}
.crop-box .crop-handle.br{bottom:-6px;right:-6px;cursor:nwse-resize;}
.crop-actions{position:absolute;left:50%;bottom:8px;transform:translateX(-50%);display:flex;gap:6px;z-index:31;}
.crop-actions button{padding:3px 10px;font-size:12px;}
/* 插入图片：全画幅（占满文字宽度）/ 局部（按原尺寸居中）
   重构（2026-08-10）：避免 specificity war，img 不设宽度，
   改用尺寸 class（ptoe-img-w*）作为唯一宽度控制；
   p 用 text-align 控制位置（inline-block img 响应对齐） */
.editable p.ptoe-img-full{text-indent:0;}
.editable p.ptoe-img-fit{text-indent:0;}
.editable p.ptoe-img-full img{display:inline-block;max-width:100%;height:auto;vertical-align:middle;}
.editable p.ptoe-img-fit img{display:inline-block;max-width:100%;height:auto;vertical-align:middle;}
/* 尺寸 class：唯一宽度控制（全画幅默认 w100，局部默认无尺寸=原图） */
.editable .ptoe-img-w25{width:25%}
.editable .ptoe-img-w50{width:50%}
.editable .ptoe-img-w75{width:75%}
.editable .ptoe-img-w100{width:100%}
/* 位置 class：p 上 text-align 控制 img 对齐 */
.editable p.ptoe-img-left{text-align:left}
.editable p.ptoe-img-center{text-align:center}
.editable p.ptoe-img-right{text-align:right}
/* 行内图片（2026-08-10）：直接嵌在文字流中（无 <p> 包裹），
   vertical-align 控制上下对齐；尺寸 class 同样生效 */
.editable img.ptoe-img-inline{display:inline-block;max-width:100%;height:auto;vertical-align:middle;}
.editable img.ptoe-img-vtop{vertical-align:top;}
.editable img.ptoe-img-vmid{vertical-align:middle;}
.editable img.ptoe-img-vbot{vertical-align:bottom;}
/* 搜索 / 替换弹窗（工具栏「搜」按钮打开；结果列表点击跳转、↑↓上一个/下一个） */
#searchModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
.search-modal{max-width:640px;width:94%;display:flex;flex-direction:column;}
/* 导出弹窗（工具栏「导出」按钮打开；复用 .search-modal 布局） */
#exportModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#indentModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
.indent-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;margin:10px 0;font-size:13px;}
.indent-grid label{display:flex;align-items:center;gap:6px;white-space:nowrap;}
.indent-grid input[type="number"]{width:72px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;}
.indent-grid select{padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;background:#fff;}
.indent-preview{border:1px dashed var(--border);border-radius:6px;padding:10px 12px;margin:8px 0 12px;min-height:56px;overflow:auto;background:#fafbfc;}
.indent-preview p{margin:0;}
.export-desc{font-size:13px;color:#5a6b7c;margin:0 0 14px;line-height:1.6;}
.export-actions{display:flex;gap:10px;justify-content:flex-end;}
.search-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;}
.search-head h3{margin:0;}
.search-head .x-btn{width:26px;height:26px;padding:0;line-height:1;border:none;background:transparent;color:#8a97a6;font-size:16px;border-radius:4px;cursor:pointer;}
.search-head .x-btn:hover{background:#dfe7f3;color:#1c2733;}
.search-row{display:flex;align-items:center;gap:8px;margin-bottom:8px;}
.search-row input[type="text"]{flex:1;min-width:0;padding:6px 8px;border:1px solid var(--border);border-radius:5px;font:inherit;}
.search-regex{display:inline-flex;align-items:center;gap:3px;font-size:13px;color:#33414f;white-space:nowrap;}
.search-nav{display:flex;align-items:center;gap:8px;margin:2px 0 8px;}
.search-nav button{width:32px;height:28px;padding:0;border:1px solid var(--border);background:#fff;border-radius:5px;cursor:pointer;font-size:14px;}
.search-nav button:hover{background:#eef3fb;border-color:var(--accent);}
#searchPos{font-size:12px;color:#5a6b7c;min-width:70px;text-align:center;}
#searchList{max-height:45vh;overflow:auto;border:1px solid var(--border);border-radius:6px;padding:6px;background:#fafbfc;font-size:12px;}
.sr-head{display:flex;align-items:center;gap:8px;padding:2px 0 6px;color:#5a6b7c;font-weight:600;}
.sr-item{padding:6px 8px;border:1px solid var(--border);border-radius:6px;margin-bottom:6px;cursor:pointer;background:#fff;}
.sr-item:hover{border-color:var(--accent);background:#f2f7ff;}
.sr-item.current{border-color:var(--accent);background:#eaf2ff;box-shadow:0 0 0 1px var(--accent);}
.sr-page{font-size:11px;color:#5a6b7c;margin-bottom:2px;}
.sr-ctx{color:#33414f;line-height:1.5;word-break:break-all;}
.sr-ctx mark{background:#ffe08a;color:#5c4000;border-radius:2px;padding:0 2px;}
.sr-empty{color:#8a97a6;padding:6px 2px;}
#modalBg{position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#finishModalBg{position:fixed;inset:0;z-index:70;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#historyModalBg{position:fixed;inset:0;z-index:70;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#helpModalBg{position:fixed;inset:0;z-index:65;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#historyTable{table-layout:fixed;}
#historyTable th{position:sticky;top:0;background:#f4f6f9;}
#historyTable td{word-break:break-all;vertical-align:top;}
/* 历史记录行：文件名支持 inline rename、路径三行 clamp、✎ 图标 */
.hist-name-display {display:inline-block;max-width:calc(100% - 20px);word-break:break-all;line-height:1.4;}
.hist-rename-icon {display:inline-block;cursor:pointer;margin-left:4px;color:#666;font-size:12px;vertical-align:middle;}
.hist-rename-input {font-size:12px;padding:2px 3px;margin:2px 0;background:#fafafa;border:1px solid #ddd;border-radius:3px;width:auto;}
/* 路径列：最多 3 行，悬停显示完整路径 */
#historyTable td.hist-path {display: -webkit-box;-webkit-line-clamp: 3;-webkit-box-orient: vertical;overflow: hidden;cursor:help;}
.modal{background:#fff;border-radius:10px;padding:18px 22px;max-width:520px;width:92%;max-height:80vh;overflow:auto;}
.modal h3{margin:0 0 10px;}
.modal h4{margin:14px 0 6px;font-size:14px;color:#1c2733;}
.help-table{width:100%;border-collapse:collapse;font-size:13px;}
.help-table td{padding:4px 8px;border-bottom:1px solid var(--border);vertical-align:top;}
.help-table td:first-child{white-space:nowrap;color:#33414f;font-weight:600;}
#shortcutTable{width:100%;border-collapse:collapse;}
#shortcutTable td{padding:6px 8px;border-bottom:1px solid var(--border);font-size:14px;}
#shortcutTable tr{cursor:pointer;}
#shortcutTable tr:hover td{background:#f7fafd;}
kbd{background:#eef1f5;border:1px solid #c9d1da;border-radius:3px;padding:1px 6px;font-size:12px;font-family:inherit;}
#closeSettings{margin-top:12px;}
#formatRulesModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#formatRulesTable{width:100%;border-collapse:collapse;font-size:13px;}
#formatRulesTable td{padding:6px 8px;border-bottom:1px solid var(--border);vertical-align:top;}
#formatRulesTable tr:hover td{background:#f7fafd;}
#formatRulesTable .fr-name{font-weight:600;white-space:nowrap;}
#formatRulesTable .fr-sum{color:#5a6b7c;font-size:12px;}
#formatRulesTable .fr-order{color:#5a6b7c;font-size:12px;white-space:nowrap;}
#formatRulesTable button{padding:2px 8px;font-size:12px;}
#formatRulesTable button:disabled{opacity:.45;cursor:default;}
#formatRulesModalBg .fr-opts{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:13px;}
#formatRulesModalBg .fr-opts label{display:inline-flex;align-items:center;gap:3px;color:#33414f;}
#formatRulesModalBg input[type=text]{padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;}
#formatRulesModalBg select{padding:3px 5px;border:1px solid var(--border);border-radius:4px;font:inherit;}
#formatRulesModalBg .fr-cond{font-size:13px;color:#33414f;}
#frRuleModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#frFmtPopupBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#frRuleModalBg .fr-opts{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:13px;}
#frRuleModalBg .fr-opts label{display:inline-flex;align-items:center;gap:3px;color:#33414f;}
#frFmtPopupBg .fr-opts{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:13px;}
#frFmtPopupBg .fr-opts label{display:inline-flex;align-items:center;gap:3px;color:#33414f;}
#frRuleModalBg input[type=text]{padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;}
#frRuleModalBg select{padding:3px 5px;border:1px solid var(--border);border-radius:4px;font:inherit;}
.fr-cond-row{display:flex;align-items:center;gap:6px;margin:5px 0;flex-wrap:wrap;}
.fr-cond-row select{padding:3px 5px;border:1px solid var(--border);border-radius:4px;font:inherit;}
.fr-cond-row input[type=text]{padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;width:200px;}
.fr-cond-row button{padding:2px 8px;font-size:12px;}
.fr-cond-row button:disabled{opacity:.45;cursor:default;}
.fr-tags{display:inline-flex;flex-wrap:wrap;gap:2px;align-items:center;}
.fr-tag{display:inline-block;background:#e8f1fb;color:#1a5fb4;border:1px solid #bcd6f0;border-radius:3px;padding:1px 6px;font-size:12px;margin:2px;}
.fr-tag-none{background:#f0f2f4;color:#5a6b7c;border-color:#d5dbe1;}
.fr-tags-empty{color:#9aa7b4;font-size:12px;}
@media (max-width:900px){
  /* 窄屏单列布局：回退自然高度（图上文下纵向堆叠），文字窗不裁剪 */
  .page-row{grid-template-columns:1fr;}
  .img-panel img{width:100%;}
  .editable{font-size:calc(var(--editor-font-size) + 2px);height:auto;min-height:160px;overflow-y:visible;}
}
</style>
</head>
<body>
<div id="toolbar">
  <div class="tb-group" role="group" aria-label="格式">
    <button type="button" class="ic-btn" data-op="bold" onmousedown="event.preventDefault()" title="粗体" aria-label="粗体"><span class="ic-b">B</span></button>
    <button type="button" class="ic-btn" data-op="italic" onmousedown="event.preventDefault()" title="斜体" aria-label="斜体"><span class="ic-i">I</span></button>
    <button type="button" class="ic-btn" data-op="heading" onmousedown="event.preventDefault()" title="标题：正文↔一级标题↔…↔六级标题循环" aria-label="标题"><span class="ic-h">标</span></button>
    <button type="button" class="ic-btn" data-op="p" onmousedown="event.preventDefault()" title="正文：转为普通段落" aria-label="正文"><span class="ic-p">正</span></button>
    <button type="button" class="ic-btn" data-op="remove" onmousedown="event.preventDefault()" title="清除格式" aria-label="清除格式"><span class="ic-t">清</span></button>
    <button type="button" class="ic-btn" data-op="note" onmousedown="event.preventDefault()" title="注释：把当前块设为注释（小字灰色）" aria-label="注释">注</button>
    <button type="button" class="ic-btn" data-op="strip_ws" onmousedown="event.preventDefault()" title="去空（去除段落内全部空白，保留换行）" aria-label="去空">去空</button>
    <button type="button" class="ic-btn" id="colorBtn" onmousedown="event.preventDefault()" title="文本颜色" aria-label="文本颜色">色</button>
    <button type="button" class="ic-btn" id="formatBrushBtn" onmousedown="event.preventDefault()" title="格式刷" aria-label="格式刷">刷</button>
    <button type="button" class="ic-btn" id="formatRulesBtn" onmousedown="event.preventDefault()" title="格式规则：对选中文字一键应用自定义规则（可多条叠加 / 条件分支；Ctrl+Shift+Q）" aria-label="格式规则">规</button>
  </div>
<div class="tb-group" role="group" aria-label="对齐">
     <button type="button" class="ic-btn" data-op="align_left" onmousedown="event.preventDefault()" title="居左" aria-label="居左">左</button>
     <button type="button" class="ic-btn" data-op="align_center" onmousedown="event.preventDefault()" title="居中" aria-label="居中">中</button>
     <button type="button" class="ic-btn" data-op="align_right" onmousedown="event.preventDefault()" title="居右" aria-label="居右">右</button>
     <button type="button" class="ic-btn" data-op="flush" onmousedown="event.preventDefault()" title="顶格" aria-label="顶格">顶格</button>
     <button type="button" class="ic-btn" data-op="indent" onmousedown="event.preventDefault()" title="缩进" aria-label="缩进">缩进</button>
     <button type="button" class="ic-btn" id="indentDlgBtn" onmousedown="event.preventDefault()" title="段落设置：左/右缩进、首行/悬挂缩进、段前段后与行距（导出 EPUB 生效）" aria-label="段落设置">¶</button>
   </div>
  <div class="tb-group" role="group" aria-label="标记">
    <button type="button" class="ic-btn" data-op="marker_full" onmousedown="event.preventDefault()" title="全文标记：当前文章到此结束，后续内容属于新文章（开新页）" aria-label="全文标记">篇</button>
    <button type="button" class="ic-btn" data-op="marker_note" onmousedown="event.preventDefault()" title="注释标记：插入到光标处，由对应注释段落替换（数量需一一匹配）" aria-label="注释标记">释</button>
    <button type="button" class="ic-btn" data-op="marker_join" onmousedown="event.preventDefault()" title="段落标记：插入到光标处；段首=与上一段合并，段尾=与下一段合并" aria-label="段落标记">段</button>
    <button type="button" class="ic-btn" data-op="marker_page" onmousedown="event.preventDefault()" title="换页标记：从此处之后的内容显示在新的一页" aria-label="换页标记">页</button>
  </div>
  <div class="tb-group" role="group" aria-label="转换">
    <button type="button" id="toSimplifiedBtn" title="把全部页面文字转为简体（繁体→简体）">繁→简</button>
    <button type="button" id="toTraditionBtn" title="把全部页面文字转为繁体（简体→繁体）">简→繁</button>
    <button type="button" id="mdToggleBtn" title="切换 Markdown 源码 / 富文本编辑模式（详见帮助）">Markdown</button>
  </div>
  <div class="tb-group" role="group" aria-label="文本">
    <button type="button" id="cleanBtn" title="智能清理：合并被 OCR 拆散的小段落、清除段首 #/* 等符号、归一化中英文标点、移除残留的 HTML 标签">清理</button>
    <button type="button" id="proofreadBtn" title="文字纠错下拉菜单：校正当前页 / 应用全部候选 / 清除标注 / 回退原文" aria-label="文字纠错">校 <span class="ptoe-caret">▾</span></button>
  </div>
  <div class="tb-group" role="group" aria-label="撤销重做">
    <button type="button" id="undoBtn" class="ic-btn" onmousedown="event.preventDefault()" disabled title="撤回上一步（Ctrl+Z）" aria-label="撤回（Ctrl+Z）">↶</button>
    <button type="button" id="redoBtn" class="ic-btn" onmousedown="event.preventDefault()" disabled title="前进下一步（Ctrl+Y / Ctrl+Shift+Z）" aria-label="前进（Ctrl+Y）">↷</button>
  </div>
  <div class="tb-group" role="group" aria-label="图片">
    <select id="imgModeSel" hidden title="插入图片的显示模式：全画幅=占满文字宽度，局部=按原尺寸居中，行内=嵌在文字中间（50% 宽度）">
      <option value="full">全画幅</option>
      <option value="fit">局部</option>
      <option value="inline">行内</option>
    </select>
    <button type="button" id="imgExternalBtn" onmousedown="event.preventDefault()" title="从本地文件选择图片，插入到文字光标处">外部</button>
    <input type="file" id="imgExternalInput" accept="image/*" style="display:none"/>
  </div>
  <div class="tb-group" role="group" aria-label="搜索替换">
    <button type="button" id="searchOpenBtn" class="primary" title="搜索/替换全部页面：弹出窗口显示所有匹配结果，支持上一个/下一个跳转、替换当前与全部替换">搜</button>
  </div>
  <div class="tb-group" role="group" aria-label="字号与跳转">
    <label>字号 <select id="fontSizeSel">
      <option value="12">12</option><option value="13">13</option><option value="14" selected>14</option>
      <option value="15">15</option><option value="16">16</option><option value="17">17</option>
      <option value="18">18</option><option value="20">20</option>
    </select></label>
    <label>跳转 <input type="number" id="pageJump" min="1" placeholder="页码"></label>
    <button type="button" id="jumpBtn" title="跳转到指定页码">跳转</button>
  </div>
  <span class="spacer"></span>
  <span id="prCount" title="当前页面存在的可纠错文字数量（未采纳/未忽略的错误标注）">可纠错数：<b id="prCountNum">0</b></span>
  <div class="tb-group tb-main" role="group" aria-label="工具与操作">
    <button type="button" id="helpBtn" title="帮助：Markdown 格式、快捷键与标记说明">帮助</button>
    <button type="button" id="historyBtn" title="历史记录：查看/管理本地矫正缓存（文件名与路径分列、多版本）">历史记录</button>
    <button type="button" id="settingsBtn" title="设置">设置</button>
    <button type="button" id="exportBtn" title="导出：把全部页面的文字（含未保存的修改）导出为 TXT / DOCX 文件，保存位置由弹窗选择">导出</button>
    <span id="pos" aria-live="off"></span>
    <span id="status">加载中 ...</span>
    <button type="button" id="stageBtn" title="暂存：把当前修改暂时保存到本地历史缓存（不转换，可随时恢复）">暂存</button>
    <button type="button" id="saveBtn">保存</button>
    <button type="button" id="finishBtn" class="primary">完成并转换</button>
  </div>
</div>
<div id="hintbar">
  <span class="hint-text">左侧原图（点击切换预览/原图），右侧文字可直接编辑。选中文字弹出<b>图标快捷菜单</b>（悬停有提示）；支持<b>粗体</b>、<i>斜体</i>、标题、注释、居左/居中/居右与<span class="ptoe-marker">全文/段落/注释/换页标记</span>（标记插入到光标处；段落标记段首=合上段、段尾=合下段；换页标记=此后内容显示在新的一页）。可切换 <b>Markdown 模式</b>（#标题、**粗体**、*斜体*）、繁简转换、字号调整与页码跳转，详见「帮助」。</span>
  <button type="button" id="hintClose" title="关闭提示（可随时在「帮助」中查看）" aria-label="关闭提示">✕</button>
</div>
<div id="pages"></div>
<div id="popup"></div>
<div id="tip"></div>
<!-- 右键上下文菜单（2026-08-08）：编辑区内右键弹出；重识别/插入标记/导出/Markdown 提示/保存/暂存 -->
<div id="contextMenu" hidden>
  <button type="button" class="ctx-item" data-ctx="reocr">重识别</button>
  <button type="button" class="ctx-item" data-ctx="clear">清除</button>
  <div class="ctx-item ctx-sub" data-ctx="marker">插入标记 <span class="ctx-arrow">▸</span>
    <div class="ctx-submenu" id="ctxMarkerSub">
      <button type="button" class="ctx-item" data-ctx-marker="join">段落标记</button>
      <button type="button" class="ctx-item" data-ctx-marker="page">换页标记</button>
      <button type="button" class="ctx-item" data-ctx-marker="full">全文标记</button>
      <button type="button" class="ctx-item" data-ctx-marker="note">注释标记</button>
    </div>
  </div>
  <div class="ctx-item ctx-sub" data-ctx="export">导出 <span class="ctx-arrow">▸</span>
    <div class="ctx-submenu" id="ctxExportSub">
      <button type="button" class="ctx-item" data-ctx-export="txt">txt格式</button>
      <button type="button" class="ctx-item" data-ctx-export="docx">docx格式</button>
      <button type="button" class="ctx-item" data-ctx-export="md">md格式</button>
      <button type="button" class="ctx-item" data-ctx-export="epub">epub格式</button>
    </div>
  </div>
  <div class="ctx-item ctx-sub" data-ctx="rules">添加规则 <span class="ctx-arrow">▸</span>
    <div class="ctx-submenu" id="ctxRulesSub"></div>
  </div>
  <button type="button" class="ctx-item" data-ctx="clearpage">清空</button>
  <button type="button" class="ctx-item" data-ctx="fmtall">格式化</button>
  <div class="ctx-sep"></div>
  <button type="button" class="ctx-item" data-ctx="save">保存</button>
  <button type="button" class="ctx-item" data-ctx="stage">暂存</button>
</div>
<div id="proofreadMenu">
  <button type="button" id="prMenuCorrect" role="menuitem">校正</button>
  <button type="button" id="prMenuReocr" role="menuitem">重识别</button>
  <button type="button" id="prMenuApply" role="menuitem">应用</button>
  <button type="button" id="prMenuClear" role="menuitem">清除</button>
  <button type="button" id="prMenuRevert" role="menuitem">回退</button>
  <div style="padding:6px 10px;border-top:1px solid #eee;margin-top:6px;">
    <label style="display:block;font-size:13px;" title="默认只执行三条规则：连续重复文字 / 连续标点 / 中文中的连续字母">启用原有规则（半角转全角/引号配对/混淆表/词典） <input type="checkbox" id="prLegacyRules"></label>
    <label style="display:block;font-size:13px;margin-top:6px;">启用 LLM 深度校对 <input type="checkbox" id="prLlmEnable"></label>
    <label style="display:block;font-size:13px;margin-top:6px;">模型 <select id="prLlmModel" style="width:130px;"></select></label>
    <small style="color:#666;display:block;margin-top:4px;">启用后每次校正会额外调用本地 llama-server 进行深度校对。模型留空则使用当前选中模型。</small>
    <div style="display:flex;gap:6px;margin-top:8px;">
      <button type="button" id="prLlmStart" style="flex:1;">启动服务</button>
      <button type="button" id="prLlmStop" style="flex:1;">停止服务</button>
      <button type="button" id="prLlmSwitch" style="flex:1;" title="用当前所选模型重启服务：自动停止旧模型并加载新模型（无需先手动停止）">切换模型</button>
    </div>
    <small id="prLlmStatus" style="color:#666;display:block;margin-top:4px;"></small>
  </div>
</div>
<div id="searchModalBg"><div class="modal search-modal">
  <div class="search-head"><h3>搜索 / 替换</h3><button type="button" id="searchCloseBtn" class="x-btn" title="关闭搜索" aria-label="关闭搜索">✕</button></div>
  <div class="search-row">
    <input type="text" id="searchInput" placeholder="搜索词（可正则）">
    <label class="search-regex" title="勾选后按正则表达式搜索，否则按普通文本"><input type="checkbox" id="searchRegex">正则</label>
    <button type="button" id="searchBtn" class="primary">搜索</button>
    <button type="button" id="searchClearBtn" class="primary" title="清除全部文字标记与搜索结果" aria-label="清除搜索">清理</button>
  </div>
  <div class="search-row">
    <input type="text" id="replaceInput" placeholder="替换为">
    <button type="button" id="replaceBtn" title="替换当前选中的匹配（支持正则）">替换当前</button>
    <button type="button" id="replaceAllBtn" title="把当前搜索词在所有页面中替换为「替换为」的内容（支持正则）">全部替换</button>
  </div>
  <div class="search-nav">
    <button type="button" id="searchPrevBtn" title="上一个匹配" aria-label="上一个匹配">↑</button>
    <span id="searchPos"></span>
    <button type="button" id="searchNextBtn" title="下一个匹配" aria-label="下一个匹配">↓</button>
  </div>
  <div class="sr-head"><span id="srCount"></span></div>
  <div id="searchList"></div>
</div></div>
<div id="exportModalBg"><div class="modal search-modal">
  <div class="search-head"><h3>导出</h3><button type="button" id="exportCloseBtn" class="x-btn" title="关闭导出" aria-label="关闭导出">✕</button></div>
  <p class="export-desc">把全部页面的文字（含未保存的修改）导出为文件；点击下方按钮后弹出窗口选择保存位置。DOCX 中标题自动加粗加大并居中，带章节大纲；对齐、缩进等段落格式同步导出；Markdown 与界面规则保持一致。</p>
  <div class="export-actions">
    <button type="button" id="exportDocxBtn" title="导出为 Word 文档（.docx）">导出为 DOCX</button>
    <button type="button" id="exportMdBtn" title="导出为 Markdown 文件（.md）">导出为 MD</button>
    <button type="button" id="exportTxtBtn" class="primary" title="导出为纯文本文件（.txt）">导出为 TXT</button>
  </div>
</div></div>
<div id="indentModalBg"><div class="modal search-modal">
  <div class="search-head"><h3>段落设置</h3><button type="button" id="indCloseBtn" class="x-btn" title="关闭段落设置" aria-label="关闭段落设置">✕</button></div>
  <p style="font-size:12px;color:#5a6b7c;margin:4px 0;">作用于当前选中/光标所在段落（可跨多段）。缩进单位为字符，间距单位为行；设置随内容保存并在导出 EPUB 时生效。</p>
  <div class="indent-grid">
    <label>左缩进 <input type="number" id="indLeft" step="0.5" min="0" max="16"> 字符</label>
    <label>右缩进 <input type="number" id="indRight" step="0.5" min="0" max="16"> 字符</label>
    <label>特殊格式
      <select id="indSpecial">
        <option value="">无</option>
        <option value="first">首行缩进</option>
        <option value="hang">悬挂缩进</option>
      </select>
    </label>
    <label>缩进值 <input type="number" id="indVal" step="0.5" min="0" max="16"> 字符</label>
    <label>段前 <input type="number" id="indBefore" step="0.5" min="0" max="8"> 行</label>
    <label>段后 <input type="number" id="indAfter" step="0.5" min="0" max="8"> 行</label>
    <label>行距
      <select id="indLh">
        <option value="">默认</option>
        <option value="1">单倍</option>
        <option value="1.5">1.5 倍</option>
        <option value="2">双倍</option>
      </select>
    </label>
  </div>
  <div class="indent-preview" id="indPreview"><p>预览：段落文本示例，用于查看缩进与间距效果。The quick brown fox 123.</p></div>
  <div class="export-actions">
    <button type="button" id="indClearBtn" title="清除所选段落的全部缩进与间距设置">清除格式</button>
    <button type="button" id="indOkBtn" class="primary" title="把设置应用到所选段落">确定</button>
  </div>
</div></div>
<div id="modalBg"><div class="modal">
  <h3>设置</h3>
  <div class="settings-tabs">
    <button type="button" class="settings-tab active" data-tab="shortcuts">快捷键</button>
    <button type="button" class="settings-tab" data-tab="fonts">字体</button>
    <button type="button" class="settings-tab" data-tab="ui">界面</button>
  </div>
  <div class="settings-panels">
    <div class="settings-panel" id="panel-shortcuts">
      <p style="font-size:12px;color:#5a6b7c;margin:8px 0;">每个操作绑定一个组合键；点击某行后按下新组合键完成绑定，Del/Backspace 清除，Esc 取消。绑定保存在本浏览器（localStorage）并同步到配置文件。</p>
      <table id="shortcutTable"></table>
    </div>
    <div class="settings-panel" id="panel-fonts" style="display:none;">
      <p style="font-size:12px;color:#5a6b7c;margin:8px 0;">设置各类文本的字体族（CSS font-family），留空则使用浏览器默认。修改后实时生效，保存到配置文件。</p>
      <div style="display:grid;grid-template-columns:120px 1fr;gap:8px 12px;align-items:center;margin-top:8px;">
        <label>正文字体</label>
        <input type="text" id="fontBody" placeholder="如：serif, 'Microsoft YaHei', sans-serif" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font:inherit;">
        <label>标题字体</label>
        <input type="text" id="fontHeading" placeholder="如：sans-serif, 'Microsoft YaHei', serif" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font:inherit;">
        <label>注释字体</label>
        <input type="text" id="fontNote" placeholder="如：serif, 'KaiTi', sans-serif" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font:inherit;">
        <label>引用字体</label>
        <input type="text" id="fontCitation" placeholder="如：cursive, 'FangSong', serif" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font:inherit;">
      </div>
      <div style="margin-top:12px;padding-top:8px;border-top:1px solid var(--border);">
        <label style="display:flex;align-items:center;gap:8px;font-size:13px;">
          <input type="checkbox" id="citationItalicEnabled" style="width:16px;height:16px;">
          启用引用斜体（citation 格式自动应用 italic）
        </label>
      </div>
    </div>
    <div class="settings-panel" id="panel-ui" style="display:none;">
      <h4 style="margin:8px 0 4px;">提示延迟</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">鼠标悬停按钮超过设定时间（毫秒）才显示提示文字，提示中会附带对应快捷键；0 = 立即显示。</p>
      <label style="font-size:13px;">提示延迟（毫秒） <input type="number" id="tipDelayInput" min="0" max="5000" step="100" style="width:90px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;"></label>
      <h4 style="margin:16px 0 4px;">编辑器字号</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">调整编辑区显示字号（视图偏好，不写入保存内容）。</p>
      <label style="font-size:13px;">字号（px） <input type="number" id="editorFontSizeInput" min="10" max="28" step="1" style="width:70px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;"></label>
    </div>
  </div>
  <button type="button" id="closeSettings" class="primary" style="margin-top:12px;">关闭</button>
</div></div>
<div id="finishModalBg"><div class="modal">
  <h3 id="finishTitle">转换完成</h3>
  <p id="finishMsg" style="font-size:14px;color:#33414f;">是否关闭当前页面？</p>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px;">
    <button type="button" id="closePageBtn">关闭页面</button>
    <button type="button" id="stayPageBtn" class="primary">留在本页</button>
  </div>
</div></div>
<div id="formatRulesModalBg"><div class="modal" style="max-width:780px;">
  <div class="search-head"><h3>格式规则</h3><button type="button" id="formatRulesCloseBtn" class="x-btn" title="关闭" aria-label="关闭">✕</button></div>
  <p style="font-size:12px;color:#5a6b7c;margin-top:0;">对选中的文字一键应用自定义格式。每条规则含一个有序条件列表（像列表一样按顺序判断）：每个条件可单独设置格式（含「无」= 不处理文本）；求值模式「第一个匹配即停」= 首个匹配条件生效即停，「所有匹配都应用」= 全部匹配条件的格式按序叠加（冲突格式自动跳过）。选中文字为空时按光标所在段落处理块级格式。规则按列表顺序执行。</p>
  <table id="formatRulesTable">
    <thead><tr style="text-align:left;color:#33414f;">
      <th style="padding:6px 8px;border-bottom:1px solid var(--border);">顺序</th>
      <th style="padding:6px 8px;border-bottom:1px solid var(--border);">名称</th>
      <th style="padding:6px 8px;border-bottom:1px solid var(--border);">条件（含格式）</th>
      <th style="padding:6px 8px;border-bottom:1px solid var(--border);">操作</th>
    </tr></thead>
    <tbody id="formatRulesBody"></tbody>
  </table>
  <div style="margin-top:10px;display:flex;gap:8px;align-items:center;">
    <button type="button" id="formatRuleNewBtn" class="primary">新建规则</button>
    <button type="button" id="formatRulesApplyAllBtn" title="按列表顺序执行全部规则；冲突格式（对齐/块标签互斥、remove 与其他）自动跳过">应用全部规则</button>
  </div>
</div></div>
<div id="frRuleModalBg"><div class="modal" style="max-width:760px;">
  <div class="search-head"><h3>编辑规则</h3><button type="button" id="frRuleCloseBtn" class="x-btn" title="关闭" aria-label="关闭">✕</button></div>
  <div style="margin-bottom:8px;">
    <label style="font-size:13px;">规则名称 <input type="text" id="frName" placeholder="如：书名标题" style="width:240px;"></label>
  </div>
  <div style="margin-bottom:8px;">
    <label style="font-size:13px;">快捷位 <input type="checkbox" id="frPin"></label>
  </div>
  <div style="margin-bottom:8px;">
    <label style="font-size:13px;">简称 <input type="text" id="frLabel" maxlength="4" placeholder="如：标"></label>
  </div>
  <div style="margin-bottom:10px;font-size:13px;color:#33414f;">
    求值模式
    <select id="frMode">
      <option value="first">第一个匹配即停</option>
      <option value="all">所有匹配都应用</option>
    </select>
    <span style="color:#5a6b7c;font-size:12px;margin-left:6px;">第一个匹配即停：首个匹配条件生效即停；所有匹配都应用：全部匹配条件的格式按序叠加（冲突自动跳过）。</span>
  </div>
  <div style="margin-bottom:6px;font-size:13px;color:#33414f;">条件列表（按顺序判断，空条件内容 = 无条件恒匹配）：</div>
  <div id="frConditions"></div>
  <div style="margin-top:8px;">
    <button type="button" id="frAddCondBtn">添加条件</button>
  </div>
  <div style="margin-top:12px;display:flex;gap:8px;">
    <button type="button" id="frSaveBtn" class="primary">保存规则</button>
    <button type="button" id="frCancelBtn">取消</button>
  </div>
</div></div>
<div id="frFmtPopupBg"><div class="modal" style="max-width:420px;">
  <div class="search-head"><h3>应用格式</h3><button type="button" id="frFmtPopupCloseBtn" class="x-btn" title="关闭" aria-label="关闭">✕</button></div>
  <div id="frFmtOpts" class="fr-opts"></div>
  <div style="margin-top:12px;display:flex;gap:8px;">
    <button type="button" id="frFmtOkBtn" class="primary">确认</button>
    <button type="button" id="frFmtCancelBtn">取消</button>
  </div>
</div></div>
<div id="historyModalBg"><div class="modal" style="max-width:960px; min-width:360px;">
  <h3>历史记录</h3>
  <p style="font-size:12px;color:#5a6b7c;">本地矫正缓存（同一文件保留多个版本，v1 为最新）。文件名与路径分列显示，同名不同路径的文件可区分；勾选后可删除或导出（支持多选）。</p>
  <div style="max-height:50vh;overflow:auto;border:1px solid var(--border);border-radius:4px;margin-top:6px;">
    <table id="historyTable" style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead><tr style="text-align:left;color:#33414f;">
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);width:34px;"><input type="checkbox" id="historyCheckAll" title="全选"></th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);width:20%;">文件名</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);width:32%;">文件路径</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);width:7%;">版本</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);width:16%;">更新时间</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);width:11%;">校正页码</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);width:8%;">操作</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;">
    <button type="button" id="historyImportBtn">导入</button>
    <button type="button" id="historyExportBtn">导出</button>
    <input type="file" id="historyImportFile" accept=".json,application/json,.zip,application/zip" style="display:none">
    <button type="button" id="historyDeleteBtn">删除</button>
    <button type="button" id="historyDeleteAllBtn" style="display:none">全部删除</button>
    <button type="button" id="historyCloseBtn" class="primary">关闭</button>
  </div>
</div></div>
<div id="helpModalBg"><div class="modal" style="max-width:800px;max-height:80vh;overflow:auto;">
  <div class="search-head"><h3>帮助</h3><button type="button" id="closeHelp" class="x-btn" title="关闭" aria-label="关闭">✕</button></div>
  <div id="helpContent" style="line-height:1.7;font-size:13px;"></div>
</div></div>
<div id="errPopup" style="display:none;position:fixed;z-index:65;flex-direction:row;align-items:center;gap:8px;padding:8px;background:#fff;border:1px solid #ccc;border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.2);">
  <button id="errOk" title="采纳（Enter）">采纳</button>
  <button id="errNo" title="忽略（Esc）">忽略</button>
</div>
<!-- 图片设置弹窗：点击编辑区内的图片弹出，可调整大小/位置/删除 -->
<div id="imgPopup" style="display:none;position:fixed;z-index:65;flex-direction:column;gap:6px;padding:10px;background:#fff;border:1px solid #ccc;border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.2);min-width:140px;">
  <div style="font-weight:600;font-size:13px;margin-bottom:2px;">图片设置</div>
  <div style="display:flex;flex-wrap:wrap;gap:4px;">
    <span style="width:100%;font-size:12px;color:#666;">布局</span>
    <button type="button" class="img-pop-btn" data-img-op="layout" data-img-val="full" title="全画幅：导出 EPUB 时独占一页，前后内容另起一页；大小设置不影响导出">全画幅</button>
    <button type="button" class="img-pop-btn" data-img-op="layout" data-img-val="fit" title="局部：与前后内容共占一页（不强制分页）；大小设置在导出中生效">局部</button>
    <button type="button" class="img-pop-btn" data-img-op="layout" data-img-val="inline" title="行内：嵌在文字中间（默认 50% 宽度）">行内</button>
  </div>
  <div style="display:flex;flex-wrap:wrap;gap:4px;">
    <span style="width:100%;font-size:12px;color:#666;">大小</span>
    <button type="button" class="img-pop-btn" data-img-op="size" data-img-val="original">原尺寸</button>
    <button type="button" class="img-pop-btn" data-img-op="size" data-img-val="w25">25%</button>
    <button type="button" class="img-pop-btn" data-img-op="size" data-img-val="w50">50%</button>
    <button type="button" class="img-pop-btn" data-img-op="size" data-img-val="w75">75%</button>
    <button type="button" class="img-pop-btn" data-img-op="size" data-img-val="w100">100%</button>
  </div>
  <div id="imgPosRow" style="display:flex;flex-wrap:wrap;gap:4px;">
    <span style="width:100%;font-size:12px;color:#666;">位置</span>
    <button type="button" class="img-pop-btn" data-img-op="pos" data-img-val="left">左</button>
    <button type="button" class="img-pop-btn" data-img-op="pos" data-img-val="center">中</button>
    <button type="button" class="img-pop-btn" data-img-op="pos" data-img-val="right">右</button>
  </div>
  <div id="imgVPosRow" style="display:none;flex-wrap:wrap;gap:4px;">
    <span style="width:100%;font-size:12px;color:#666;">位置（行内）</span>
    <button type="button" class="img-pop-btn" data-img-op="pos" data-img-val="vtop">顶</button>
    <button type="button" class="img-pop-btn" data-img-op="pos" data-img-val="vmid">中</button>
    <button type="button" class="img-pop-btn" data-img-op="pos" data-img-val="vbot">底</button>
  </div>
  <div style="display:flex;gap:4px;margin-top:2px;">
    <button type="button" class="img-pop-btn" data-img-op="delete" style="flex:1;color:#c0392b;">删除</button>
  </div>
</div>
<div id="toast" aria-hidden="true"></div>
<script src="/ui/app.js">