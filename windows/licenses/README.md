# 隨 exe 附上的授權原文

建置時只複製這裡的檔案，不在建置當下連網下載，成品才可重現。更新相依套件或授權改版時，
人工核對後再更新這裡的檔案與下表。

| 檔案 | 內容 | 來源 | 取得日期 |
| --- | --- | --- | --- |
| `cuda-eula.md` | NVIDIA CUDA Toolkit EULA（Last updated: January 7, 2025），含 Attachment A 可散布清單 | https://docs.nvidia.com/cuda/eula/index.html | 2026-09-30 |
| `cudnn-sla.md` | NVIDIA cuDNN Software License Agreement（runtime .dll 可散布） | https://docs.nvidia.com/deeplearning/cudnn/backend/latest/reference/eula.html | 2026-09-30 |
| `NVIDIA-Open-Model-License.pdf` | NVIDIA Open Model License Agreement（June 14, 2024），C-RADIOv2-H 的授權 | https://developer.download.nvidia.com/licenses/nvidia-open-model-license-agreement-june-2024.pdf | 2026-09-30 |
| `tokenizers.txt` | tokenizers 0.22.2 的 Apache-2.0 全文（wheel 未附） | https://github.com/huggingface/tokenizers/blob/v0.22.2/LICENSE | 2026-09-30 |

`<套件名>.txt` 的檔案由 `third_party_licenses.py` 在該套件 wheel 沒附授權全文時使用。
