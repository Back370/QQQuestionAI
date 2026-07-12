import glob, os
import onnxruntime as ort

MODEL_DIR = "/app/model"

candidates = sorted(glob.glob(os.path.join(MODEL_DIR, "*.onnx")))
if not candidates:
    raise RuntimeError(f"No .onnx file found in {MODEL_DIR}: {os.listdir(MODEL_DIR)}")

# 量子化版があれば優先
model_path = next((p for p in candidates if "quantized" in p), candidates[0])
print(f"[startup] loading model: {model_path}", flush=True)

session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])