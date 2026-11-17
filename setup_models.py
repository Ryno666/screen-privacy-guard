#!/usr/bin/env python3
"""Fetch only public model weights; never sends camera data."""
import hashlib
from pathlib import Path
import tempfile
import urllib.request

MODELS = {
    'face_detection_yunet_2023mar.onnx': (
        'https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx',
        '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4'),
    'face_recognition_sface_2021dec.onnx': (
        'https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx',
        '0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79'),
    'face_landmarker.task': (
        'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task',
        '64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff'),
}
MODEL_DIR = Path(__file__).resolve().parent / 'models'


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def ensure_models(download=False):
    MODEL_DIR.mkdir(exist_ok=True)
    for name, (url, expected) in MODELS.items():
        target = MODEL_DIR / name
        if target.exists() and digest(target) == expected:
            continue
        if not download:
            raise RuntimeError(f'模型缺失或校验失败：{name}。请重新运行 setup.sh。')
        print(f'下载公开模型：{name}', flush=True)
        with tempfile.NamedTemporaryFile(dir=MODEL_DIR, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                with urllib.request.urlopen(url, timeout=60) as response:
                    total = 0
                    while chunk := response.read(1024 * 256):
                        total += len(chunk)
                        if total > 60 * 1024 * 1024:
                            raise RuntimeError('模型下载体积异常。')
                        handle.write(chunk)
                handle.flush()
                if digest(temporary) != expected:
                    raise RuntimeError(f'模型校验失败：{name}；未安装。')
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    ensure_models(download=True)
    print('公开模型已就绪，SHA-256 校验通过。')
