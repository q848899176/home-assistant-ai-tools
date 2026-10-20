"""独立服务配置；密钥不参与对象展示。"""

import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    bind_host: str
    port: int
    api_key: str = field(repr=False)
    allowed_sources: list[str]
    models: dict

    def __post_init__(self):
        address = ipaddress.ip_address(self.bind_host)
        if address.is_unspecified or address.is_multicast or not address.is_private:
            raise ValueError("请选择明确的本机局域网接口")
        if len(self.api_key) < 24 or not 1024 <= self.port <= 65535:
            raise ValueError("服务密钥至少24位，端口须在1024至65535之间")
        if not self.allowed_sources:
            raise ValueError("请指定允许访问的主机")
        for source in self.allowed_sources:
            ipaddress.ip_network(source, strict=False)
        for model, value in self.models.items():
            if not model or not Path(value["path"]).is_dir():
                raise ValueError("模型必须先安装到本机目录")
            if not value.get("revision") or not set(value.get("capabilities", ())) <= {"stt", "tts"}:
                raise ValueError("模型版本或能力配置无效")


def load_settings(path: Path) -> Settings:
    return Settings(**json.loads(path.read_text()))
