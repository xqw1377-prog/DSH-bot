"""测试会话默认开发模式:内部服务鉴权在单测中放行。
生产语义(未配置密钥即失败关闭)由各服务鉴权测试显式覆盖。
"""

import os

os.environ.setdefault("DSH_ENV", "development")
