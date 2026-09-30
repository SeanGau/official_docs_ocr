"""建置用：依 PyInstaller 實際打包的檔案產生 THIRD_PARTY_LICENSES.txt。

    python windows/third_party_licenses.py <PyInstaller workpath\\ocr_app> <輸出檔>

以 PyInstaller 的 PYZ-00.toc／COLLECT-00.toc 為準，逐檔對回安裝它的套件（dist-info 的 RECORD），
只列真的進了成品的東西。遇到下列情況直接失敗，避免默默發佈不合規的成品：
  * 打包進去的套件授權是 GPL／AGPL（LGPL 可）；
  * 有檔案對不到任何套件，也不屬於已知的 Python、Tcl/Tk、MSVC runtime、PyInstaller；
  * torch 帶進來的 NVIDIA DLL 不在 CUDA EULA Attachment A 或 cuDNN 可散布清單內；
  * 套件沒有附授權全文，windows/licenses/ 也沒有補。
"""

from __future__ import annotations

import ast
import os
import re
import sys
from importlib import metadata
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EXTRA_LICENSES = HERE / "licenses"

# GPL／AGPL 會讓整包 exe 受 copyleft 約束，並與 NVIDIA CUDA/cuDNN 授權的
# 「不得使 SDK 受要求公開原始碼的開源授權約束」條款衝突。
FORBIDDEN = re.compile(r"(?<![L])\b(A?GPL|GNU (Affero )?General Public)", re.I)

# NVIDIA CUDA Toolkit EULA Attachment A（Windows）與 cuDNN SLA（runtime .dll）列為可散布的檔案。
NVIDIA_DLL = re.compile(r"^(cu|nv)", re.I)
NVIDIA_REDISTRIBUTABLE = re.compile(
    r"^(cudart64|cublas64|cublasLt64|cufft64|cufftw64|curand64|cusolver64|cusparse64"
    r"|nvrtc64|nvrtc-builtins64|nvJitLink|cupti64|nvToolsExt64|cudnn(_\w+)?64)_[\w.]+\.dll$",
    re.I,
)
MSVC_RUNTIME = re.compile(r"^(vcruntime140(_1)?|msvcp140(_\w+)?|concrt140)\.dll$", re.I)


def norm(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def bundled_sources(workdir: Path) -> list[tuple[str, str]]:
    """(成品內路徑, 來源檔) 清單。"""
    entries = []
    pyz = ast.literal_eval((workdir / "PYZ-00.toc").read_text(encoding="utf-8"))
    entries += [(dest, src) for dest, src, _ in pyz[1]]
    collect = ast.literal_eval((workdir / "COLLECT-00.toc").read_text(encoding="utf-8"))
    entries += [(dest, src) for dest, src, _ in collect[0]]
    return [(dest, src) for dest, src in entries if src]


def license_texts(dist: metadata.Distribution) -> list[tuple[str, str]]:
    """套件附帶的授權全文；wheel 沒附時用 windows/licenses/<套件名>.txt。"""
    names = dist.metadata.get_all("License-File") or []
    files = {str(f).replace("\\", "/"): f for f in dist.files or ()}
    found = []
    for name in names:
        for key, f in files.items():
            if ".dist-info/" in key and (key.endswith(f"/licenses/{name}") or key.endswith(f".dist-info/{name}")):
                found.append((name, Path(dist.locate_file(f)).read_text(encoding="utf-8", errors="replace")))
                break
    if not found:
        found = [
            (key.split(".dist-info/", 1)[1], Path(dist.locate_file(f)).read_text(encoding="utf-8", errors="replace"))
            for key, f in files.items()
            if ".dist-info/" in key and re.search(r"(LICEN[CS]E|COPYING|NOTICE)", key.rsplit("/", 1)[-1], re.I)
        ]
    extra = EXTRA_LICENSES / f"{dist.metadata['Name'].lower()}.txt"
    if not found and extra.is_file():
        found = [(extra.name + "（取自上游原始碼庫，wheel 未附）", extra.read_text(encoding="utf-8"))]
    return found


def license_label(dist: metadata.Distribution) -> str:
    md = dist.metadata
    expr = md.get("License-Expression")
    if expr:
        return expr
    classifiers = [c.split(" :: ")[-1] for c in md.get_all("Classifier") or [] if c.startswith("License ::")]
    first_line = (md.get("License") or "").strip().splitlines()
    return "; ".join(filter(None, [first_line[0] if first_line else "", *classifiers])) or "UNKNOWN"


def section(title: str, body: str) -> str:
    return f"{'=' * 78}\n{title}\n{'=' * 78}\n\n{body.strip()}\n\n"


def main(workdir: Path, out: Path) -> None:
    owner: dict[str, metadata.Distribution] = {}
    for dist in metadata.distributions():
        for f in dist.files or ():
            owner.setdefault(norm(dist.locate_file(f)), dist)

    base = norm(sys.base_prefix)
    root, work = norm(ROOT), norm(workdir)
    used: dict[str, metadata.Distribution] = {}
    python_files, msvc, nvidia, unknown = [], set(), set(), []
    for dest, src in bundled_sources(workdir):
        source = norm(src)
        dll = Path(dest).name
        if dist := owner.get(source):
            used.setdefault(dist.metadata["Name"].lower(), dist)
            if dist.metadata["Name"].lower() == "torch" and dll.lower().endswith(".dll") and NVIDIA_DLL.match(dll) \
                    and not dll.lower().startswith(("torch", "c10")):
                nvidia.add(dll)
        elif source.startswith(root + os.sep) or source.startswith(work + os.sep):
            continue  # 本專案程式與 PyInstaller 產生的檔案（bootloader 見下方 PyInstaller 段落）
        elif source.startswith(base + os.sep):
            python_files.append(source)
        elif MSVC_RUNTIME.match(dll):
            msvc.add(dll)
        else:
            unknown.append(src)

    errors = []
    if unknown:
        errors.append("下列打包檔案對不到授權來源：\n  " + "\n  ".join(sorted(unknown)[:30]))
    for name, dist in sorted(used.items()):
        if FORBIDDEN.search(license_label(dist)):
            errors.append(f"{dist.metadata['Name']} 的授權是 {license_label(dist)}，不能打包進 exe")
        elif not license_texts(dist):
            errors.append(f"{dist.metadata['Name']} 沒有授權全文；請放到 {EXTRA_LICENSES / (name + '.txt')}")
    bad_nvidia = sorted(d for d in nvidia if not NVIDIA_REDISTRIBUTABLE.match(d))
    if bad_nvidia:
        errors.append("下列 NVIDIA DLL 不在可散布清單內，請在 spec 排除：" + ", ".join(bad_nvidia))
    if errors:
        sys.exit("第三方授權檢查失敗：\n" + "\n".join(errors))

    parts = [
        "OfficialDocOCR 第三方元件授權\n\n"
        "本程式本身以 MIT 授權釋出（見 LICENSE）。以下是隨程式一起散布的第三方元件及其授權全文，\n"
        "依 PyInstaller 實際打包的檔案自動產生。模型檔的授權見 NOTICE.txt。\n\n"
        f"Python 套件 {len(used)} 個：\n"
        + "\n".join(f"  {d.metadata['Name']} {d.version} — {license_label(d)}" for _, d in sorted(used.items()))
        + "\n\n"
    ]

    cudnn = sorted(d for d in nvidia if d.lower().startswith("cudnn"))
    cuda = sorted(d for d in nvidia if d not in cudnn)
    parts.append(section(
        "NVIDIA CUDA Toolkit runtime（隨 PyTorch 散布）",
        "下列檔案屬 NVIDIA CUDA Toolkit，列於其 EULA Attachment A 的可散布檔案，依該 EULA 隨本程式散布：\n  "
        + "\n  ".join(cuda) + "\n\n以下為 EULA 原文（來源與取得日期見 windows/licenses/README.md）：\n\n"
        + (EXTRA_LICENSES / "cuda-eula.md").read_text(encoding="utf-8"),
    ))
    parts.append(section(
        "NVIDIA cuDNN runtime（隨 PyTorch 散布）",
        "下列檔案屬 NVIDIA cuDNN，其授權列 runtime .dll 為可散布，依該授權隨本程式散布：\n  "
        + "\n  ".join(cudnn) + "\n\n以下為授權原文（來源與取得日期見 windows/licenses/README.md）：\n\n"
        + (EXTRA_LICENSES / "cudnn-sla.md").read_text(encoding="utf-8"),
    ))
    python_license = Path(sys.base_prefix, "LICENSE.txt")
    parts.append(section(f"Python {sys.version.split()[0]}（含標準函式庫）", python_license.read_text(encoding="utf-8")))
    for name in ("tcl8.6", "tk8.6"):
        terms = Path(sys.base_prefix, "tcl", name, "license.terms")
        if any(p.startswith(norm(terms.parent)) for p in python_files) and terms.is_file():
            parts.append(section(f"{name[:-3].upper()} {name[-3:]}（tkinter 使用）", terms.read_text(encoding="utf-8")))
    if msvc:
        parts.append(section(
            "Microsoft Visual C++ Runtime",
            "下列檔案為 Microsoft Visual C++ Redistributable 的一部分，依 Microsoft Visual Studio\n"
            "授權條款中的 Distributable Code 規定隨程式散布：\n  " + "\n  ".join(sorted(msvc)),
        ))
    pyinstaller = metadata.distribution("pyinstaller")
    parts.append(section(
        f"PyInstaller {pyinstaller.version}（bootloader 與 runtime）",
        "PyInstaller 以 GPL-2.0-or-later 授權，並附「bootloader exception」：以 PyInstaller 建置的程式\n"
        "可依任意授權散布（含 bootloader）。全文：\n\n"
        + "\n\n".join(text for _, text in license_texts(pyinstaller)),
    ))
    for _, dist in sorted(used.items()):
        body = f"授權：{license_label(dist)}\n"
        for name, text in license_texts(dist):
            body += f"\n----- {name} -----\n\n{text.strip()}\n"
        parts.append(section(f"{dist.metadata['Name']} {dist.version}", body))

    out.write_text("".join(parts), encoding="utf-8")
    print(f"已寫入 {out}（{len(used)} 個套件、{len(nvidia)} 個 NVIDIA DLL）")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
