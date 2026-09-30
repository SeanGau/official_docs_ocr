"""地端版模型檔的離線檢查、下載與打包。

模型版本固定在 model_manifest.json（repo、commit、每個檔案的大小），檢查時完全不連網：
只比對 Hugging Face hub cache 目錄裡的 refs/main 與 snapshot 檔案大小。

    python model_store.py manifest        # 開發用：依 PINNED 重新產生 model_manifest.json
    python model_store.py download <dir>  # 下載到 <dir>（hub cache 格式）
    python model_store.py stage <dir>     # 建置用：整理成不含 symlink 的可攜 cache

本模組在匯入時不 import huggingface_hub：它在 import 時就讀 HF_HUB_* 環境變數，
GUI 必須先決定 cache 位置與離線模式才能 import。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MANIFEST_PATH = Path(__file__).with_name("model_manifest.json")

# (repo, commit, allow_patterns)。只收執行必需檔與授權檔，不收 Dockerfile、測試、範例等開發檔，
# 避免使用者既有的 HF cache 因為缺這些檔被判定要下載。
# Nemotron 的 vision encoder 以 auto_map 指向 C-RADIOv2-H 的 remote code（不指定 revision，
# 所以離線時讀 refs/main）；那邊只需要 .py，權重已在 Nemotron 裡。C-RADIOv2-H 沒有
# LICENSE 檔，授權宣告在 README.md。
PINNED = (
    (
        "nvidia/NVIDIA-Nemotron-Parse-2.0",
        "b6742064f4a8cf22a10383ece5e7fbead355ac04",
        [
            "model.safetensors", "config.json", "generation_config.json",
            "preprocessor_config.json", "processor_config.json", "special_tokens_map.json",
            "tokenizer.json", "tokenizer_config.json", "chat_template.jinja",
            "hf_nemotron_parse_*.py", "LICENSE", "README.md",
        ],
    ),
    ("nvidia/C-RADIOv2-H", "0d8f4c18c877166eda07ddae1386bcad256b7a6a", ["*.py", "README.md"]),
)


@dataclass(frozen=True)
class Missing:
    repo: str
    path: str
    size: int


def load_manifest() -> list[dict]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["repos"]


def main_model_id() -> str:
    return load_manifest()[0]["repo"]


def app_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "OfficialDocParser"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "official_doc_parser"


def download_store() -> Path:
    """下載與讀取模型的 Hugging Face hub cache，規則與 huggingface_hub 相同：
    HF_HUB_CACHE（或舊名 HUGGINGFACE_HUB_CACHE）> HF_HOME/hub > ~/.cache/huggingface/hub。

    與其他 Hugging Face 工具共用，已下載過就不必重下。不 import huggingface_hub 來算：
    它在 import 時就讀 HF_HUB_OFFLINE，GUI 程序得等載入模型前才設定。
    """
    for name in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(name):
            return Path(os.environ[name]).expanduser()
    hf_home = os.environ.get("HF_HOME") or Path(
        os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    ) / "huggingface"
    return Path(hf_home).expanduser() / "hub"


def candidate_stores() -> list[Path]:
    """依序檢查：exe 內附（唯讀）→ 使用者的 Hugging Face cache（也是下載位置）。"""
    stores = []
    if getattr(sys, "frozen", False):
        stores.append(Path(sys._MEIPASS) / "hf_hub")
    stores.append(download_store())
    return stores


def _repo_dir(store: Path, repo: str) -> Path:
    return store / f"models--{repo.replace('/', '--')}"


def find_missing(store: Path) -> list[Missing]:
    """列出 store 裡缺少或大小不符的檔案；空 list 代表可以完全離線執行。"""
    missing = []
    for entry in load_manifest():
        repo_dir = _repo_dir(store, entry["repo"])
        snapshot = repo_dir / "snapshots" / entry["commit"]
        for path, size in entry["files"].items():
            try:
                ok = (snapshot / path).stat().st_size == size
            except OSError:
                ok = False
            if not ok:
                missing.append(Missing(entry["repo"], path, size))
        try:
            ref_ok = (repo_dir / "refs" / "main").read_text(encoding="ascii").strip() == entry["commit"]
        except OSError:
            ref_ok = False
        if not ref_ok:
            missing.append(Missing(entry["repo"], "refs/main", 0))
    return missing


def find_ready_store() -> Path | None:
    for store in candidate_stores():
        if not find_missing(store):
            return store
    return None


def total_size() -> int:
    return sum(sum(e["files"].values()) for e in load_manifest())


def _write_ref(store: Path, repo: str, commit: str) -> None:
    refs = _repo_dir(store, repo) / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "main").write_text(commit, encoding="ascii")


def _progress_tqdm(on_bytes: Callable[[int], None]):
    """給 snapshot_download 的 tqdm_class：回報「寫入磁碟的位元組」在所有 repo 的累計量。

    snapshot_download 每個 repo 都會新建進度條、n 從 0 開始，所以這裡累加增量，
    不直接回報 n；下載 thread 會同時呼叫 update，累計值用 lock 保護。
    """
    import threading

    from tqdm.std import tqdm

    lock = threading.Lock()
    done = 0

    class ProgressTqdm(tqdm):
        def __init__(self, *args, **kwargs):
            self._report = kwargs.get("unit") == "B" and "Reconstruct" in (kwargs.get("desc") or "")
            super().__init__(*args, **kwargs)

        def display(self, *args, **kwargs):
            """不輸出任何文字；進度只經由 on_bytes 回報。"""

        def update(self, n=1):
            nonlocal done
            if self._report and n:
                with lock:
                    done += int(n)
                    total = done
                on_bytes(total)
            return super().update(n)

    return ProgressTqdm


def download(store: Path, on_bytes: Callable[[int], None] | None = None) -> None:
    """把固定版本的模型下載到 store；已完整的檔案會略過。

    on_bytes 收到的是本次呼叫在所有 repo 累計寫入的位元組數。下載後會再檢查一次，
    仍有缺檔就丟 RuntimeError。
    """
    from huggingface_hub import snapshot_download

    tqdm_class = _progress_tqdm(on_bytes) if on_bytes else None
    for entry in load_manifest():
        snapshot_download(
            entry["repo"],
            revision=entry["commit"],
            cache_dir=store,
            allow_patterns=list(entry["files"]),
            tqdm_class=tqdm_class,
        )
        _write_ref(store, entry["repo"], entry["commit"])
    if missing := find_missing(store):
        raise RuntimeError(f"下載後仍缺少 {len(missing)} 個檔案，例如 {missing[0].repo}/{missing[0].path}")


def stage(dest: Path) -> None:
    """建置用：下載到使用者 HF cache，再複製成實體檔案（解參考 symlink）到 dest。"""
    from huggingface_hub import snapshot_download

    if dest.exists():
        shutil.rmtree(dest)
    for entry in load_manifest():
        snapshot = Path(snapshot_download(
            entry["repo"], revision=entry["commit"], allow_patterns=list(entry["files"]),
        ))
        target = _repo_dir(dest, entry["repo"]) / "snapshots" / entry["commit"]
        for path in entry["files"]:
            (target / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(snapshot / path, target / path)
        _write_ref(dest, entry["repo"], entry["commit"])
        print(f"已整理 {entry['repo']}@{entry['commit']}", flush=True)
    if missing := find_missing(dest):
        sys.exit(f"整理後仍缺少 {len(missing)} 個檔案，例如 {missing[0]}")


def write_manifest() -> None:
    """開發用：依 PINNED 向 Hugging Face 查詢檔案清單與大小，寫入 model_manifest.json。"""
    from fnmatch import fnmatch

    from huggingface_hub import HfApi

    api = HfApi()
    repos = []
    for repo, commit, patterns in PINNED:
        files = {
            item.path: item.size
            for item in api.list_repo_tree(repo, revision=commit, recursive=True)
            if getattr(item, "size", None) is not None
            and (patterns is None or any(fnmatch(item.path, p) for p in patterns))
        }
        repos.append({"repo": repo, "commit": commit, "files": dict(sorted(files.items()))})
    MANIFEST_PATH.write_text(
        json.dumps({"repos": repos}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(f"已寫入 {MANIFEST_PATH}（{sum(len(r['files']) for r in repos)} 個檔案）")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "manifest" and len(sys.argv) == 2:
        write_manifest()
    elif command in ("download", "stage") and len(sys.argv) == 3:
        (download if command == "download" else stage)(Path(sys.argv[2]))
    else:
        sys.exit(__doc__)
