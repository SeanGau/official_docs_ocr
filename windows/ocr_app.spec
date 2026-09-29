# PyInstaller spec：把地端版桌面程式 ocr_app.py 打包成 Windows onedir 應用程式。
# 用 windows/build.ps1 建置，不要直接呼叫。
#
# Nemotron-Parse 2.0 與 C-RADIO 的模型程式碼是 trust_remote_code，PyInstaller 分析不到
# 它們的 import，所以這裡明列那些套件。
# 環境變數 OCR_DOC_HF_HUB 指向 model_store.py stage 整理好的 hub cache 時，打包進
# _internal\hf_hub（完全離線版）；未設定則不附模型，程式開啟時會提示下載。
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = os.path.dirname(SPECPATH)

# remote code 直接 import 的第三方套件（timm 以名稱查 registry 建 backbone，要整包收）。
REMOTE_CODE_PACKAGES = ("timm", "open_clip", "einops", "torchvision")

hiddenimports = ["distutils.version", "quopri", "hf_xet"]
datas = [(os.path.join(ROOT, "model_manifest.json"), ".")]
for package in REMOTE_CODE_PACKAGES:
    hiddenimports += collect_submodules(package)
    datas += collect_data_files(package)
# Transformers 依模型設定以字串 lazy import 各模型模組（mbart、donut…）。
hiddenimports += collect_submodules("transformers")
datas += collect_data_files("transformers")

# 這些套件在執行時用 importlib.metadata 檢查相依版本。
for dist in ("transformers", "accelerate", "timm", "open_clip_torch", "torch", "torchvision"):
    datas += copy_metadata(dist, recursive=True)

if os.environ.get("OCR_DOC_HF_HUB"):
    datas.append((os.environ["OCR_DOC_HF_HUB"], "hf_hub"))

a = Analysis(
    [os.path.join(ROOT, "ocr_app.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=hiddenimports,
    # 保留 .py 原始碼：torch.jit 與 transformers 會對這些模組呼叫 inspect.getsource。
    module_collection_mode={
        "torch": "pyz+py",
        "torchvision": "pyz+py",
        "timm": "pyz+py",
        "open_clip": "pyz+py",
        "transformers": "pyz+py",
    },
    excludes=["anthropic", "openai", "google.genai"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [("X utf8_mode=1", None, "OPTION")],
    exclude_binaries=True,
    name="OfficialDocOCR",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="OfficialDocOCR", upx=False)
