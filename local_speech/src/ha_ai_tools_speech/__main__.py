import argparse
from pathlib import Path

import uvicorn

from .app import create_app
from .config import load_settings
from .runtime import MLXRuntime

parser = argparse.ArgumentParser()
parser.add_argument("--config", type=Path, required=True)
args = parser.parse_args()
settings = load_settings(args.config)
uvicorn.run(create_app(settings, MLXRuntime(settings)), host=settings.bind_host,
            port=settings.port, access_log=False, log_level="warning")
