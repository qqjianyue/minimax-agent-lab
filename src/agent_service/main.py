"""服务进程入口。

    python -m agent_service.main

只负责把配置加载、容器组装、HTTP 服务器启动这三件事连起来。
真正的组装逻辑在 :func:`agent_service.container.build_container`。
"""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from agent_core.config import load_settings
from agent_core.errors import ConfigurationError
from agent_service.app import create_app
from agent_service.container import build_container


def configure_logging(level: str) -> None:
    """配置根日志。

    刻意使用最简格式：不引入会把请求体打进日志的中间件，也不打印任何配置对象
    （配置对象里可能有凭据，虽然 ``SecretStr`` 会掩码，但少一处泄露点总没坏处）。
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    _ = argv
    secrets_file = os.environ.get("MINIMAX_AGENT_SECRETS_FILE")

    try:
        settings = load_settings(secrets_file=secrets_file)
    except ConfigurationError as exc:
        # 启动期配置错误直接退出，不要带半个可用的进程跑起来
        print(f"[agent-service] 配置错误: {exc}", file=sys.stderr)
        return 78  # EX_CONFIG

    configure_logging(settings.app.log_level)

    if not settings.llm.api_key.get_secret_value():
        # 不退出：/healthz 与 /guard/inspect 不需要 key，可以先跑起来排查
        logging.getLogger("agent_service").warning(
            "未配置 MiniMax API Key；/chat 将返回 503，/healthz 与 /guard/inspect 可用"
        )

    container = build_container(settings=settings)
    app = create_app(container)

    uvicorn.run(
        app,
        host=settings.app.host,
        port=settings.app.port,
        log_level=settings.app.log_level.lower(),
        access_log=False,  # 访问日志会把路径与状态打出来，收益低噪音高
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
