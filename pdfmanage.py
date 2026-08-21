import base64
import hashlib
import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import Pool, cpu_count
from pathlib import Path


# 检测路径的文件夹是否存在
def cpath(path: Path | str) -> bool:
    """Return True if path exists and is a directory.

    Accepts either a Path or a string.
    """
    fold_path = Path(path)
    return fold_path.exists() and fold_path.is_dir()


def createdic(name: str, base_dir: Path | str | None = None) -> Path:
    """Create a data subdirectory for `name` under `base_dir` (or script dir/data).

    If data/NAME exists, append an incremental suffix: NAME_1, NAME_2, ... and
    return the Path of the created directory.
    """
    base = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent
    data_dir = base / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    candidate = data_dir / name
    if not candidate.exists():
        candidate.mkdir()
        return candidate

    # find next available name
    i = 1
    while True:
        candidate_i = data_dir / f"{name}_{i}"
        if not candidate_i.exists():
            candidate_i.mkdir()
            return candidate_i
        i += 1


def is_pdf_file(path: Path | str) -> bool:
    """Quickly detect whether a file is a PDF by searching the magic header.

    Some PDF files may include a UTF BOM, leading whitespace, or comments before
    the "%PDF-" marker. Read a small prefix and search for the marker instead
    of insisting it be the first 5 bytes.
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        return False
    try:
        with p.open("rb") as fh:
            # read a small chunk (1KB) which should contain the PDF header even
            # if there is a short preamble (BOM, whitespace, or comment lines)
            prefix = fh.read(1024)
            return b"%PDF-" in prefix
    except Exception:
        return False


# 分割目录内的标记文件：记录 PDF 内容哈希 + 分割参数，用于相同输入时复用图片
_SPLIT_MARKER = ".ptoe_split.json"


def _pdf_sha256(pdf_path: Path | str) -> str:
    """PDF 文件内容哈希（流式，避免整读大文件）。"""
    h = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_existing_split(
    pdf_path: Path | str,
    dpi: int,
    fmt: str,
    pdf_hash: str | None = None,
    preprocess: dict | None = None,
) -> tuple[Path, list[Path]] | None:
    """查找同一 PDF（内容哈希一致）且同一 dpi/fmt/预处理配置 的已分割图片目录。

    命中条件：目录内有 .ptoe_split.json 标记，pdf_hash/dpi/fmt/preprocess 全部一致，
    且 1..pages 的页图都完整存在（防止半途中断/手动删图后的残缺目录被复用）。
    preprocess 为归一化后的预处理配置（None=未启用）；旧标记无该键视为 None，
    与「未启用预处理」兼容——开关变化时缓存自动失效重新分割。
    返回 (out_dir, out_paths)；未命中返回 None（需要重新分割）。
    """
    p = Path(pdf_path)
    base = Path(__file__).resolve().parent
    data_dir = base / "data"
    if not data_dir.is_dir():
        return None
    if pdf_hash is None:
        try:
            pdf_hash = _pdf_sha256(p)
        except Exception:
            return None
    for d in sorted(data_dir.glob(f"{p.stem}*")):
        if not d.is_dir():
            continue
        mf = d / _SPLIT_MARKER
        if not mf.is_file():
            continue
        try:
            meta = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (
            meta.get("pdf_hash") != pdf_hash
            or meta.get("dpi") != dpi
            or meta.get("fmt") != fmt
            or meta.get("preprocess") != (preprocess or None)
            or not isinstance(meta.get("pages"), int)
        ):
            continue
        pages = meta["pages"]
        if pages <= 0:
            continue
        if any(not (d / f"{n}.{fmt}").is_file() for n in range(1, pages + 1)):
            continue
        out_paths = [d / f"{n}.{fmt}" for n in range(1, pages + 1)]
        return d, out_paths
    return None


def _write_split_marker(
    out_dir: Path,
    pdf_path: Path | str,
    dpi: int,
    fmt: str,
    pages: int,
    pdf_hash: str | None = None,
    preprocess: dict | None = None,
) -> None:
    """在分割目录写标记（下次相同输入直接复用）。写入失败不影响主流程。

    preprocess 记录归一化预处理配置（None=未启用），供 _find_existing_split
    比对——开关/参数变化时缓存自动失效。
    """
    try:
        meta = {
            "pdf_hash": pdf_hash or _pdf_sha256(pdf_path),
            "dpi": dpi,
            "fmt": fmt,
            "pages": pages,
            "preprocess": preprocess or None,
        }
        (out_dir / _SPLIT_MARKER).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _normalize_prep_cfg(cfg) -> dict | None:
    """归一化图片预处理配置；未启用返回 None。

    cfg 为 config.json 的 image_preprocess 键（dict）或 None。
    返回 {gray, denoise, sharpen, binarize}（enabled 已剥离）或 None。
    """
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return None
    return {
        "gray": bool(cfg.get("gray", True)),
        "denoise": bool(cfg.get("denoise", True)),
        "sharpen": bool(cfg.get("sharpen", True)),
        "binarize": bool(cfg.get("binarize", False)),
    }


_PREP_WARNED = False


def _apply_preprocess_array(arr, prep: dict):
    """对图像 ndarray 应用 OpenCV 预处理：灰度→中值去噪→锐化→可选二值化。

    cv2/numpy 未安装时打印一次中文提示并原样返回（绝不因缺依赖中断分割）；
    单步处理失败也只跳过该步/整体回退，不影响主流程。
    """
    global _PREP_WARNED
    try:
        import cv2
    except Exception:
        if not _PREP_WARNED:
            _PREP_WARNED = True
            print("      未安装 opencv-python，已跳过图片预处理（pip install opencv-python）")
        return arr
    try:
        # OCR 场景颜色无意义：统一在灰度域处理
        gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY) if getattr(arr, "ndim", 2) == 3 else arr
        if prep.get("denoise"):
            gray = cv2.medianBlur(gray, 3)
        if prep.get("sharpen"):
            blur = cv2.GaussianBlur(gray, (0, 0), 3)
            gray = cv2.addWeighted(gray, 1.5, blur, -0.5, 0)
        if prep.get("binarize"):
            gray = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15
            )
        return gray
    except Exception as exc:
        print(f"      图片预处理失败，已使用原图：{exc}")
        return arr


def _write_preprocessed(pix, out_path: str, fmt: str, prep: dict) -> None:
    """把 Pixmap 经 OpenCV 预处理后写盘；任何失败都回退 pix.save 原图。"""
    try:
        import cv2
        import numpy as np

        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )
        if pix.n == 4:
            arr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
        else:
            arr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        processed = _apply_preprocess_array(arr, prep)
        ext = ".jpg" if fmt.lower() in ("jpg", "jpeg") else ".png"
        ok, buf = cv2.imencode(ext, processed)
        if not ok:
            raise RuntimeError("cv2.imencode 失败")
        buf.tofile(out_path)
    except Exception:
        # 兜底：预处理/编码任何环节失败都退回原始渲染结果
        if fmt.lower() in ("jpg", "jpeg"):
            pix.save(out_path, jpg_quality=100)
        else:
            pix.save(out_path)


# 多进程渲染阈值：页数低于此值直接顺序渲染（spawn 开销 > 并行收益）
# 调低阈值以更早启用并行渲染，提升中小型 PDF 的分割速度
_PARALLEL_PAGE_THRESHOLD = 8
# 最大并行 worker 数（Windows spawn 开销约 0.1s/worker，不宜过多）
# 根据 CPU 核心数动态调整，至少保留 4 个 worker 保证并发度
_MAX_WORKERS = max(4, (os.cpu_count() or 1) - 1)


def _render_page_range(args: tuple) -> list[dict]:
    """多进程 worker：渲染指定页范围，返回 [{page, path, error}, ...]。

    每个 worker 独立 fitz.open() 打开 PDF（PyMuPDF 非线程安全，
    不同进程必须各自持有独立 Document 对象）。
    args = (pdf_path, start, end, out_dir, dpi, fmt, mat_scale, prep)
    prep 为归一化预处理配置（None=不预处理，走 pix.save 原路径）。
    """
    pdf_path, start, end, out_dir, dpi, fmt, mat_scale, prep = args
    import fitz  # 每个子进程延迟导入

    results: list[dict] = []
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        for i in range(start, end):
            results.append({"page": i, "path": None, "error": str(exc)})
        return results

    try:
        mat = fitz.Matrix(mat_scale, mat_scale)
        for i in range(start, end):
            page_num = i + 1  # 1-based
            try:
                pix = doc[i].get_pixmap(matrix=mat, alpha=False)
                filename = f"{page_num}.{fmt}"
                out_path = os.path.join(out_dir, filename)
                if prep:
                    _write_preprocessed(pix, out_path, fmt, prep)
                elif fmt.lower() in ("jpg", "jpeg"):
                    pix.save(out_path, jpg_quality=100)
                else:
                    pix.save(out_path)
                results.append({"page": page_num, "path": out_path, "error": None})
            except Exception as exc:
                results.append({"page": page_num, "path": None, "error": str(exc)})
    finally:
        doc.close()

    return results


def split_pdf_to_images(
    pdf_path: Path | str,
    *,
    dpi: int = 200,
    fmt: str = "png",
    preprocess=None,
) -> tuple[Path, list[Path]]:
    """Split a PDF into per-page images.

    Args:
      pdf_path: path to the PDF file
      dpi: output dpi for rasterization (default 200)
      fmt: image format ("png" or "jpg").
      preprocess: OpenCV 预处理配置（config.json image_preprocess 形状）；
        None 时从 configmanage.get_config(show_dialogs=False) 懒读取，
        显式传 dict 可覆盖（测试钩子）。enabled=false/None 均不预处理。

    Returns:
      (output_folder, list_of_image_paths)

    Raises:
      RuntimeError when required backend (PyMuPDF) is missing or the file cannot be read.

    相同 PDF（内容哈希一致）+ 相同 dpi/fmt/预处理配置 时直接复用已有分割图片，
    不重新切图：分割目录内有 .ptoe_split.json 标记记录（pdf_hash/dpi/fmt/pages/
    preprocess），命中且页图齐全即返回已有 (out_dir, out_paths)。PDF 内容或
    参数（含预处理开关）变化时重新分割（createdic 自动生成新目录），并写入新标记。
    """
    p = Path(pdf_path)
    if not p.exists() or not p.is_file():
        raise RuntimeError(f"PDF path does not exist or is not a file: {p}")

    # 解析预处理配置：显式参数优先，否则懒读 config.json（headless 安全）
    if preprocess is None:
        try:
            from configmanage import get_config

            preprocess = get_config(show_dialogs=False).get("image_preprocess")
        except Exception:
            preprocess = None
    prep = _normalize_prep_cfg(preprocess)
    if prep:
        print("      图片预处理已启用（OpenCV：灰度/去噪/锐化）")

    # 复用已有分割：相同内容 + 相同参数时不重新切图，直接返回既有图片
    pdf_hash = _pdf_sha256(p)
    reused = _find_existing_split(p, dpi, fmt, pdf_hash=pdf_hash, preprocess=prep)
    if reused is not None:
        out_dir, out_paths = reused
        print(f"      reusing {len(out_paths)} existing page image(s) in {out_dir}")
        return out_dir, out_paths

    from importlib import import_module

    try:
        fitz = import_module("fitz")  # PyMuPDF
    except Exception as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("PyMuPDF is required (pip install PyMuPDF)") from exc

    try:
        doc = fitz.open(str(p))
    except Exception as exc:
        raise RuntimeError(f"Failed to open PDF {p}: {exc}") from exc

    try:
        if doc.needs_pass:
            try:
                doc.authenticate("")
            except Exception:
                raise RuntimeError(
                    "PDF is encrypted and cannot be opened without a password"
                )

        total_pages = doc.page_count
        out_dir = createdic(p.stem)
        mat_scale = dpi / 72.0

        # 多进程渲染：页数足够时按页范围分片并行（PyMuPDF 非线程安全，
        # 必须用多进程，每个子进程独立 fitz.open()）
        if total_pages >= _PARALLEL_PAGE_THRESHOLD:
            n_workers = min(cpu_count(), _MAX_WORKERS, total_pages)
            seg_size = (total_pages + n_workers - 1) // n_workers
            chunks = []
            for w in range(n_workers):
                seg_start = w * seg_size
                seg_end = min(seg_start + seg_size, total_pages)
                if seg_start >= seg_end:
                    break
                chunks.append((str(p), seg_start, seg_end, str(out_dir), dpi, fmt, mat_scale, prep))
            # 先关闭父进程 doc，子进程各自打开
            doc.close()
            doc = None
            print(f"      多进程渲染：{total_pages} 页 / {len(chunks)} worker")

            with Pool(processes=len(chunks)) as pool:
                chunk_results = pool.map(_render_page_range, chunks)

            # 按页码排序合并结果
            all_results = []
            for chunk in chunk_results:
                all_results.extend(chunk)
            all_results.sort(key=lambda r: r["page"])

            errors = []
            out_paths = []
            for r in all_results:
                if r["error"]:
                    errors.append(f"页 {r['page']}: {r['error']}")
                else:
                    out_paths.append(Path(r["path"]))
            if errors:
                print(f"      {len(errors)} 页渲染失败：{'; '.join(errors[:5])}")
        else:
            # 少量页使用 ThreadPoolExecutor 并行渲染（PyMuPDF 在 get_pixmap 时释放 GIL，
            # 线程可提供真正的并行性，避免多进程 spawn 开销）
            mat = fitz.Matrix(mat_scale, mat_scale)
            out_paths = [None] * total_pages

            def _render_one(i_page):
                i, page = i_page
                try:
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    filename = f"{i}.{fmt}"
                    out_path = out_dir / filename
                    if prep:
                        _write_preprocessed(pix, str(out_path), fmt, prep)
                    elif fmt.lower() in ("jpg", "jpeg"):
                        pix.save(str(out_path), jpg_quality=100)
                    else:
                        pix.save(str(out_path))
                    return i, out_path, None
                except Exception as exc:
                    return i, None, str(exc)

            # 使用线程池，worker 数不超过页面数和 CPU 核心数
            max_workers = min(total_pages, max(2, (os.cpu_count() or 1) - 1))
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = [ex.submit(_render_one, (i, page)) for i, page in enumerate(doc, start=1)]
                for fut in as_completed(futures):
                    i, out_path, err = fut.result()
                    if err:
                        print(f"      页 {i} 渲染失败：{err}")
                    else:
                        out_paths[i - 1] = out_path

            # 过滤掉失败的页
            out_paths = [p for p in out_paths if p is not None]

        _write_split_marker(
            out_dir, p, dpi, fmt, len(out_paths), pdf_hash=pdf_hash, preprocess=prep
        )
        return out_dir, out_paths
    finally:
        if doc is not None:
            doc.close()


class ImageItem:
    """Represents an image file and its base64 representation.

    By default the base64 representation is written to a temporary file to avoid
    keeping very large strings in memory. Set store_in_memory=True to keep the
    base64 string in RAM (faster, higher memory usage).
    """

    def __init__(
        self,
        path: Path | str,
        store_in_memory: bool = False,
        temp_dir: str | None = None,
    ):
        self.path = Path(path)
        self.store_in_memory = store_in_memory
        self.base64_str: str | None = None
        self.base64_file: Path | None = None
        self.error: str | None = None
        self._temp_dir = temp_dir

    def encode_base64(self) -> str | None:
        """Encode the image file to base64 and store result according to configuration.
        Returns the base64 string on success, or None on error.
        """
        try:
            # already encoded in memory
            if self.base64_str is not None:
                return self.base64_str
            # already encoded in file
            if self.base64_file is not None and self.base64_file.exists():
                if self.base64_str is not None:
                    return self.base64_str
                with open(self.base64_file, "r", encoding="utf-8") as f:
                    b64 = f.read()
                if self.store_in_memory:
                    self.base64_str = b64
                return b64

            # read raw bytes and encode
            with open(self.path, "rb") as f:
                data = f.read()
            b64 = base64.b64encode(data).decode("ascii")
            if self.store_in_memory:
                self.base64_str = b64
            else:
                fd, tmp = tempfile.mkstemp(
                    prefix="img_b64_", suffix=".b64", dir=self._temp_dir
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as tf:
                        tf.write(b64)
                    self.base64_file = Path(tmp)
                except Exception:
                    try:
                        os.close(fd)
                    except Exception:
                        pass
                    raise
            return b64
        except Exception as e:
            self.error = str(e)
            return None

    def get_base64(self) -> str | None:
        """Return the base64 string, encoding it if necessary."""
        if self.base64_str is not None:
            return self.base64_str
        if self.base64_file is not None and self.base64_file.exists():
            with open(self.base64_file, "r", encoding="utf-8") as f:
                self.base64_str = f.read()
            return self.base64_str
        return self.encode_base64()

    def clear(self) -> None:
        """Free in-memory or on-disk encoded data."""
        self.base64_str = None
        if self.base64_file is not None:
            try:
                os.remove(self.base64_file)
            except Exception:
                pass
            self.base64_file = None


class ImageQueue:
    """Thread-safe FIFO queue for ImageItem objects.

    The queue supports preloading (encoding) all images ahead of time so
    downstream consumers don't need to perform conversion during requests.
    """

    def __init__(self, store_in_memory: bool = False, temp_dir: str | None = None):
        self._queue: list[ImageItem] = []
        self._lock = threading.Lock()
        self.store_in_memory = store_in_memory
        self.temp_dir = temp_dir

    def add(self, image_path: Path | str, encode: bool = False) -> ImageItem:
        item = ImageItem(
            image_path, store_in_memory=self.store_in_memory, temp_dir=self.temp_dir
        )
        if encode:
            item.encode_base64()
        with self._lock:
            self._queue.append(item)
        return item

    def add_many(
        self, paths: list[Path | str], encode: bool = False, max_workers: int = 4
    ) -> list[ImageItem]:
        items: list[ImageItem] = []
        for p in paths:
            items.append(self.add(p, encode=False))
        if encode:
            self.preload_all(max_workers=max_workers)
        return items

    def preload_all(self, max_workers: int = 4) -> None:
        """Encode all queued images concurrently. Errors are recorded on items."""
        with self._lock:
            items = [
                it
                for it in self._queue
                if it.base64_str is None
                and (it.base64_file is None or not it.base64_file.exists())
            ]
        if not items:
            return
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(it.encode_base64): it for it in items}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception:
                    # individual item records error
                    pass

    def get_next(self, as_base64: bool = True) -> object | None:
        """Pop next image. If as_base64 True return base64 string, else ImageItem."""
        with self._lock:
            if not self._queue:
                return None
            item = self._queue.pop(0)
        if as_base64:
            return item.get_base64()
        return item

    def peek(self) -> ImageItem | None:
        with self._lock:
            return self._queue[0] if self._queue else None

    def size(self) -> int:
        with self._lock:
            return len(self._queue)

    def clear(self, free_files: bool = True) -> None:
        with self._lock:
            items = list(self._queue)
            self._queue.clear()
        if free_files:
            for it in items:
                it.clear()


__all__ = [
    "ImageItem",
    "ImageQueue",
    "cpath",
    "createdic",
    "is_pdf_file",
    "split_pdf_to_images",
]
