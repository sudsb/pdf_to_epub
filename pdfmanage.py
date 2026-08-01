from pathlib import Path
from typing import List, Tuple
import base64
import tempfile
import threading
import os
from typing import Optional, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed



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


def split_pdf_to_images(
    pdf_path: Path | str, *, dpi: int = 200, fmt: str = "png"
) -> Tuple[Path, List[Path]]:
    """Split a PDF into per-page images.

    Args:
      pdf_path: path to the PDF file
      dpi: output dpi for rasterization (default 200)
      fmt: image format ("png" or "jpg").

    Returns:
      (output_folder, list_of_image_paths)

    Raises:
      RuntimeError when required backend (PyMuPDF) is missing or the file cannot be read.
    """
    from importlib import import_module

    try:
        fitz = import_module("fitz")  # PyMuPDF
    except Exception as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("PyMuPDF is required (pip install PyMuPDF)") from exc

    p = Path(pdf_path)
    if not p.exists() or not p.is_file():
        raise RuntimeError(f"PDF path does not exist or is not a file: {p}")

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

        out_dir = createdic(p.stem)
        mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        out_paths: List[Path] = []
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=mat, alpha=False)
            filename = f"{i}.{fmt}"
            out_path = out_dir / filename
            if fmt.lower() in ("jpg", "jpeg"):
                pix.save(str(out_path), jpg_quality=100)
            else:
                pix.save(str(out_path))
            out_paths.append(out_path)

        return out_dir, out_paths
    finally:
        doc.close()
class ImageItem:
    """Represents an image file and its base64 representation.

    By default the base64 representation is written to a temporary file to avoid
    keeping very large strings in memory. Set store_in_memory=True to keep the
    base64 string in RAM (faster, higher memory usage).
    """
    def __init__(self, path: Path | str, store_in_memory: bool = False, temp_dir: Optional[str] = None):
        self.path = Path(path)
        self.store_in_memory = store_in_memory
        self.base64_str: Optional[str] = None
        self.base64_file: Optional[Path] = None
        self.error: Optional[str] = None
        self._temp_dir = temp_dir

    def encode_base64(self) -> Optional[str]:
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
                with open(self.base64_file, 'r', encoding='utf-8') as f:
                    b64 = f.read()
                if self.store_in_memory:
                    self.base64_str = b64
                return b64

            # read raw bytes and encode
            with open(self.path, 'rb') as f:
                data = f.read()
            b64 = base64.b64encode(data).decode('ascii')
            if self.store_in_memory:
                self.base64_str = b64
            else:
                fd, tmp = tempfile.mkstemp(prefix='img_b64_', suffix='.b64', dir=self._temp_dir)
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as tf:
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

    def get_base64(self) -> Optional[str]:
        """Return the base64 string, encoding it if necessary."""
        if self.base64_str is not None:
            return self.base64_str
        if self.base64_file is not None and self.base64_file.exists():
            with open(self.base64_file, 'r', encoding='utf-8') as f:
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
    def __init__(self, store_in_memory: bool = False, temp_dir: Optional[str] = None):
        self._queue: List[ImageItem] = []
        self._lock = threading.Lock()
        self.store_in_memory = store_in_memory
        self.temp_dir = temp_dir

    def add(self, image_path: Path | str, encode: bool = False) -> ImageItem:
        item = ImageItem(image_path, store_in_memory=self.store_in_memory, temp_dir=self.temp_dir)
        if encode:
            item.encode_base64()
        with self._lock:
            self._queue.append(item)
        return item

    def add_many(self, paths: List[Path | str], encode: bool = False, max_workers: int = 4) -> List[ImageItem]:
        items: List[ImageItem] = []
        for p in paths:
            items.append(self.add(p, encode=False))
        if encode:
            self.preload_all(max_workers=max_workers)
        return items

    def preload_all(self, max_workers: int = 4) -> None:
        """Encode all queued images concurrently. Errors are recorded on items."""
        with self._lock:
            items = [it for it in self._queue if it.base64_str is None and (it.base64_file is None or not it.base64_file.exists())]
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

    def get_next(self, as_base64: bool = True) -> Optional[object]:
        """Pop next image. If as_base64 True return base64 string, else ImageItem."""
        with self._lock:
            if not self._queue:
                return None
            item = self._queue.pop(0)
        if as_base64:
            return item.get_base64()
        return item

    def peek(self) -> Optional[ImageItem]:
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



__all__ = ["cpath", "createdic", "is_pdf_file", "split_pdf_to_images", "ImageItem", "ImageQueue"]
