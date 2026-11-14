"""``python -m bdh_supervisor``: serve the API on the unix socket."""
import logging
import os
import sys

import uvicorn

from .api import create_app
from .config import Config
from .docker_driver import DockerDriver


def main():
    logging.basicConfig(level=os.environ.get("BDH_LOG_LEVEL", "INFO"), stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = Config.from_env().validate()
    except ValueError as exc:
        print(f"bdh-supervisor: {exc}", file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(cfg.socket_path), exist_ok=True)
    if os.path.exists(cfg.socket_path):
        os.unlink(cfg.socket_path)
    app = create_app(cfg, DockerDriver(cfg))
    uvicorn.run(app, uds=cfg.socket_path, log_level="info", access_log=False, proxy_headers=False,
                server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
